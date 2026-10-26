"""icloud-calendar — iCloud Calendar tools for one Hermes profile, deny-by-default.

An iCloud app-specific password opens every calendar on the account, so the limits live
here: only calendars named in this profile's policy are ever listed, read or written.
Anything else (a Family calendar, say) is invisible — filtered at discovery, refused by
name, and event IDs are checked to belong to an allowed calendar.

Policy: ``$HERMES_HOME/icloud-calendar.json`` (read on every call)

    {
      "timezone": "America/New_York",
      "default_calendar": "Personal",
      "calendars": {"Personal": "rw", "School": "rw", "Portfolio Site booking": "ro"},
      "allow_delete": false
    }

Secrets (profile .env): ICLOUD_USERNAME (Apple ID email), ICLOUD_APP_PASSWORD.
"""

from __future__ import annotations

import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

try:
    from . import caldav_client as cd
except ImportError:  # loaded as a top-level module (tests)
    import caldav_client as cd  # type: ignore

logger = logging.getLogger(__name__)

POLICY_NAME = "icloud-calendar.json"
TOOLSET = "icloud_calendar"
MAX_RANGE_DAYS = 120


def _secret(name: str) -> str:
    try:
        from agent.secret_scope import get_secret
        return get_secret(name, "") or ""
    except ImportError:
        return os.environ.get(name, "")


def _home() -> Path:
    try:
        from hermes_constants import get_hermes_home
        return Path(get_hermes_home())
    except ImportError:
        return Path(os.environ.get("HERMES_HOME", "~/.hermes")).expanduser()


class PolicyError(Exception):
    pass


class Policy:
    def __init__(self, raw: dict):
        try:
            self.tz = ZoneInfo(str(raw.get("timezone") or "UTC"))
        except (ZoneInfoNotFoundError, ValueError):
            raise PolicyError(f"unknown timezone {raw.get('timezone')!r}")
        cals = raw.get("calendars") or {}
        if not isinstance(cals, dict) or not cals:
            raise PolicyError("'calendars' must map at least one calendar name to 'rw' or 'ro'")
        self.access: dict[str, str] = {}
        for name, mode in cals.items():
            mode = str(mode).lower()
            if mode not in ("rw", "ro"):
                raise PolicyError(f"calendar {name!r}: access must be 'rw' or 'ro', not {mode!r}")
            self.access[str(name).strip().casefold()] = mode
        self.display = {str(n).strip().casefold(): str(n).strip() for n in cals}
        self.default = str(raw.get("default_calendar") or "").strip()
        if self.default and self.mode(self.default) != "rw":
            raise PolicyError(f"default_calendar {self.default!r} must be listed as 'rw'")
        self.allow_delete = bool(raw.get("allow_delete", False))
        self.base_url = str(raw.get("caldav_url") or cd.DEFAULT_URL)

    def mode(self, name: str) -> Optional[str]:
        return self.access.get((name or "").strip().casefold())


def load_policy() -> Policy:
    path = _home() / POLICY_NAME
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise PolicyError(f"no {POLICY_NAME} in this profile; calendar access is off")
    except (OSError, ValueError) as exc:
        raise PolicyError(f"unreadable {POLICY_NAME}: {exc}")
    if not isinstance(raw, dict):
        raise PolicyError(f"{POLICY_NAME} must be a JSON object")
    return Policy(raw)


def _client(policy: Policy) -> cd.CalDAVClient:
    user, pw = _secret("ICLOUD_USERNAME"), _secret("ICLOUD_APP_PASSWORD")
    if not user or not pw:
        raise PolicyError("ICLOUD_USERNAME and ICLOUD_APP_PASSWORD must be set in this profile's .env")
    return cd.CalDAVClient(user, pw, policy.base_url)


def visible_calendars(policy: Policy, client: cd.CalDAVClient) -> list[cd.Calendar]:
    """Server calendars filtered to the policy. Unlisted calendars never leave this function."""
    cals = [c for c in client.list_calendars()
            if policy.mode(c.name) and (not c.components or "VEVENT" in c.components)]
    return cals


