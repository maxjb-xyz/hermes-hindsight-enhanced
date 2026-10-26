"""Test stub mirroring the Hermes MemoryProvider ABC surface the plugin uses."""
import contextvars, threading
from abc import ABC, abstractmethod

def spawn_context_thread(target, *, name, daemon=True, args=(), kwargs=None):
    ctx = contextvars.copy_context()
    return threading.Thread(target=lambda: ctx.run(target, *args, **(kwargs or {})), name=name, daemon=daemon)

class MemoryProvider(ABC):
    @property
    @abstractmethod
    def name(self): ...
    @abstractmethod
    def is_available(self): ...
    @abstractmethod
    def initialize(self, session_id, **kwargs): ...
    @abstractmethod
    def get_tool_schemas(self): ...
    def save_config(self, values, hermes_home): ...
