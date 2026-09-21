# Self-hosted cloud worker — library 0.3.0

This runbook puts the A&A controller and its per-session worker containers on a cloud VM, so
remote harnesses get a real browser with virtual microphone, speaker, camera and display. It is
the self-hosted equivalent of a hosted browser service, with media devices included.

Nothing here has been verified on a cloud provider yet. Measure before relying on the sizing
figures, and record results in [validation](validation.md).

## Choose the host

- **Ubuntu 24.04 x86-64 virtual machine.** Container services without kernel module access
  (Fargate, Cloud Run, most managed Kubernetes node pools) cannot load `v4l2loopback`, so they
  cannot provide the camera. Audio and display capture work in any Docker host.
- **Size per concurrent session.** Each worker container is capped at `--cpus=4`,
  `--memory=8g`, `--pids-limit=512` (`src/mba/controller.py`). Start with 4 vCPU / 8 GiB per
  planned concurrent session plus 2 vCPU / 4 GiB for the host, then measure with the soak test.
- **Disk.** Recordings are VP8 1280×720 at 30 FPS plus WAV tracks; reserve several GiB per
  recorded hour. The worker refuses to start below `min_free_disk_mb` (default 1024).

## Provision

```sh
sudo apt-get update && sudo apt-get install -y docker.io git python3 python3-venv
sudo usermod -aG docker "$USER"            # log out and back in
git clone <your repository> aa && cd aa
curl -LsSf https://astral.sh/uv/install.sh | sh
uv sync --locked --extra remote
sudo sh deploy/provision-host.sh           # only when camera input is required
uv run python deploy/build.py --platform linux/amd64 --tag mba-worker:0.3.0
uv run mba preflight --image mba-worker:0.3.0   # also checks the camera devices
```

`deploy/provision-host.sh` loads `v4l2loopback` with `/dev/video10` and `/dev/video11` and
persists the configuration. Each camera is leased to one session at a time; add devices to
serve more concurrent camera sessions and pass them with `--cameras`. `mba preflight` fails
when the listed camera devices are missing; on an audio/display-only host, skip it.

## Run the controller

The controller must run as a non-root user in the `docker` group. It starts one hardened worker
container per session (seccomp profile, all capabilities dropped, `no-new-privileges`, the
host user's UID) and publishes each worker only on `127.0.0.1`.

```sh
export MBA_TOKEN="$(openssl rand -hex 32)"
uv run mba serve --root /srv/mba/artifacts --image mba-worker:0.3.0 \
  --seccomp deploy/seccomp.json --cameras /dev/video10,/dev/video11 --host 127.0.0.1 --port 8090
```

Run it under systemd (`Restart=on-failure`, `User=` the deploy user, `EnvironmentFile=` a
root-owned file containing `MBA_TOKEN`). Store the token in your secret manager; clients send
it as `Authorization: Bearer …`.

## Put TLS in front

Expose only the reverse proxy. Example Caddy site:

```caddy
aa.example.com {
    request_body {
        max_size 512MB          # asset uploads
    }
    reverse_proxy 127.0.0.1:8090 {
        flush_interval -1       # live event and media WebSockets
        transport http {
            read_timeout 10m
            write_timeout 10m
        }
    }
}
```

Nginx works equally well with `proxy_http_version 1.1`, the `Upgrade`/`Connection` headers,
`client_max_body_size 512m` and `proxy_read_timeout 600s`.

Firewall: allow 443 (and 22 from your admin network) only. Never publish worker container
ports, the native Playwright gateway, or VNC/noVNC ports from `deploy/Dockerfile.viewer`.

## Use it

```python
from mba import SessionConfig
from mba.sdk import Client

async with Client("https://aa.example.com", token) as client:
    session = await client.start_session(SessionConfig(mode="audio", camera=False))
```

The TypeScript `@mba/harness-client` has the same audio, camera, visual, recording and event
methods. `mba mcp --worker-url https://aa.example.com` (install the `mcp` extra) exposes the session tools to an MCP
client with `MBA_TOKEN` in its environment.

## Operate

- **Artifacts** live under `--root/<session-id>/`. Sync completed sessions to object storage and
  prune locally; the controller does not expire them.
- **Cleanup.** Workers carry the label `mba.managed=true`. After a crash, list leftovers with
  `docker ps -a --filter label=mba.managed=true` before removing them.
- **Upgrades.** Build a new tagged image, restart the controller with `--image`, and keep
  clients on the same A&A version: 0.3 clients and 0.2 workers are not compatible.
- **Acceptance on this host.** Run `uv run python deploy/test-http-container.py --image
  mba-worker:0.3.0`, then `MBA_LINUX_TESTS=1 uv run pytest tests/test_linux.py` for camera,
  isolation and timing checks, and the opt-in soak test before production use.
