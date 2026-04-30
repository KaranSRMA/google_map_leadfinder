from __future__ import annotations

import re
from typing import Optional


DAY_SEQUENCE = ("Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday")
DAY_ABBREVIATIONS = {
    "Sunday": "sun",
    "Monday": "mon",
    "Tuesday": "tue",
    "Wednesday": "wed",
    "Thursday": "thu",
    "Friday": "fri",
    "Saturday": "sat",
}
DAY_ALIASES = {
    "sunday": "Sunday",
    "monday": "Monday",
    "tuesday": "Tuesday",
    "wednesday": "Wednesday",
    "thursday": "Thursday",
    "friday": "Friday",
    "saturday": "Saturday",
    "sun": "Sunday",
    "mon": "Monday",
    "tue": "Tuesday",
    "wed": "Wednesday",
    "thu": "Thursday",
    "fri": "Friday",
    "sat": "Saturday",
}
DAY_PATTERN = "|".join(sorted(DAY_ALIASES, key=len, reverse=True))


def normalize_hour_text(value: str) -> str:
    normalized = (
        value.replace("\u202f", " ")
        .replace("\xa0", " ")
        .replace("–", "-")
        .replace("—", "-")
        .replace("−", "-")
    )
    return re.sub(r"\s+", " ", normalized).strip()


def parse_clock_time(value: str) -> Optional[int]:
    normalized = normalize_hour_text(value).lower().replace(".", "")
    match = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?\s*([ap])m", normalized)
    if not match:
        return None

    hour = int(match.group(1)) % 12
    minute = int(match.group(2) or "0")
    if match.group(3) == "p":
        hour += 12
    return hour * 60 + minute


def parse_hour_ranges(details: str) -> Optional[list[tuple[int, int]]]:
    normalized = normalize_hour_text(details)
    lowered = normalized.lower()

    if any(token in lowered for token in ("closed", "off")):
        return []
    if "open 24 hours" in lowered:
        return [(0, 24 * 60)]

    matches = re.findall(
        r"(\d{1,2}(?::\d{2})?\s*[APap]\.?M\.?)\s*(?:to|-)\s*(\d{1,2}(?::\d{2})?\s*[APap]\.?M\.?)",
        normalized,
        flags=re.IGNORECASE,
    )
    if not matches:
        return None

    ranges: list[tuple[int, int]] = []
    for start_text, end_text in matches:
        start = parse_clock_time(start_text)
        end = parse_clock_time(end_text)
        if start is None or end is None:
            return None
        if end <= start:
            end += 24 * 60
        ranges.append((start, end))
    return ranges


def format_clock_time(total_minutes: int) -> str:
    normalized = total_minutes % (24 * 60)
    hour, minute = divmod(normalized, 60)
    suffix = "am" if hour < 12 else "pm"
    display_hour = hour % 12 or 12
    if minute == 0:
        return f"{display_hour}{suffix}"
    return f"{display_hour}:{minute:02d}{suffix}"


def format_hour_window(start_minutes: int, end_minutes: int) -> str:
    if start_minutes == 0 and end_minutes == 24 * 60:
        return "24 hours"
    return f"{format_clock_time(start_minutes)}-{format_clock_time(end_minutes)}"


def round_minutes(value: float, step: int = 15) -> int:
    return int((value + (step / 2)) // step) * step


def infer_rounding_step(open_windows: list[tuple[int, int]]) -> int:
    minute_values = [minutes % 60 for window in open_windows for minutes in window]
    if all(value == 0 for value in minute_values):
        return 60
    if all(value in {0, 30} for value in minute_values):
        return 30
    if all(value % 15 == 0 for value in minute_values):
        return 15
    return 5


def extract_day_entries(raw_hours: str) -> list[tuple[str, str]]:
    normalized = normalize_hour_text(raw_hours).replace(";", " ")
    entries: list[tuple[str, str]] = []
    pattern = re.compile(
        rf"\b(?P<day>{DAY_PATTERN})\b\s*[:,]?\s*(?P<details>.*?)(?=(?:\b(?:{DAY_PATTERN})\b\s*[:,]?)|$)",
        flags=re.IGNORECASE,
    )
    for match in pattern.finditer(normalized):
        raw_day = match.group("day").lower()
        day = DAY_ALIASES.get(raw_day)
        details = match.group("details").strip(" ,-")
        if day and details:
            entries.append((day, details))
    return entries


def summarize_weekly_hours(raw_hours: str, fallback_timing: str = "") -> str:
    source = raw_hours.strip() or fallback_timing.strip()
    if not source:
        return ""

    entries = extract_day_entries(source)
    if not entries:
        return normalize_hour_text(fallback_timing or raw_hours)

    open_windows: list[tuple[int, int]] = []
    closed_days: list[str] = []

    for day, details in entries:
        ranges = parse_hour_ranges(details)
        if ranges is None:
            continue
        if not ranges:
            closed_days.append(day)
            continue
        open_windows.append((min(start for start, _ in ranges), max(end for _, end in ranges)))

    if not open_windows and not closed_days:
        return normalize_hour_text(fallback_timing or raw_hours)
    if not open_windows:
        closed_suffix = ",".join(DAY_ABBREVIATIONS[day] for day in DAY_SEQUENCE if day in closed_days)
        return f"closed/{closed_suffix}" if closed_suffix else "closed"

    unique_windows = {window for window in open_windows}
    if len(unique_windows) == 1:
        start_minutes, end_minutes = open_windows[0]
    else:
        step = infer_rounding_step(open_windows)
        start_minutes = round_minutes(sum(start for start, _ in open_windows) / len(open_windows), step)
        end_minutes = round_minutes(sum(end for _, end in open_windows) / len(open_windows), step)

    summary = format_hour_window(start_minutes, end_minutes)
    if not closed_days:
        return summary

    closed_suffix = ",".join(DAY_ABBREVIATIONS[day] for day in DAY_SEQUENCE if day in closed_days)
    return f"{summary}/{closed_suffix}" if closed_suffix else summary
