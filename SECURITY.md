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
