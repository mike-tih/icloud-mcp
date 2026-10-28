import ipaddress
import socket
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from icloud_mcp import subscriptions
from icloud_mcp.config import config
from icloud_mcp.subscriptions import FeedError, parse_feed

SECRET = "tok_SECRET123"
FEED_URL = f"https://feeds.example.org/cal.ics?token={SECRET}"

RANGE_START = datetime(2026, 3, 1, tzinfo=UTC)
RANGE_END = datetime(2026, 3, 31, 23, 59, 59, tzinfo=UTC)


def ics(*events: str) -> bytes:
    return ("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//t//EN\r\n" + "".join(events) + "END:VCALENDAR\r\n").encode()


def vevent(*lines: str) -> str:
    return "BEGIN:VEVENT\r\nDTSTAMP:20260101T000000Z\r\n" + "".join(f"{x}\r\n" for x in lines) + "END:VEVENT\r\n"


def run_full(data: bytes, start=RANGE_START, end=RANGE_END):
    return parse_feed(data, "Team", "https://p1-caldav.icloud.com/1/calendars/ABC/", start, end)


def run(data: bytes, start=RANGE_START, end=RANGE_END):
    return run_full(data, start, end)[0]


# ---------------------------------------------------------------------------
# parse_feed
# ---------------------------------------------------------------------------


def test_single_timed_event_in_range():
    events = run(ics(vevent("UID:a", "DTSTART:20260310T180000Z", "DTEND:20260310T190000Z", "SUMMARY:Game", "LOCATION:Field 2")))
    assert len(events) == 1
    e = events[0]
    assert e["summary"] == "Game" and e["location"] == "Field 2"
    assert e["start"] == "2026-03-10T18:00:00+00:00"
    assert e["all_day"] is False and e["recurring"] is False and e["subscribed"] is True
    assert e["calendar"] == "Team"
    assert e["id"].endswith("#a")


def test_events_outside_range_are_dropped():
    data = ics(
        vevent("UID:a", "DTSTART:20260201T180000Z", "DTEND:20260201T190000Z", "SUMMARY:Before"),
        vevent("UID:b", "DTSTART:20260410T180000Z", "DTEND:20260410T190000Z", "SUMMARY:After"),
    )
    assert run(data) == []


def test_all_day_event_uses_dates():
    events = run(ics(vevent("UID:a", "DTSTART;VALUE=DATE:20260315", "DTEND;VALUE=DATE:20260316", "SUMMARY:Holiday")))
    assert events[0]["all_day"] is True
    assert events[0]["start"] == "2026-03-15" and events[0]["end"] == "2026-03-16"


def test_all_day_event_without_dtend_lasts_one_day():
    events = run(ics(vevent("UID:a", "DTSTART;VALUE=DATE:20260315", "SUMMARY:Holiday")))
    assert events[0]["end"] == "2026-03-16"


def test_weekly_recurrence_expands_and_honours_exdate():
    data = ics(
        vevent(
            "UID:r",
            "DTSTART:20260302T180000Z",
            "DTEND:20260302T190000Z",
            "RRULE:FREQ=WEEKLY;BYDAY=MO",
            "EXDATE:20260316T180000Z",
            "SUMMARY:Practice",
        )
    )
    starts = [e["start"][:10] for e in run(data)]
    assert starts == ["2026-03-02", "2026-03-09", "2026-03-23", "2026-03-30"]
    assert all(e["recurring"] for e in run(data))


def test_recurrence_override_replaces_occurrence():
    data = ics(
        vevent("UID:r", "DTSTART:20260302T180000Z", "DTEND:20260302T190000Z", "RRULE:FREQ=WEEKLY;COUNT=3", "SUMMARY:Practice"),
        vevent(
            "UID:r",
            "RECURRENCE-ID:20260309T180000Z",
            "DTSTART:20260309T200000Z",
            "DTEND:20260309T210000Z",
            "SUMMARY:Practice (moved)",
        ),
    )
    events = sorted(run(data), key=lambda e: e["start"])
    assert [(e["summary"], e["start"][:16]) for e in events] == [
        ("Practice", "2026-03-02T18:00"),
        ("Practice (moved)", "2026-03-09T20:00"),
        ("Practice", "2026-03-16T18:00"),
    ]


