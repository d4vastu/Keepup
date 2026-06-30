# Security Policy

## Supported Versions

Keepup is pre-1.0 beta software. Only the current `0.x` release line receives
security fixes. Older minor versions are not supported — please upgrade before
reporting an issue.

| Version | Supported          |
| ------- | ------------------ |
| 0.x (latest) | :white_check_mark: |
| < latest 0.x | :x:           |

## Reporting a Vulnerability

**Please do not open a public GitHub issue for security problems.**

Use GitHub's private vulnerability reporting:

1. Go to the [Security tab](https://github.com/d4vastu/Keepup/security) of this
   repository
2. Click **Report a vulnerability**
3. Fill in the form — the report is visible only to maintainers

If private vulnerability reporting is unavailable for any reason, contact the
maintainer directly via the email address listed on their GitHub profile.

### What to expect

- **Acknowledgement:** within 7 days of receipt.
- **Fix timeline:** Keepup is maintained by a single person on a best-effort
  basis. There is no SLA for releasing a fix; complex issues may take weeks.
- **Disclosure:** once a fix is released, the report is made public via a
  GitHub Security Advisory crediting the reporter (unless they prefer to remain
  anonymous).

## Scope

Keepup is designed for **personal homelab use**. It has not been audited and is
not hardened for:

- Multi-tenant or shared environments
- Direct exposure to the public internet
- Compliance-regulated workloads

Reports about issues that only manifest in those scenarios are still welcome,
but the fix priority will reflect the project's intended scope. See the README
for the full "homelab only" framing.

Out of scope:

- Self-XSS that requires the user to paste attacker-controlled content into the
  browser DevTools console
- Vulnerabilities in third-party dependencies that have not yet been published
  upstream — please report those to the upstream project first
- Findings from automated scanners with no demonstrated exploit path

## Credential storage & secret management

Keepup connects to your hosts, so it necessarily holds credentials (SSH
passwords, SSH private keys, sudo passwords, and integration API keys). Here is
exactly how they are protected, and where the protection ends.

**What is protected:**

- Credentials are stored **Fernet-encrypted at rest** in
  `./data/credentials.json` — never in plaintext, and never in `config.yml`.
- The encryption key and the credential store are written with `0o600`
  permissions (owner read/write only).
- SSH private keys are referenced by bare filename and resolved through a
  path-traversal guard, so a crafted name cannot read files outside the keys
  directory.

**What is _not_ protected by default — and you should understand this:**

By default the encryption key is auto-generated to `./data/.secret`, which lives
in the **same volume** as the encrypted `credentials.json`. At-rest encryption
therefore protects you against a *partial* leak (e.g. the credentials file alone
being copied or committed), but **not** against anyone who obtains the whole
`./data` volume — they get the lock and the key together.

This matters most for **backups**. A snapshot or archive of `./data` (for
example a **Proxmox Backup Server** job, or the `tar` in the README's Backup
section) contains both the key and the ciphertext. Anyone who can restore that
backup can decrypt every credential. Treat a `./data` backup as equivalent to a
plaintext copy of all your homelab credentials, and store it accordingly.

**Hardening — keep the key out of the data volume:**

You can supply the encryption key from a source outside `./data` so backups of
the volume no longer contain it. Set **one** of:

| Variable | Meaning |
|---|---|
| `KEEPUP_SECRET_KEY` | The Fernet key itself, as an environment variable. |
| `KEEPUP_SECRET_KEY_FILE` | Path to a file containing the key — e.g. a Docker / Podman / Kubernetes secret mounted at `/run/secrets/keepup_secret_key`. |

`KEEPUP_SECRET_KEY` takes precedence over `KEEPUP_SECRET_KEY_FILE`, which takes
precedence over the auto-generated `./data/.secret`. When either is set, Keepup
uses that key and never writes `.secret` into the data volume.

If the supplied key is malformed (or the file path cannot be read), Keepup
**fails fast at startup** with a clear error rather than silently generating a
new key — a silently regenerated key would make your existing credential store
undecryptable.

**Migrating an existing install:** your current key is the contents of
`./data/.secret`. Move it into a Docker secret or your environment, set
`KEEPUP_SECRET_KEY` / `KEEPUP_SECRET_KEY_FILE`, then remove `.secret` from the
data volume. Keep the key backed up **separately** from the `./data` volume —
without it the credential store cannot be decrypted.
