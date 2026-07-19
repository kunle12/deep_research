# Local SearXNG Setup Guide

Step-by-step instructions to set up a fully working SearXNG metasearch engine on
Linux or macOS for use with the deep-research agent (or any local tool that
needs a privacy-respecting search API).

---

## What you'll end up with

- A SearXNG container listening on `http://localhost:8080`
- JSON search API at `http://localhost:8080/search?q=<query>&format=json`
- Web UI at `http://localhost:8080` (for manual testing)
- No public internet exposure — bound to `127.0.0.1` only

---

## Prerequisites

| Requirement | Check |
|---|---|
| Docker Engine ≥ 20.10 | `docker --version` |
| Docker Compose ≥ 2.0 | `docker compose version` |
| 2 GB free disk | `df -h /var/lib/docker` |
| Ports 8080 free | `lsof -i :8080` |

If Docker isn't installed, install it first:

- **macOS**: `brew install docker docker-compose`
- **Linux (Debian/Ubuntu)**: `sudo apt install docker.io docker-compose-plugin`
- **Linux (Arch)**: `sudo pacman -S docker docker-compose`

Then add your user to the `docker` group and reboot (or start the Docker daemon):

```bash
sudo usermod -aG docker $USER
# Then log out and back in, or:
newgrp docker
```

---

## Step 1 — Clone the SearXNG Docker repo

```bash
cd /usr/local   # or wherever you keep server infrastructure
sudo git clone https://github.com/searxng/searxng-docker.git
sudo chown -R $USER:docker searxng-docker
cd searxng-docker
```

---

## Step 2 — Create the `.env` file

SearXNG Docker uses `.env` for hostname and Let's Encrypt email. Since this is a
**local-only** instance we don't need Let's Encrypt, but the file must exist.

```bash
cat > .env << 'EOF'
SEARXNG_HOSTNAME=localhost
LETSENCRYPT_EMAIL=admin@localhost.localdomain
EOF
```

---

## Step 3 — Generate a secret key

SearXNG needs a `secret_key` for session encryption. Generate one:

```bash
docker run --rm -d alpine:latest sh -c "apk add --quiet openssl && openssl rand -hex 32" 2>/dev/null || \
openssl rand -hex 32
```

Copy the output (a 64-character hex string). You'll need it in the next step.

---

## Step 4 — Configure `searxng/settings.yml`

Create and edit the configuration file:

```bash
mkdir -p searxng
cat > searxng/settings.yml << 'SEARXNG_YAML'
############################
# SearXNG settings.yml     #
# Local-only instance       #
############################

use_default_settings: true

server:
  # CHANGE THIS to the key you generated in step 3
  secret_key: "REPLACE_WITH_YOUR_SECRET_KEY"
  port: 8080
  bind_address: "127.0.0.1"
  limiter: false            # no rate limiting needed for local use
  image_proxy: true         # proxy images through SearXNG for privacy
  public_instance: false
  method: "POST"            # POST by default; GET also works

search:
  formats:
    - html
    - json                  # required for the API
    - rss
  safe_search: 0
  autocomplete: "duckduckgo"

ui:
  static_use_hash: true
  center_results: true
  results_on_new_tab: false
  infinite_scroll: false
  tags: true
  categories_as_tabs: true
  query_in_header: true

# Engines you want enabled.
# By default ~84 engines are enabled. The list below shows only a subset.
# Comment out engines you don't want. Uncomment ones you do want.

engines:
  # General web search
  - name: google
    engine: google
    shortcut: go
    categories: general
    disabled: false

  - name: bing
    engine: bing
    shortcut: bi
    categories: general
    disabled: false

  - name: duckduckgo
    engine: duckduckgo
    shortcut: ddg
    categories: general
    disabled: false

  # Academic / specialized
  - name: wikipedia
    engine: wikipedia
    shortcut: wi
    categories: general
    disabled: false

  - name: wikidata
    engine: wikidata
    shortcut: wd
    categories: general
    disabled: false

  # News
  - name: google news
    engine: google_news
    shortcut: gn
    categories: news
    disabled: false

  # Images
  - name: google images
    engine: google_images
    shortcut: goi
    categories: images
    disabled: false

  - name: bing images
    engine: bing_images
    shortcut: bii
    categories: images
    disabled: false
SEARXNG_YAML
```

