# Cursor hooks

This project uses Cursor hooks under `.cursor/hooks/` for verification and notifications.

## iMessage notifications

You can receive iMessage notifications when:

- **Task completed:** A run finishes (stop hook). You get a message like "Cursor has completed (personal_agent)."
- **Needs approval:** When the agent refuses to implement a Linear issue because it is not approved, it runs a script so you get "Cursor needs approval: &lt;issue&gt;."

### Setup

1. Set `CURSOR_IMESSAGE_BUDDY` to your iMessage recipient (e.g. your phone number in E.164 format):

   ```bash
   export CURSOR_IMESSAGE_BUDDY=""
   ```

   Add this to `~/.zshenv` or `~/.zshrc`. Hooks run with a minimal environment; if the variable is unset there, the script tries reading it from `~/.zshenv` so your existing shell config is used.

2. macOS **Messages** must be set up and signed into iMessage; the recipient must be a valid iMessage buddy.

If `CURSOR_IMESSAGE_BUDDY` is still unset after that, notification scripts no-op and exit successfully so hooks do not fail.

### Scripts

| Script | Purpose |
|--------|--------|
| `.cursor/hooks/notify-reminder.sh "Message"` | Send one Reminder to the all devices. Used by the stop hook and by the agent when approval is needed. |
| `.cursor/hooks/notify-on-stop.sh` | Stop hook: on `status == "completed"`, calls `notify-reminder.sh` with a completion message. |

The Linear Implement Gate rule instructs the agent to run `notify-reminder.sh` when it cannot implement because the issue is not approved.
