# Atlas Memory

**Private and shared memory for a team of Hermes Bots, set up in Hermes Desktop.**

Atlas Memory is a [Hermes Agent](https://github.com/NousResearch/hermes-agent) memory provider backed by a self-hosted [Hindsight](https://hindsight.vectorize.io) server. It gives every Bot (Hermes profile) three kinds of memory:

| Memory | Who can read it | How it's written |
| --- | --- | --- |
| **Private bank** | Only this Bot | Every completed conversation turn, automatically |
| **Section bank** | Every Bot in the same section (team) | Only when a Bot deliberately shares a fact |
| **Recall-only banks** | This Bot, read-only | Never written by this Bot |

Before each turn, it searches every bank the Bot is allowed to read in parallel and injects what's relevant, labeled by source:

```
[private: orion]
- Maximus prefers weekly reviews on Mondays
[section: keyframes]
- Launch codename is Juniper; target is March
```

## Why

Hermes runs one external memory provider per profile. The official Hindsight provider supports extra write banks, but every extra write bank receives *every* conversation turn, so private chatter floods any shared bank. Atlas Memory keeps automatic writes private and makes sharing a deliberate act.

## Features

- **Team memory without the noise.** Turns go to the private bank. Bots share facts with their section through the `atlas_memory_share` tool.
- **Multi-bank recall.** One Bot can read any number of banks, so a coordinator Bot can see every team's shared memory.
- **Bank allowlist, enforced in code.** A Bot can only read or write banks listed in its own settings. Asking for another bank is refused, not just discouraged.
- **Credential redaction.** API keys (OpenAI, Anthropic, Stripe, GitHub, Slack, AWS, Google), JWTs, bearer tokens, private keys and `password: ...` values are replaced with `[REDACTED]` before anything leaves the process.
- **Desktop-native.** Ships a declarative settings panel, so you select and configure it in **Settings → Memory & Context** with no config files.
- **Safe defaults.** Subagents and cron runs never write memory. A slow or down bank never blocks recall from the others. The first turn waits at most 4 seconds for memory.
- **No dependencies.** Standard library only; it talks to the Hindsight REST API directly.

## Example: a five-section team

| Bot | Private bank | Section bank | Recall-only banks |
| --- | --- | --- | --- |
| Atlas (coordinator) | `atlas` | | `personal, school, keyframes, engineering` |
| Artemis | `artemis` | `personal` | |
| Nada | `nada` | `school` | |
| Orion, CTO, CMO, CPO | own name | `keyframes` | |
| Apollo | `apollo` | `engineering` | |

Any layout works: one shared bank for everyone, overlapping read access, a Bot with no section, and so on.

## Requirements

- Hermes Agent with declarative memory panels (current Desktop builds).
- A Hindsight server reachable from the Hermes gateway.

## Setup

### 1. Run a Hindsight server

Hindsight uses an LLM to extract facts, so it needs a model key. On the machine running Hermes:

```bash
uv tool install hindsight-api
```

Create `~/.config/hindsight/env`:

```ini
HINDSIGHT_API_HOST=127.0.0.1
HINDSIGHT_API_PORT=8888
HINDSIGHT_API_LLM_PROVIDER=anthropic
HINDSIGHT_API_LLM_MODEL=claude-haiku-4-5
HINDSIGHT_API_LLM_API_KEY=sk-ant-...
```

Then run `hindsight-api` with that environment, for example as a systemd user service with `EnvironmentFile=%h/.config/hindsight/env`. It uses an embedded PostgreSQL by default and creates banks on first write.

> **Bind to `127.0.0.1`.** Hindsight defaults to `0.0.0.0`. If your Bots run commands in Docker sandboxes, a server on all interfaces lets a sandbox call Hindsight directly and skip the allowlist.

### 2. Install the plugin

**From Hermes Desktop:** Capabilities → Plugins → **Install from Git**, enter `https://github.com/maxjb-xyz/hermes-hindsight-enhanced`, and pick the profile. Repeat for each profile.

**From a terminal:** clone once and link it into every profile:

```bash
git clone https://github.com/maxjb-xyz/hermes-hindsight-enhanced ~/atlas-memory
for d in ~/.hermes ~/.hermes/profiles/*; do mkdir -p "$d/plugins" && ln -sfn ~/atlas-memory "$d/plugins/atlas-memory"; done
hermes gateway restart
```

### 3. Configure each Bot in Desktop

1. Open **Settings → Memory & Context**.
2. In **Applies to**, pick the profile.
3. Set **Memory provider** to **Atlas Memory**.
4. Fill in **Private bank**, **Section bank** and **Recall-only banks**.
5. Save and start a new chat.

## Settings

Stored per profile in `$HERMES_HOME/atlas-memory/config.json`.

| Setting | Default | Meaning |
| --- | --- | --- |
| `private_bank` | profile name | This Bot's own bank |
| `section_bank` | none | Shared bank this Bot can write to |
| `read_banks` | none | Comma-separated recall-only banks |
| `api_url` | `http://127.0.0.1:8888` | Hindsight server |
| `auto_retain` | `true` | Save each turn to the private bank |
| `auto_recall` | `true` | Inject memories before each turn |
| `recall_budget` | `mid` | `low`, `mid` or `high` Hindsight search effort |
| `recall_max_tokens` | `1200` | Token budget per bank per recall |

Secret: `ATLAS_MEMORY_API_KEY` (optional, sent as a Bearer token) for a Hindsight server that requires auth.

Bank names may use letters, digits, `_`, `-` and `.`, up to 64 characters. Invalid names are ignored and reported.

## Tools the Bot gets

| Tool | Arguments | Does |
| --- | --- | --- |
| `atlas_memory_share` | `content`, `scope` (`section` or `private`), `context` | Saves a fact deliberately; falls back to private if the Bot has no section |
| `atlas_memory_recall` | `query`, `scope` (`all`, `private`, `section`, or a readable bank name) | Searches memory on demand |

## Security notes

- The allowlist protects against a Bot asking for the wrong bank. It doesn't stop code that can reach the Hindsight server directly, so keep the server on loopback and run Bot commands in sandboxes.
- Redaction is pattern-based and conservative. It reduces risk but isn't a guarantee; keep "never store credentials" in each Bot's `SOUL.md` too.
- Conversation content is sent to your Hindsight server and to the LLM it uses for fact extraction.

## Development

```bash
python3 tests/run_without_pytest.py   # or: pytest tests/
```

Tests run against stubs of the Hermes interfaces and a fake Hindsight HTTP server.

## Companion plugins

- [`extras/kanban-board-pin`](extras/kanban-board-pin) pins each profile's Kanban tools to its own board(s), since Hermes's current-board pointer is install-wide.
- [`extras/icloud-calendar`](extras/icloud-calendar) gives one profile iCloud Calendar tools with a deny-by-default calendar allowlist and per-calendar read-only.

## License

MIT