**Important**: Replace `REPLACE_WITH_YOUR_SECRET_KEY` with the hex string from
step 3. You can do this with:

```bash
KEY=$(openssl rand -hex 32)
sed -i '' "s|REPLACE_WITH_YOUR_SECRET_KEY|$KEY|" searxng/settings.yml
# On Linux, use: sed -i "s|REPLACE_WITH_YOUR_SECRET_KEY|$KEY|" searxng/settings.yml
```

---

## Step 5 — Disable Caddy (local-only mode)

The default `docker-compose.yml` includes Caddy for TLS. Since we're local-only,
we can either leave Caddy running (harmless but unnecessary) or strip it out.

**Option A — keep Caddy (simplest):** Just start everything. Caddy will serve
on port 8080 and proxy to SearXNG on port 8080 internally. No harm done.

**Option B — remove Caddy (cleaner):** Create a docker-compose override:

```bash
cat > docker-compose.local.yml << 'EOF'
version: '3.7'

services:
  searxng:
    ports:
      - "127.0.0.1:8080:8080"

  caddy:
    profiles:
      - never   # disable Caddy entirely
EOF
```

This binds SearXNG directly to `127.0.0.1:8080` and disables the Caddy
reverse-proxy container.

---

## Step 6 — Start the services

```bash
docker compose up -d
```

If you created the override file:

```bash
docker compose -f docker-compose.yml -f docker-compose.local.yml up -d
```

Wait for everything to come up. First pull may take a minute.

Check status:

```bash
docker compose ps
# Both searxng and (optionally) caddy should show "running"
```

---

## Step 7 — Verify it works

### Web UI

Open `http://localhost:8080` in your browser. You should see the SearXNG
search page. Type a query and hit Enter — results should appear.

### JSON API (what the agent uses)

```bash
curl -s 'http://localhost:8080/search?q=hello+world&format=json' | head -20
```

You should see JSON output with `query`, `results`, etc.

### Check logs

```bash
docker compose logs --tail=50 searxng
```

---

## Step 8 — Tweak engine settings (optional)

SearXNG enables ~84 engines by default. Many will return errors because they
require API keys or are blocked by rate limits. You can:

1. Open the web UI at `http://localhost:8080`
2. Click the hamburger menu (top-right) → "Settings"
3. Scroll through the engines list and disable ones that show errors

Or edit `searxng/settings.yml` directly:

```yaml
engines:
  - name: google
    engine: google
    disabled: true   # set true to disable
```

After editing, restart:

```bash
docker compose restart searxng
```

---

## Step 9 — Configure the deep-research agent to use SearXNG

Edit your `config.yaml`:

```yaml
search:
  primary: "searxng"               # use SearXNG as primary backend
  fallback_chain: ["tavily"]       # Tavily as fallback (if you have a key)
  searxng:
    url: "http://localhost:8080/search"
    max_results: 10
```

Now the agent will search through your local SearXNG instance.

---

## Step 10 — Keep it running (optional)

The container will restart automatically if the Docker host reboots (unless you
used `docker run` directly). To stop/restart:

```bash
docker compose stop   # pause
docker compose start  # resume
docker compose down   # full stop + remove containers
```

---

## Troubleshooting

### Engine errors in the UI

Some engines require API keys (e.g., Google Custom Search). Disable them via
settings. The remaining engines will still work fine.

### `docker compose` not found

Install Docker Compose plugin:

```bash
# macOS: already included in docker
# Debian/Ubuntu: sudo apt install docker-compose-plugin
# Arch: sudo pacman -S docker-compose
```

### Port 8080 already in use

Change the port in `searxng/settings.yml` (`server.port`) and the
`docker-compose.local.yml` mapping.

### No results from JSON API

Make sure `json` is listed under `search.formats` in settings.yml. The default
template above includes it.

### Container won't start — check logs

```bash
docker compose logs --tail=100
```

Common issues: secret key not set, port conflict, or Docker not running.

---

## Quick reference

