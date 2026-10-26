"""Atlas Memory — a Hermes memory provider over a shared Hindsight server.

Each Bot (Hermes profile) gets:
  * a private bank: conversation turns are retained here automatically;
  * an optional section bank: shared with the other Bots in its section, written
    only when the Bot deliberately calls ``atlas_memory_share``;
  * optional recall-only banks: searched but never written (Atlas reads every section).

Recall searches every readable bank in parallel and labels each result with its bank.
Banks outside a profile's own config are refused, so a Bot cannot read or write
another section by naming its bank. Credential-shaped strings are redacted before
anything is sent to Hindsight.

Stdlib only: it talks to the Hindsight REST API directly
(``POST /v1/default/banks/{bank}/memories`` and ``/memories/recall``).
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Optional

from agent.memory_provider import MemoryProvider, spawn_context_thread

try:  # profile-scoped secret lookup; absent in very old Hermes builds
    from agent.secret_scope import get_secret
except Exception:  # pragma: no cover
    def get_secret(name: str, default: Optional[str] = None) -> Optional[str]:
        return os.environ.get(name, default)

logger = logging.getLogger(__name__)

NAME = "atlas-memory"
DEFAULT_URL = "http://127.0.0.1:8888"
_BANK_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_MAX_RETAIN_CHARS = 12000
_PREFETCH_WAIT = 4.0  # seconds a cold prefetch may block the first turn
_HTTP_TIMEOUT = 20.0

# Credential shapes redacted before anything leaves the process. Conservative on purpose:
# a false positive costs one "[REDACTED]" in a memory; a miss stores a live secret.
_SECRET_PATTERNS = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
    re.compile(r"\b(?:sk|rk|pk)_(?:live|test)_[A-Za-z0-9]{10,}\b"),          # Stripe
    re.compile(r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_-]{20,}\b"),                  # OpenAI / Anthropic
    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}\b"),              # GitHub
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"),                          # Slack
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),                                      # AWS key id
    re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b"),                                # Google API key
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),  # JWT
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/-]{20,}=*", re.I),
    re.compile(
        r"(?i)\b(password|passwd|pwd|secret|api[_-]?key|access[_-]?token|auth[_-]?token|private[_-]?key)"
        r"(\s*[:=]\s*)(\"[^\"]+\"|'[^']+'|\S+)"
    ),
]


def redact(text: str) -> tuple[str, int]:
    """Return ``(text with credential-shaped substrings replaced, number of redactions)``."""
    count = 0

    def _sub(pattern: re.Pattern, s: str) -> str:
        nonlocal count

        def repl(m: re.Match) -> str:
            nonlocal count
            count += 1
            if m.lastindex and m.lastindex >= 3:  # key = value form: keep the key name
                return f"{m.group(1)}{m.group(2)}[REDACTED]"
            return "[REDACTED]"

        return pattern.sub(repl, s)

    for p in _SECRET_PATTERNS:
        text = _sub(p, text)
    return text, count


def _as_bool(value: Any, default: bool) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _as_int(value: Any, default: int) -> int:
    try:
        return max(100, int(str(value).strip()))
    except (TypeError, ValueError):
        return default


def _bank_list(value: Any) -> list[str]:
    if isinstance(value, (list, tuple)):
        items = value
    else:
        items = str(value or "").split(",")
    out: list[str] = []
    for item in items:
        bank = str(item).strip()
        if bank and bank not in out:
            out.append(bank)
    return out


def _valid_bank(bank: str) -> bool:
    return bool(_BANK_RE.fullmatch(bank or ""))


def load_config(hermes_home: str) -> dict[str, Any]:
    path = Path(hermes_home) / NAME / "config.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        logger.warning("atlas-memory: unreadable config %s: %s", path, exc)
        return {}


class HindsightHTTP:
    """Minimal thread-safe Hindsight REST client (one urllib request per call)."""

    def __init__(self, base_url: str, api_key: str = "", timeout: float = _HTTP_TIMEOUT):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        req = urllib.request.Request(
            self.base_url + path,
            data=json.dumps(body).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        if self.api_key:
            req.add_header("Authorization", f"Bearer {self.api_key}")
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            raw = resp.read()
        return json.loads(raw or b"{}")

    def retain(self, bank: str, content: str, context: str = "", tags: Optional[list[str]] = None) -> None:
        item: dict[str, Any] = {"content": content}
        if context:
            item["context"] = context
        if tags:
            item["tags"] = tags
        self._post(f"/v1/default/banks/{urllib.parse.quote(bank, safe='')}/memories",
                   {"items": [item], "async": True})

    def recall(self, bank: str, query: str, budget: str, max_tokens: int) -> list[str]:
        try:
            data = self._post(
                f"/v1/default/banks/{urllib.parse.quote(bank, safe='')}/memories/recall",
                {"query": query, "budget": budget, "max_tokens": max_tokens},
            )
        except urllib.error.HTTPError as exc:
            if exc.code == 404:  # bank not created yet: nothing to recall
                return []
            raise
        return [str(r.get("text", "")).strip() for r in data.get("results") or [] if r.get("text")]


RETAIN_SCHEMA = {
    "name": "atlas_memory_share",
    "description": (
        "Deliberately save a fact. scope='section' shares it with the other Bots in your "
        "section (decisions, project status, preferences that matter to the team). "
        "scope='private' keeps it in your own memory. Never include passwords, keys or tokens."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "content": {"type": "string", "description": "The fact, written so it stands on its own."},
            "scope": {"type": "string", "enum": ["section", "private"], "default": "section"},
            "context": {"type": "string", "description": "Optional short label, e.g. 'launch planning'."},
        },
        "required": ["content"],
    },
}

RECALL_SCHEMA = {
    "name": "atlas_memory_recall",
    "description": (
        "Search memory. scope='all' searches every bank you can read; 'private', 'section', "
        "or a specific bank name narrows it. Results are labeled with their bank."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "scope": {"type": "string", "default": "all",
                      "description": "'all', 'private', 'section', or one of your readable bank names."},
        },
        "required": ["query"],
    },
}


class AtlasMemoryProvider(MemoryProvider):
    """One instance per profile; every binding is made in ``initialize()``."""

    def __init__(self) -> None:
        self._client: Optional[HindsightHTTP] = None
        self._private = ""
        self._section = ""
        self._read_only: list[str] = []
        self._auto_retain = True
        self._auto_recall = True
        self._budget = "mid"
        self._max_tokens = 1200
        self._agent_name = "Hermes"
        self._session_id = ""
        self._write_enabled = True
        self._prefetch_lock = threading.Lock()
        self._prefetch_cache: dict[str, str] = {}
        self._prefetch_thread: Optional[threading.Thread] = None
        self._writer_threads: list[threading.Thread] = []
        self._config_error = ""

    # -- identity ---------------------------------------------------------------

    @property
    def name(self) -> str:
        return NAME

    def is_available(self) -> bool:
        return True  # stdlib only; connectivity is checked lazily, never here

    def unavailable_reason(self) -> str:
        return self._config_error

    # -- lifecycle --------------------------------------------------------------

    def initialize(self, session_id: str, **kwargs) -> None:
        hermes_home = str(kwargs.get("hermes_home") or "")
        cfg = load_config(hermes_home) if hermes_home else {}
        identity = str(kwargs.get("agent_identity") or "").strip()

        url = str(cfg.get("api_url") or get_secret("ATLAS_MEMORY_API_URL", "") or DEFAULT_URL)
        api_key = str(get_secret("ATLAS_MEMORY_API_KEY", "") or "")
        self._client = HindsightHTTP(url, api_key)

        self._private = str(cfg.get("private_bank") or identity or "hermes").strip()
        self._section = str(cfg.get("section_bank") or "").strip()
        self._read_only = [b for b in _bank_list(cfg.get("read_banks"))
                           if b not in (self._private, self._section)]
        bad = [b for b in [self._private, self._section, *self._read_only] if b and not _valid_bank(b)]
        if bad:
            self._config_error = f"invalid bank name(s): {', '.join(bad)}"
            logger.warning("atlas-memory: %s; those banks are ignored", self._config_error)
            if self._private in bad:
                self._private = "hermes"
            if self._section in bad:
                self._section = ""
            self._read_only = [b for b in self._read_only if b not in bad]

        self._auto_retain = _as_bool(cfg.get("auto_retain"), True)
        self._auto_recall = _as_bool(cfg.get("auto_recall"), True)
        budget = str(cfg.get("recall_budget") or "mid").lower()
        self._budget = budget if budget in {"low", "mid", "high"} else "mid"
        self._max_tokens = _as_int(cfg.get("recall_max_tokens"), 1200)
        self._agent_name = identity or "Hermes"
        self._session_id = session_id or ""
        # Subagents, cron runs and flushes must not write turns (provider contract).
        self._write_enabled = str(kwargs.get("agent_context") or "primary") == "primary"
        logger.info("atlas-memory: private=%s section=%s read_only=%s url=%s",
                    self._private, self._section or "-", self._read_only, url)

    def on_session_switch(self, new_session_id: str, **kwargs) -> None:
        self._session_id = str(new_session_id or "")
        with self._prefetch_lock:
            self._prefetch_cache.clear()

    def shutdown(self) -> None:
        for t in [self._prefetch_thread, *self._writer_threads]:
            if t is not None and t.is_alive():
                t.join(timeout=5.0)

    # -- banks ------------------------------------------------------------------

    def readable_banks(self) -> list[tuple[str, str]]:
        """``[(label, bank)]`` in priority order."""
        banks = [("private", self._private)]
        if self._section:
            banks.append(("section", self._section))
        banks += [("shared", b) for b in self._read_only]
        return banks

    def _resolve_scope(self, scope: str) -> list[tuple[str, str]]:
        scope = (scope or "all").strip()
        readable = self.readable_banks()
        if scope == "all":
            return readable
        if scope == "private":
            return readable[:1]
        if scope == "section":
            return [(l, b) for l, b in readable if l == "section"]
        return [(l, b) for l, b in readable if b == scope]  # explicit bank: allowlisted only

    # -- recall -----------------------------------------------------------------

    def _recall_banks(self, query: str, banks: list[tuple[str, str]]) -> list[tuple[str, str, list[str]]]:
        results: dict[str, list[str]] = {}

        def worker(bank: str) -> None:
            try:
                results[bank] = self._client.recall(bank, query, self._budget, self._max_tokens)
            except Exception as exc:  # one slow or down bank must not sink the others
                logger.warning("atlas-memory: recall from %s failed: %r", bank, exc)
                results[bank] = []

        threads = [spawn_context_thread(worker, name=f"atlas-memory-recall-{b}", args=(b,)) for _, b in banks]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=_HTTP_TIMEOUT + 2)

        seen: set[str] = set()
        out = []
        for label, bank in banks:
            kept = [r for r in results.get(bank, []) if r not in seen]
            seen.update(kept)
            if kept:
                out.append((label, bank, kept))
        return out

    @staticmethod
    def _format(groups: list[tuple[str, str, list[str]]]) -> str:
        lines: list[str] = []
        for label, bank, items in groups:
            lines.append(f"[{label}: {bank}]")
            lines.extend(f"- {text}" for text in items)
        return "\n".join(lines)

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        if not self._auto_recall or not query.strip() or self._client is None:
            return
        key = session_id or self._session_id

        def job() -> None:
            text = self._format(self._recall_banks(query, self.readable_banks()))
            with self._prefetch_lock:
                self._prefetch_cache[key] = text

        if self._prefetch_thread and self._prefetch_thread.is_alive():
            return  # one in flight is enough; the next turn will queue again
        self._prefetch_thread = spawn_context_thread(job, name="atlas-memory-prefetch")
        self._prefetch_thread.start()

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        if not self._auto_recall or self._client is None:
            return ""
        key = session_id or self._session_id
        with self._prefetch_lock:
            cached = self._prefetch_cache.pop(key, None)
        if cached is not None:
            return cached
        # Cold start (first turn): a bounded synchronous recall so turn one has memory.
        holder: dict[str, str] = {}

        def job() -> None:
            holder["text"] = self._format(self._recall_banks(query, self.readable_banks()))

        t = spawn_context_thread(job, name="atlas-memory-cold-prefetch")
        t.start()
        t.join(timeout=_PREFETCH_WAIT)
        return holder.get("text", "")

    # -- retain -----------------------------------------------------------------

    def _write(self, bank: str, content: str, context: str, tags: list[str]) -> int:
        clean, n = redact(content)
        clean = clean[:_MAX_RETAIN_CHARS]
        self._client.retain(bank, clean, context=context, tags=tags)
        return n

    def _write_background(self, bank: str, content: str, context: str, tags: list[str]) -> None:
        def job() -> None:
            try:
                self._write(bank, content, context, tags)
            except Exception as exc:
                logger.warning("atlas-memory: retain to %s failed: %r", bank, exc)

        self._writer_threads = [t for t in self._writer_threads if t.is_alive()]
        t = spawn_context_thread(job, name="atlas-memory-retain")
        self._writer_threads.append(t)
        t.start()

    def sync_turn(self, user_content: str, assistant_content: str, *, session_id: str = "",
                  messages: Optional[list[dict[str, Any]]] = None, **kwargs) -> None:
        if not (self._auto_retain and self._write_enabled and self._client):
            return
        if not (user_content or "").strip() and not (assistant_content or "").strip():
            return
        content = f"Maximus: {user_content.strip()}\n{self._agent_name}: {assistant_content.strip()}"
        self._write_background(
            self._private, content,
            context=f"conversation between Maximus and {self._agent_name}",
            tags=[f"bot:{self._agent_name}", f"session:{session_id or self._session_id}"],
        )

    # -- tools ------------------------------------------------------------------

    def system_prompt_block(self) -> str:
        banks = ", ".join(f"{b} ({l})" for l, b in self.readable_banks())
        share = (f"Use atlas_memory_share(scope='section') to share decisions and status with "
                 f"your section ({self._section})." if self._section else
                 "You have no section bank; atlas_memory_share saves privately.")
        return (
            "## Long-term memory (Atlas Memory)\n"
            f"Readable banks: {banks}. Relevant memories are injected automatically each turn. "
            f"{share} Use atlas_memory_recall to search deliberately. "
            "Never store passwords, API keys, tokens or other credentials in memory."
        )

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        return [RETAIN_SCHEMA, RECALL_SCHEMA]

    def handle_tool_call(self, tool_name: str, args: dict[str, Any], **kwargs) -> str:
        try:
            if self._client is None:
                return json.dumps({"error": "atlas-memory is not initialized"})
            if tool_name == "atlas_memory_share":
                return self._tool_share(args or {})
            if tool_name == "atlas_memory_recall":
                return self._tool_recall(args or {})
            return json.dumps({"error": f"Unknown tool: {tool_name}"})
        except Exception as exc:
            logger.warning("atlas-memory: %s failed: %r", tool_name, exc)
            return json.dumps({"error": f"{tool_name} failed: {exc}"})

    def _tool_share(self, args: dict[str, Any]) -> str:
        content = str(args.get("content") or "").strip()
        if not content:
            return json.dumps({"error": "Missing required parameter: content"})
        if not self._write_enabled:
            return json.dumps({"error": "Memory writes are disabled in this context (subagent/cron)."})
        scope = str(args.get("scope") or "section").strip()
        if scope not in {"section", "private"}:
            return json.dumps({"error": "scope must be 'section' or 'private'"})
        if scope == "section" and not self._section:
            scope = "private"
        bank = self._section if scope == "section" else self._private
        redactions = self._write(bank, content, str(args.get("context") or ""),
                                 [f"bot:{self._agent_name}", "shared" if scope == "section" else "private"])
        msg = f"Saved to {scope} memory ({bank})."
        if redactions:
            msg += f" {redactions} credential-like value(s) were redacted before saving."
        return json.dumps({"result": msg})

    def _tool_recall(self, args: dict[str, Any]) -> str:
        query = str(args.get("query") or "").strip()
        if not query:
            return json.dumps({"error": "Missing required parameter: query"})
        scope = str(args.get("scope") or "all")
        banks = self._resolve_scope(scope)
        if not banks:
            allowed = ["all", "private"] + (["section"] if self._section else []) + [b for _, b in self.readable_banks()]
            return json.dumps({"error": f"Bank '{scope}' is not readable by this Bot. Allowed: {', '.join(allowed)}"})
        text = self._format(self._recall_banks(query, banks))
        return json.dumps({"result": text or "No relevant memories found."})

    # -- setup (CLI wizard; the Desktop panel uses config_schema.py) -------------

    def get_config_schema(self) -> list[dict[str, Any]]:
        return [
            {"key": "private_bank", "description": "Private bank for this Bot (blank = profile name)", "default": ""},
            {"key": "section_bank", "description": "Shared section bank (blank = none)", "default": ""},
            {"key": "read_banks", "description": "Recall-only banks, comma-separated", "default": ""},
            {"key": "api_url", "description": "Hindsight server URL", "default": DEFAULT_URL},
            {"key": "api_key", "description": "Hindsight API key (blank for a local server)",
             "secret": True, "required": False, "env_var": "ATLAS_MEMORY_API_KEY"},
        ]

    def save_config(self, values: dict[str, Any], hermes_home: str) -> None:
        path = Path(hermes_home) / NAME / "config.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        merged = {**load_config(hermes_home), **{k: v for k, v in values.items() if k != "api_key"}}
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(merged, indent=2), encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)


def register(ctx) -> None:
    ctx.register_memory_provider(AtlasMemoryProvider())
