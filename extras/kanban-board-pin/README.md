# kanban-board-pin

Pins each Hermes profile's Kanban tools to its own board.

Hermes stores one **install-wide** current-board pointer, so `hermes -p orion kanban boards switch keyframes` changes it for every profile. Every `kanban_*` tool accepts a `board` argument, and this plugin fills and polices it per profile:

- **Default board:** a call that names no board goes to the profile's default.
- **Allowlist:** a call aimed at any other board is refused with a clear error the agent can act on.
- **Workers untouched:** dispatcher-spawned workers are already pinned to their card's board by Hermes.

It uses two documented Hermes middleware hooks: `tool_request` fills the board and `tool_execution` enforces the allowlist. Standard library only.

## Install

The plugin lives in `extras/kanban-board-pin` of the [hermes-hindsight-enhanced](https://github.com/maxjb-xyz/hermes-hindsight-enhanced) repo. With that repo cloned to `~/atlas-memory`, link it into each profile that needs a pin and enable it:

```bash
ln -sfn ~/atlas-memory/extras/kanban-board-pin ~/.hermes/profiles/orion/plugins/kanban-board-pin
hermes -p orion plugins enable kanban-board-pin
```

Or in Hermes Desktop: **Capabilities → Plugins → Install from Git** with `https://github.com/maxjb-xyz/hermes-hindsight-enhanced/tree/main/extras/kanban-board-pin`.

## Configure

Create `$HERMES_HOME/kanban-board-pin.json` in each profile:

```json
{"default_board": "keyframes", "allowed_boards": ["keyframes"]}
```

| Key | Meaning |
| --- | --- |
| `default_board` | Board used when a call names none. Always allowed. |
| `allowed_boards` | Every board this profile may touch. |

With no `default_board`, every call must name a board from `allowed_boards`. Useful for a coordinator that works across boards. With no config file, the plugin does nothing for that profile. An unreadable config fails closed.

## Tests

```bash
python3 tests/run_tests.py
```