| Component | Location / URL |
|---|---|
| Web UI | `http://localhost:8080` |
| JSON API | `http://localhost:8080/search?q=<query>&format=json` |
| Config file | `searxng/settings.yml` |
| Docker compose | `docker-compose.yml` |
| Override file | `docker-compose.local.yml` (optional) |
| Container name | `searxng-docker-searxng-1` |
| Stop everything | `docker compose down` |
| Restart SearXNG | `docker compose restart searxng` |

---

## Alternative: Non-Docker setup (direct Python installation)

If you prefer not to use Docker, you can install SearXNG directly with Python.
This method runs SearXNG as a native process using the development server
(not recommended for production, but fine for local / single-user use).

### Prerequisites

| Requirement | Check |
|---|---|
| Python ≥ 3.10 | `python3 --version` |
| Git | `git --version` |
| Port 8080 free | `lsof -i :8080` |

No Docker needed.

---

### Step 1 — Create the SearXNG user (Linux only; skip on macOS)

On Linux, create a dedicated system user. On macOS you can run as your own user.

```bash
# Linux only
sudo useradd --shell /bin/bash --system \
    --home-dir "/usr/local/searxng" \
    --comment 'Privacy-respecting metasearch engine' \
    searxng
sudo mkdir -p /usr/local/searxng
sudo chown searxng:searxng /usr/local/searxng
```

On macOS, just pick a directory:

```bash
sudo mkdir -p /usr/local/searxng
sudo chown $USER /usr/local/searxng
```

---

### Step 2 — Clone the SearXNG source

```bash
cd /usr/local/searxng
git clone https://github.com/searxng/searxng.git searxng-src
cd searxng-src
```

---

### Step 3 — Create a Python virtual environment

```bash
# Create a virtual environment (not in the source tree)
python3 -m venv /usr/local/searxng/searx-pyenv

# Activate it (Linux as searxng user)
sudo -u searxng -H bash -c 'source /usr/local/searxng/searx-pyenv/bin/activate && echo "venv ready"'

# Or on macOS / as your own user:
source /usr/local/searxng/searx-pyenv/bin/activate
```

---

### Step 4 — Install SearXNG and dependencies

Inside the virtual environment, install SearXNG in editable mode:

```bash
# Activate first, then:
pip install -U pip setuptools wheel
pip install -U pyyaml msgspec typing-extensions pybind11
pip install --use-pep517 --no-build-isolation -e /usr/local/searxng/searxng-src
```

This installs all required dependencies from `requirements.txt` and puts the
`searx` package on your Python path.

---

### Step 5 — Create the configuration file

Create `/etc/searxng/settings.yml` (or any path you prefer):

```bash
sudo mkdir -p /etc/searxng
```

Then write this config:

```bash
cat > /etc/searxng/settings.yml << 'SEARXNG_YAML'
############################
# SearXNG settings.yml     #
# Local non-Docker instance #
############################

use_default_settings: true

server:
  secret_key: "REPLACE_WITH_YOUR_SECRET_KEY"
  port: 8080
  bind_address: "127.0.0.1"
  limiter: false
  image_proxy: true
  public_instance: false
  method: "POST"

search:
  formats:
    - html
    - json
    - rss
  safe_search: 0
  autocomplete: "duckduckgo"

ui:
  static_use_hash: true
  center_results: true
  results_on_new_tab: false
  infinite_scroll: false
  tags: true
  categories_as_tabs: true
  query_in_header: true

engines:
  - name: google
    engine: google
    shortcut: go
    categories: general
    disabled: false
  - name: bing
    engine: bing
    shortcut: bi
    categories: general
    disabled: false
  - name: duckduckgo
    engine: duckduckgo
    shortcut: ddg
    categories: general
    disabled: false
  - name: wikipedia
    engine: wikipedia
    shortcut: wi
    categories: general
    disabled: false
SEARXNG_YAML
```

Generate and insert the secret key:

```bash
KEY=$(openssl rand -hex 32)
# macOS:
sed -i '' "s|REPLACE_WITH_YOUR_SECRET_KEY|$KEY|" /etc/searxng/settings.yml
# Linux:
# sed -i "s|REPLACE_WITH_YOUR_SECRET_KEY|$KEY|" /etc/searxng/settings.yml
```

---

### Step 6 — Start SearXNG

Set the config path and run the development server:

```bash
export SEARXNG_SETTINGS_PATH=/etc/searxng/settings.yml
cd /usr/local/searxng/searxng-src
python -m searx.webapp
```