def test_cancelled_event_is_skipped():
    assert run(ics(vevent("UID:a", "DTSTART:20260310T180000Z", "STATUS:CANCELLED", "SUMMARY:Off"))) == []


def test_floating_time_uses_default_timezone():
    events = run(ics(vevent("UID:a", "DTSTART:20260310T180000", "DTEND:20260310T190000", "SUMMARY:Local")))
    # conftest sets DEFAULT_TIMEZONE=Europe/Berlin; 10 March is before DST, so +01:00.
    assert events[0]["start"] == "2026-03-10T18:00:00+01:00"
    assert events[0]["end"] == "2026-03-10T19:00:00+01:00"


def test_garbage_feed_raises_feed_error():
    with pytest.raises(FeedError):
        run(b"<html>not a calendar</html>")


def test_long_description_is_truncated():
    events = run(ics(vevent("UID:a", "DTSTART:20260310T180000Z", "DTEND:20260310T190000Z", "DESCRIPTION:" + "x" * 5000)))
    assert len(events[0]["description"]) <= subscriptions.DESCRIPTION_MAX_CHARS


# ---------------------------------------------------------------------------
# URL handling and fetching
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("webcal://h.example/x.ics", "http://h.example/x.ics"),
        ("webcals://h.example/x.ics", "https://h.example/x.ics"),
        ("https://h.example/x.ics?t=1", "https://h.example/x.ics?t=1"),
    ],
)
def test_normalize_feed_url(raw, expected):
    assert subscriptions._normalize_feed_url(raw) == expected


@pytest.mark.parametrize("raw", ["file:///etc/passwd", "ftp://h.example/x", "https://user:pw@h.example/x", "not a url"])
def test_normalize_feed_url_rejects(raw):
    with pytest.raises(FeedError):
        subscriptions._normalize_feed_url(raw)


def _resolve_to(monkeypatch, address):
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, port: [(socket.AF_INET, 0, 0, "", (address, 0))])


@pytest.mark.parametrize(
    "address",
    ["127.0.0.1", "10.0.0.5", "192.168.1.1", "169.254.169.254", "0.0.0.0", "100.101.102.103",
     "::1", "::ffff:127.0.0.1", "::ffff:10.0.0.5", "fec0::1"],
)
def test_non_public_destinations_refused(monkeypatch, address):
    _resolve_to(monkeypatch, address)
    with pytest.raises(FeedError, match="non-public"):
        subscriptions._resolve_public("https://h.example/x.ics")


def test_any_non_public_answer_refuses_the_host(monkeypatch):
    monkeypatch.setattr(
        socket, "getaddrinfo",
        lambda host, port: [(socket.AF_INET, 0, 0, "", ("93.184.216.34", 0)), (socket.AF_INET, 0, 0, "", ("10.0.0.5", 0))],
    )
    with pytest.raises(FeedError, match="non-public"):
        subscriptions._resolve_public("https://h.example/x.ics")


@pytest.mark.parametrize("exc", [socket.gaierror("nope"), UnicodeError("label too long")])
def test_resolution_errors_become_feed_errors(monkeypatch, exc):
    def boom(host, port):
        raise exc

    monkeypatch.setattr(socket, "getaddrinfo", boom)
    with pytest.raises(FeedError, match="did not resolve"):
        subscriptions._resolve_public("https://h.example/x.ics")


def test_malformed_urls_become_feed_errors():
    for raw in ["http://[::1/x.ics", "https://h.example:notaport/x.ics"]:
        with pytest.raises(FeedError, match="malformed"):
            subscriptions._normalize_feed_url(raw)


def test_public_destination_allowed_and_returned_for_pinning(monkeypatch):
    _resolve_to(monkeypatch, "93.184.216.34")
    assert subscriptions._resolve_public("https://h.example/x.ics") == "93.184.216.34"


def test_private_destination_allowed_when_configured(monkeypatch):
    _resolve_to(monkeypatch, "10.0.0.5")
    monkeypatch.setattr(config, "SUBSCRIPTIONS_ALLOW_PRIVATE", True)
    assert subscriptions._resolve_public("http://h.example/x.ics") == "10.0.0.5"


