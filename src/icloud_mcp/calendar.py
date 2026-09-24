"""CalDAV operations for iCloud calendars.

Ported from the battle-tested Resonar agent tools: VEVENT-capability based
calendar filtering (Reminders lists are VTODO-only and cannot hold events),
client-side expansion of recurring series, per-event DAV clients bound to the
event's own host (iCloud shards calendars across ``pNN-caldav.icloud.com``),
RRULE support, IANA timezone handling and iTIP invitations over SMTP.
"""

import logging
import re
import smtplib
from datetime import UTC, date, datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formatdate
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import caldav
from dateutil import tz as dateutil_tz
from dateutil.rrule import rrulestr

from .auth import require_auth
from .config import config
from .mail_utils import check_recipients_allowed, clean_rich_text, parse_recipients
from .urls import ensure_icloud_url

logger = logging.getLogger(__name__)

REMINDERS_NOTE = (
    "iCloud does not allow creating events in Reminders lists. "
    "They are VTODO-only system calendars and can only be read. "
    "Use another calendar to create events."
)


# ---------------------------------------------------------------------------
# Clients
# ---------------------------------------------------------------------------


def _get_caldav_client(email: str, password: str) -> caldav.DAVClient:
    return caldav.DAVClient(url=config.CALDAV_SERVER, username=email, password=password)


def _client_for_url(url: str, email: str, password: str) -> caldav.DAVClient:
    """DAV client bound to the host of ``url``.

    iCloud returns calendar/event URLs on per-user shards
    (``https://p72-caldav.icloud.com/...``). Using the generic
    ``caldav.icloud.com`` client for those URLs breaks URL joining.
    """
    url = ensure_icloud_url(url, "calendar/event")
    parsed = urlparse(url)
    return caldav.DAVClient(
        url=f"{parsed.scheme}://{parsed.netloc}", username=email, password=password
    )


# ---------------------------------------------------------------------------
# Helpers: recurrence, dates, timezones, iCalendar text
# ---------------------------------------------------------------------------


def normalize_rrule(rrule: str, dtstart: datetime | None = None) -> str:
    """Validate an RFC 5545 recurrence rule and return it as an ``RRULE:`` line.

    Accepts ``FREQ=...`` or ``RRULE:FREQ=...``. The event's own start acts as
    DTSTART, so a DTSTART inside the rule is rejected.
    """
    rule = rrule.strip()
    if rule.upper().startswith("RRULE:"):
        rule = rule[len("RRULE:"):]
    if "DTSTART" in rule.upper():
        raise ValueError(
            "Do not include DTSTART in rrule; the event start time is the series start."
        )
    try:
        rrulestr(rule, dtstart=dtstart or datetime(2000, 1, 1))
    except Exception as e:
        raise ValueError(f"Invalid RRULE '{rrule}': {e}") from e
    return f"RRULE:{rule}"


def _zone(tz_name: str | None) -> ZoneInfo:
    name = tz_name or config.DEFAULT_TIMEZONE
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as e:
        raise ValueError(
            f"Unknown timezone {name!r}; use an IANA name such as 'Europe/Berlin' or 'UTC'."
        ) from e


def _parse_when(value: str, tz_name: str | None) -> date | datetime:
    """Parse ``YYYY-MM-DD`` (all-day date) or ISO datetime.

    Naive datetimes are interpreted in ``tz_name`` (default: DEFAULT_TIMEZONE).
    Datetimes with an explicit offset are kept as-is.
    """
    text = value.strip()
    if len(text) == 10:
        return date.fromisoformat(text)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_zone(tz_name))
    return dt


def _parse_range_bound(value: str | None, default: datetime, end_of_day: bool) -> datetime:
    if not value:
        return default
    parsed = _parse_when(value, None)
    if isinstance(parsed, datetime):
        return parsed
    dt = datetime.combine(parsed, datetime.min.time())
    if end_of_day:
        dt = dt.replace(hour=23, minute=59, second=59)
    return dt.replace(tzinfo=_zone(None))


def _escape_text(text: str) -> str:
    return (
        text.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
    )


