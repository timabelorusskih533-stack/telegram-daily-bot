"""Чтение расписания с сайта karacikov-raspisanie.github.io.

Расписание хранится прямо в index.html в виде JS-констант:
S — предметы, P — типы занятий, T — преподаватели,
RAW — строки «дд.мм|время|предмет|тип|преподаватель», TP/TI — темы.
"""
import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta

SITE_URL = "https://karacikov-raspisanie.github.io/"
END = {"09:00": "10:30", "10:45": "12:15", "12:30": "14:00", "16:00": "17:30"}


@dataclass
class Lesson:
    day: date
    start: str
    end: str
    subject: str
    kind: str
    teacher: str
    topic: str

    @property
    def start_dt(self) -> datetime:
        h, m = map(int, self.start.split(":"))
        return datetime(self.day.year, self.day.month, self.day.day, h, m)


def _js_value(src: str, name: str):
    """Достаёт значение `const NAME=...;` из JS-кода."""
    m = re.search(r"const\s+" + name + r"\s*=", src)
    if not m:
        raise ValueError(f"Не найдено {name} в коде сайта")
    i = m.end()
    if src[i] == "[":
        # Массив: ищем парную скобку, учитывая строки
        depth, j, in_str, esc = 0, i, False, False
        while True:
            ch = src[j]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
            elif ch == '"':
                in_str = True
            elif ch == "[":
                depth += 1
            elif ch == "]":
                depth -= 1
                if depth == 0:
                    return json.loads(src[i:j + 1])
            j += 1
    # Строка, возможно склеенная из частей: "..."\n+"..."
    end = src.index(";\n", i)
    parts = re.findall(r'"((?:[^"\\]|\\.)*)"', src[i:end])
    return "".join(json.loads(f'"{p}"') for p in parts)


def _fmt_topic(x: str) -> str:
    x = (x or "").strip()
    if not x:
        return ""
    if re.match(r"^т\.\s*\d", x):
        return re.sub(r"^т\.\s*", "Тема ", x)
    return re.sub(r"^т\.\s*", "", x)


def parse_schedule(html: str) -> list[Lesson]:
    s = _js_value(html, "S")
    p = _js_value(html, "P")
    t = _js_value(html, "T")
    raw = _js_value(html, "RAW")
    tp = _js_value(html, "TP")
    ti = _js_value(html, "TI")
    ym = re.search(r"YEAR\s*=\s*(\d{4})", html)
    year = int(ym.group(1)) if ym else date.today().year

    lessons = []
    for i, row in enumerate(r for r in raw.split(";") if r):
        d, start, si, pi, tei = row.split("|")
        dd, mm = map(int, d.split("."))
        topic = tp[ti[i]] if i < len(ti) and ti[i] < len(tp) else ""
        lessons.append(Lesson(
            day=date(year, mm, dd),
            start=start,
            end=END.get(start, ""),
            subject=s[int(si)],
            kind=p[int(pi)],
            teacher=t[int(tei)],
            topic=_fmt_topic(topic),
        ))
    lessons.sort(key=lambda x: (x.day, x.start))
    return lessons
