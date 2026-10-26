"""Self-contained tests against a fake CalDAV server (no pytest): python3 tests/run_tests.py"""
import base64, importlib.util, json, os, re, sys, tempfile, threading, traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("icloud_calendar", ROOT / "__init__.py",
                                              submodule_search_locations=[str(ROOT)])
mod = importlib.util.module_from_spec(spec); sys.modules["icloud_calendar"] = mod; spec.loader.exec_module(mod)

EV = lambda uid, summ, s, e, extra="": f"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\nUID:{uid}\r\nDTSTART:{s}\r\nDTEND:{e}\r\nSUMMARY:{summ}\r\n{extra}END:VEVENT\r\nEND:VCALENDAR\r\n"

class Fake:
    def __init__(self):
        self.cals = {"/cal/personal/": "Personal", "/cal/family/": "Family", "/cal/booking/": "Portfolio Site booking",
                     "/cal/work/": "Work", "/cal/school/": "School"}
        self.objs = {
            "/cal/personal/a.ics": EV("a", "Dentist", "20261027T150000Z", "20261027T160000Z"),
            "/cal/family/f.ics": EV("f", "Family secret", "20261027T170000Z", "20261027T180000Z"),
            "/cal/booking/b.ics": EV("b", "Client call", "20261028T140000Z", "20261028T150000Z"),
            "/cal/school/r.ics": EV("r", "Lecture", "20261027T130000Z", "20261027T140000Z", "RRULE:FREQ=WEEKLY\r\n"),
        }
        self.etags = {k: '"1"' for k in self.objs}
        self.log = []
        outer = self
        class H(BaseHTTPRequestHandler):
            def log_message(self, *a): pass
            def _body(self):
                n = int(self.headers.get("Content-Length") or 0); return self.rfile.read(n).decode() if n else ""
            def _auth(self):
                ok = self.headers.get("Authorization") == "Basic " + base64.b64encode(b"me@icloud.com:pw").decode()
                if not ok: self.send_response(401); self.end_headers()
                return ok
            def _send(self, code, text="", ctype="application/xml", headers=None):
                b = text.encode(); self.send_response(code); self.send_header("Content-Type", ctype)
                for k, v in (headers or {}).items(): self.send_header(k, v)
                self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
            def do_PROPFIND(self):
                if not self._auth(): return
                body = self._body(); outer.log.append(("PROPFIND", self.path))
                if "current-user-principal" in body:
                    return self._send(207, '<d:multistatus xmlns:d="DAV:"><d:response><d:href>/</d:href><d:propstat><d:prop><d:current-user-principal><d:href>/p/</d:href></d:current-user-principal></d:prop></d:propstat></d:response></d:multistatus>')
                if "calendar-home-set" in body:
                    return self._send(207, '<d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav"><d:response><d:href>/p/</d:href><d:propstat><d:prop><c:calendar-home-set><d:href>/cal/</d:href></c:calendar-home-set></d:prop></d:propstat></d:response></d:multistatus>')
                resp = "".join(f'<d:response><d:href>{h}</d:href><d:propstat><d:prop><d:resourcetype><d:collection/><c:calendar/></d:resourcetype><d:displayname>{n}</d:displayname><c:supported-calendar-component-set><c:comp name="VEVENT"/></c:supported-calendar-component-set></d:prop></d:propstat></d:response>' for h, n in outer.cals.items())
                self._send(207, f'<d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav"><d:response><d:href>/cal/</d:href><d:propstat><d:prop><d:resourcetype><d:collection/></d:resourcetype></d:prop></d:propstat></d:response>{resp}</d:multistatus>')
            def do_REPORT(self):
                if not self._auth(): return
                self._body(); outer.log.append(("REPORT", self.path))
                from xml.sax.saxutils import escape
                resp = "".join(f'<d:response><d:href>{h}</d:href><d:propstat><d:prop><d:getetag>{outer.etags[h]}</d:getetag><c:calendar-data>{escape(v)}</c:calendar-data></d:prop></d:propstat></d:response>' for h, v in outer.objs.items() if h.startswith(self.path))
                self._send(207, f'<d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">{resp}</d:multistatus>')
            def do_GET(self):
                if not self._auth(): return
                outer.log.append(("GET", self.path))
                if self.path not in outer.objs: return self._send(404)
                self._send(200, outer.objs[self.path], "text/calendar", {"ETag": outer.etags[self.path]})
            def do_PUT(self):
                if not self._auth(): return
                body = self._body(); outer.log.append(("PUT", self.path))
                if self.headers.get("If-None-Match") == "*" and self.path in outer.objs: return self._send(412)
                im = self.headers.get("If-Match")
                if im and outer.etags.get(self.path) != im: return self._send(412)
                outer.objs[self.path] = body; outer.etags[self.path] = f'"{int(outer.etags.get(self.path, chr(34)+"0"+chr(34)).strip(chr(34)))+1}"'
                self._send(201)
            def do_DELETE(self):
                if not self._auth(): return
                outer.log.append(("DELETE", self.path)); outer.objs.pop(self.path, None); self._send(204)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

