# systemd unit files

Persistent copies of `/etc/systemd/system/` unit files used on the VPS, so
they can be re-installed on a fresh host without re-deriving them.

## Install

```bash
# from /opt/seshat on the target host
sudo install -m 644 infrastructure/systemd/seshat-soak.service /etc/systemd/system/
sudo install -m 644 infrastructure/systemd/seshat-soak.timer   /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now seshat-soak.timer
```

## Inspect

```bash
systemctl list-timers seshat-soak              # next/last fire times
systemctl status seshat-soak.timer             # timer state
journalctl -fu seshat-soak.service             # follow service logs
ls -la telemetry/soak/                         # dated output files
```

## Units

| Unit | Purpose | Schedule |
|---|---|---|
| `seshat-soak.service` + `.timer` | FRE-380 Stage 1 daily soak measurement | 09:00 UTC daily |
| `claude-remote-control@.service` | Remote Control server per dispatch stream (ADR-0110 T4); `@build1`/`@build2`/`@adr` | long-running (`Restart=always`) |
| `seshat-dispatch-orchestrator.service` | Dispatch orchestrator loop (ADR-0110 T4); dispatch-only | long-running (`Restart=always`) |

See `docs/runbooks/dispatch-orchestrator.md` for the dispatch units' enable-once
precondition, guardrails, and recovery.

## Adding a new timer

1. Copy an existing pair, rename to `<job>.service` / `<job>.timer`
2. Edit `ExecStart=` (service) and `OnCalendar=` (timer)
3. `sudo install -m 644 …` both into `/etc/systemd/system/`
4. `sudo systemctl daemon-reload && sudo systemctl enable --now <job>.timer`

## Removing

```bash
sudo systemctl disable --now <job>.timer
sudo rm /etc/systemd/system/<job>.{service,timer}
sudo systemctl daemon-reload
```
