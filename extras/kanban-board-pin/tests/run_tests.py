"""Self-contained tests (no pytest): python3 tests/run_tests.py"""
import importlib.util, json, os, sys, tempfile, traceback
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests" / "stubs"))
spec = importlib.util.spec_from_file_location("kbp", ROOT / "__init__.py"); kbp = importlib.util.module_from_spec(spec); spec.loader.exec_module(kbp)

def home(cfg):
    d = tempfile.mkdtemp(); os.environ["HERMES_HOME"] = d
    if cfg is not None: Path(d, kbp.CONFIG_NAME).write_text(cfg if isinstance(cfg, str) else json.dumps(cfg))

def run(tool, args):
    req = kbp.fill_default_board(tool_name=tool, args=args, original_args=args)
    args = req["args"] if req else args
    called = {}
    out = kbp.enforce_allowed_boards(tool_name=tool, args=args, next_call=lambda p=None: called.setdefault("ok", args) and "EXECUTED")
    return args, out

def t_default_filled():
    home({"default_board": "keyframes"}); a, out = run("kanban_create", {"title": "x"})
    assert a["board"] == "keyframes" and out == "EXECUTED"
def t_explicit_kept():
    home({"default_board": "keyframes", "allowed_boards": ["engineering"]}); a, out = run("kanban_create", {"board": "engineering"})
    assert a["board"] == "engineering" and out == "EXECUTED"
def t_foreign_refused():
    home({"default_board": "keyframes"}); a, out = run("kanban_create", {"board": "personal"})
    assert "not allowed" in json.loads(out)["error"]
def t_case_insensitive():
    home({"default_board": "Keyframes"}); a, out = run("kanban_list", {"board": "KEYFRAMES"})
    assert out == "EXECUTED"
def t_no_default_requires_board():
    home({"allowed_boards": ["personal", "school"]}); a, out = run("kanban_create", {"title": "x"})
    assert "must name one" in json.loads(out)["error"]
    a, out = run("kanban_create", {"board": "school"}); assert out == "EXECUTED"
def t_no_config_noop():
    home(None); a, out = run("kanban_create", {"title": "x"}); assert "board" not in a and out == "EXECUTED"
def t_other_tools_untouched():
    home({"default_board": "keyframes"}); a, out = run("terminal", {"command": "ls"}); assert "board" not in a and out == "EXECUTED"
def t_worker_untouched():
    home({"default_board": "keyframes"}); os.environ["HERMES_KANBAN_TASK"] = "t_1"
    try:
        a, out = run("kanban_complete", {"board": "personal"}); assert out == "EXECUTED"
    finally: del os.environ["HERMES_KANBAN_TASK"]
def t_bad_config_fails_closed():
    home("{not json"); a, out = run("kanban_create", {"board": "keyframes"}); assert "unreadable" in json.loads(out)["error"]

failed = 0
for n, f in list(globals().items()):
    if n.startswith("t_"):
        try: f(); print("PASS", n)
        except Exception: failed += 1; print("FAIL", n); traceback.print_exc()
print("failed:", failed); sys.exit(1 if failed else 0)