def setup(policy_extra=None):
    f = Fake(); d = tempfile.mkdtemp(); os.environ["HERMES_HOME"] = d
    os.environ["ICLOUD_USERNAME"] = "me@icloud.com"; os.environ["ICLOUD_APP_PASSWORD"] = "pw"
    pol = {"timezone": "America/New_York", "default_calendar": "Personal", "caldav_url": f.url,
           "calendars": {"Personal": "rw", "School": "rw", "Portfolio Site booking": "ro"}}
    pol.update(policy_extra or {})
    Path(d, "icloud-calendar.json").write_text(json.dumps(pol))
    return f

call = lambda name, args=None: json.loads(mod.HANDLERS[name](args or {}))

def t_list_hides_unlisted():
    f = setup(); r = call("icloud_calendar_list")
    names = {c["name"] for c in r["calendars"]}
    assert names == {"Personal", "School", "Portfolio Site booking"}, names
    assert {c["name"]: c["access"] for c in r["calendars"]}["Portfolio Site booking"] == "read-only"

def t_events_never_touch_family():
    f = setup(); r = call("icloud_calendar_events", {"start": "2026-10-26", "end": "2026-10-30"})
    titles = {e["title"] for e in r["events"]}
    assert titles == {"Dentist", "Client call", "Lecture"}, titles
    assert not any("/cal/family/" in p or "/cal/work/" in p for _, p in f.log if _ == "REPORT")
    dent = next(e for e in r["events"] if e["title"] == "Dentist")
    assert dent["start"] == "2026-10-27T11:00-04:00", dent["start"]

def t_named_family_refused():
    setup(); r = call("icloud_calendar_events", {"calendars": ["Family"]})
    assert "not available" in r["error"] and "Family" not in r["error"].split("Available:")[1]

def t_create_on_default():
    f = setup(); r = call("icloud_calendar_create", {"title": "Gym, legs", "start": "2026-10-29T07:00"})
    href = r["created"]["id"]; assert href.startswith(f.url + "cal/personal/")
    body = f.objs[href.replace(f.url[:-1], "")]
    assert "SUMMARY:Gym\\, legs" in body and "DTSTART:20261029T110000Z" in body and "DTEND:20261029T120000Z" in body

def t_create_all_day():
    f = setup(); r = call("icloud_calendar_create", {"title": "Trip", "start": "2026-11-02", "calendar": "school"})
    body = f.objs[r["created"]["id"].replace(f.url[:-1], "")]
    assert "DTSTART;VALUE=DATE:20261102" in body and "DTEND;VALUE=DATE:20261103" in body

def t_create_readonly_refused():
    f = setup(); n = len(f.objs)
    r = call("icloud_calendar_create", {"title": "x", "start": "2026-10-29T07:00", "calendar": "Portfolio Site booking"})
    assert "read-only" in r["error"] and len(f.objs) == n

