# Update Dashboard

A self-hosted dashboard for monitoring and applying OS package updates and Docker Compose stack updates across multiple Linux hosts.

Built with FastAPI + HTMX. No JavaScript frameworks, no database — just a single Docker container.

## Features

- **OS package updates** — checks `apt` on each host via SSH, shows pending updates with version diffs
- **One-click apt upgrade** — runs `apt upgrade` remotely with live output
- **Reboot detection** — shows a "Reboot required" badge and restart button when `/var/run/reboot-required` exists
- **Docker Compose monitoring** — connects to hosts via SSH to discover running stacks, compares image digests against the registry to detect available updates — no Portainer required
- **Portainer support** — optionally use a Portainer API as an alternative Docker backend
- **One-click stack redeploy** — pulls latest images and restarts the stack
- **Auto-discovery** — when you add a host and save credentials, the dashboard checks for running Compose stacks and offers to monitor them
- **Encrypted credential store** — SSH keys, SSH passwords, and sudo passwords are stored encrypted on disk; nothing sensitive ever touches `config.yml`
- **Sudo modal** — non-root users are prompted for their sudo password inline on Update/Restart, with an option to save it for future runs
- **Admin panel** — manage hosts, credentials, and SSH settings through the UI, no file editing required

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
      - ./data:/app/data         # encrypted credentials + encryption key
    environment:
      - CONFIG_PATH=/app/config/config.yml
      - DATA_PATH=/app/data
      # Optional: Portainer backend
      # - PORTAINER_URL=https://192.168.1.x:9443
      # - PORTAINER_API_KEY=your_portainer_api_key_here
      # - PORTAINER_VERIFY_SSL=false
      # Optional: Docker Hub credentials (raises anonymous pull rate limit)
      # - DOCKERHUB_USERNAME=your_username
      # - DOCKERHUB_TOKEN=your_access_token
    restart: unless-stopped
```

### 3. Start

```bash
docker compose up -d
```

Open **http://localhost:8765** — then go to **/admin** to add your hosts.

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

If you already run Portainer, set these environment variables and both backends will operate simultaneously:

```yaml
environment:
  - PORTAINER_URL=https://192.168.1.x:9443
  - PORTAINER_API_KEY=your_portainer_api_key_here
  - PORTAINER_VERIFY_SSL=false
```

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
| `DATA_PATH` | `/app/data` | Directory for the encrypted credential store |
| `PORTAINER_URL` | — | Portainer instance URL (enables Portainer backend) |
| `PORTAINER_API_KEY` | — | Portainer API key (generate in User settings → Access tokens) |
| `PORTAINER_VERIFY_SSL` | `false` | Set `true` if Portainer uses a trusted certificate |
| `DOCKERHUB_USERNAME` | — | Docker Hub username — raises rate limit from 100 to 200 pulls/6h |
| `DOCKERHUB_TOKEN` | — | Docker Hub access token (not your login password) |

---

## Architecture

```
FastAPI (Python)
├── SSH via asyncssh
│   ├── apt check / upgrade / reboot on each host
│   └── docker compose ls / inspect / pull / up -d per host
├── Container backends (protocol-based, pluggable)
│   ├── SSHDockerBackend  — direct SSH + docker CLI (no Portainer needed)
│   └── PortainerBackend  — Portainer API via httpx (optional)
├── Encrypted credential store (Fernet)
│   └── SSH keys, SSH passwords, sudo passwords — never in config.yml
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