def _find(policy: Policy, cals: list[cd.Calendar], name: str) -> cd.Calendar:
    mode = policy.mode(name)
    if not mode:
        raise PolicyError(f"calendar {name!r} is not available. Available: {', '.join(c.name for c in cals) or 'none'}")
    for c in cals:
        if c.name.strip().casefold() == name.strip().casefold():
            return c
    raise PolicyError(f"calendar {name!r} is allowed but was not found in iCloud (check the exact name)")


def _owning(cals: list[cd.Calendar], href: str) -> cd.Calendar:
    for c in cals:
        if href.startswith(c.url):
            return c
    raise PolicyError("that event is not in an available calendar")


def _ok(**kw) -> str:
    return json.dumps({"ok": True, **kw}, default=str)


def _err(msg: str) -> str:
    return json.dumps({"error": msg})


def _tool(fn):
    def handler(args: dict, **kwargs) -> str:
        try:
            return fn(args or {})
        except (PolicyError, cd.CalDAVError, ValueError) as exc:
            return _err(str(exc))
        except Exception as exc:  # never crash the agent loop
            logger.warning("icloud-calendar %s failed: %r", fn.__name__, exc)
            return _err(f"{fn.__name__} failed: {exc}")
    handler.__name__ = fn.__name__
    return handler


def _event_json(e: cd.Event, cal: cd.Calendar, policy: Policy) -> dict:
    return {"id": e.href, "calendar": cal.name, "title": e.summary, "start": cd.to_iso(e.start, policy.tz),
            "end": cd.to_iso(e.end, policy.tz), "all_day": e.all_day, "location": e.location or None,
            "notes": (e.description[:500] or None), "recurring": e.recurring,
            "editable": policy.mode(cal.name) == "rw" and not e.recurring}


def _range(policy: Policy, args: dict) -> tuple[datetime, datetime]:
    tz = policy.tz
    today = datetime.now(tz).date()
    s_raw, e_raw = args.get("start"), args.get("end")
    if s_raw:
        s, all_day = cd.parse_input_time(str(s_raw), tz)
        start = datetime(s.year, s.month, s.day, tzinfo=tz) if all_day else s
    else:
        start = datetime(today.year, today.month, today.day, tzinfo=tz)
    if e_raw:
        e, all_day = cd.parse_input_time(str(e_raw), tz)
        end = datetime(e.year, e.month, e.day, tzinfo=tz) + timedelta(days=1) if all_day else e
    else:
        end = start + timedelta(days=7)
    if end <= start:
        raise ValueError("end must be after start")
    if end - start > timedelta(days=MAX_RANGE_DAYS):
        raise ValueError(f"range is limited to {MAX_RANGE_DAYS} days")
    return start, end


# ---------------------------------------------------------------- tools

@_tool
def icloud_calendar_list(args: dict) -> str:
    policy = load_policy()
    cals = visible_calendars(policy, _client(policy))
    return _ok(calendars=[{"name": c.name, "access": "read-write" if policy.mode(c.name) == "rw" else "read-only"}
                          for c in cals], default=policy.default or None, timezone=str(policy.tz))


@_tool
def icloud_calendar_events(args: dict) -> str:
    policy = load_policy()
    client = _client(policy)
    cals = visible_calendars(policy, client)
    wanted = args.get("calendars")
    if wanted:
        cals = [_find(policy, cals, n) for n in wanted]
    start, end = _range(policy, args)
    with ThreadPoolExecutor(max_workers=min(6, max(1, len(cals)))) as pool:
        results = list(pool.map(lambda c: (c, client.events(c, start, end)), cals))
    events = [_event_json(e, c, policy) for c, evs in results for e in evs]
    events.sort(key=lambda e: (e["start"] or ""))
    return _ok(range={"start": start.isoformat(timespec="minutes"), "end": end.isoformat(timespec="minutes")},
               events=events, count=len(events))