class FakeResponse:
    def __init__(self, status=200, body=b"", headers=None):
        self.status_code = status
        self._body = body
        self.headers = headers or {}
        self.is_redirect = status in (301, 302, 303, 307, 308)

    def iter_content(self, chunk_size=65536):
        for i in range(0, len(self._body), chunk_size):
            yield self._body[i : i + chunk_size]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def public_dns(monkeypatch):
    _resolve_to(monkeypatch, "93.184.216.34")


def test_fetch_returns_body(monkeypatch, public_dns):
    monkeypatch.setattr(subscriptions, "_open", lambda url, ip: FakeResponse(body=b"BEGIN:VCALENDAR"))
    assert subscriptions._fetch_feed(FEED_URL) == b"BEGIN:VCALENDAR"


def test_fetch_pins_the_vetted_address(monkeypatch, public_dns):
    seen = {}

    def fake_open(url, ip):
        seen["ip"] = ip
        return FakeResponse(body=b"x")

    monkeypatch.setattr(subscriptions, "_open", fake_open)
    subscriptions._fetch_feed(FEED_URL)
    assert seen["ip"] == "93.184.216.34"


def test_fetch_http_error_has_status_but_not_url(monkeypatch, public_dns):
    monkeypatch.setattr(subscriptions, "_open", lambda url, ip: FakeResponse(status=410))
    with pytest.raises(FeedError) as exc:
        subscriptions._fetch_feed(FEED_URL)
    assert "410" in str(exc.value) and SECRET not in str(exc.value)


def test_fetch_network_error_does_not_leak_url(monkeypatch, public_dns):
    def boom(url, ip):
        raise subscriptions.requests.ConnectionError(f"failed for {url}")

    monkeypatch.setattr(subscriptions, "_open", boom)
    with pytest.raises(FeedError) as exc:
        subscriptions._fetch_feed(FEED_URL)
    assert SECRET not in str(exc.value) and "ConnectionError" in str(exc.value)


def test_fetch_enforces_size_cap(monkeypatch, public_dns):
    monkeypatch.setattr(subscriptions, "MAX_FEED_BYTES", 100)
    monkeypatch.setattr(subscriptions, "_open", lambda url, ip: FakeResponse(body=b"x" * 1000))
    with pytest.raises(FeedError, match="size limit"):
        subscriptions._fetch_feed(FEED_URL)


def test_redirect_to_private_address_refused(monkeypatch):
    answers = {"public.example": "93.184.216.34", "internal.example": "10.0.0.5"}
    monkeypatch.setattr(
        socket, "getaddrinfo", lambda host, port: [(socket.AF_INET, 0, 0, "", (answers[host], 0))]
    )
    monkeypatch.setattr(
        subscriptions, "_open",
        lambda url, ip: FakeResponse(status=302, headers={"Location": "http://internal.example/x"}),
    )
    with pytest.raises(FeedError, match="non-public"):
        subscriptions._fetch_feed("https://public.example/cal.ics")


def test_redirect_loop_is_bounded(monkeypatch, public_dns):
    monkeypatch.setattr(
        subscriptions, "_open", lambda url, ip: FakeResponse(status=302, headers={"Location": FEED_URL})
    )
    with pytest.raises(FeedError, match="redirects"):
        subscriptions._fetch_feed(FEED_URL)


# ---------------------------------------------------------------------------
# Discovery and the tool operations
# ---------------------------------------------------------------------------

HOME = "https://p1-caldav.icloud.com:443/123/calendars/"

PROPFIND = f"""<?xml version="1.0"?>
<multistatus xmlns="DAV:" xmlns:cs="http://calendarserver.org/ns/">
 <response><href>/123/calendars/AAA/</href><propstat><prop>
   <displayname>Packers</displayname>
   <resourcetype><collection/><cs:subscribed/></resourcetype>
   <cs:source><href>{FEED_URL}</href></cs:source>
 </prop></propstat></response>
 <response><href>/123/calendars/BBB/</href><propstat><prop>
   <displayname>Yacht Club</displayname>
   <resourcetype><collection/><cs:subscribed/></resourcetype>
   <cs:source><href>https://other.example/y.ics</href></cs:source>
 </prop></propstat></response>
 <response><href>/123/calendars/CCC/</href><propstat><prop>
   <displayname>Family</displayname>
   <resourcetype><collection/><calendar xmlns="urn:ietf:params:xml:ns:caldav"/></resourcetype>
 </prop></propstat></response>
</multistatus>"""


