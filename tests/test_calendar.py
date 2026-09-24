from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest
import vobject

from icloud_mcp import calendar
from icloud_mcp.calendar import (
    _dt_property,
    _escape_text,
    _parse_when,
    _set_datetime,
    _vevent_to_dict,
    normalize_rrule,
)

SAMPLE = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//x//EN
BEGIN:VTIMEZONE
TZID:Europe/Madrid
BEGIN:STANDARD
DTSTART:20251026T030000
TZNAME:CET
TZOFFSETFROM:+0200
TZOFFSETTO:+0100
END:STANDARD
END:VTIMEZONE
BEGIN:VEVENT
UID:abc
DTSTAMP:20250101T000000Z
DTSTART;TZID=Europe/Madrid:20250110T100000
DTEND;TZID=Europe/Madrid:20250110T110000
SUMMARY:Test\\, event
RRULE:FREQ=WEEKLY;BYDAY=FR
ATTENDEE;CN=a@x.com:mailto:a@x.com
END:VEVENT
END:VCALENDAR"""


def test_normalize_rrule_accepts_both_forms():
    assert normalize_rrule("FREQ=WEEKLY;BYDAY=TU") == "RRULE:FREQ=WEEKLY;BYDAY=TU"
    assert normalize_rrule("RRULE:FREQ=DAILY;COUNT=3") == "RRULE:FREQ=DAILY;COUNT=3"


def test_normalize_rrule_rejects_dtstart_and_garbage():
    with pytest.raises(ValueError):
        normalize_rrule("DTSTART:20250101T000000\nRRULE:FREQ=DAILY")
    with pytest.raises(ValueError):
        normalize_rrule("FREQ=SOMETIMES")


def test_parse_when_date_and_datetime():
    assert _parse_when("2025-06-01", None) == date(2025, 6, 1)
    dt = _parse_when("2025-06-01T10:00:00", "America/New_York")
    assert dt.tzinfo == ZoneInfo("America/New_York")
    dt = _parse_when("2025-06-01T10:00:00", None)
    assert dt.tzinfo == ZoneInfo("Europe/Berlin")
    dt = _parse_when("2025-06-01T10:00:00Z", None)
    assert dt.utcoffset().total_seconds() == 0


def test_dt_property_forms():
    line, tzid = _dt_property("DTSTART", date(2025, 6, 1))
    assert line == "DTSTART;VALUE=DATE:20250601" and tzid is None
    line, tzid = _dt_property("DTSTART", datetime(2025, 6, 1, 10, tzinfo=ZoneInfo("Europe/Berlin")))
    assert line == "DTSTART;TZID=Europe/Berlin:20250601T100000" and tzid == "Europe/Berlin"
    line, tzid = _dt_property("DTEND", datetime(2025, 6, 1, 10, tzinfo=ZoneInfo("UTC")))
    assert line == "DTEND:20250601T100000Z" and tzid is None


def test_escape_text():
    assert _escape_text("a,b;c\nd\\e") == "a\\,b\\;c\\nd\\\\e"


def test_vevent_to_dict():
    vevent = vobject.readOne(SAMPLE).vevent
    data = _vevent_to_dict(vevent, "https://p1-caldav.icloud.com/e.ics", "Work")
    assert data["summary"] == "Test, event"
    assert data["start"].startswith("2025-01-10T10:00:00")
    assert data["start_timezone"] == "Europe/Madrid"
    assert data["recurring"] is True and data["rrule"] == "FREQ=WEEKLY;BYDAY=FR"
    assert data["attendees"] == ["a@x.com"]
    assert data["all_day"] is False


def test_set_datetime_keeps_existing_zone_and_changes_zone():
    cal = vobject.readOne(SAMPLE)
    ve = cal.vevent
    _set_datetime(ve.dtstart, datetime(2025, 2, 1, 9, 0, tzinfo=ZoneInfo("Europe/Berlin")), None)
    # tz-aware input with no explicit timezone -> stored as UTC
    assert any(line.startswith("DTSTART:20250201T080000Z") for line in cal.serialize().splitlines())

    cal = vobject.readOne(SAMPLE)
    ve = cal.vevent
    _set_datetime(ve.dtstart, datetime(2025, 2, 1, 9, 0), "America/New_York")
    lines = cal.serialize().splitlines()
    assert "DTSTART;TZID=America/New_York:20250201T090000" in lines

    cal = vobject.readOne(SAMPLE)
    _set_datetime(cal.vevent.dtend, date(2025, 3, 2), None)
    assert "DTEND;VALUE=DATE:20250302" in cal.serialize().splitlines()


class _FakeEvent:
    def __init__(self):
        self.url = "https://p1-caldav.icloud.com/123/calendars/work/x.ics"


class _FakeCalendar:
    name = "Work"
    url = "https://p1-caldav.icloud.com/123/calendars/work/"

    def __init__(self):
        self.ical = None

    def get_supported_components(self):
        return ["VEVENT"]

    def add_event(self, ical):
        self.ical = ical
        return _FakeEvent()


def test_create_event_builds_valid_ical(monkeypatch):
    fake = _FakeCalendar()
    monkeypatch.setattr(calendar, "require_auth", lambda: ("me@icloud.com", "pw"))
    monkeypatch.setattr(calendar, "_get_caldav_client", lambda e, p: object())
    monkeypatch.setattr(calendar, "_resolve_calendar", lambda client, cid, e, p: fake)
    sent = []
    monkeypatch.setattr(calendar, "_send_calendar_invitation", lambda *a, **k: sent.append(a[2]))

    result = calendar.create_event(
        "Standup, daily",
        "2025-06-02T09:30:00",
        "2025-06-02T09:45:00",
        description="Line1\nLine2",
        attendees=["a@x.com", "me@icloud.com"],
        timezone="Europe/Berlin",
        rrule="FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR",
    )
    parsed = vobject.readOne(fake.ical)
    ve = parsed.vevent
    assert ve.summary.value == "Standup, daily"
    assert ve.description.value == "Line1\nLine2"
    assert ve.rrule.value == "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR"
    assert "BEGIN:VTIMEZONE" in fake.ical and "TZID:Europe/Berlin" in fake.ical
    assert "DTSTART;TZID=Europe/Berlin:20250602T093000" in fake.ical
    assert result["timezone"] == "Europe/Berlin"
    assert sent == ["a@x.com"]  # organizer is not invited to their own event


def test_create_all_day_event(monkeypatch):
    fake = _FakeCalendar()
    monkeypatch.setattr(calendar, "require_auth", lambda: ("me@icloud.com", "pw"))
    monkeypatch.setattr(calendar, "_get_caldav_client", lambda e, p: object())
    monkeypatch.setattr(calendar, "_resolve_calendar", lambda client, cid, e, p: fake)
    result = calendar.create_event("Holiday", "2025-06-02", "2025-06-02")
    assert "DTSTART;VALUE=DATE:20250602" in fake.ical
    assert "DTEND;VALUE=DATE:20250603" in fake.ical
    assert result["all_day"] is True


def test_create_event_rejects_mixed_and_reversed(monkeypatch):
    monkeypatch.setattr(calendar, "require_auth", lambda: ("me@icloud.com", "pw"))
    monkeypatch.setattr(calendar, "_get_caldav_client", lambda e, p: object())
    monkeypatch.setattr(calendar, "_resolve_calendar", lambda client, cid, e, p: _FakeCalendar())
    with pytest.raises(ValueError):
        calendar.create_event("x", "2025-06-02", "2025-06-02T10:00:00")
    with pytest.raises(ValueError):
        calendar.create_event("x", "2025-06-02T11:00:00", "2025-06-02T10:00:00")


ALARM_SAMPLE = SAMPLE.replace(
    "END:VEVENT",
    "BEGIN:VALARM\nACTION:DISPLAY\nDESCRIPTION:Reminder\nTRIGGER:-PT1H\nEND:VALARM\n"
    "BEGIN:VALARM\nACTION:DISPLAY\nDESCRIPTION:Reminder\nTRIGGER;RELATED=START:PT9H\nEND:VALARM\nEND:VEVENT",
)


def test_alarm_read_back():
    vevent = vobject.readOne(ALARM_SAMPLE).vevent
    assert calendar._alarm_minutes(vevent) == [60, -540]
    assert _vevent_to_dict(vevent, "u", "c")["reminders"] == [60, -540]


def test_alarm_lines_and_replace():
    assert calendar._alarm_lines(15) == [
        "BEGIN:VALARM", "ACTION:DISPLAY", "DESCRIPTION:Reminder", "TRIGGER:-PT15M", "END:VALARM"
    ]
    assert calendar._alarm_lines(-540)[3] == "TRIGGER:PT540M"
    cal = vobject.readOne(ALARM_SAMPLE)
    for alarm in list(cal.vevent.valarm_list):
        cal.vevent.remove(alarm)
    calendar._add_alarm(cal.vevent, 10)
    assert calendar._alarm_minutes(cal.vevent) == [10]
    assert "TRIGGER:-PT10M" in cal.serialize()


def test_create_event_with_reminders(monkeypatch):
    fake = _FakeCalendar()
    monkeypatch.setattr(calendar, "require_auth", lambda: ("me@icloud.com", "pw"))
    monkeypatch.setattr(calendar, "_get_caldav_client", lambda e, p: object())
    monkeypatch.setattr(calendar, "_resolve_calendar", lambda client, cid, e, p: fake)
    result = calendar.create_event("x", "2025-06-02T10:00:00", "2025-06-02T11:00:00", reminders=[60, 0])
    assert fake.ical.count("BEGIN:VALARM") == 2 and "TRIGGER:-PT60M" in fake.ical and "TRIGGER:PT0M" in fake.ical
    assert result["reminders"] == [60, 0]


def test_description_html_rendered_to_text(monkeypatch):
    monkeypatch.setattr(calendar.config, "HTML_MODE", "text")
    cal = vobject.readOne(SAMPLE)
    cal.vevent.add("description").value = "Park<br>2803 Ave<br><a href='x'>map</a>"
    data = _vevent_to_dict(cal.vevent, "u", "c")
    assert "<br>" not in data["description"] and "2803 Ave" in data["description"]


def test_reminder_minutes_accepts_time_of_day():
    assert calendar._reminder_minutes([60, "10", -5], None) == [60, 10, -5]
    # all-day event: 09:00 on the day -> 540 minutes after midnight
    assert calendar._reminder_minutes(["09:00"], date(2025, 6, 2)) == [-540]
    # timed event starting 10:30: 09:00 the same day -> 90 minutes before
    assert calendar._reminder_minutes(["9:00"], datetime(2025, 6, 2, 10, 30)) == [90]
    with pytest.raises(ValueError):
        calendar._reminder_minutes(["25:00"], None)
    with pytest.raises(ValueError):
        calendar._reminder_minutes(["soon"], None)


def test_create_all_day_event_with_time_of_day_reminder(monkeypatch):
    fake = _FakeCalendar()
    monkeypatch.setattr(calendar, "require_auth", lambda: ("me@icloud.com", "pw"))
    monkeypatch.setattr(calendar, "_get_caldav_client", lambda e, p: object())
    monkeypatch.setattr(calendar, "_resolve_calendar", lambda client, cid, e, p: fake)
    result = calendar.create_event("Birthday", "2025-06-02", "2025-06-02", reminders=["09:00"])
    assert "TRIGGER:PT540M" in fake.ical and result["reminders"] == [-540]
