"""Minimal CalDAV client + iCalendar helpers (stdlib only), aimed at iCloud.

Covers what the tools need: principal / calendar-home discovery, calendar listing,
time-range event queries (server-side recurrence expansion), and GET/PUT/DELETE of
single events with ETag preconditions.
"""

from __future__ import annotations

import base64
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

NS = {"d": "DAV:", "c": "urn:ietf:params:xml:ns:caldav", "cs": "http://calendarserver.org/ns/",
      "ic": "http://apple.com/ns/ical/"}
DEFAULT_URL = "https://caldav.icloud.com/"


class CalDAVError(Exception):
    pass


@dataclass
class Calendar:
    name: str
    url: str
    components: list[str] = field(default_factory=list)


@dataclass
class Event:
    uid: str
    href: str
    etag: str
    summary: str
    start: datetime | date
    end: datetime | date | None
    all_day: bool
    location: str = ""
    description: str = ""
    recurring: bool = False
    raw: str = ""


# ---------------------------------------------------------------- HTTP

class CalDAVClient:
    def __init__(self, username: str, password: str, base_url: str = DEFAULT_URL, timeout: float = 30.0):
        self.base_url = base_url if base_url.endswith("/") else base_url + "/"
        self.timeout = timeout
        token = base64.b64encode(f"{username}:{password}".encode()).decode()
        self._auth = f"Basic {token}"
        self._ctx = ssl.create_default_context()

    def request(self, method: str, url: str, body: Optional[str] = None, headers: Optional[dict] = None,
                depth: Optional[str] = None) -> tuple[int, dict, str]:
        full = urllib.parse.urljoin(self.base_url, url)
        if urllib.parse.urlparse(full).scheme != "https" and not full.startswith("http://127.0.0.1"):
            raise CalDAVError(f"refusing non-HTTPS URL {full}")
        req = urllib.request.Request(full, data=body.encode("utf-8") if body is not None else None, method=method)
        req.add_header("Authorization", self._auth)
        if body is not None:
            req.add_header("Content-Type", "application/xml; charset=utf-8" if body.lstrip().startswith("<")
                           else "text/calendar; charset=utf-8")
        if depth is not None:
            req.add_header("Depth", depth)
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=self._ctx if full.startswith("https") else None) as r:
                return r.status, dict(r.headers), r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            text = e.read().decode("utf-8", "replace")
            if e.code == 401:
                raise CalDAVError("iCloud rejected the credentials (check the Apple ID and app-specific password)")
            if e.code == 412:
                raise CalDAVError("the event changed since it was read; fetch it again and retry")
            raise CalDAVError(f"{method} {full} failed: HTTP {e.code} {text[:200]}")

    def _propfind(self, url: str, props: str, depth: str = "0") -> ET.Element:
        body = f'<?xml version="1.0"?><d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav" xmlns:cs="http://calendarserver.org/ns/"><d:prop>{props}</d:prop></d:propfind>'
        _, _, text = self.request("PROPFIND", url, body, depth=depth)
        return ET.fromstring(text)

    def calendar_home(self) -> str:
        root = self._propfind(self.base_url, "<d:current-user-principal/>")
        href = root.find(".//d:current-user-principal/d:href", NS)
        if href is None or not href.text:
            raise CalDAVError("server did not return a principal")
        principal = urllib.parse.urljoin(self.base_url, href.text.strip())
        root = self._propfind(principal, "<c:calendar-home-set/>")
        home = root.find(".//c:calendar-home-set/d:href", NS)
        if home is None or not home.text:
            raise CalDAVError("server did not return a calendar home")
        return urllib.parse.urljoin(principal, home.text.strip())

    def list_calendars(self) -> list[Calendar]:
        home = self.calendar_home()
        root = self._propfind(home, "<d:resourcetype/><d:displayname/><c:supported-calendar-component-set/>", depth="1")
        out = []
        for resp in root.findall("d:response", NS):
            if resp.find(".//d:resourcetype/c:calendar", NS) is None:
                continue
            href = resp.findtext("d:href", default="", namespaces=NS).strip()
            name = (resp.findtext(".//d:displayname", default="", namespaces=NS) or "").strip()
            comps = [c.get("name", "") for c in resp.findall(".//c:supported-calendar-component-set/c:comp", NS)]
            out.append(Calendar(name=name or href.rstrip("/").rsplit("/", 1)[-1],
                                url=urllib.parse.urljoin(home, href), components=comps))
        return out

    def events(self, cal: Calendar, start: datetime, end: datetime) -> list[Event]:
        s, e = _utc_stamp(start), _utc_stamp(end)
        body = ('<?xml version="1.0"?><c:calendar-query xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
                f'<d:prop><d:getetag/><c:calendar-data><c:expand start="{s}" end="{e}"/></c:calendar-data></d:prop>'
                '<c:filter><c:comp-filter name="VCALENDAR"><c:comp-filter name="VEVENT">'
                f'<c:time-range start="{s}" end="{e}"/></c:comp-filter></c:comp-filter></c:filter></c:calendar-query>')
        _, _, text = self.request("REPORT", cal.url, body, depth="1")
        root = ET.fromstring(text)
        out = []
        for resp in root.findall("d:response", NS):
            href = urllib.parse.urljoin(cal.url, resp.findtext("d:href", default="", namespaces=NS).strip())
            etag = resp.findtext(".//d:getetag", default="", namespaces=NS) or ""
            data = resp.findtext(".//c:calendar-data", default="", namespaces=NS) or ""
            out.extend(parse_events(data, href, etag))
        return out

    def get(self, href: str) -> tuple[str, str]:
        _, headers, text = self.request("GET", href)
        etag = next((v for k, v in headers.items() if k.lower() == "etag"), "")
        return text, etag

    def put(self, href: str, ics: str, etag: Optional[str] = None, create: bool = False) -> None:
        h = {"If-None-Match": "*"} if create else ({"If-Match": etag} if etag else {})
        self.request("PUT", href, ics, headers=h)

    def delete(self, href: str, etag: Optional[str] = None) -> None:
        self.request("DELETE", href, headers={"If-Match": etag} if etag else {})