@pytest.fixture
def fake_account(monkeypatch):
    class FakeClient:
        def principal(self):
            return SimpleNamespace(calendar_home_set=SimpleNamespace(url=HOME))

        def request(self, url, method, body, headers):
            assert method == "PROPFIND" and headers["Depth"] == "1"
            return SimpleNamespace(raw=PROPFIND)

    monkeypatch.setattr(subscriptions, "_get_caldav_client", lambda e, p: FakeClient())
    monkeypatch.setattr(config, "SUBSCRIPTIONS_INCLUDE", frozenset())
    monkeypatch.setattr(config, "SUBSCRIPTIONS_EXCLUDE", frozenset())


def test_discover_finds_only_subscribed_collections(fake_account):
    found = subscriptions._discover("e", "p")
    assert [s["name"] for s in found] == ["Packers", "Yacht Club"]
    assert found[0]["id"] == "https://p1-caldav.icloud.com/123/calendars/AAA/".replace("https://p1-caldav.icloud.com", "https://p1-caldav.icloud.com:443")
    assert found[0]["source"] == FEED_URL


def test_list_subscriptions_hides_feed_url(fake_account):
    result = subscriptions.list_subscriptions()
    assert {s["name"] for s in result} == {"Packers", "Yacht Club"}
    flat = repr(result)
    assert SECRET not in flat and "token" not in flat
    assert {s["feed_host"] for s in result} == {"feeds.example.org", "other.example"}


def test_include_limits_to_named_subscriptions(fake_account, monkeypatch):
    monkeypatch.setattr(config, "SUBSCRIPTIONS_INCLUDE", frozenset({"packers"}))
    assert [s["name"] for s in subscriptions.list_subscriptions()] == ["Packers"]


def test_include_filter_applies_to_event_reads(fake_account, monkeypatch):
    monkeypatch.setattr(config, "SUBSCRIPTIONS_INCLUDE", frozenset({"packers"}))
    monkeypatch.setattr(subscriptions, "_fetch_feed", _feed_for)
    result = subscriptions.list_subscribed_events(None, "2026-03-01", "2026-03-31")
    assert result["feeds_checked"] == 1 and result["feed_errors"] == []


def test_include_and_exclude_filters(fake_account, monkeypatch):
    monkeypatch.setattr(config, "SUBSCRIPTIONS_INCLUDE", frozenset({"packers", "yacht club"}))
    monkeypatch.setattr(config, "SUBSCRIPTIONS_EXCLUDE", frozenset({"yacht club"}))
    assert [s["name"] for s in subscriptions.list_subscriptions()] == ["Packers"]


def _feed_for(source: str) -> bytes:
    if "other.example" in source:
        raise FeedError("HTTP 410")
    return ics(vevent("UID:g", "DTSTART:20260310T180000Z", "DTEND:20260310T190000Z", "SUMMARY:Game"))


def test_list_subscribed_events_reports_failures_and_hides_urls(fake_account, monkeypatch):
    monkeypatch.setattr(subscriptions, "_fetch_feed", _feed_for)
    result = subscriptions.list_subscribed_events(None, "2026-03-01", "2026-03-31")
    assert [e["summary"] for e in result["events"]] == ["Game"]
    assert result["feeds_checked"] == 2
    assert result["feed_errors"] == [{"calendar": "Yacht Club", "error": "HTTP 410"}]
    assert SECRET not in repr(result)


def test_list_subscribed_events_for_one_calendar(fake_account, monkeypatch):
    monkeypatch.setattr(subscriptions, "_fetch_feed", _feed_for)
    result = subscriptions.list_subscribed_events(
        "https://p1-caldav.icloud.com:443/123/calendars/AAA/", "2026-03-01", "2026-03-31"
    )
    assert result["feeds_checked"] == 1 and result["feed_errors"] == []


