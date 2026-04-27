# Seshat Python Sandbox

Docker image used by the `run_python` primitive tool to execute untrusted Python
scripts in a hardened, isolated environment.

## Image

`seshat-sandbox-python:0.1`

## Build

```bash
make sandbox-build
# or
docker build -t seshat-sandbox-python:0.1 docker/sandbox/ -f docker/sandbox/Dockerfile.python
```

## Pre-installed libraries

`requests`, `httpx`, `pandas`, `numpy`, `pyyaml`

## Security model

- Runs as non-root user `appuser` (uid/gid 1000)
- Read-only root filesystem (`--read-only`)
- `/tmp` mounted as `tmpfs` (64 MB, rw) for temporary files
- `/sandbox` bind-mounted from per-trace host scratch dir (rw)
- No network access by default (`--network=none`); `network=True` uses `cloud-sim`
- All Linux capabilities dropped (`--cap-drop=ALL`)
- No privilege escalation (`--security-opt=no-new-privileges`)
