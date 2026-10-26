"""Test stub mirroring hermes plugins/memory/config_schema.py dataclasses."""
from dataclasses import dataclass, field as dataclass_field
KIND_TEXT="text"; KIND_SELECT="select"; KIND_SECRET="secret"; KIND_BOOL="bool"; KIND_NUMBER="number"; KIND_JSON="json"
STORAGE_FLAT_JSON="flat_json"
@dataclass(frozen=True)
class ProviderFieldOption:
    value: str; label: str; description: str = ""
@dataclass(frozen=True)
class ProviderField:
    key: str; label: str; kind: str = KIND_TEXT; default: str = ""; description: str = ""; placeholder: str = ""
    options: tuple = (); env_key: str | None = None; aliases: tuple = (); env_fallbacks: tuple = ()
    inline: bool = False; group: str = ""; info: str = ""; scope: str = "host"
@dataclass(frozen=True)
class ProviderConfigSchema:
    name: str; label: str; storage: str = STORAGE_FLAT_JSON; docs_url: str = ""
    fields: tuple = dataclass_field(default_factory=tuple)