def test_unknown_calendar_id_is_rejected(fake_account):
    with pytest.raises(ValueError, match="not a subscribed calendar"):
        subscriptions.list_subscribed_events("https://p1-caldav.icloud.com:443/123/calendars/CCC/")


def test_foreign_host_calendar_id_is_rejected(fake_account):
    with pytest.raises(ValueError, match="untrusted host"):
        subscriptions.list_subscribed_events("https://evil.example/123/calendars/AAA/")


def test_end_before_start_is_rejected(fake_account):
    with pytest.raises(ValueError, match="end_date"):
        subscriptions.list_subscribed_events(None, "2026-03-10", "2026-03-01")


def test_default_range_is_now_to_thirty_days(fake_account, monkeypatch):
    seen = {}

    def spy(data, name, cid, start, end):
        seen["span"] = end - start
        return [], []

    monkeypatch.setattr(subscriptions, "_fetch_feed", lambda s: b"")
    monkeypatch.setattr(subscriptions, "parse_feed", spy)
    subscriptions.list_subscribed_events()
    assert timedelta(days=29) < seen["span"] <= timedelta(days=31)


# ---------------------------------------------------------------------------
# Silent-failure regressions
# ---------------------------------------------------------------------------


def test_all_day_series_with_date_until_is_expanded():
    data = ics(vevent("UID:h", "DTSTART;VALUE=DATE:20260302", "RRULE:FREQ=WEEKLY;UNTIL=20260316", "SUMMARY:Break"))
    events, problems = run_full(data)
    assert [e["start"] for e in events] == ["2026-03-02", "2026-03-09", "2026-03-16"]
    assert problems == []


def test_timed_series_with_floating_until_is_expanded():
    data = ics(
        vevent("UID:t", "DTSTART:20260302T180000Z", "DTEND:20260302T190000Z", "RRULE:FREQ=DAILY;UNTIL=20260304T000000", "SUMMARY:x")
    )
    assert len(run(data)) >= 2


def test_cancelled_single_occurrence_does_not_reappear():
    data = ics(
        vevent("UID:r", "DTSTART:20260302T180000Z", "DTEND:20260302T190000Z", "RRULE:FREQ=DAILY;COUNT=3", "SUMMARY:Standup"),
        vevent("UID:r", "RECURRENCE-ID:20260303T180000Z", "DTSTART:20260303T180000Z", "STATUS:CANCELLED", "SUMMARY:Standup"),
    )
    assert [e["start"][:10] for e in run(data)] == ["2026-03-02", "2026-03-04"]


@pytest.mark.parametrize("status", ["CANCELLED", "cancelled", "Cancelled"])
def test_cancelled_master_is_skipped_case_insensitively(status):
    assert run(ics(vevent("UID:a", "DTSTART:20260310T180000Z", f"STATUS:{status}", "SUMMARY:Off"))) == []


def test_override_moved_into_the_range_is_emitted():
    data = ics(
        vevent("UID:r", "DTSTART:20260223T180000Z", "DTEND:20260223T190000Z", "RRULE:FREQ=WEEKLY;COUNT=2", "SUMMARY:Practice"),
        vevent("UID:r", "RECURRENCE-ID:20260302T180000Z", "DTSTART:20260315T180000Z", "DTEND:20260315T190000Z", "SUMMARY:Moved"),
    )
    assert [(e["summary"], e["start"][:10]) for e in run(data)] == [("Moved", "2026-03-15")]


def test_override_moved_out_of_the_range_disappears():
    data = ics(
        vevent("UID:r", "DTSTART:20260309T180000Z", "DTEND:20260309T190000Z", "RRULE:FREQ=WEEKLY;COUNT=1", "SUMMARY:Practice"),
        vevent("UID:r", "RECURRENCE-ID:20260309T180000Z", "DTSTART:20260501T180000Z", "DTEND:20260501T190000Z", "SUMMARY:Moved"),
    )
    assert run(data) == []


def test_uidless_events_get_distinct_ids():
    data = ics(
        vevent("DTSTART:20260310T180000Z", "DTEND:20260310T190000Z", "SUMMARY:A"),
        vevent("DTSTART:20260311T180000Z", "DTEND:20260311T190000Z", "SUMMARY:B"),
    )
    ids = [e["id"] for e in run(data)]
    assert len(set(ids)) == 2


