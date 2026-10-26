"""kanban-board-pin — per-profile Kanban board defaults and allowlists.

Hermes keeps one install-wide "current board" pointer (``<root>/kanban/current``), so
``hermes -p orion kanban boards switch keyframes`` moves it for every profile. Every
``kanban_*`` tool already accepts a ``board`` argument; this plugin fills and polices it
per profile so an orchestrator can't drop a card on another section's board.

Config, read per call from the active profile: ``$HERMES_HOME/kanban-board-pin.json``

    {"default_board": "keyframes", "allowed_boards": ["keyframes"]}

* ``default_board`` is used when a call names no board.
* ``allowed_boards`` lists every board the profile may touch. ``default_board`` is always
  allowed. Empty or missing means "only the default board".
* No config file: the plugin does nothing for that profile.
* Dispatcher-spawned workers (``HERMES_KANBAN_TASK`` set) are left alone: Hermes already
  pins them to their card's board.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

CONFIG_NAME = "kanban-board-pin.json"
SOURCE = "kanban-board-pin"


def _home() -> Path:
    from hermes_constants import get_hermes_home  # context-aware: the active profile

    return Path(get_hermes_home())


def load_policy(home: Optional[Path] = None) -> Optional[dict[str, Any]]:
    """``{"default": str|None, "allowed": set[str]}`` for the active profile, or None (no config)."""
    path = (home or _home()) / CONFIG_NAME
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        logger.warning("kanban-board-pin: unreadable %s (%s); refusing board-less kanban calls", path, exc)
        return {"default": None, "allowed": set(), "error": f"unreadable {CONFIG_NAME}"}
    if not isinstance(raw, dict):
        return {"default": None, "allowed": set(), "error": f"{CONFIG_NAME} must be a JSON object"}
    default = str(raw.get("default_board") or "").strip().lower() or None
    allowed = {str(b).strip().lower() for b in raw.get("allowed_boards") or [] if str(b).strip()}
    if default:
        allowed.add(default)
    return {"default": default, "allowed": allowed}


def _applies(tool_name: Any) -> bool:
    return isinstance(tool_name, str) and tool_name.startswith("kanban_") and not os.environ.get("HERMES_KANBAN_TASK")


def fill_default_board(**kwargs: Any) -> Optional[dict[str, Any]]:
    """tool_request middleware: add the profile's default board when the call names none."""
    if not _applies(kwargs.get("tool_name")):
        return None
    policy = load_policy()
    if not policy or not policy.get("default"):
        return None
    args = dict(kwargs.get("args") or {})
    if str(args.get("board") or "").strip():
        return None
    args["board"] = policy["default"]
    return {"args": args, "source": SOURCE, "reason": f"default board {policy['default']}"}


def enforce_allowed_boards(**kwargs: Any) -> Any:
    """tool_execution middleware: refuse calls aimed at a board outside the profile's allowlist."""
    next_call = kwargs["next_call"]
    if not _applies(kwargs.get("tool_name")):
        return next_call()
    policy = load_policy()
    if policy is None:
        return next_call()
    args = kwargs.get("args") or {}
    board = str(args.get("board") or "").strip().lower()
    allowed = sorted(policy["allowed"])
    if policy.get("error"):
        reason = policy["error"]
    elif not board:
        reason = "this profile has no default board, so every kanban call must name one"
    elif board in policy["allowed"]:
        return next_call()
    else:
        reason = f"board '{board}' is not allowed for this profile"
    return json.dumps({"error": f"kanban-board-pin: {reason}. Allowed boards: {', '.join(allowed) or 'none'}."})


def register(ctx) -> None:
    ctx.register_middleware("tool_request", fill_default_board)
    ctx.register_middleware("tool_execution", enforce_allowed_boards)
