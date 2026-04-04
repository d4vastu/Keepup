# Update Dashboard

A self-hosted dashboard for monitoring and applying OS package updates and Docker Compose stack updates across multiple Linux hosts.

Built with FastAPI + HTMX. No JavaScript frameworks, no database — just a single Docker container.

## Features

- **OS package updates** — checks `apt`, `dnf`, `yum`, `zypper`, `pacman`, and `apk` on each host via SSH, shows pending updates with version diffs
- **One-click upgrade** — runs the appropriate upgrade command remotely with live output
- **Reboot detection** — shows a "Reboot required" badge and restart button after kernel/system updates
- **Docker Compose monitoring** — connects to hosts via SSH to discover running stacks, compares image digests against the registry to detect available updates — no Portainer required
- **Portainer support** — optionally use a Portainer API as an alternative Docker backend
- **One-click stack redeploy** — pulls latest images and restarts the stack
- **Auto-discovery** — when you add a host and save credentials, the dashboard checks for running Compose stacks and offers to monitor them
- **Auto-updates** — schedule unattended OS upgrades and Docker stack redeployments per host/stack with individual cron schedules; optional auto-reboot per host
- **Notification bell** — persists auto-update run history; badge turns red on failures so you know when something needs attention
- **Encrypted credential store** — SSH keys, SSH passwords, sudo passwords, API keys, and tokens are stored encrypted on disk; nothing sensitive ever touches `config.yml`
- **Sudo modal** — non-root users are prompted for their sudo password inline on Update/Restart, with an option to save it for future runs
- **Fully UI-managed** — hosts, credentials, SSH settings, Portainer, DockerHub, and auto-update schedules are all configured through the admin panel; no file editing or env vars required after initial setup

---

## Quick Start

### 1. Create the directory structure

```bash
mkdir update-dashboard && cd update-dashboard
mkdir config data
```

### 2. Create docker-compose.yml

```yaml
services:
  update-dashboard:
    image: ghcr.io/d4vastu/update-dashboard:latest
    container_name: update-dashboard
    ports:
      - "8765:8000"
    volumes:
      - ./config:/app/config    # hosts + SSH settings
      - ./data:/app/data        # encrypted credentials + encryption key
    environment:
      - CONFIG_PATH=/app/config/config.yml
      - DATA_PATH=/app/data
    restart: unless-stopped
```

### 3. Start

```bash
docker compose up -d
```

Open **http://localhost:8765** — then go to **/admin** to add your hosts and configure connections.

