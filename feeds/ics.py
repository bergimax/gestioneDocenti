"""A small RFC 5545 writer: CRLF lines, 75-octet folding, text escaping.

Times are written in UTC, converted from the school time zone, so no VTIMEZONE
block is needed.
"""

import datetime
from zoneinfo import ZoneInfo

from django.conf import settings
from django.utils import timezone

from scheduling.models import Day

DAY_OFFSET = {day: i for i, day in enumerate(Day.values)}
PRODID = "-//gestioneDocenti//Orario//IT"
LINE_OCTETS = 75


def escape(text):
    text = str(text).replace("\r\n", "\n").replace("\r", "\n")
    return (
        text.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\n", "\\n")
    )


def fold(line):
    """Split a content line into physical lines of at most 75 octets."""
    out, current, size = [], "", 0
    for char in line:
        width = len(char.encode("utf-8"))
        # continuation lines start with one space, which counts too
        if size + width > LINE_OCTETS:
            out.append(current)
            current, size = " ", 1
        current += char
        size += width
    out.append(current)
    return "\r\n".join(out)


def utc_stamp(moment):
    return moment.astimezone(datetime.UTC).strftime("%Y%m%dT%H%M%SZ")


def local_hour(week_start, day, hour):
    """The UTC datetime of `hour`:00 on `day` of the week starting `week_start`."""
    date = week_start + datetime.timedelta(days=DAY_OFFSET[day])
    return datetime.datetime(
        date.year, date.month, date.day, hour, tzinfo=ZoneInfo(settings.TIME_ZONE)
    )


def uid(slot):
    return (
        f"{slot.timetable.week_start}-{slot.school_class_id}-{slot.day}-"
        f"{slot.start_hour}-{slot.teacher_id}@gestionedocenti"
    )


def build_calendar(name, events):
    """`events` is a list of (slot, summary); returns the VCALENDAR text."""
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:{PRODID}",
        "CALSCALE:GREGORIAN",
        f"X-WR-CALNAME:{escape(name)}",
    ]
    seen = set()
    for slot, summary in events:
        key = uid(slot)
        if key in seen:
            continue
        seen.add(key)
        timetable = slot.timetable
        start = local_hour(timetable.week_start, slot.day, slot.start_hour)
        end = local_hour(timetable.week_start, slot.day, slot.end_hour)
        stamp = timetable.approved_at or timezone.now()
        lines += [
            "BEGIN:VEVENT",
            f"UID:{key}",
            f"DTSTAMP:{utc_stamp(stamp)}",
            f"DTSTART:{utc_stamp(start)}",
            f"DTEND:{utc_stamp(end)}",
            f"SUMMARY:{escape(summary)}",
            "END:VEVENT",
        ]
    lines.append("END:VCALENDAR")
    return "".join(fold(line) + "\r\n" for line in lines)