@_tool
def icloud_calendar_free_busy(args: dict) -> str:
    policy = load_policy()
    client = _client(policy)
    cals = visible_calendars(policy, client)
    start, end = _range(policy, args)
    busy = []
    for c in cals:
        for e in client.events(c, start, end):
            if e.all_day:
                continue
            s, f = max(e.start, start), min(e.end or e.start, end)
            if s < f:  # clip to the range; skip anything outside it
                busy.append((s, f))
    busy.sort()
    merged: list[list[datetime]] = []
    for s, e in busy:
        if merged and s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    return _ok(busy=[{"start": cd.to_iso(s, policy.tz), "end": cd.to_iso(e, policy.tz)} for s, e in merged],
               note="Busy blocks across available calendars; all-day events are excluded.")


@_tool
def icloud_calendar_create(args: dict) -> str:
    policy = load_policy()
    title = str(args.get("title") or "").strip()
    if not title:
        raise ValueError("title is required")
    name = str(args.get("calendar") or policy.default or "").strip()
    if not name:
        raise ValueError("calendar is required (no default_calendar is set)")
    if policy.mode(name) != "rw":
        raise PolicyError(f"calendar {name!r} is read-only or unavailable")
    start, all_day = cd.parse_input_time(str(args["start"]), policy.tz)
    if args.get("end"):
        end, _ = cd.parse_input_time(str(args["end"]), policy.tz)
    else:
        end = start + timedelta(days=1) if all_day else start + timedelta(hours=1)
    if isinstance(start, date) and not isinstance(start, datetime) and isinstance(end, date) \
            and not isinstance(end, datetime) and end <= start:
        end = start + timedelta(days=1)
    if end <= start:
        raise ValueError("end must be after start")
    client = _client(policy)
    cal = _find(policy, visible_calendars(policy, client), name)
    ics, uid = cd.build_event(title, start, end, str(args.get("location") or ""), str(args.get("notes") or ""))
    href = cal.url + uid.split("@")[0] + ".ics"
    client.put(href, ics, create=True)
    return _ok(created={"id": href, "calendar": cal.name, "title": title,
                        "start": cd.to_iso(start, policy.tz), "end": cd.to_iso(end, policy.tz)})


def _load_editable(policy: Policy, client: cd.CalDAVClient, event_id: str):
    cals = visible_calendars(policy, client)
    cal = _owning(cals, event_id)
    if policy.mode(cal.name) != "rw":
        raise PolicyError(f"calendar {cal.name!r} is read-only")
    ics, etag = client.get(event_id)
    evs = cd.parse_events(ics, event_id, etag, policy.tz)
    if not evs:
        raise PolicyError("event not found")
    if any(e.recurring for e in evs) or len(evs) > 1:
        raise PolicyError("recurring events can't be changed with this tool; edit them in Calendar")
    return cal, ics, etag, evs[0]


@_tool
def icloud_calendar_update(args: dict) -> str:
    policy = load_policy()
    event_id = str(args.get("id") or "")
    if not event_id:
        raise ValueError("id is required (from icloud_calendar_events)")
    client = _client(policy)
    cal, ics, etag, ev = _load_editable(policy, client, event_id)
    changes: dict[str, str] = {}
    if args.get("title"):
        changes["SUMMARY"] = f"SUMMARY:{cd._escape(str(args['title']))}"
    if "location" in args:
        changes["LOCATION"] = f"LOCATION:{cd._escape(str(args['location'] or ''))}"
    if "notes" in args:
        changes["DESCRIPTION"] = f"DESCRIPTION:{cd._escape(str(args['notes'] or ''))}"
    start, end = ev.start, ev.end
    if args.get("start"):
        start, _ = cd.parse_input_time(str(args["start"]), policy.tz)
        if not args.get("end") and ev.end is not None:
            end = start + (ev.end - ev.start)  # keep the duration
    if args.get("end"):
        end, _ = cd.parse_input_time(str(args["end"]), policy.tz)
    if end is not None and end <= start:
        raise ValueError("end must be after start")
    if start != ev.start:
        changes["DTSTART"] = cd._fmt_prop("DTSTART", start)
    if end != ev.end and end is not None:
        changes["DTEND"] = cd._fmt_prop("DTEND", end)
    if not changes:
        raise ValueError("nothing to change")
    client.put(event_id, cd.patch_event(ics, changes), etag=etag)
    return _ok(updated={"id": event_id, "calendar": cal.name, "changed": sorted(changes)})


