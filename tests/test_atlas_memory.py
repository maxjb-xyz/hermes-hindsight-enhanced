"""Tests against stubbed Hermes interfaces and a fake Hindsight HTTP server.

Run: python3 -m pytest tests/ -q   (from the plugin root)
"""
import importlib.util
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests" / "stubs"))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


plugin = _load("atlas_memory_plugin", ROOT / "__init__.py")
schema_mod = _load("atlas_memory_schema", ROOT / "config_schema.py")


class FakeHindsight:
    def __init__(self):
        self.banks: dict[str, list[str]] = {}
        self.requests: list[tuple[str, dict, dict]] = []
        handler = self._handler()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def _handler(self):
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.requests.append((self.path, body, dict(self.headers)))
                parts = self.path.split("/")  # /v1/default/banks/{bank}/memories[/recall]
                bank = parts[4]
                if self.path.endswith("/memories"):
                    for item in body["items"]:
                        outer.banks.setdefault(bank, []).append(item["content"])
                    self._send(200, {"success": True})
                elif self.path.endswith("/recall"):
                    if bank not in outer.banks:
                        self._send(404, {"detail": "bank not found"})
                        return
                    q = body["query"].lower()
                    hits = [t for t in outer.banks[bank] if any(w in t.lower() for w in q.split())]
                    self._send(200, {"results": [{"id": str(i), "text": t} for i, t in enumerate(hits)]})

            def _send(self, code, payload):
                raw = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        return H

    def close(self):
        self.server.shutdown()


@pytest.fixture
def hs():
    s = FakeHindsight()
    yield s
    s.close()


def make(tmp_path, hs, cfg, identity="orion", context="primary"):
    home = tmp_path / identity
    (home / "atlas-memory").mkdir(parents=True)
    (home / "atlas-memory" / "config.json").write_text(json.dumps({"api_url": hs.url, **cfg}))
    p = plugin.AtlasMemoryProvider()
    p.initialize("sess-1", hermes_home=str(home), agent_identity=identity, agent_context=context)
    return p


def wait_writers(p):
    for t in p._writer_threads:
        t.join(5)


def test_schema_names_match_provider():
    s = schema_mod.CONFIG_SCHEMA
    assert s.name == plugin.NAME == "atlas-memory"
    keys = {f.key for f in s.fields}
    assert {"private_bank", "section_bank", "read_banks", "api_url", "api_key"} <= keys
    secret = next(f for f in s.fields if f.key == "api_key")
    assert secret.kind == "secret" and secret.env_key == "ATLAS_MEMORY_API_KEY"


def test_auto_retain_goes_to_private_only(tmp_path, hs):
    p = make(tmp_path, hs, {"private_bank": "orion", "section_bank": "keyframes"})
    p.sync_turn("our launch codename is Juniper", "Noted.")
    wait_writers(p)
    assert "orion" in hs.banks and "keyframes" not in hs.banks
    assert "Juniper" in hs.banks["orion"][0]


def test_share_section_and_cross_bot_recall(tmp_path, hs):
    orion = make(tmp_path, hs, {"private_bank": "orion", "section_bank": "keyframes"})
    res = json.loads(orion.handle_tool_call("atlas_memory_share", {"content": "Launch codename is Juniper"}))
    assert "section memory (keyframes)" in res["result"]
    cpo = make(tmp_path, hs, {"private_bank": "keyframes-cpo", "section_bank": "keyframes"}, identity="keyframes-cpo")
    out = json.loads(cpo.handle_tool_call("atlas_memory_recall", {"query": "codename"}))["result"]
    assert "[section: keyframes]" in out and "Juniper" in out
    artemis = make(tmp_path, hs, {"private_bank": "artemis", "section_bank": "personal"}, identity="artemis")
    out = json.loads(artemis.handle_tool_call("atlas_memory_recall", {"query": "codename"}))["result"]
    assert "Juniper" not in out


