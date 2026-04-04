# Update Dashboard

A self-hosted dashboard for monitoring and applying OS package updates and Docker stack image updates across multiple Linux hosts.

Built with FastAPI + HTMX. No JavaScript frameworks, no database — just a single Docker container.

![Dashboard screenshot](docs/screenshot.png)

## Features

- **OS package updates** — checks `apt` on each host via SSH, shows pending updates with version diffs
- **One-click apt upgrade** — runs `apt upgrade` remotely with live log streaming
- **Reboot detection** — shows a "Reboot required" badge and restart button when `/var/run/reboot-required` exists
- **Docker stack monitoring** — compares running image digests against the registry to detect available updates (via Portainer API)
- **One-click stack redeploy** — pulls latest images and restarts the stack via Portainer
- **Admin panel** — manage hosts and SSH settings through the UI, no file editing required

## Quick Start

### 1. Clone the repo

```bash
git clone https://github.com/d4vastu/update-dashboard.git
cd update-dashboard
```

### 2. Create your config file

```bash
cp config.example.yml config.yml
```

Edit `config.yml` to add your hosts and SSH settings (or use the Admin panel after starting).

### 3. Add your SSH key

Place your SSH private key in the `keys/` directory:

```bash
mkdir -p keys
cp ~/.ssh/id_ed25519 keys/
```

The key must allow passwordless login to your hosts as root (or the configured user).

### 4. Configure environment variables

Edit the `environment:` section in `docker-compose.yml`:

```yaml
environment:
  - PORTAINER_URL=https://192.168.1.x:9443
  - PORTAINER_API_KEY=your_portainer_api_key_here
  - PORTAINER_VERIFY_SSL=false
```

See [Environment Variables](#environment-variables) below for the full list.

### 5. Start the container

```bash
docker compose up -d
```

Open **http://localhost:8765** in your browser.

---

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `PORTAINER_URL` | For Docker monitoring | Full URL to your Portainer instance, e.g. `https://192.168.1.10:9443` |
| `PORTAINER_API_KEY` | For Docker monitoring | Portainer API key. Generate one in Portainer → User settings → Access tokens |
| `PORTAINER_VERIFY_SSL` | No | Set to `true` if Portainer uses a trusted certificate. Default: `false` |
| `DOCKERHUB_USERNAME` | No | Docker Hub username. Raises rate limit from 100 to 200 pulls/6h |
| `DOCKERHUB_TOKEN` | No | Docker Hub access token (not your password) |

---

## config.yml

Hosts and SSH settings live in `config.yml`. This file is mounted into the container and can be edited via the **Admin panel** at `/admin`.

```yaml
ssh:
  default_key: /app/keys/id_ed25519   # path inside the container
  default_user: root
  default_port: 22
  connect_timeout: 15                  # seconds
  command_timeout: 600                 # seconds (10 min for apt upgrade)

hosts:
  - name: "My Server"
    host: 192.168.1.10

  - name: "Another Host"
    host: 192.168.1.20
    user: ubuntu        # overrides default_user
    port: 2222          # overrides default_port
```

**Note:** `config.yml` is gitignored. Never commit it — it contains your host IPs and may contain sensitive paths.

---

## Admin Panel

Navigate to **/admin** to:

- Add, edit, or remove hosts
- Update SSH defaults (user, port, key path, timeouts)
- View connection status for Portainer and Docker Hub

Changes to hosts and SSH settings are written to `config.yml` immediately and take effect on the next check — no restart required.

---

## SSH Key Setup

Your hosts need to accept passwordless SSH from the container. The recommended setup:

```bash
# Generate a dedicated key for the dashboard
ssh-keygen -t ed25519 -f keys/id_ed25519 -N ""

# Copy the public key to each host
ssh-copy-id -i keys/id_ed25519.pub root@192.168.1.10
```

The `entrypoint.sh` script automatically fixes key permissions (`chmod 600`) at container startup since Docker volumes don't preserve them.

---

## Building from Source

```bash
git clone https://github.com/d4vastu/update-dashboard.git
cd update-dashboard
docker compose build
docker compose up -d
```

Or run locally for development:

```bash
pip install -r requirements.txt
CONFIG_PATH=./config.yml uvicorn app.main:app --reload --port 8000
```

---

## Architecture

```
FastAPI (Python)
├── SSH via asyncssh        → apt check / upgrade / reboot on each host
├── Portainer API via httpx → list stacks, compare image digests, redeploy
└── HTMX frontend           → partial HTML responses, no page reloads
```

Config is loaded from `config.yml` on each request (hosts + SSH). Secrets are injected via environment variables and never written to disk.

---

## License

MIT
