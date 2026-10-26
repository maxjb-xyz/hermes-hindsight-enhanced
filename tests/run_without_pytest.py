"""Minimal runner for environments without pytest (supports tmp_path, monkeypatch, hs)."""
import inspect, os, sys, tempfile, traceback, types
from pathlib import Path
fake = types.ModuleType("pytest"); fake.fixture = lambda f: f; sys.modules["pytest"] = fake
sys.path.insert(0, str(Path(__file__).parent))
import test_atlas_memory as t

class MP:
    def __init__(self): self.saved = {}
    def setenv(self, k, v): self.saved.setdefault(k, os.environ.get(k)); os.environ[k] = v
    def undo(self):
        for k, v in self.saved.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)

failed = 0
for name, fn in inspect.getmembers(t, inspect.isfunction):
    if not name.startswith("test_"): continue
    params = inspect.signature(fn).parameters
    hs = t.FakeHindsight() if "hs" in params else None
    mp = MP()
    with tempfile.TemporaryDirectory() as d:
        kw = {"tmp_path": Path(d), "hs": hs, "monkeypatch": mp}
        try:
            fn(**{k: kw[k] for k in params}); print("PASS", name)
        except Exception:
            failed += 1; print("FAIL", name); traceback.print_exc()
        finally:
            mp.undo(); hs and hs.close()
print("failed:", failed); sys.exit(1 if failed else 0)