def test_allowlist_refuses_foreign_bank(tmp_path, hs):
    hs.banks["keyframes"] = ["secret plan"]
    artemis = make(tmp_path, hs, {"private_bank": "artemis", "section_bank": "personal"}, identity="artemis")
    res = json.loads(artemis.handle_tool_call("atlas_memory_recall", {"query": "plan", "scope": "keyframes"}))
    assert "not readable" in res["error"]
    assert not any("/keyframes/" in path for path, _, _ in hs.requests)


def test_atlas_multi_bank_recall_is_labeled_and_deduped(tmp_path, hs):
    hs.banks.update({"atlas": ["Maximus likes dry wit"], "keyframes": ["Juniper launch in March"],
                     "school": ["POS 3703 draft due Friday"], "personal": ["Juniper launch in March"]})
    atlas = make(tmp_path, hs, {"private_bank": "atlas",
                                "read_banks": "personal, school, keyframes, engineering"}, identity="default")
    text = atlas.prefetch("juniper draft wit")
    assert "[private: atlas]" in text and "[shared: school]" in text
    assert text.count("Juniper launch in March") == 1  # deduped across banks
    # engineering does not exist yet (404) and must not break recall
    assert "engineering" not in text


def test_redaction_before_send(tmp_path, hs):
    p = make(tmp_path, hs, {"private_bank": "apollo", "section_bank": "engineering"}, identity="apollo")
    res = json.loads(p.handle_tool_call("atlas_memory_share", {
        "content": "deploy token ghp_abcdefghijklmnopqrstuvwxyz0123 and password: hunter22 and sk_live_abcdefghijklmn"}))
    assert "3 credential-like" in res["result"]
    stored = hs.banks["engineering"][0]
    assert "ghp_" not in stored and "hunter22" not in stored and "sk_live" not in stored
    assert "password: [REDACTED]" in stored


def test_subagent_context_never_writes(tmp_path, hs):
    p = make(tmp_path, hs, {"private_bank": "apollo"}, identity="apollo", context="subagent")
    p.sync_turn("hello", "world")
    wait_writers(p)
    res = json.loads(p.handle_tool_call("atlas_memory_share", {"content": "x"}))
    assert "disabled" in res["error"] and hs.banks == {}


def test_queue_prefetch_then_prefetch_uses_cache(tmp_path, hs):
    hs.banks["orion"] = ["Juniper"]
    p = make(tmp_path, hs, {"private_bank": "orion"})
    p.queue_prefetch("juniper")
    p._prefetch_thread.join(5)
    n = len(hs.requests)
    assert "Juniper" in p.prefetch("juniper")
    assert len(hs.requests) == n  # served from cache, no new request


def test_server_down_degrades_gracefully(tmp_path, hs):
    p = make(tmp_path, hs, {"private_bank": "orion", "api_url": "http://127.0.0.1:9"})
    assert p.prefetch("anything") == ""
    res = json.loads(p.handle_tool_call("atlas_memory_recall", {"query": "x"}))
    assert res["result"] == "No relevant memories found."


def test_invalid_bank_names_ignored(tmp_path, hs):
    p = make(tmp_path, hs, {"private_bank": "orion", "section_bank": "../etc", "read_banks": "ok, bad/name"})
    assert p._section == "" and p._read_only == ["ok"] and "invalid" in p.unavailable_reason()


def test_api_key_sent_as_bearer(tmp_path, hs, monkeypatch):
    monkeypatch.setenv("ATLAS_MEMORY_API_KEY", "k123")
    p = make(tmp_path, hs, {"private_bank": "orion"})
    p.handle_tool_call("atlas_memory_share", {"content": "x", "scope": "private"})
    assert hs.requests[-1][2].get("Authorization") == "Bearer k123"


def test_save_config_roundtrip(tmp_path):
    p = plugin.AtlasMemoryProvider()
    p.save_config({"private_bank": "nada", "section_bank": "school", "api_key": "nope"}, str(tmp_path))
    cfg = plugin.load_config(str(tmp_path))
    assert cfg == {"private_bank": "nada", "section_bank": "school"}
