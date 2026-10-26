"""Atlas Memory's declared config surface, rendered by the Hermes Desktop memory panel.

Values land in $HERMES_HOME/atlas-memory/config.json (storage: flat_json), which is the
file the provider reads. This module may import only the pure-data schema module.
"""

from plugins.memory.config_schema import (
    KIND_BOOL,
    KIND_NUMBER,
    KIND_SECRET,
    KIND_SELECT,
    KIND_TEXT,
    ProviderConfigSchema,
    ProviderField,
    ProviderFieldOption,
)

CONFIG_SCHEMA = ProviderConfigSchema(
    name="atlas-memory",
    label="Atlas Memory",
    docs_url="https://hindsight.vectorize.io",
    fields=(
        ProviderField(
            key="private_bank",
            label="Private bank",
            kind=KIND_TEXT,
            default="",
            placeholder="e.g. orion",
            description="This Bot's own memory. Conversation turns are saved here. Blank = the profile name.",
            inline=True,
        ),
        ProviderField(
            key="section_bank",
            label="Section bank",
            kind=KIND_TEXT,
            default="",
            placeholder="e.g. keyframes",
            description="Shared with the other Bots in this section. Written only when the Bot deliberately shares.",
            inline=True,
        ),
        ProviderField(
            key="read_banks",
            label="Recall-only banks",
            kind=KIND_TEXT,
            default="",
            placeholder="e.g. personal, school, keyframes, engineering",
            description="Extra banks this Bot can search but never write (comma-separated). Atlas lists every section.",
            inline=True,
        ),
        ProviderField(
            key="api_url",
            label="Hindsight URL",
            kind=KIND_TEXT,
            default="http://127.0.0.1:8888",
            env_fallbacks=("ATLAS_MEMORY_API_URL",),
            inline=True,
        ),
        ProviderField(
            key="api_key",
            label="Hindsight API key",
            kind=KIND_SECRET,
            env_key="ATLAS_MEMORY_API_KEY",
            description="Only needed if your Hindsight server requires auth. Leave blank for a local server.",
            group="Connection",
        ),
        ProviderField(
            key="auto_retain",
            label="Save conversations automatically",
            kind=KIND_BOOL,
            default=True,
            description="Save each completed turn to the private bank.",
            group="Behavior",
        ),
        ProviderField(
            key="auto_recall",
            label="Recall automatically",
            kind=KIND_BOOL,
            default=True,
            description="Search all readable banks before each turn and inject what's relevant.",
            group="Behavior",
        ),
        ProviderField(
            key="recall_budget",
            label="Recall effort",
            kind=KIND_SELECT,
            default="mid",
            options=(
                ProviderFieldOption("low", "Low", "Fastest"),
                ProviderFieldOption("mid", "Mid", "Balanced"),
                ProviderFieldOption("high", "High", "Most thorough"),
            ),
            group="Behavior",
        ),
        ProviderField(
            key="recall_max_tokens",
            label="Recall tokens per bank",
            kind=KIND_NUMBER,
            default="1200",
            group="Behavior",
        ),
    ),
)