def test_recurring_occurrences_get_distinct_ids():
    data = ics(vevent("UID:r", "DTSTART:20260302T180000Z", "DTEND:20260302T190000Z", "RRULE:FREQ=DAILY;COUNT=3", "SUMMARY:x"))
    ids = [e["id"] for e in run(data)]
    assert len(ids) == len(set(ids)) == 3


def test_occurrence_cap_is_reported(monkeypatch):
    monkeypatch.setattr(subscriptions, "MAX_OCCURRENCES_PER_EVENT", 5)
    data = ics(vevent("UID:h", "DTSTART:20260301T000000Z", "DTEND:20260301T003000Z", "RRULE:FREQ=HOURLY", "SUMMARY:Spam"))
    events, problems = run_full(data)
    assert len(events) == 5
    assert any("cut at 5 occurrences" in p for p in problems)


def test_bad_event_is_counted_not_hidden():
    data = ics(
        vevent("UID:good", "DTSTART:20260310T180000Z", "DTEND:20260310T190000Z", "SUMMARY:Good"),
        vevent("UID:bad", "DTSTART:20260310T180000Z", "DTEND:20260310T190000Z", "RRULE:FREQ=BOGUS", "SUMMARY:Bad"),
        vevent("UID:nostart", "SUMMARY:No start"),
    )
    events, problems = run_full(data)
    assert [e["summary"] for e in events] == ["Good"]
    assert problems == ["2 event(s) skipped: could not be parsed"]


def test_invalid_default_timezone_is_a_clear_error(monkeypatch, fake_account):
    monkeypatch.setattr(config, "DEFAULT_TIMEZONE", "Not/AZone")
    with pytest.raises(ValueError, match="DEFAULT_TIMEZONE"):
        run(ics(vevent("UID:a", "DTSTART;VALUE=DATE:20260310", "SUMMARY:x")))
    with pytest.raises(ValueError, match="DEFAULT_TIMEZONE"):
        subscriptions.list_subscribed_events()


def test_unexpected_error_in_one_feed_does_not_abort_the_others(fake_account, monkeypatch):
    def fetch(source):
        if "other.example" in source:
            raise RuntimeError(f"boom {source}")
        return _feed_for(source)

    monkeypatch.setattr(subscriptions, "_fetch_feed", fetch)
    result = subscriptions.list_subscribed_events(None, "2026-03-01", "2026-03-31")
    assert [e["summary"] for e in result["events"]] == ["Game"]
    assert result["feed_errors"] == [{"calendar": "Yacht Club", "error": "unexpected RuntimeError"}]
    assert "other.example" not in repr(result)


def test_filter_matching_nothing_is_reported(fake_account, monkeypatch):
    monkeypatch.setattr(config, "SUBSCRIPTIONS_INCLUDE", frozenset({"holidyas"}))
    monkeypatch.setattr(subscriptions, "_fetch_feed", _feed_for)
    result = subscriptions.list_subscribed_events(None, "2026-03-01", "2026-03-31")
    assert result["feeds_checked"] == 0
    assert result["unmatched_filters"] == ["holidyas"]


def test_events_sort_by_instant_across_offsets():
    data = ics(
        vevent("UID:b", "DTSTART;TZID=America/Chicago:20260310T230000", "DTEND;TZID=America/Chicago:20260310T233000", "SUMMARY:Later"),
        vevent("UID:a", "DTSTART:20260311T000000Z", "DTEND:20260311T003000Z", "SUMMARY:Earlier"),
    )
    result_events = run(data)
    assert len(result_events) == 2  # parse_feed order is unspecified; sorting happens in the tool


def test_tool_sorts_chronologically_across_offsets(fake_account, monkeypatch):
    feed = ics(
        vevent("UID:b", "DTSTART;TZID=America/Chicago:20260310T230000", "DTEND;TZID=America/Chicago:20260310T233000", "SUMMARY:Later"),
        vevent("UID:a", "DTSTART:20260311T000000Z", "DTEND:20260311T003000Z", "SUMMARY:Earlier"),
    )
    monkeypatch.setattr(config, "SUBSCRIPTIONS_INCLUDE", frozenset({"packers"}))
    monkeypatch.setattr(subscriptions, "_fetch_feed", lambda source: feed)
    result = subscriptions.list_subscribed_events(None, "2026-03-01", "2026-03-31")
    assert [e["summary"] for e in result["events"]] == ["Earlier", "Later"]