def t_create_family_refused():
    setup(); r = call("icloud_calendar_create", {"title": "x", "start": "2026-10-29T07:00", "calendar": "Family"})
    assert "read-only or unavailable" in r["error"]

def t_update_moves_and_keeps_duration():
    f = setup(); eid = f.url[:-1] + "/cal/personal/a.ics"
    r = call("icloud_calendar_update", {"id": eid, "start": "2026-10-27T13:00", "title": "Dentist (moved)"})
    assert r["ok"] and "DTSTART" in r["updated"]["changed"]
    body = f.objs["/cal/personal/a.ics"]
    assert "DTSTART:20261027T170000Z" in body and "DTEND:20261027T180000Z" in body and "SUMMARY:Dentist (moved)" in body
    assert body.count("BEGIN:VEVENT") == 1

def t_update_booking_refused():
    f = setup(); before = f.objs["/cal/booking/b.ics"]
    r = call("icloud_calendar_update", {"id": f.url[:-1] + "/cal/booking/b.ics", "title": "hijack"})
    assert "read-only" in r["error"] and f.objs["/cal/booking/b.ics"] == before

def t_update_family_id_refused():
    f = setup(); r = call("icloud_calendar_update", {"id": f.url[:-1] + "/cal/family/f.ics", "title": "x"})
    assert "not in an available calendar" in r["error"]

def t_update_recurring_refused():
    f = setup(); r = call("icloud_calendar_update", {"id": f.url[:-1] + "/cal/school/r.ics", "title": "x"})
    assert "recurring" in r["error"]

def t_delete_disabled_by_default():
    f = setup(); r = call("icloud_calendar_delete", {"id": f.url[:-1] + "/cal/personal/a.ics"})
    assert "disabled" in r["error"] and "/cal/personal/a.ics" in f.objs

def t_delete_when_allowed():
    f = setup({"allow_delete": True}); r = call("icloud_calendar_delete", {"id": f.url[:-1] + "/cal/personal/a.ics"})
    assert r["ok"] and "/cal/personal/a.ics" not in f.objs

def t_free_busy_merges():
    f = setup(); r = call("icloud_calendar_free_busy", {"start": "2026-10-27", "end": "2026-10-27"})
    assert r["busy"] == [{"start": "2026-10-27T09:00-04:00", "end": "2026-10-27T10:00-04:00"},
                         {"start": "2026-10-27T11:00-04:00", "end": "2026-10-27T12:00-04:00"}], r["busy"]

def t_missing_secret():
    setup(); del os.environ["ICLOUD_APP_PASSWORD"]; r = call("icloud_calendar_list")
    assert "ICLOUD_APP_PASSWORD" in r["error"]

def t_bad_auth():
    setup(); os.environ["ICLOUD_APP_PASSWORD"] = "wrong"; r = call("icloud_calendar_list")
    assert "rejected the credentials" in r["error"]

def t_no_policy_off():
    setup(); os.remove(Path(os.environ["HERMES_HOME"], "icloud-calendar.json")); r = call("icloud_calendar_list")
    assert "calendar access is off" in r["error"]

def t_default_must_be_rw():
    setup({"default_calendar": "Portfolio Site booking"}); r = call("icloud_calendar_list")
    assert "must be listed as 'rw'" in r["error"]

def t_folding_unicode():
    ics, _ = mod.cd.build_event("Café " * 30, mod.cd.date(2026, 1, 1), mod.cd.date(2026, 1, 2))
    for line in ics.split("\r\n"):
        assert len(line.encode()) <= 75
    ev = mod.cd.parse_events(ics, "x", "e")[0]; assert ev.summary == "Café " * 30

failed = 0
for n, fn in list(globals().items()):
    if n.startswith("t_"):
        try: fn(); print("PASS", n)
        except Exception: failed += 1; print("FAIL", n); traceback.print_exc()
print("failed:", failed); sys.exit(1 if failed else 0)