# ---------------------------------------------------------------- iCalendar

def _utc_stamp(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def unfold(text: str) -> list[str]:
    lines: list[str] = []
    for raw in text.replace("\r\n", "\n").split("\n"):
        if raw.startswith((" ", "\t")) and lines:
            lines[-1] += raw[1:]
        elif raw:
            lines.append(raw)
    return lines


def _unescape(v: str) -> str:
    return v.replace("\\n", "\n").replace("\\N", "\n").replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\")


def _escape(v: str) -> str:
    return v.replace("\\", "\\\\").replace("\n", "\\n").replace(",", "\\,").replace(";", "\\;")


def _split(line: str) -> tuple[str, dict, str]:
    head, _, value = line.partition(":")
    parts = head.split(";")
    params = {}
    for p in parts[1:]:
        k, _, v = p.partition("=")
        params[k.upper()] = v.strip('"')
    return parts[0].upper(), params, value


def parse_dt(value: str, params: dict, fallback_tz: ZoneInfo) -> tuple[datetime | date, bool]:
    if params.get("VALUE") == "DATE" or re.fullmatch(r"\d{8}", value):
        return datetime.strptime(value, "%Y%m%d").date(), True
    if value.endswith("Z"):
        return datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc), False
    naive = datetime.strptime(value, "%Y%m%dT%H%M%S")
    tz = fallback_tz
    if "TZID" in params:
        try:
            tz = ZoneInfo(params["TZID"])
        except (ZoneInfoNotFoundError, ValueError):
            pass
    return naive.replace(tzinfo=tz), False


