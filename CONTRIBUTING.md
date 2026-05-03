# Contributing to Keepup

Thanks for your interest in Keepup. This document describes how to set up a
local dev environment, run the test suite, and submit changes.

> Keepup is maintained by a single person and tracks day-to-day work in a
> private project tracker. **The OpenProject ticket workflow you may see
> referenced in commit messages is maintainer-only — outside contributors do
> not need to follow it.** Use GitHub issues and pull requests as described
> below.

## Project Philosophy

Keepup is intentionally designed as a project others can fork and build on. The
audience for this repo is not just current contributors — it's a developer
skimming the code on GitHub deciding whether to spend time on it. Every commit,
comment, and PR should respect that audience.

- **Readability over cleverness.** A function should be understandable on first
  read by someone who has never seen this codebase. If a block requires tribal
  knowledge or conversation context, add a one-line comment with the *why*.
- **Module-level docstrings.** Every `.py` file in `app/` opens with a 1–3 line
  docstring describing its role. Update it when the role changes.
- **Document non-obvious contracts.** Public functions get a docstring when
  their behaviour, constraints, or side effects aren't clear from name and
  signature alone. Skip docstrings that just restate the code.
- **PR descriptions are self-contained.** Reference the issue, but write the
  description so a GitHub reader understands the *what* and the *why* without
  leaving the page. Include screenshots for user-visible changes.
- **README is part of the product.** Keep features, screenshots, and setup
  instructions current. If a change touches what the user sees, the README
  probably needs an update too.

## Quick start

```bash
git clone https://github.com/d4vastu/Keepup.git
cd Keepup
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt

DATA_PATH=./data uvicorn app.main:app --reload --port 8000
```

The dev server is at <http://localhost:8000>. The first time you load it, the
setup wizard will guide you through creating an admin account.

The test suite uses isolated temp directories — running it does not touch your
local `./data` or any real SSH host.

## Tests and lint

Both must pass before you submit a PR.

```bash
pytest --cov=app --cov-fail-under=95
ruff check app/ tests/
```

Coverage must stay at or above 95%. If your change drops coverage, add tests.

## Branch and commit conventions

Use a topic branch named after the kind of work:

| Prefix         | When to use                                           |
| -------------- | ----------------------------------------------------- |
| `feat/<slug>`  | New functionality                                     |
| `fix/<slug>`   | Bug fix                                               |
| `docs/<slug>`  | Documentation only                                    |
| `chore/<slug>` | Tooling, CI, dependency bumps, repo housekeeping      |

Commit subjects should be short, imperative, and self-explanatory ("Add
Pushover retry on 5xx", not "fixed bug"). Conventional Commits style is
welcome but not required.

If you'd like the same branch-name guardrail locally, run
`bash scripts/install-hooks.sh` to install the pre-commit hook. It is
optional.

## Pull requests

- Keep PRs focused — one logical change per PR makes review faster.
- Fill out the PR template (it appears automatically when you open the PR).
- Link the related GitHub issue with `Fixes #N` or `Closes #N` so it auto-closes
  on merge.
- For any user-visible change, attach a screenshot or short screen recording.
- For any change to setup, configuration, or features, update the README.

A maintainer will review and either approve, request changes, or explain why
the change doesn't fit the project's scope.

## Reporting bugs and requesting features

Use the GitHub issue templates:

- [Bug report](.github/ISSUE_TEMPLATE/bug_report.md)
- [Feature request](.github/ISSUE_TEMPLATE/feature_request.md)

## Security issues

**Do not open a public issue for security problems.** Follow the process in
[SECURITY.md](SECURITY.md), which uses GitHub's private vulnerability
reporting.