def _dt_property(name: str, value: date | datetime) -> tuple[str, str | None]:
    """Return an iCalendar DTSTART/DTEND line and the TZID it references (if any)."""
    if isinstance(value, datetime):
        tzinfo = value.tzinfo
        key = getattr(tzinfo, "key", None)
        if tzinfo is None:
            return f"{name}:{value.strftime('%Y%m%dT%H%M%SZ')}", None
        if key == "UTC" or value.utcoffset() == timedelta(0):
            return f"{name}:{value.astimezone(UTC).strftime('%Y%m%dT%H%M%SZ')}", None
        if key:
            return f"{name};TZID={key}:{value.strftime('%Y%m%dT%H%M%S')}", key
        # Fixed offset without an IANA name: store as UTC.
        return f"{name}:{value.astimezone(UTC).strftime('%Y%m%dT%H%M%SZ')}", None
    return f"{name};VALUE=DATE:{value.strftime('%Y%m%d')}", None


def _vtimezone_block(tzid: str, first: date, last: date) -> str:
    """VTIMEZONE component for ``tzid`` covering ``[first, last]`` (best effort)."""
    try:
        import icalendar

        component = icalendar.Timezone.from_tzinfo(
            ZoneInfo(tzid), first_date=first, last_date=last
        )
        return component.to_ical().decode("utf-8").replace("\r\n", "\n").strip()
    except Exception as e:  # pragma: no cover - depends on icalendar internals
        logger.debug("Could not build VTIMEZONE for %s: %s", tzid, e)
        return ""