@_tool
def icloud_calendar_delete(args: dict) -> str:
    policy = load_policy()
    if not policy.allow_delete:
        raise PolicyError("deleting events is disabled for this profile (allow_delete is false); ask Maximus to remove it")
    event_id = str(args.get("id") or "")
    if not event_id:
        raise ValueError("id is required")
    client = _client(policy)
    cal, _, etag, ev = _load_editable(policy, client, event_id)
    client.delete(event_id, etag=etag)
    return _ok(deleted={"id": event_id, "calendar": cal.name, "title": ev.summary})


_TIME = {"type": "string", "description": "ISO 8601, e.g. 2026-10-27T14:00 (profile timezone if no offset) or 2026-10-27 for all-day."}

SCHEMAS = {
    "icloud_calendar_list": {"description": "List Maximus's iCloud calendars available to you and whether each is read-write or read-only.",
                             "parameters": {"type": "object", "properties": {}}},
    "icloud_calendar_events": {"description": "List events in a time range (default: the next 7 days) across available calendars, or only the named ones.",
                               "parameters": {"type": "object", "properties": {
                                   "start": _TIME, "end": _TIME,
                                   "calendars": {"type": "array", "items": {"type": "string"}}}}},
    "icloud_calendar_free_busy": {"description": "Merged busy blocks across available calendars, for finding free time.",
                                  "parameters": {"type": "object", "properties": {"start": _TIME, "end": _TIME}}},
    "icloud_calendar_create": {"description": "Create an event on a read-write calendar (default calendar if omitted). Default length: 1 hour, or 1 day for all-day.",
                               "parameters": {"type": "object", "required": ["title", "start"], "properties": {
                                   "title": {"type": "string"}, "start": _TIME, "end": _TIME,
                                   "calendar": {"type": "string"}, "location": {"type": "string"},
                                   "notes": {"type": "string"}}}},
    "icloud_calendar_update": {"description": "Change a single (non-recurring) event on a read-write calendar. Moving start keeps the duration unless end is given.",
                               "parameters": {"type": "object", "required": ["id"], "properties": {
                                   "id": {"type": "string", "description": "Event id from icloud_calendar_events."},
                                   "title": {"type": "string"}, "start": _TIME, "end": _TIME,
                                   "location": {"type": "string"}, "notes": {"type": "string"}}}},
    "icloud_calendar_delete": {"description": "Delete a single event on a read-write calendar, only if this profile allows deletes. Always confirm with Maximus first.",
                               "parameters": {"type": "object", "required": ["id"], "properties": {
                                   "id": {"type": "string"}}}},
}

HANDLERS = {
    "icloud_calendar_list": icloud_calendar_list, "icloud_calendar_events": icloud_calendar_events,
    "icloud_calendar_free_busy": icloud_calendar_free_busy, "icloud_calendar_create": icloud_calendar_create,
    "icloud_calendar_update": icloud_calendar_update, "icloud_calendar_delete": icloud_calendar_delete,
}


def register(ctx) -> None:
    for name, handler in HANDLERS.items():
        ctx.register_tool(name=name, toolset=TOOLSET, schema={"name": name, **SCHEMAS[name]},
                          handler=handler, description=SCHEMAS[name]["description"], emoji="📅")
