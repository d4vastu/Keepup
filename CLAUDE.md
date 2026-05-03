# Keepup — Claude Code Instructions

This file holds project-wide guidance for any Claude Code session working on
Keepup. It is intentionally generic so a fork can use it as-is.

> **Maintainer note:** the project owner's personal workflow (issue tracker,
> ticket templates, status flow, role definitions, credentials) lives in
> `CLAUDE.local.md`, which is gitignored. A template is committed at
> `CLAUDE.local.md.example` — copy it to `CLAUDE.local.md` and fill it in if
> you are the maintainer. Outside contributors do not need this file.

## Project Philosophy

The full philosophy lives in [CONTRIBUTING.md](CONTRIBUTING.md#project-philosophy)
so contributors and Claude read the same source of truth. The short version:
readability over cleverness, module-level docstrings on every `app/` file,
self-contained PR descriptions, and the README is part of the product.

## Development Rules

- **After cloning**, run `bash scripts/install-hooks.sh` to install the
  branch-name pre-commit hook (optional but recommended).
- **95% test coverage** must be maintained at all times
  (`pytest --cov=app --cov-fail-under=95`).
- **Every new feature with UI changes** should have a design discussion before
  any code is written.
- **Before implementing any new feature**, search online to check whether a
  library or existing solution already covers it.
- **CSS-safe HTML IDs**: any string from user config or auto-generation used as
  an HTML `id` or CSS selector must be validated safe (`[a-z0-9-]` only).
  Include a test case with special chars (e.g. parentheses) on every PR that
  touches config-derived IDs.
- **Lint with ruff**: `ruff check app/ tests/` must pass.

## Branch and commit conventions

- Topic branches: `feat/<slug>`, `fix/<slug>`, `docs/<slug>`, `chore/<slug>`.
- Commit subjects are short, imperative, and self-explanatory.
- See [CONTRIBUTING.md](CONTRIBUTING.md) for the full contributor workflow,
  including PR templates and review expectations.

## Release Rules

- Do NOT bump `app/__version__.py` or create git tags / GitHub releases
  autonomously. The maintainer decides when to release and what version number
  to use.
- Notify the maintainer when a change is merged and ready for release; wait
  for an explicit "release" instruction before tagging.