def test_dropped_events_are_counted(fake_account, monkeypatch):
    monkeypatch.setattr(subscriptions, "MAX_EVENTS", 1)
    monkeypatch.setattr(config, "SUBSCRIPTIONS_INCLUDE", frozenset({"packers"}))
    feed = ics(
        vevent("UID:a", "DTSTART:20260310T180000Z", "DTEND:20260310T190000Z", "SUMMARY:A"),
        vevent("UID:b", "DTSTART:20260311T180000Z", "DTEND:20260311T190000Z", "SUMMARY:B"),
    )
    monkeypatch.setattr(subscriptions, "_fetch_feed", lambda source: feed)
    result = subscriptions.list_subscribed_events(None, "2026-03-01", "2026-03-31")
    assert len(result["events"]) == 1 and result["truncated"] is True and result["dropped_events"] == 1


class _Client:
    def __init__(self, raw):
        self._raw = raw

    def principal(self):
        return SimpleNamespace(calendar_home_set=SimpleNamespace(url=HOME))

    def request(self, url, method, body, headers):
        return SimpleNamespace(raw=self._raw)


@pytest.mark.parametrize(
    "raw",
    [
        "<html><body>Sign in</body></html>",
        "not xml at all",
        '<multistatus xmlns="DAV:"/>',
    ],
)
def test_unexpected_propfind_reply_raises_instead_of_returning_nothing(monkeypatch, raw):
    monkeypatch.setattr(subscriptions, "_get_caldav_client", lambda e, p: _Client(raw))
    with pytest.raises(FeedError):
        subscriptions._discover("e", "p")


def test_subscription_without_source_is_reported(monkeypatch):
    raw = """<multistatus xmlns="DAV:" xmlns:cs="http://calendarserver.org/ns/">
      <response><href>/123/calendars/ZZZ/</href><propstat><prop>
        <displayname>Orphan</displayname>
        <resourcetype><collection/><cs:subscribed/></resourcetype>
      </prop></propstat></response></multistatus>"""
    monkeypatch.setattr(subscriptions, "_get_caldav_client", lambda e, p: _Client(raw))
    monkeypatch.setattr(config, "SUBSCRIPTIONS_INCLUDE", frozenset())
    monkeypatch.setattr(config, "SUBSCRIPTIONS_EXCLUDE", frozenset())
    assert subscriptions.list_subscriptions() == [{"id": "https://p1-caldav.icloud.com:443/123/calendars/ZZZ/", "name": "Orphan", "feed_host": ""}]
    result = subscriptions.list_subscribed_events(None, "2026-03-01", "2026-03-31")
    assert result["feed_errors"] == [{"calendar": "Orphan", "error": "subscription has no feed URL"}]


# ---------------------------------------------------------------------------
# Time limit, pinning against DNS rebinding, RDATE, zero-length events
# ---------------------------------------------------------------------------


def test_slow_feed_hits_the_total_time_limit(monkeypatch, public_dns):
    monkeypatch.setattr(subscriptions, "TOTAL_TIMEOUT", -1.0)
    monkeypatch.setattr(subscriptions, "_open", lambda url, ip: FakeResponse(body=b"x" * 10))
    with pytest.raises(FeedError, match="too long"):
        subscriptions._fetch_feed(FEED_URL)


def test_rdate_adds_instances():
    data = ics(
        vevent("UID:a", "DTSTART:20260303T090000Z", "DTEND:20260303T100000Z", "RRULE:FREQ=DAILY;COUNT=1",
               "RDATE:20260310T090000Z", "SUMMARY:Extra")
    )
    assert [e["start"][:10] for e in run(data)] == ["2026-03-03", "2026-03-10"]


def test_rdate_without_rrule():
    data = ics(vevent("UID:a", "DTSTART:20260303T090000Z", "DTEND:20260303T100000Z", "RDATE:20260310T090000Z", "SUMMARY:x"))
    assert len(run(data)) == 2