You should see:

```
INFO:root: start gevent subprocess ...
SearXNG webapp serving at 127.0.0.1:8080
```

Verify:

```bash
curl -s 'http://localhost:8080/search?q=hello&format=json' | head -20
```

---

### Step 7 — (Optional) Run with uWSGI for production-like stability

Install uWSGI in the virtual environment:

```bash
pip install uwsgi
```

Create `/etc/searxng/searxng-uwsgi.ini`:

```ini
[uwsgi]
uid = searxng
gid = searxng
env = LANG=C.UTF-8
env = LC_ALL=C.UTF-8
chdir = /usr/local/searxng/searxng-src/searx
env = SEARXNG_SETTINGS_PATH=/etc/searxng/settings.yml
module = searx.webapp
virtualenv = /usr/local/searxng/searx-pyenv
pythonpath = /usr/local/searxng/searxng-src
socket = /usr/local/searxng/run/socket
buffer-size = 8192
enable-threads = true
workers = 4
threads = 4
offload-threads = 4
disable-logging = true
chmod-socket = 666
```

On macOS, omit `uid`/`gid` lines and run as your own user. Start with:

```bash
# HTTP-only mode (no Unix socket):
uwsgi --http 127.0.0.1:8080 --module searx.webapp \
  --virtualenv /usr/local/searxng/searx-pyenv \
  --pythonpath /usr/local/searxng/searxng-src \
  --env SEARXNG_SETTINGS_PATH=/etc/searxng/settings.yml \
  --buffer-size 8192 --enable-threads --workers 4 --threads 4
```

---

### Step 8 — (Linux only) systemd service for auto-start

Create `/etc/systemd/system/searxng.service`:

```ini
[Unit]
Description=SearXNG metasearch engine
After=network.target

[Service]
Type=simple
User=searxng
Group=searxng
Environment=SEARXNG_SETTINGS_PATH=/etc/searxng/settings.yml
WorkingDirectory=/usr/local/searxng/searxng-src/searx
ExecStart=/usr/local/searxng/searx-pyenv/bin/uwsgi --http 127.0.0.1:8080 \
  --module searx.webapp \
  --virtualenv /usr/local/searxng/searx-pyenv \
  --pythonpath /usr/local/searxng/searxng-src \
  --buffer-size 8192 --enable-threads --workers 4 --threads 4
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Enable and start:

```bash
sudo systemctl enable searxng
sudo systemctl start searxng
sudo systemctl status searxng
```

---

### Step 9 — Wire the agent to use the non-Docker SearXNG

Same `config.yaml` as the Docker setup:

```yaml
search:
  primary: "searxng"
  fallback_chain: ["tavily"]
  searxng:
    url: "http://localhost:8080/search"
    max_results: 10
```

---

### Non-Docker quick reference

| Component | Location / URL |
|---|---|
| Web UI | `http://localhost:8080` |
| JSON API | `http://localhost:8080/search?q=<query>&format=json` |
| Config file | `/etc/searxng/settings.yml` |
| Source code | `/usr/local/searxng/searxng-src` |
| Virtual env | `/usr/local/searxng/searx-pyenv` |
| Dev server | `python -m searx.webapp` |
| uWSGI config | `/etc/searxng/searxng-uwsgi.ini` |
| systemd service | `sudo systemctl start searxng` |
| Stop dev server | Ctrl+C |
| Stop uWSGI | `kill $(pgrep -f searx.webapp)` |

---

## Enabling the Scholar engine (optional)

SearXNG ships with a `scholar` engine that queries Google Scholar directly. To enable:

**Docker:** edit `searxng/settings.yml` (mounted volume) and add under `engines:`

```yaml
  scholar:
    enabled: true
    shortcut: sch
    engine: scholar
    paging: true
    first_page_number: 0
    display_title: Scholar
    timeout: 30
    search_range: 5
    language: en
```

**Non-Docker:** edit `/etc/searxng/settings.yml` directly, same stanza.

Then restart SearXNG (`docker compose restart` or `sudo systemctl restart searxng`). Verify by searching `?q=transformer&categories=scholar&format=json` at your API URL.

Once enabled, set `scholar.primary: "searxng"` in `config.yaml` (see config.example.yaml).
