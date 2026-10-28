"""Read-only access to subscribed (webcal / ICS feed) calendars.

iCloud lists a subscribed calendar as a CalDAV collection of type ``subscribed``
(not ``calendar``), so ``principal.calendars()`` never returns it, and the
collection itself serves no events over CalDAV. What iCloud does return, in the
``cs:source`` property, is the original feed URL. This module reads those URLs
from the account's own collection listing and fetches the feeds directly.

Security notes:

* Feed URLs routinely embed secret tokens. They are used internally and never
  returned or logged; tool output carries only the feed's host name.
* The account credentials are never sent to a feed host (plain, unauthenticated
  requests only).
* Feed URLs come from the account, not from the caller, but are still treated as
  untrusted: http/https only, no embedded credentials, redirects re-validated,
  non-global addresses (private, loopback, link-local, CGNAT/Tailscale, ...) refused
  unless ``ICLOUD_SUBSCRIPTIONS_ALLOW_PRIVATE`` is set. The host is resolved once and
  the connection is pinned to the vetted address (the Host header and TLS server name
  keep the original name), so a second DNS answer cannot redirect the request. Size
  and total time are capped, and ``.netrc`` / proxy environment settings are ignored.
* Event text comes from third parties. Callers must treat it as data, never as
  instructions.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, date, datetime, time, timedelta
from time import monotonic
from typing import Any
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import icalendar
import requests
from dateutil.rrule import rruleset, rrulestr
from defusedxml import ElementTree as SafeET
from requests.adapters import HTTPAdapter

from .auth import require_auth
from .calendar import _get_caldav_client, _parse_range_bound, _value_to_iso
from .config import config
from .mail_utils import clean_rich_text
from .urls import ensure_icloud_url

logger = logging.getLogger("icloud_mcp.subscriptions")

MAX_FEED_BYTES = 5 * 1024 * 1024
MAX_REDIRECTS = 3
MAX_OCCURRENCES_PER_EVENT = 1000
MAX_EVENTS = 1000
MAX_RECURRENCE_ID_KEYS = 5000
FETCH_TIMEOUT = (5, 20)
TOTAL_TIMEOUT = 30.0  # wall-clock budget per feed across redirects and slow reads
FETCH_WORKERS = 8
DESCRIPTION_MAX_CHARS = 500
TEXT_MAX_CHARS = 200

_PROPFIND_BODY = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<d:propfind xmlns:d="DAV:" xmlns:cs="http://calendarserver.org/ns/">'
    "<d:prop><d:displayname/><d:resourcetype/><cs:source/></d:prop></d:propfind>"
)


class FeedError(Exception):
    """A feed could not be fetched or parsed. The message never contains the URL."""


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _validate_timezone() -> str:
    name = config.DEFAULT_TIMEZONE
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError) as e:
        raise ValueError(f"DEFAULT_TIMEZONE {name!r} is not a valid IANA timezone") from e
    return name


def _discover(email: str, password: str) -> list[dict[str, str]]:
    """Subscribed collections of the account: ``id``, ``name`` and the private ``source``.

    ``source`` is empty for a subscription iCloud returned without a feed URL; callers
    report those instead of dropping them. A response that is not a CalDAV multistatus
    raises, so a changed server reply is never mistaken for "no subscriptions".
    """
    client = _get_caldav_client(email, password)
    home = client.principal().calendar_home_set
    home_url = str(home.url)
    response = client.request(
        home_url, "PROPFIND", _PROPFIND_BODY, {"Depth": "1", "Content-Type": "application/xml"}
    )
    raw = getattr(response, "raw", None)
    if raw is None:
        raise FeedError("unexpected PROPFIND response from iCloud")
    try:
        root = SafeET.fromstring(raw if isinstance(raw, bytes) else raw.encode("utf-8"))
    except Exception as e:
        raise FeedError("PROPFIND response is not valid XML") from e
    if _local(root.tag) != "multistatus":
        raise FeedError("PROPFIND response is not a CalDAV multistatus")

    found = []
    responses = 0
    for resp in root.iter():
        if _local(resp.tag) != "response":
            continue
        responses += 1
        href = name = source = ""
        subscribed = False
        for el in resp.iter():
            tag = _local(el.tag)
            if tag == "href" and not href and el.text:
                href = el.text.strip()
            elif tag == "displayname" and el.text:
                name = el.text
            elif tag == "subscribed":
                subscribed = True
            elif tag == "source":
                hrefs = [i.text.strip() for i in el.iter() if _local(i.tag) == "href" and i.text]
                source = hrefs[0] if hrefs else (el.text or "").strip()
        if subscribed and href:
            found.append({"id": urljoin(home_url, href), "name": name or "Unnamed Subscription", "source": source})
    if responses == 0:
        raise FeedError("PROPFIND response listed no collections")
    return found


def _excluded(name: str) -> bool:
    """True when config filters the subscription out (INCLUDE set and not listed, or EXCLUDE listed)."""
    key = name.strip().lower()
    if config.SUBSCRIPTIONS_INCLUDE and key not in config.SUBSCRIPTIONS_INCLUDE:
        return True
    return key in config.SUBSCRIPTIONS_EXCLUDE


def _unmatched_filters(discovered: list[dict[str, str]]) -> list[str]:
    """INCLUDE/EXCLUDE names that match no subscription (usually a typo)."""
    names = {s["name"].strip().lower() for s in discovered}
    unmatched = sorted((config.SUBSCRIPTIONS_INCLUDE | config.SUBSCRIPTIONS_EXCLUDE) - names)
    if unmatched:
        logger.warning("ICLOUD_SUBSCRIPTIONS_INCLUDE/EXCLUDE names match no subscription: %s", unmatched)
    return unmatched


def _feed_host(source: str) -> str:
    return urlparse(source).hostname or ""


def list_subscriptions() -> list[dict[str, Any]]:
    """Subscribed calendars with their IDs. The feed URL is never included, only its host."""
    email, password = require_auth()
    discovered = _discover(email, password)
    _unmatched_filters(discovered)
    result = []
    for sub in discovered:
        if _excluded(sub["name"]):
            continue
        result.append({"id": sub["id"], "name": sub["name"], "feed_host": _feed_host(sub["source"])})
    return sorted(result, key=lambda s: s["name"].lower())


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------


def _normalize_feed_url(source: str) -> str:
    try:
        parsed = urlparse(source.strip())
        parsed.port  # noqa: B018 - raises ValueError for an invalid port
    except ValueError as e:
        raise FeedError("feed URL is malformed") from e
    scheme = parsed.scheme.lower()
    if scheme == "webcal":
        scheme = "http"
    elif scheme == "webcals":
        scheme = "https"
    if scheme not in ("http", "https") or not parsed.hostname:
        raise FeedError("feed URL is not http(s)")
    if parsed.username or parsed.password:
        raise FeedError("feed URL contains credentials")
    return parsed._replace(scheme=scheme).geturl()


def _resolve_public(url: str) -> str:
    """Resolve the feed host once and return the address to connect to.

    Unless ICLOUD_SUBSCRIPTIONS_ALLOW_PRIVATE is set, every resolved address must be
    globally routable. The caller pins the connection to the returned address.
    """
    host = urlparse(url).hostname or ""
    try:
        infos = socket.getaddrinfo(host, None)
    except (socket.gaierror, UnicodeError) as e:
        raise FeedError("feed host did not resolve") from e
    if not infos:
        raise FeedError("feed host did not resolve")
    addresses = []
    for info in infos:
        try:
            address = ipaddress.ip_address(info[4][0].split("%")[0])
        except ValueError as e:
            raise FeedError("feed host resolved to an unparseable address") from e
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
            address = address.ipv4_mapped
        addresses.append(address)
    if not config.SUBSCRIPTIONS_ALLOW_PRIVATE:
        for address in addresses:
            if not address.is_global or address.is_multicast or getattr(address, "is_site_local", False):
                raise FeedError("feed host resolves to a non-public address")
    return str(addresses[0])


class _PinnedAdapter(HTTPAdapter):
    """Connect to a vetted IP while keeping the original Host header and TLS server name."""

    def __init__(self, ip: str, hostname: str):
        self._ip = ip
        self._hostname = hostname
        super().__init__()

    def init_poolmanager(self, connections, maxsize, block=False, **pool_kwargs):
        super().init_poolmanager(
            connections,
            maxsize,
            block,
            server_hostname=self._hostname,
            assert_hostname=self._hostname,
            **pool_kwargs,
        )

    def send(self, request, **kwargs):
        parsed = urlparse(request.url)
        ip = f"[{self._ip}]" if ":" in self._ip else self._ip
        port = f":{parsed.port}" if parsed.port else ""
        request.headers["Host"] = parsed.netloc
        request.url = parsed._replace(netloc=f"{ip}{port}").geturl()
        return super().send(request, **kwargs)


@contextmanager
def _open(url: str, ip: str):
    """GET ``url`` (unauthenticated, no redirects) over a connection pinned to ``ip``."""
    session = requests.Session()
    session.trust_env = False  # no .netrc credentials, no proxy environment
    adapter = _PinnedAdapter(ip, urlparse(url).hostname or "")
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    try:
        with session.get(url, stream=True, timeout=FETCH_TIMEOUT, allow_redirects=False) as resp:
            yield resp
    finally:
        session.close()


def _fetch_feed(source: str) -> bytes:
    if not source:
        raise FeedError("subscription has no feed URL")
    deadline = monotonic() + TOTAL_TIMEOUT
    url = _normalize_feed_url(source)
    for _ in range(MAX_REDIRECTS + 1):
        ip = _resolve_public(url)
        try:
            with _open(url, ip) as resp:
                if resp.is_redirect or resp.status_code in (301, 302, 303, 307, 308):
                    location = resp.headers.get("Location")
                    if not location:
                        raise FeedError(f"HTTP {resp.status_code} without Location")
                    url = _normalize_feed_url(urljoin(url, location))
                    continue
                if resp.status_code != 200:
                    raise FeedError(f"HTTP {resp.status_code}")
                body = bytearray()
                for chunk in resp.iter_content(chunk_size=65536):
                    body.extend(chunk)
                    if len(body) > MAX_FEED_BYTES:
                        raise FeedError("feed larger than the size limit")
                    if monotonic() > deadline:
                        raise FeedError("feed took too long")
                return bytes(body)
        except requests.RequestException as e:
            # Exception text can embed the URL (and its token); report the class only.
            raise FeedError(type(e).__name__) from None
        if monotonic() > deadline:
            raise FeedError("feed took too long")
    raise FeedError("too many redirects")


# ---------------------------------------------------------------------------
# Parsing and recurrence expansion
# ---------------------------------------------------------------------------


def _to_aware(value: date | datetime, tzname: str) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=ZoneInfo(tzname))
    return datetime.combine(value, time.min, tzinfo=ZoneInfo(tzname))


def _localize(value: date | datetime, tzname: str) -> date | datetime:
    """Floating datetimes get the default timezone so every timed value carries an offset."""
    if isinstance(value, datetime) and value.tzinfo is None:
        return value.replace(tzinfo=ZoneInfo(tzname))
    return value


def _prop_values(component: Any, name: str) -> list[date | datetime]:
    """All values of a possibly repeated, possibly multi-valued date property (e.g. EXDATE)."""
    raw = component.get(name)
    if raw is None:
        return []
    items = raw if isinstance(raw, list) else [raw]
    values = []
    for item in items:
        values.extend(entry.dt for entry in item.dts)
    return values


def _occurrence_key(value: date | datetime, tzname: str) -> Any:
    if isinstance(value, datetime):
        return _to_aware(value, tzname).astimezone(UTC)
    return value


def _cancelled(component: Any) -> bool:
    return str(component.get("STATUS") or "").strip().upper() == "CANCELLED"


def _event_end(component: Any, start: date | datetime) -> date | datetime:
    if component.get("DTEND") is not None:
        return component.decoded("DTEND")
    if component.get("DURATION") is not None:
        return start + component.decoded("DURATION")
    return start + timedelta(days=1) if not isinstance(start, datetime) else start


def _overlaps(start: date | datetime, length: timedelta, range_start: datetime, range_end: datetime, tzname: str) -> bool:
    s = _to_aware(start, tzname)
    if length <= timedelta(0):
        return range_start <= s < range_end
    return s < range_end and s + length > range_start


def _rrule_text(rrule: Any, anchor: datetime) -> str:
    """RRULE text with UNTIL expressed in UTC.

    dateutil requires an aware UNTIL when DTSTART is aware, but feeds commonly give an
    all-day UNTIL date (or a floating one) next to a date/floating DTSTART.
    """
    parts = dict(rrule)
    until = parts.get("UNTIL")
    if until:
        fixed = []
        for u in until:
            if isinstance(u, datetime):
                aware = u if u.tzinfo else u.replace(tzinfo=anchor.tzinfo)
            else:
                aware = datetime.combine(u, time(23, 59, 59), tzinfo=anchor.tzinfo)
            fixed.append(aware.astimezone(UTC))
        parts["UNTIL"] = fixed
    return icalendar.vRecur(parts).to_ical().decode()


def _expand(
    component: Any, range_start: datetime, range_end: datetime, tzname: str
) -> tuple[list[tuple[date | datetime, date | datetime]], bool]:
    """((start, end) of each occurrence overlapping the range, whether the occurrence cap was hit)."""
    start = component.decoded("DTSTART")
    end = _event_end(component, start)
    all_day = not isinstance(start, datetime)
    length = _to_aware(end, tzname) - _to_aware(start, tzname)
    rrule = component.get("RRULE")
    rdates = _prop_values(component, "RDATE")

    if rrule is None and not rdates:
        hit = _overlaps(start, length, range_start, range_end, tzname)
        return ([(start, end)] if hit else []), False

    anchor = _to_aware(start, tzname)
    rule = rruleset()
    if rrule is not None:
        rule.rrule(rrulestr(_rrule_text(rrule, anchor), dtstart=anchor))
    rule.rdate(anchor)  # DTSTART is always an instance
    for extra in rdates:
        rule.rdate(_to_aware(extra, tzname))
    excluded = {_occurrence_key(v, tzname) for v in _prop_values(component, "EXDATE")}
    out: list[tuple[date | datetime, date | datetime]] = []
    for occurrence in rule.xafter(range_start - length, inc=True):
        if occurrence >= range_end:
            break
        value: date | datetime = occurrence.date() if all_day else occurrence
        if _occurrence_key(value, tzname) in excluded or not _overlaps(value, length, range_start, range_end, tzname):
            continue
        if len(out) >= MAX_OCCURRENCES_PER_EVENT:
            return out, True
        out.append((value, (value + (end - start)) if all_day else occurrence + length))
    return out, False


def _trim(value: Any, limit: int) -> str:
    text = clean_rich_text(value).strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def parse_feed(
    data: bytes, calendar_name: str, calendar_id: str, range_start: datetime, range_end: datetime
) -> tuple[list[dict[str, Any]], list[str]]:
    """Events of one ICS feed overlapping ``[range_start, range_end)``, plus problems.

    ``problems`` lists anything that made the result incomplete (events that could not be
    parsed, a series cut at the occurrence cap), so callers never see a silent gap.
    """
    tzname = _validate_timezone()
    try:
        cal = icalendar.Calendar.from_ical(data)
    except Exception as e:
        raise FeedError("feed is not valid iCalendar") from e

    skipped = 0
    capped = 0
    masters: list[tuple[str, Any]] = []
    overrides: dict[tuple[str, Any], Any] = {}
    for index, component in enumerate(cal.walk("VEVENT")):
        try:
            if component.get("DTSTART") is None:
                skipped += 1
                continue
            uid = str(component.get("UID") or "").strip()
            recurrence_id = component.get("RECURRENCE-ID")
            if recurrence_id is not None:
                if not uid:
                    uid = f"nouid-override-{index}"
                if len(overrides) < MAX_RECURRENCE_ID_KEYS:
                    overrides[(uid, _occurrence_key(recurrence_id.dt, tzname))] = component
            else:
                masters.append((uid or f"nouid-{index}", component))
        except Exception:
            skipped += 1

    events: list[dict[str, Any]] = []

    def emit(source: Any, uid: str, start: date | datetime, end: date | datetime, recurring: bool) -> None:
        start_iso = _value_to_iso(_localize(start, tzname))
        events.append(
            {
                "id": f"{calendar_id}#{uid}" + (f"@{start_iso}" if recurring else ""),
                "summary": _trim(source.get("SUMMARY"), TEXT_MAX_CHARS),
                "description": _trim(source.get("DESCRIPTION"), DESCRIPTION_MAX_CHARS),
                "location": _trim(source.get("LOCATION"), TEXT_MAX_CHARS),
                "start": start_iso,
                "end": _value_to_iso(_localize(end, tzname)),
                "all_day": not isinstance(start, datetime),
                "recurring": recurring,
                "calendar": calendar_name,
                "subscribed": True,
                "_sort": _to_aware(start, tzname).timestamp(),
            }
        )

    for uid, component in masters:
        if _cancelled(component):
            continue
        try:
            occurrences, hit_cap = _expand(component, range_start, range_end, tzname)
            if hit_cap:
                capped += 1
            recurring = component.get("RRULE") is not None or component.get("RDATE") is not None
            for start, end in occurrences:
                # An override replaces its occurrence; it is emitted below at its own time,
                # which may fall in the range even when the original time does not.
                if (uid, _occurrence_key(start, tzname)) in overrides:
                    continue
                emit(component, uid, start, end, recurring)
        except Exception:
            skipped += 1

    for (uid, _key), component in overrides.items():
        if _cancelled(component):
            continue
        try:
            start = component.decoded("DTSTART")
            end = _event_end(component, start)
            length = _to_aware(end, tzname) - _to_aware(start, tzname)
            if _overlaps(start, length, range_start, range_end, tzname):
                emit(component, uid, start, end, True)
        except Exception:
            skipped += 1

    problems = []
    if skipped:
        problems.append(f"{skipped} event(s) skipped: could not be parsed")
    if capped:
        problems.append(f"{capped} recurring series cut at {MAX_OCCURRENCES_PER_EVENT} occurrences")
    if skipped or capped:
        logger.warning("Feed %r incomplete: %s", calendar_name, "; ".join(problems))
    return events, problems


# ---------------------------------------------------------------------------
# Operation
# ---------------------------------------------------------------------------


def list_subscribed_events(
    calendar_id: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
) -> dict[str, Any]:
    email, password = require_auth()
    _validate_timezone()
    now = datetime.now(UTC)
    start = _parse_range_bound(start_date, now, end_of_day=False)
    end = _parse_range_bound(end_date, now + timedelta(days=30), end_of_day=True)
    if end <= start:
        raise ValueError("end_date must be after start_date")

    discovered = _discover(email, password)
    unmatched = _unmatched_filters(discovered)
    subscriptions = [s for s in discovered if not _excluded(s["name"])]
    if calendar_id:
        wanted = ensure_icloud_url(calendar_id, "calendar")
        subscriptions = [s for s in subscriptions if s["id"].rstrip("/") == wanted.rstrip("/")]
        if not subscriptions:
            raise ValueError("calendar_id is not a subscribed calendar; see calendar_list_subscriptions")

    def load(sub: dict[str, str]) -> tuple[dict[str, str], list[dict[str, Any]], list[str], str | None]:
        try:
            data = _fetch_feed(sub["source"])
            feed_events, problems = parse_feed(data, sub["name"], sub["id"], start, end)
            return sub, feed_events, problems, None
        except FeedError as e:
            return sub, [], [], str(e)
        except Exception as e:  # one bad feed must not abort the others; class name only
            logger.warning("Subscription %r failed: %s", sub["name"], type(e).__name__)
            return sub, [], [], f"unexpected {type(e).__name__}"

    events: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    with ThreadPoolExecutor(max_workers=FETCH_WORKERS) as pool:
        for sub, feed_events, problems, error in pool.map(load, subscriptions):
            if error is not None:
                errors.append({"calendar": sub["name"], "error": error})
            for problem in problems:
                errors.append({"calendar": sub["name"], "error": problem})
            events.extend(feed_events)
    events.sort(key=lambda e: e["_sort"])
    for event in events:
        del event["_sort"]
    dropped = max(0, len(events) - MAX_EVENTS)
    return {
        "events": events[:MAX_EVENTS],
        "truncated": dropped > 0,
        "dropped_events": dropped,
        "feeds_checked": len(subscriptions),
        "feed_errors": errors,
        "unmatched_filters": unmatched,
    }
