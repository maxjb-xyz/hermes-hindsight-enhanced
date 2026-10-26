# icloud-calendar

iCloud Calendar tools for a single Hermes profile, over CalDAV. Standard library only.

An iCloud **app-specific password can't be limited to certain calendars**: it opens every calendar on the account. This plugin enforces the limits in code instead:

- **Deny by default.** Only calendars named in the profile's policy are ever listed, queried or written. Anything else (a Family calendar, say) is filtered out at discovery and refused by name.
- **Per-calendar read-only.** Mark a calendar `ro` and creates, updates and deletes are refused.
- **Event IDs are checked** to belong to an allowed calendar, so a guessed ID can't reach a hidden one.
- **Deletes are off** unless the policy sets `allow_delete: true`.
- **Recurring events are read-only** through this tool; edit series in Calendar.
- **Edits are safe against races:** updates and deletes send the event's ETag, so a change made elsewhere since the read is refused, not overwritten.

## Tools

| Tool | Does |
| --- | --- |
| `icloud_calendar_list` | Available calendars and their access |
| `icloud_calendar_events` | Events in a range (default: next 7 days), all or named calendars |
| `icloud_calendar_free_busy` | Merged busy blocks for finding free time |
| `icloud_calendar_create` | New event on a read-write calendar |
| `icloud_calendar_update` | Change title, time, location or notes of a single event |
| `icloud_calendar_delete` | Delete an event, only if the policy allows |

## Setup

1. Create an app-specific password at [appleid.apple.com](https://appleid.apple.com) → Sign-In and Security → App-Specific Passwords.
2. Add to the profile's `.env`:

   ```
   ICLOUD_USERNAME=you@icloud.com
   ICLOUD_APP_PASSWORD=xxxx-xxxx-xxxx-xxxx
   ```

3. Create `$HERMES_HOME/icloud-calendar.json`. Names must match iCloud exactly (case-insensitive):

   ```json
   {
     "timezone": "America/New_York",
     "default_calendar": "Personal",
     "calendars": {
       "Personal": "rw",
       "School": "rw",
       "Portfolio Site booking": "ro"
     },
     "allow_delete": false
   }
   ```

4. Link the plugin into that profile only, enable it, and restart the gateway:

   ```bash
   ln -sfn ~/atlas-memory/extras/icloud-calendar ~/.hermes/profiles/artemis/plugins/icloud-calendar
   hermes -p artemis plugins enable icloud-calendar
   hermes gateway restart
   ```

The policy is read on every call, so edits apply immediately.

## Tests

```bash
python3 tests/run_tests.py
```

Runs against a fake CalDAV server: hidden calendars never queried, read-only refusals, ETag updates, timezone handling, line folding.
