# Cursor hooks

This project uses Cursor hooks under `.cursor/hooks/` for verification and notifications.

## Reminders (notifications)

Reminders are created so you see them on all iCloud-connected devices (Mac, iPhone, iPad, Watch) when:

- **Run completed:** A Cursor run finishes (stop hook). The reminder includes the project name, e.g. "Cursor run completed — personal_agent. Review when back."
- **Needs approval:** When the agent refuses to implement a Linear issue because it is not approved, it runs `notify-reminder.sh` so you get a reminder like "Cursor needs approval: &lt;issue id or title&gt;."

### Setup

1. Set `CURSOR_IMESSAGE_BUDDY` to any non-empty value to enable reminders (e.g. your phone number in E.164 if you also use iMessage, or a placeholder like `1`):

   ```bash
   export CURSOR_IMESSAGE_BUDDY=""
   ```

   Add this to `~/.zshenv` or `~/.zshrc`. Hooks run with a minimal environment; if the variable is unset there, the script may read from `~/.zshenv`.

2. Use **Reminders** (macOS) with iCloud sync so reminders appear on all your devices. No Messages setup required for reminders.

If `CURSOR_IMESSAGE_BUDDY` is unset, notification scripts no-op and exit 0 so hooks do not fail.

### Scripts

| Script | Purpose |
|--------|--------|
| `.cursor/hooks/notify-reminder.sh "Message"` | Create one Reminder (due in 1 minute). Used by the stop hook and by the agent when approval is needed. |
| `.cursor/hooks/notify-on-stop.sh` | Stop hook: on `status == "completed"`, calls `notify-reminder.sh` with a message that includes the project name. |

The Linear Implement Gate rule instructs the agent to run `notify-reminder.sh` when it cannot implement because the issue is not approved.
