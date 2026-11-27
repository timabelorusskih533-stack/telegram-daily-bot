"""Распознавание речи для голосовых напоминаний (офлайн, Vosk).

Модель скачивается один раз при первом запуске в папку данных и остаётся там.
Голосовое (ogg/opus) декодируется в 16 кГц моно через PyAV — системный ffmpeg не нужен.
"""
import asyncio
import io
import json
import logging
import os
import tempfile
import zipfile

import aiohttp

MODEL_URL = "https://alphacephei.com/vosk/models/vosk-model-small-ru-0.22.zip"
MODEL_NAME = "vosk-model-small-ru-0.22"

_model = None
_loading = False


def model_ready() -> bool:
    return _model is not None


def is_loading() -> bool:
    return _loading


def _model_path(data_dir: str) -> str:
    return os.path.join(data_dir, MODEL_NAME)


def _unzip(zip_path: str, dest: str):
    with zipfile.ZipFile(zip_path) as z:
        z.extractall(dest)


async def ensure_model(data_dir: str):
    """Один раз скачивает и загружает модель. Работает в фоне, не блокируя бота."""
    global _model, _loading
    if _model is not None or _loading:
        return
    _loading = True
    try:
        path = _model_path(data_dir)
        if not os.path.isdir(path):
            logging.info("Скачиваю модель распознавания речи (~45 МБ)...")
            # zip качаем во временную папку, чтобы не упираться в квоту данных
            zip_path = os.path.join(tempfile.gettempdir(), MODEL_NAME + ".zip")
            async with aiohttp.ClientSession() as s:
                async with s.get(MODEL_URL, timeout=aiohttp.ClientTimeout(total=900)) as r:
                    with open(zip_path, "wb") as f:
                        async for chunk in r.content.iter_chunked(1 << 16):
                            f.write(chunk)
            await asyncio.to_thread(_unzip, zip_path, data_dir)
            os.remove(zip_path)
            logging.info("Модель распознавания распакована")
        from vosk import Model, SetLogLevel
        SetLogLevel(-1)
        _model = await asyncio.to_thread(Model, path)
        logging.info("Модель распознавания загружена, голосовые доступны")
    except Exception as e:
        logging.error(f"Не удалось подготовить модель распознавания: {e}")
    finally:
        _loading = False


def _decode_pcm16(data: bytes) -> bytes:
    """ogg/opus -> 16 кГц моно s16le."""
    import av
    ic = av.open(io.BytesIO(data))
    resampler = av.AudioResampler(format="s16", layout="mono", rate=16000)
    out = bytearray()
    for frame in ic.decode(audio=0):
        for rf in resampler.resample(frame):
            out += bytes(rf.planes[0])
    for rf in resampler.resample(None):
        out += bytes(rf.planes[0])
    ic.close()
    return bytes(out)


def _recognize(pcm: bytes) -> str:
    from vosk import KaldiRecognizer
    rec = KaldiRecognizer(_model, 16000)
    rec.AcceptWaveform(pcm)
    return json.loads(rec.FinalResult()).get("text", "").strip()


async def transcribe(audio_bytes: bytes) -> str:
    """Голосовое -> текст. Пустая строка, если модель не готова или не распозналось."""
    if _model is None:
        return ""
    try:
        pcm = await asyncio.to_thread(_decode_pcm16, audio_bytes)
        return await asyncio.to_thread(_recognize, pcm)
    except Exception as e:
        logging.error(f"Ошибка распознавания: {e}")
        return ""