def parse_events(ics: str, href: str, etag: str, tz: ZoneInfo | None = None) -> list[Event]:
    tz = tz or ZoneInfo("UTC")
    events, cur, depth = [], None, 0
    for line in unfold(ics):
        name, params, value = _split(line)
        if name == "BEGIN" and value.upper() == "VEVENT":
            cur, depth = {"params": {}}, 1
            continue
        if cur is None:
            continue
        if name == "BEGIN":
            depth += 1  # nested VALARM etc.
            continue
        if name == "END":
            depth -= 1
            if depth == 0 and value.upper() == "VEVENT":
                if "DTSTART" in cur:
                    start, all_day = parse_dt(cur["DTSTART"], cur["params"].get("DTSTART", {}), tz)
                    end = None
                    if "DTEND" in cur:
                        end, _ = parse_dt(cur["DTEND"], cur["params"].get("DTEND", {}), tz)
                    events.append(Event(
                        uid=cur.get("UID", ""), href=href, etag=etag, summary=_unescape(cur.get("SUMMARY", "")),
                        start=start, end=end, all_day=all_day, location=_unescape(cur.get("LOCATION", "")),
                        description=_unescape(cur.get("DESCRIPTION", "")),
                        recurring="RRULE" in cur or "RECURRENCE-ID" in cur, raw=ics))
                cur = None
            continue
        if depth == 1:
            cur[name] = value
            cur["params"][name] = params
    return events


def _fmt_prop(name: str, value: datetime | date) -> str:
    if isinstance(value, datetime):
        return f"{name}:{_utc_stamp(value)}"
    return f"{name};VALUE=DATE:{value.strftime('%Y%m%d')}"


def _fold(line: str) -> str:
    out, b = [], line.encode("utf-8")
    while len(b) > 75:
        cut = 75
        while cut > 0 and (b[cut] & 0xC0) == 0x80:  # don't split a UTF-8 sequence
            cut -= 1
        out.append(b[:cut].decode("utf-8"))
        b = b" " + b[cut:]
    out.append(b.decode("utf-8"))
    return "\r\n".join(out)


def build_event(summary: str, start: datetime | date, end: datetime | date, location: str = "",
                description: str = "", uid: Optional[str] = None) -> tuple[str, str]:
    uid = uid or f"{uuid.uuid4()}@atlas-hermes"
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Atlas Hermes//icloud-calendar//EN", "BEGIN:VEVENT",
             f"UID:{uid}", f"DTSTAMP:{_utc_stamp(datetime.now(timezone.utc))}",
             _fmt_prop("DTSTART", start), _fmt_prop("DTEND", end), f"SUMMARY:{_escape(summary)}"]
    if location:
        lines.append(f"LOCATION:{_escape(location)}")
    if description:
        lines.append(f"DESCRIPTION:{_escape(description)}")
    lines += ["END:VEVENT", "END:VCALENDAR"]
    return "\r\n".join(_fold(l) for l in lines) + "\r\n", uid


def patch_event(ics: str, changes: dict[str, str]) -> str:
    """Replace top-level VEVENT properties (first VEVENT only); add missing ones before END:VEVENT."""
    out, in_event, done, depth = [], False, set(), 0
    for line in unfold(ics):
        name, _, value = _split(line)
        if name == "BEGIN" and value.upper() == "VEVENT" and not in_event and not done:
            in_event, depth = True, 1
            out.append(line)
            continue
        if in_event:
            if name == "BEGIN":
                depth += 1
            elif name == "END":
                depth -= 1
                if depth == 0:
                    for k, v in changes.items():
                        if k not in done:
                            out.append(v)
                    done.update(changes)
                    in_event = False
                    out.append(line)
                    continue
            if depth == 1 and name in changes:
                if name not in done:
                    out.append(changes[name])
                    done.add(name)
                continue
        out.append(line)
    return "\r\n".join(_fold(l) for l in out) + "\r\n"


def to_iso(v: datetime | date | None, tz: ZoneInfo) -> Optional[str]:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.astimezone(tz).isoformat(timespec="minutes")
    return v.isoformat()


def parse_input_time(value: str, tz: ZoneInfo) -> tuple[datetime | date, bool]:
    value = value.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return date.fromisoformat(value), True
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return (dt if dt.tzinfo else dt.replace(tzinfo=tz)), False


def day_bounds(d: date, tz: ZoneInfo) -> tuple[datetime, datetime]:
    s = datetime(d.year, d.month, d.day, tzinfo=tz)
    return s, s + timedelta(days=1)