def _value_to_iso(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _prop_tzid(prop: Any) -> str:
    params = getattr(prop, "params", {}) or {}
    for key in ("TZID", "X-VOBJ-ORIGINAL-TZID"):
        values = params.get(key)
        if values:
            return values[0]
    value = getattr(prop, "value", None)
    key = getattr(getattr(value, "tzinfo", None), "key", None)
    return key or ""


def _attendee_emails(vevent: Any) -> list[str]:
    result = []
    if hasattr(vevent, "attendee_list"):
        for att in vevent.attendee_list:
            value = str(getattr(att, "value", "") or "")
            if value.lower().startswith("mailto:"):
                value = value[7:]
            if value:
                result.append(value)
    return result


def _alarm_minutes(vevent: Any) -> list[int]:
    """Minutes before start for each display/audio VALARM (negative = after start)."""
    result = []
    for alarm in getattr(vevent, "valarm_list", []) or []:
        trigger = getattr(alarm, "trigger", None)
        if trigger is None:
            continue
        value = trigger.value
        if isinstance(value, timedelta):
            related = (trigger.params.get("RELATED") or ["START"])[0].upper()
            if related != "START":
                continue
            result.append(int(-value.total_seconds() // 60))
    return result


_TIME_OF_DAY_RE = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*$")


def _reminder_minutes(reminders: list[Any] | None, start: date | datetime | None) -> list[int]:
    """Normalise reminder specs to minutes before start.

    An int is minutes before start (negative = after start). A "HH:MM" string is a
    time of day on the event's start date, which is what people mean for all-day
    events ("remind me at 09:00"); for timed events it is converted relative to the
    start time.
    """
    result: list[int] = []
    for item in reminders or []:
        if isinstance(item, bool):
            raise ValueError(f"Invalid reminder {item!r}: expected minutes or 'HH:MM'")
        if isinstance(item, int):
            result.append(item)
            continue
        if isinstance(item, float) and item.is_integer():
            result.append(int(item))
            continue
        text = str(item).strip()
        match = _TIME_OF_DAY_RE.match(text)
        if match:
            hours, minutes = int(match.group(1)), int(match.group(2))
            if hours > 23 or minutes > 59:
                raise ValueError(f"Invalid reminder time {text!r}")
            of_day = hours * 60 + minutes
            if isinstance(start, datetime):
                result.append(start.hour * 60 + start.minute - of_day)
            else:
                result.append(-of_day)
            continue
        if text.lstrip("-").isdigit():
            result.append(int(text))
            continue
        raise ValueError(f"Invalid reminder {item!r}: expected minutes before start or 'HH:MM'")
    return result


def _validate_attendees(attendees: list[str] | None) -> list[str]:
    """Normalise attendee addresses and enforce EMAIL_SEND_ALLOWLIST."""
    if not attendees:
        return []
    addresses = []
    for item in attendees:
        for parsed in parse_recipients(item, "attendees"):
            addresses.append(parsed.rsplit("<", 1)[-1].rstrip(">") if "<" in parsed else parsed)
    check_recipients_allowed(addresses)
    return addresses


def _add_alarm(vevent: Any, minutes_before: int) -> None:
    alarm = vevent.add("valarm")
    alarm.add("action").value = "DISPLAY"
    alarm.add("description").value = "Reminder"
    alarm.add("trigger").value = timedelta(minutes=-int(minutes_before))


def _alarm_lines(minutes_before: int) -> list[str]:
    minutes = int(minutes_before)
    sign = "-" if minutes > 0 else ""
    return [
        "BEGIN:VALARM",
        "ACTION:DISPLAY",
        "DESCRIPTION:Reminder",
        f"TRIGGER:{sign}PT{abs(minutes)}M",
        "END:VALARM",
    ]


def _vevent_to_dict(vevent: Any, url: str, calendar_name: str) -> dict[str, Any]:
    def text(attr: str) -> str:
        prop = getattr(vevent, attr, None)
        return str(prop.value) if prop is not None and prop.value is not None else ""

    start_prop = getattr(vevent, "dtstart", None)
    end_prop = getattr(vevent, "dtend", None)
    start_value = start_prop.value if start_prop is not None else None
    all_day = isinstance(start_value, date) and not isinstance(start_value, datetime)

    rrule = text("rrule")
    return {
        "id": url,
        "url": url,
        "summary": text("summary"),
        "description": clean_rich_text(text("description")),
        "location": clean_rich_text(text("location")),
        "start": _value_to_iso(start_value),
        "end": _value_to_iso(end_prop.value) if end_prop is not None else None,
        "all_day": all_day,
        "start_timezone": _prop_tzid(start_prop) if start_prop is not None else "",
        "end_timezone": _prop_tzid(end_prop) if end_prop is not None else "",
        "recurring": bool(rrule) or hasattr(vevent, "recurrence_id"),
        "rrule": rrule,
        "reminders": _alarm_minutes(vevent),
        "attendees": _attendee_emails(vevent),
        "calendar": calendar_name,
    }


def _supports_events(cal: caldav.Calendar) -> bool | None:
    """True/False when the server tells us, None when unknown."""
    try:
        components = cal.get_supported_components()
    except Exception:
        return None
    if not components:
        return None
    return "VEVENT" in components


def _event_calendars(principal: caldav.Principal) -> list[caldav.Calendar]:
    calendars = principal.calendars()
    usable = []
    for cal in calendars:
        if cal.name and "⚠" in cal.name:
            continue
        if _supports_events(cal) is False:
            continue
        usable.append(cal)
    return usable or list(calendars)


# ---------------------------------------------------------------------------
# iTIP invitations
# ---------------------------------------------------------------------------


def _send_calendar_invitation(
    organizer_email: str,
    organizer_password: str,
    attendee_email: str,
    ical_data: str,
    summary: str,
    start: str,
    end: str,
    location: str | None = None,
    method: str = "REQUEST",
) -> None:
    """Email an iTIP REQUEST/CANCEL for the event to one attendee."""
    from .mail import _append_to_sent, _close_imap_client, _get_imap_client

    msg = MIMEMultipart("alternative")
    msg["From"] = organizer_email
    msg["To"] = attendee_email
    msg["Subject"] = (
        f"Cancelled: {summary}" if method == "CANCEL" else f"Invitation: {summary}"
    )
    msg["Date"] = formatdate(localtime=True)

    text_body = (
        f"{'The following event has been cancelled' if method == 'CANCEL' else 'You have been invited to the following event'}:\n\n"
        f"Summary: {summary}\nStart: {start}\nEnd: {end}"
    )
    if location:
        text_body += f"\nLocation: {location}"
    text_body += f"\n\nOrganizer: {organizer_email}"
    msg.attach(MIMEText(text_body, "plain"))

    lines = ical_data.strip().replace("\r\n", "\n").split("\n")
    if lines and lines[0] == "BEGIN:VCALENDAR" and not any(
        line.startswith("METHOD:") for line in lines
    ):
        lines.insert(1, f"METHOD:{method}")
    if not any(line.startswith("ORGANIZER") for line in lines):
        for i, line in enumerate(lines):
            if line.startswith("UID:"):
                lines.insert(
                    i + 1, f"ORGANIZER;CN={organizer_email}:mailto:{organizer_email}"
                )
                break
    if method == "CANCEL" and not any(line.startswith("STATUS:CANCELLED") for line in lines):
        lines = [line for line in lines if not line.startswith("STATUS:")]
        for i, line in enumerate(lines):
            if line.startswith("UID:"):
                lines.insert(i + 1, "STATUS:CANCELLED")
                break
    ical_with_method = "\n".join(lines)

    cal_part = MIMEText(ical_with_method, "calendar", "utf-8")
    cal_part.add_header("Content-Class", "urn:content-classes:calendarmessage")
    cal_part.replace_header(
        "Content-Type", f"text/calendar; method={method}; charset=UTF-8"
    )
    msg.attach(cal_part)

    smtp_client = smtplib.SMTP(config.SMTP_SERVER, config.SMTP_PORT, timeout=30)
    try:
        smtp_client.starttls()
        smtp_client.login(organizer_email, organizer_password)
        smtp_client.send_message(msg, from_addr=organizer_email, to_addrs=[attendee_email])
    finally:
        try:
            smtp_client.quit()
        except Exception:
            pass

    try:
        imap_client = _get_imap_client(organizer_email, organizer_password)
        try:
            _append_to_sent(imap_client, msg.as_bytes())
        finally:
            _close_imap_client(imap_client)
    except Exception as e:
        logger.warning("Could not save invitation copy to Sent: %s", e)


def _notify_attendees(
    email: str,
    password: str,
    attendees: list[str],
    ical_data: str,
    summary: str,
    start: str,
    end: str,
    location: str | None,
    method: str,
) -> list[str]:
    """Send iTIP mail to each attendee; returns the list of failures."""
    failed = []
    for attendee_email in attendees:
        if attendee_email.lower() == email.lower():
            continue
        try:
            _send_calendar_invitation(
                email, password, attendee_email, ical_data, summary, start, end, location, method
            )
        except Exception as e:
            logger.error("Failed to send %s to %s: %s", method, attendee_email, e)
            failed.append(attendee_email)
    return failed


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------


def list_calendars() -> list[dict[str, Any]]:
    email, password = require_auth()
    principal = _get_caldav_client(email, password).principal()

    result = []
    for cal in principal.calendars():
        supports_events = _supports_events(cal)
        item: dict[str, Any] = {
            "id": str(cal.url),
            "name": cal.name or "Unnamed Calendar",
            "url": str(cal.url),
        }
        if supports_events is False:
            item["read_only"] = True
            item["note"] = REMINDERS_NOTE
        result.append(item)
    return result


def list_events(
    calendar_id: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
) -> list[dict[str, Any]]:
    email, password = require_auth()
    client = _get_caldav_client(email, password)

    now = datetime.now(UTC)
    start = _parse_range_bound(start_date, now - timedelta(days=90), end_of_day=False)
    end = _parse_range_bound(end_date, now + timedelta(days=365), end_of_day=True)
    if end <= start:
        raise ValueError("end_date must be after start_date")

    if calendar_id:
        cal_client = _client_for_url(calendar_id, email, password)
        calendars = [caldav.Calendar(client=cal_client, url=calendar_id)]
    else:
        calendars = _event_calendars(client.principal())

    result: list[dict[str, Any]] = []
    for calendar in calendars:
        try:
            # expand=True + split_expanded (default) returns every occurrence of
            # a recurring series as its own object with the occurrence's own
            # DTSTART/DTEND. Do NOT call event.load() on those: it re-fetches the
            # series master and overwrites the expanded occurrence.
            events = calendar.search(start=start, end=end, event=True, expand=True)
        except Exception as e:
            logger.warning("Search failed for calendar %s: %s", calendar.url, e)
            continue

        try:
            calendar_name = calendar.name or "Unknown"
        except Exception:
            calendar_name = "Unknown"

        for event in events:
            try:
                if not event.data:
                    event.load()
                vevent = event.vobject_instance.vevent
                result.append(_vevent_to_dict(vevent, str(event.url), calendar_name))
            except Exception as e:
                logger.debug("Skipping malformed event %s: %s", getattr(event, "url", "?"), e)
                continue

    result.sort(key=lambda item: item.get("start") or "")
    return result


def search_events(
    query: str,
    calendar_id: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
) -> list[dict[str, Any]]:
    events = list_events(calendar_id, start_date, end_date)
    needle = query.lower()
    return [
        event
        for event in events
        if needle in event.get("summary", "").lower()
        or needle in event.get("description", "").lower()
        or needle in event.get("location", "").lower()
    ]


def _resolve_calendar(
    client: caldav.DAVClient, calendar_id: str | None, email: str, password: str
) -> caldav.Calendar:
    if calendar_id:
        cal_client = _client_for_url(calendar_id, email, password)
        calendar = caldav.Calendar(client=cal_client, url=calendar_id)
        if _supports_events(calendar) is False:
            raise ValueError(f"Cannot create events in calendar '{calendar_id}': {REMINDERS_NOTE}")
        return calendar

    candidates = _event_calendars(client.principal())
    if not candidates:
        raise ValueError("No calendars found for this account")
    return candidates[0]


def create_event(
    summary: str,
    start: str,
    end: str,
    description: str | None = None,
    location: str | None = None,
    attendees: list[str] | None = None,
    calendar_id: str | None = None,
    timezone: str | None = None,
    rrule: str | None = None,
    reminders: list[Any] | None = None,
) -> dict[str, Any]:
    email, password = require_auth()
    attendees = _validate_attendees(attendees)
    client = _get_caldav_client(email, password)
    calendar = _resolve_calendar(client, calendar_id, email, password)

    start_value = _parse_when(start, timezone)
    end_value = _parse_when(end, timezone)
    if isinstance(start_value, datetime) != isinstance(end_value, datetime):
        raise ValueError("start and end must both be dates (all-day) or both be datetimes")
    all_day = not isinstance(start_value, datetime)
    if all_day and end_value <= start_value:
        # DTEND for all-day events is exclusive; a one-day event ends the next day.
        end_value = start_value + timedelta(days=1)
    if not all_day and end_value <= start_value:
        raise ValueError("end must be after start")

    now = datetime.now(UTC)
    uid = f"{int(now.timestamp())}{now.microsecond}@icloud-mcp"
    dtstart_line, tzid = _dt_property("DTSTART", start_value)
    dtend_line, _ = _dt_property("DTEND", end_value)

    rrule_line = ""
    if rrule and rrule.strip():
        rrule_line = normalize_rrule(
            rrule, start_value if isinstance(start_value, datetime) else None
        )

    vtimezone = ""
    if tzid:
        first = start_value.date() - timedelta(days=366)
        last = start_value.date() + timedelta(days=366 * (5 if rrule_line else 1))
        vtimezone = _vtimezone_block(tzid, first, last)

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//iCloud MCP//EN",
        "CALSCALE:GREGORIAN",
    ]
    if vtimezone:
        lines.append(vtimezone)
    lines += [
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"DTSTAMP:{now.strftime('%Y%m%dT%H%M%SZ')}",
        dtstart_line,
        dtend_line,
        f"SUMMARY:{_escape_text(summary)}",
        "STATUS:CONFIRMED",
        "SEQUENCE:0",
    ]
    if rrule_line:
        lines.append(rrule_line)
    if description:
        lines.append(f"DESCRIPTION:{_escape_text(description)}")
    if location:
        lines.append(f"LOCATION:{_escape_text(location)}")
    if attendees:
        lines.append(f"ORGANIZER;CN={email}:mailto:{email}")
        for attendee_email in attendees:
            lines.append(
                f"ATTENDEE;CN={attendee_email};CUTYPE=INDIVIDUAL;ROLE=REQ-PARTICIPANT;"
                f"PARTSTAT=NEEDS-ACTION;RSVP=TRUE:mailto:{attendee_email}"
            )
    alarm_minutes = _reminder_minutes(reminders, start_value)
    for minutes in alarm_minutes:
        lines += _alarm_lines(minutes)
    lines += ["END:VEVENT", "END:VCALENDAR"]
    ical_data = "\n".join(lines)

    try:
        event = calendar.add_event(ical_data)
    except Exception as e:
        raise ValueError(f"Failed to create event in calendar '{calendar.name}': {e}") from e

    failed = []
    if attendees:
        failed = _notify_attendees(
            email, password, attendees, ical_data, summary, start, end, location, "REQUEST"
        )

    result = {
        "id": str(event.url),
        "url": str(event.url),
        "summary": summary,
        "start": _value_to_iso(start_value),
        "end": _value_to_iso(end_value),
        "all_day": all_day,
        "timezone": tzid or ("UTC" if not all_day else ""),
        "description": description or "",
        "location": location or "",
        "attendees": attendees or [],
        "rrule": rrule_line[len("RRULE:"):] if rrule_line else "",
        "reminders": alarm_minutes,
        "calendar": calendar.name,
    }
    if failed:
        result["invitations_failed"] = failed
    return result


def _load_event(event_id: str, email: str, password: str):
    client = _client_for_url(event_id, email, password)
    event = caldav.CalendarObjectResource(client=client, url=event_id)
    try:
        event.load()
    except Exception as e:
        raise ValueError(f"Could not load event {event_id}: {e}") from e
    return client, event


def _set_datetime(prop: Any, new_value: date | datetime, timezone: str | None) -> None:
    """Assign a new DTSTART/DTEND value, preserving or replacing its timezone."""
    prop.params.pop("X-VOBJ-ORIGINAL-TZID", None)
    prop.params.pop("TZID", None)
    prop.params.pop("VALUE", None)

    if not isinstance(new_value, datetime):
        prop.value = new_value
        return

    if new_value.tzinfo is not None and timezone is None:
        # Explicit offset in the input string (e.g. ...+02:00): store as UTC.
        prop.value = new_value.astimezone(UTC).replace(tzinfo=dateutil_tz.UTC)
        return

    tz_name = timezone
    if tz_name is None:
        old = getattr(prop, "value", None)
        if isinstance(old, datetime) and old.tzinfo is not None:
            # Keep the event's existing zone.
            prop.value = new_value.replace(tzinfo=old.tzinfo)
            return
        tz_name = config.DEFAULT_TIMEZONE

    _zone(tz_name)  # validate
    if tz_name == "UTC":
        prop.value = new_value.replace(tzinfo=dateutil_tz.UTC)
    else:
        prop.value = new_value.replace(tzinfo=None)
        prop.params["TZID"] = [tz_name]


def update_event(
    event_id: str,
    summary: str | None = None,
    start: str | None = None,
    end: str | None = None,
    description: str | None = None,
    location: str | None = None,
    attendees: list[str] | None = None,
    timezone: str | None = None,
    rrule: str | None = None,
    reminders: list[Any] | None = None,
) -> dict[str, Any]:
    email, password = require_auth()
    if attendees is not None:
        attendees = _validate_attendees(attendees)
    client, event = _load_event(event_id, email, password)
    vevent = event.vobject_instance.vevent

    if summary is not None:
        if hasattr(vevent, "summary"):
            vevent.summary.value = summary
        else:
            vevent.add("summary").value = summary
    if start:
        value = _parse_when(start, timezone)
        target = vevent.dtstart if hasattr(vevent, "dtstart") else vevent.add("dtstart")
        _set_datetime(target, value, timezone)
    if end:
        value = _parse_when(end, timezone)
        if hasattr(vevent, "duration"):
            vevent.remove(vevent.duration)
        target = vevent.dtend if hasattr(vevent, "dtend") else vevent.add("dtend")
        _set_datetime(target, value, timezone)
    if timezone and not start and not end:
        for attr in ("dtstart", "dtend"):
            prop = getattr(vevent, attr, None)
            if prop is not None and isinstance(prop.value, datetime):
                _set_datetime(prop, prop.value.replace(tzinfo=None), timezone)

    if rrule is not None:
        if rrule.strip() == "":
            if hasattr(vevent, "rrule"):
                vevent.remove(vevent.rrule)
        else:
            rrule_value = normalize_rrule(rrule)[len("RRULE:"):]
            if hasattr(vevent, "rrule"):
                vevent.rrule.value = rrule_value
            else:
                vevent.add("rrule").value = rrule_value

    if description is not None:
        if hasattr(vevent, "description"):
            vevent.description.value = description
        else:
            vevent.add("description").value = description
    if location is not None:
        if hasattr(vevent, "location"):
            vevent.location.value = location
        else:
            vevent.add("location").value = location

    if attendees is not None:
        if hasattr(vevent, "attendee_list"):
            for att in list(vevent.attendee_list):
                vevent.remove(att)
        for attendee_email in attendees:
            att = vevent.add("attendee")
            att.value = f"mailto:{attendee_email}"
            att.params["CN"] = [attendee_email]
            att.params["CUTYPE"] = ["INDIVIDUAL"]
            att.params["ROLE"] = ["REQ-PARTICIPANT"]
            att.params["PARTSTAT"] = ["NEEDS-ACTION"]
            att.params["RSVP"] = ["TRUE"]
        if attendees and not hasattr(vevent, "organizer"):
            org = vevent.add("organizer")
            org.value = f"mailto:{email}"
            org.params["CN"] = [email]

    if reminders is not None:
        start_prop = getattr(vevent, "dtstart", None)
        alarm_minutes = _reminder_minutes(reminders, start_prop.value if start_prop is not None else None)
        for alarm in list(getattr(vevent, "valarm_list", []) or []):
            vevent.remove(alarm)
        for minutes in alarm_minutes:
            _add_alarm(vevent, minutes)

    # Bump SEQUENCE so clients treat iTIP updates as newer than the original.
    try:
        if hasattr(vevent, "sequence"):
            vevent.sequence.value = str(int(vevent.sequence.value) + 1)
        else:
            vevent.add("sequence").value = "1"
    except Exception:
        pass

    try:
        updated_ical = event.vobject_instance.serialize()
        client.put(event_id, updated_ical, {"Content-Type": "text/calendar; charset=utf-8"})
    except Exception as e:
        raise ValueError(f"Error saving event: {e}") from e

    result = _vevent_to_dict(vevent, str(event.url), "")
    result.pop("calendar", None)

    if attendees is not None and result["attendees"]:
        failed = _notify_attendees(
            email,
            password,
            result["attendees"],
            updated_ical,
            result["summary"],
            result["start"] or "",
            result["end"] or "",
            result["location"] or None,
            "REQUEST",
        )
        if failed:
            result["invitations_failed"] = failed
    return result


def delete_event(event_id: str) -> dict[str, Any]:
    email, password = require_auth()
    client = _client_for_url(event_id, email, password)
    event = caldav.CalendarObjectResource(client=client, url=event_id)

    snapshot: dict[str, Any] | None = None
    ical_data: str | None = None
    try:
        event.load()
        vevent = event.vobject_instance.vevent
        snapshot = _vevent_to_dict(vevent, event_id, "")
        ical_data = event.vobject_instance.serialize()
    except Exception as e:
        logger.warning("Could not load event before deletion: %s", e)

    event.delete()

    result: dict[str, Any] = {"status": "success", "message": f"Event {event_id} deleted"}
    if snapshot:
        result["summary"] = snapshot["summary"]
        if snapshot["attendees"] and ical_data:
            failed = _notify_attendees(
                email,
                password,
                snapshot["attendees"],
                ical_data,
                snapshot["summary"],
                snapshot["start"] or "",
                snapshot["end"] or "",
                snapshot["location"] or None,
                "CANCEL",
            )
            if failed:
                result["cancellations_failed"] = failed
    return result