> **Portainer and DockerHub** are configured through **Admin → Connections**, not via environment variables. See [Connections](#connections) below.

---

## Adding Hosts

1. Go to **/admin → Add Host** — enter name, IP/hostname, SSH user (optional), and port (optional)
2. Click **Credentials** on the new host row — choose SSH Key or Password auth and optionally save a sudo password
3. Click **Test** to verify the connection
4. If Docker Compose stacks are running on the host, a prompt will appear automatically: choose to monitor all stacks, all including future ones, or select specific stacks

---

## SSH Authentication

Credentials are stored **encrypted** in `/app/data/credentials.json`. The encryption key is auto-generated at `/app/data/.secret` on first run. Nothing sensitive is written to `config.yml`.

### SSH Key (recommended)

Generate a dedicated key pair and authorize it on each host:

```bash
ssh-keygen -t ed25519 -f ~/.ssh/dashboard_key -N ""
ssh-copy-id -i ~/.ssh/dashboard_key.pub user@your-host
```

Then paste the private key contents into the **Credentials** form in the admin panel.

Alternatively, mount a key file and use the SSH default key path in SSH Settings:

```bash
mkdir keys
ssh-keygen -t ed25519 -f keys/id_ed25519 -N ""
ssh-copy-id -i keys/id_ed25519.pub root@your-host
```

```yaml
volumes:
  - ./keys:/app/keys:ro
```

### SSH Password

Enable `PasswordAuthentication yes` in `/etc/ssh/sshd_config` on the remote host, then enter the password in the Credentials form.

### Sudo

If your SSH user is not root, the dashboard will prompt for a sudo password inline when running updates or reboots. You can save it so future runs don't prompt again.

---

## Docker Compose Monitoring

Docker monitoring works over the same SSH connection used for OS updates — no Portainer required.

**Requirements on the remote host:**
- Docker Engine and Docker Compose v2 (`docker compose` subcommand)
- The SSH user must have permission to run `docker compose` commands (either root or a user in the `docker` group)

**How it works:**
1. The dashboard runs `docker compose ls --all --format json` to discover stacks
2. For each running container, it fetches the local image digest via `docker image inspect` and compares it against the registry
3. Updates are applied with `docker compose pull && docker compose up -d`

**Monitoring modes** (set per-host in the admin panel):
- **Monitor all** — watches all stacks present at setup time
- **Monitor all + new** — always queries the host fresh, picks up newly added stacks automatically
- **Select** — choose specific stack names to watch

### Portainer (optional)

If you already run Portainer, you can use it as an additional Docker backend alongside SSH.

Configure it in **Admin → Connections → Portainer** — enter the URL, paste your API token, and click **Test Connection** before saving. No environment variables needed.

To get your Portainer API token: open Portainer → click your username (top-right) → **Account Settings** → **Access Tokens** → **Add access token**.

---

## Auto-Updates

Schedule unattended updates in **Admin → Auto-Updates**.

**OS updates (per host):**
- Enable the toggle, set a cron schedule (UTC), and optionally enable auto-reboot
- Uses `apt upgrade` (safe mode — never removes packages)
- If auto-reboot is on, the host reboots immediately after the update if a reboot was required
- Non-root hosts require a saved sudo password (set in Admin → Hosts → Credentials)

**Docker stack redeployments (per stack):**
- Enable the toggle and set a cron schedule
- Pulls the latest image tag and restarts the stack
- Note: if your image uses the `latest` tag, a breaking upstream change could be pulled automatically — pin your image versions if that's a concern

**Notification bell:**
- Appears in every page header
- Badge turns red when an auto-update fails
- Click to see recent run history and dismiss notifications

---

## Connections

Configure Portainer and Docker Hub in **Admin → Connections** — no environment variables needed.

**Portainer** — required to use the Portainer backend for Docker monitoring. Enter your Portainer URL and API token, click **Test Connection** to verify before saving.

**Docker Hub** (optional) — without credentials, Docker Hub limits image update checks to 100/hour shared across your IP. A free account bumps this to 200/hour for you alone. Use an access token (not your password): hub.docker.com → Account Settings → Personal access tokens.

Both integrations take effect immediately on save — no restart needed.

> **Legacy:** Portainer and DockerHub can still be configured via environment variables (`PORTAINER_URL`, `PORTAINER_API_KEY`, `DOCKERHUB_USERNAME`, `DOCKERHUB_TOKEN`) if you prefer. The UI will show a nudge to migrate when env-var-only config is detected.

---

## config.yml

`config.yml` stores host topology and SSH defaults only — no secrets. It lives in the `config/` volume and is managed by the admin panel.

```yaml
ssh:
  default_key: /app/keys/id_ed25519   # path inside the container (if using key files)
  default_user: root
  default_port: 22
  connect_timeout: 15
  command_timeout: 600

hosts:
  - name: "Proxmox"
    host: 192.168.1.10

  - name: "Media Server"
    host: 192.168.1.20
    user: dmadmin
    port: 22
    docker_mode: all_and_new          # monitor all Compose stacks, including new ones

  - name: "Nextcloud"
    host: 192.168.1.30
    user: ubuntu
    docker_mode: selected
    docker_stacks:
      - nextcloud
      - nginx-proxy
```

**`docker_mode` values:**

| Value | Behaviour |
|---|---|
| `all` | Monitor stacks found when monitoring was enabled |
| `all_and_new` | Always query the host fresh; picks up new stacks automatically |
| `selected` | Only monitor the stacks listed in `docker_stacks` |
| *(absent)* | No Docker monitoring for this host |

---

## Volumes

| Volume | Purpose |
|---|---|
| `./config` | `config.yml` — host list, SSH settings. Safe to back up and commit (no secrets). |
| `./data` | `credentials.json` (encrypted) and `.secret` (encryption key). **Back up and restrict access.** |
| `./keys` (optional) | SSH key files if using file-based key auth instead of pasting keys in the UI. Mount read-only. |

---

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `CONFIG_PATH` | `/app/config.yml` | Path to `config.yml` inside the container |
| `DATA_PATH` | `/app/data` | Directory for the encrypted credential store and auto-update log |
| `PORTAINER_URL` | — | *(Legacy)* Portainer URL — prefer Admin → Connections |
| `PORTAINER_API_KEY` | — | *(Legacy)* Portainer API key — prefer Admin → Connections |
| `PORTAINER_VERIFY_SSL` | `false` | *(Legacy)* Set `true` if Portainer uses a trusted certificate |
| `DOCKERHUB_USERNAME` | — | *(Legacy)* Docker Hub username — prefer Admin → Connections |
| `DOCKERHUB_TOKEN` | — | *(Legacy)* Docker Hub access token — prefer Admin → Connections |

---

## Architecture

```
FastAPI (Python)
├── SSH via asyncssh
│   ├── multi-PM check / upgrade / reboot on each host (apt, dnf, yum, zypper, pacman, apk)
│   └── docker compose ls / inspect / pull / up -d per host
├── Container backends (protocol-based, pluggable)
│   ├── SSHDockerBackend  — direct SSH + docker CLI (no Portainer needed)
│   └── PortainerBackend  — Portainer API via httpx (optional)
├── Auto-update scheduler (APScheduler AsyncIOScheduler)
│   ├── per-host OS upgrade jobs with optional auto-reboot
│   └── per-stack Docker redeploy jobs
├── Encrypted credential store (Fernet)
│   └── SSH keys, SSH passwords, sudo passwords, API keys, tokens — never in config.yml
└── HTMX frontend — partial HTML responses, no page reloads
```

---

## Development

```bash
git clone https://github.com/d4vastu/update-dashboard.git
cd update-dashboard
pip install -r requirements.txt

# Run locally
CONFIG_PATH=./config/config.yml DATA_PATH=./data uvicorn app.main:app --reload --port 8000
```

Run tests:

```bash
pytest --cov=app --cov-fail-under=95
```

---

## License

MIT
