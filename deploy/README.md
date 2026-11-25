# systemd deployment (jetpack2)

The agent's long-running entrypoint is the FastAPI web UI
(`deep_research.webui.app:app`). It listens on `0.0.0.0:8080` so it is reachable
from the LAN, and it starts after the local SearXNG service is ready on
`127.0.0.1:8888`.

## Files

| File | Purpose |
|---|---|
| `deep-research-web.service` | System-level unit (`/etc/systemd/system`). |
| `deep-research-web.user.service` | User-level unit (`~/.config/systemd/user`), for installs without sudo. |
| `deep-research-web.env.example` | Runtime env file template (keys + Node PATH). |
| `install-system-service.sh` | Root installer for the system-level unit. |
| `install-user-service.sh` | No-sudo installer for the user-level unit. |

The runtime env file lives **outside the repo**:

```
/home/researcher/.config/deep-research/deep-research.env   (mode 600)
```

It holds `TAVILY_API_KEY`, `FIRECRAWL_API_KEY`, and a `PATH` that includes the
nvm Node bin directory (needed by the Playwright MCP browser fallback). Without
keys the agent still works through the configured SearXNG fallback.

## Install (system-level, recommended)

```bash
cd /home/researcher/deep_research
sudo deploy/install-system-service.sh
```

or manually:

```bash
sudo install -m 644 deploy/deep-research-web.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now deep-research-web.service
```

## Install (user-level, no sudo)

```bash
cd /home/researcher/deep_research
./deploy/install-user-service.sh
```

or manually:

```bash
install -d -m 755 ~/.config/systemd/user
install -m 644 deploy/deep-research-web.user.service ~/.config/systemd/user/deep-research-web.service
systemctl --user daemon-reload
systemctl --user enable --now deep-research-web.service
loginctl enable-linger "$USER"   # start at boot without logging in
```

## Verify

System-level:

```bash
systemctl status deep-research-web.service --no-pager
journalctl -u deep-research-web.service -n 50 --no-pager
curl -sS http://127.0.0.1:8080/api/health
```

User-level:

```bash
systemctl --user status deep-research-web.service --no-pager
journalctl --user-unit deep-research-web.service -n 50 --no-pager
curl -sS http://127.0.0.1:8080/api/health
```

`/api/health` returns `{"status":"ok"}`. The web UI root is `/`.

If both units are installed, disable one before enabling the other — both bind
port 8080:

```bash
systemctl --user disable --now deep-research-web.service   # before system install
```

## Notes

- **Single uvicorn worker.** Research jobs live in memory; multiple workers
  would split job state and make status/progress unreliable.
- **SearXNG readiness.** The unit waits up to 120s for
  `http://127.0.0.1:8888/` before starting and fails if it never comes up, so
  systemd retries after SearXNG finishes booting.
- **Secrets stay out of git.** Edit the env file directly; do not copy keys into
  the unit file.
- **Trusted-header SSO (`pdl.auth.sso`).** The shipped units pass
  `--no-proxy-headers` so `request.client.host` is the TCP peer. Uvicorn's
  default trusts `X-Forwarded-For` from `127.0.0.1`; with the default, a local
  user could claim to be the trusted proxy and forge `X-Auth-User`. If you
  override the unit, keep the flag (or set `forwarded_allow_ips` explicitly
  and never include loopback unless loopback really is the proxy). The public
  shell/share paths in front of this app must also clear identity headers.
- **Share tokens stay out of the journal.** The shipped units pass
  `--no-access-log` because `/api/share/<token>` puts the capability in the
  request path. Application and error logs remain; put request logging in the
  reverse proxy and redact the share prefix there (`access_log off` for it).
  If you re-enable uvicorn access logs, add a redacting formatter first.
- **Updating the code.** `git pull`, then `uv sync` (or `uv sync --extra dev`),
  then `systemctl restart deep-research-web.service`. A venv rebuild while the
  service is running is fine; restart after the sync.