def test_zero_length_event_at_range_start_is_included():
    events = run(ics(vevent("UID:a", "DTSTART:20260301T000000Z", "SUMMARY:Midnight")))
    assert [e["summary"] for e in events] == ["Midnight"]


def test_zero_length_event_at_range_end_is_excluded():
    assert run(ics(vevent("UID:a", "DTSTART:20260331T235959Z", "SUMMARY:x")), end=datetime(2026, 3, 31, 23, 59, 59, tzinfo=UTC)) == []


def test_secondly_series_is_bounded(monkeypatch):
    import time as _t

    monkeypatch.setattr(subscriptions, "MAX_OCCURRENCES_PER_EVENT", 50)
    data = ics(vevent("UID:s", "DTSTART:20260301T000000Z", "DTEND:20260301T000001Z", "RRULE:FREQ=SECONDLY", "SUMMARY:Flood"))
    t0 = _t.monotonic()
    events, problems = run_full(data)
    assert len(events) == 50 and problems and _t.monotonic() - t0 < 2.0


def test_connection_is_pinned_to_the_first_resolution(monkeypatch):
    """A second DNS answer (rebinding) must not change where the request goes: resolve once,
    connect to that address, send the original Host header."""
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    seen = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            seen["host"] = self.headers.get("Host")
            body = ics(vevent("UID:p", "DTSTART:20260310T180000Z", "DTEND:20260310T190000Z", "SUMMARY:Pinned"))
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    answers = iter(["127.0.0.1", "192.0.2.1", "192.0.2.1", "192.0.2.1"])
    calls = []

    real_getaddrinfo = socket.getaddrinfo

    def fake_getaddrinfo(host, *args, **kwargs):
        try:
            ipaddress.ip_address(host)
        except ValueError:
            calls.append(host)
            return [(socket.AF_INET, 0, 0, "", (next(answers), 0))]
        return real_getaddrinfo(host, *args, **kwargs)  # literal IPs resolve to themselves

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    monkeypatch.setattr(config, "SUBSCRIPTIONS_ALLOW_PRIVATE", True)
    try:
        data = subscriptions._fetch_feed(f"http://feed.test:{port}/cal.ics")
    finally:
        server.shutdown()
    assert b"Pinned" in data
    assert seen["host"] == f"feed.test:{port}"
    assert calls == ["feed.test"]  # requests never resolved the name itself


def test_session_ignores_netrc_and_proxy_environment(monkeypatch):
    seen = {}

    class SpySession:
        trust_env = True

        def mount(self, prefix, adapter):
            seen.setdefault("mounts", []).append(prefix)

        def get(self, url, **kw):
            seen["trust_env"] = self.trust_env
            seen["kw"] = kw
            return FakeResponse(body=b"x")

        def close(self):
            seen["closed"] = True

    monkeypatch.setattr(subscriptions.requests, "Session", SpySession)
    with subscriptions._open("https://h.example/x.ics", "93.184.216.34") as resp:
        assert resp.status_code == 200
    assert seen["trust_env"] is False and seen["closed"] is True
    assert seen["kw"]["allow_redirects"] is False and "auth" not in seen["kw"]
    assert sorted(seen["mounts"]) == ["http://", "https://"]


def test_timed_series_with_date_until_includes_the_last_day():
    data = ics(
        vevent("UID:t", "DTSTART:20260302T180000Z", "DTEND:20260302T190000Z", "RRULE:FREQ=WEEKLY;UNTIL=20260316", "SUMMARY:x")
    )
    assert [e["start"][:10] for e in run(data)] == ["2026-03-02", "2026-03-09", "2026-03-16"]


def test_propfind_with_a_foreign_root_element_is_rejected(monkeypatch):
    raw = """<error><response><href>/x/</href><resourcetype xmlns:cs="http://calendarserver.org/ns/"><cs:subscribed/></resourcetype></response></error>"""
    monkeypatch.setattr(subscriptions, "_get_caldav_client", lambda e, p: _Client(raw))
    with pytest.raises(FeedError, match="multistatus"):
        subscriptions._discover("e", "p")
