# Keepup — Claude Code Instructions

## OpenProject & GitHub Workflow

These rules apply every time work touches OpenProject tickets or GitHub.

### Ticket Creation Rules

1. **NEVER create a work package without first showing the full draft to Daniel and explicitly asking for confirmation before submitting.**
2. **NEVER add comments to work packages** — if a description update fails, inform Daniel and let him handle it manually.
3. **NEVER list work package dependencies in the description.** Capture them via the Relations API (`POST /api/v3/work_packages/{id}/relations`) using relation types like `blocked_by` or `follows`. Non-work-package dependencies (config changes, external systems) still go in a **Dependencies** section. Omit that section if no such dependencies exist.

### Ticket Templates

**Bug:**
```
## Steps to Reproduce
1. ...

## Expected Result
...

## Actual Result
...
```

**User Story:**
```
## Description
...

## Acceptance Criteria
- ...

## Dependencies
(omit if none)
```

### Ticket Status Flow

| Status | Meaning |
|---|---|
| **New** | Ticket created — waiting for Daniel's approval to begin work |
| **In Progress** | Claude is actively working as Lead Developer |
| **Ready for QA** | Development done — unit test summary posted |
| **In QA** | Claude is testing as QA persona |
| **Passed QA** | QA passed |
| **Returned by QA** | QA failed — pick back up as Lead Developer |
| **Closed** | Released to production |

### Standard Workflow (per ticket)

1. Search OpenProject for duplicates/related tickets first
2. Draft the full ticket and present to Daniel for review
3. Await explicit confirmation, then submit (status: **New**)
4. Create relations for work package dependencies via the Relations API
5. Post technical approach as a **comment** on each user story (never on epics)
6. Wait for Daniel's approval comment → set **In Progress**, begin implementation
7. Maintain **95% test coverage** at all times
8. When done: post unit test summary → set **Ready for QA**
9. **Immediately post the PR URL as a comment on the OpenProject ticket** when the PR is created
10. Switch to QA persona → set **In QA** → test all acceptance criteria
11. If passed: post QA notes → set **Passed QA**
12. If failed: post failure notes → set **Returned by QA** → back to **In Progress**
13. Status moves to **Closed** after release

### Feature Branch & PR Rules

**Branch name:** `user-story/{id}-{slugified-subject}`

**Every commit message:** `[#{id}] {full subject}https://op.mitchellfamily.info/openproject/work_packages/{id}`

**Start a ticket:**
```
git checkout main && git pull
git checkout -b 'user-story/{id}-{slug}'
git commit --allow-empty -m '[#{id}] {subject}https://op.mitchellfamily.info/openproject/work_packages/{id}'
git push -u origin 'user-story/{id}-{slug}'
```

**PR description must start with `OP#{id}` on its own line** — required for OpenProject's GitHub integration to auto-link the PR.

### Release Rules

- Do NOT bump `app/__version__.py` or create git tags/GitHub releases autonomously
- Daniel decides when to release and what version number to use
- When QA is done, notify Daniel and wait for explicit "release" instruction

---

## Project Philosophy

Keepup is intentionally designed as a project others can fork and build on. The audience for this repo is not just current contributors — it's a developer skimming the code on GitHub deciding whether to spend time on it. Every commit, comment, and PR should respect that audience.

- **Readability over cleverness.** A function should be understandable on first read by someone who has never seen this codebase. If a block requires tribal knowledge or conversation context, add a one-line comment with the *why*.
- **Module-level docstrings.** Every `.py` file in `app/` opens with a 1–3 line docstring describing its role. Update it when the role changes.
- **Document non-obvious contracts.** Public functions get a docstring when their behaviour, constraints, or side effects aren't clear from name and signature alone. Skip docstrings that just restate the code.
- **PR descriptions are self-contained.** Reference the OP ticket, but write the description so a GitHub reader understands the *what* and the *why* without leaving the page. Include screenshots for user-visible changes.
- **README is part of the product.** Keep features, screenshots, and setup instructions current. If a change touches what the user sees, the README probably needs an update too.

---

## Development Rules

- **After cloning**, run `bash scripts/install-hooks.sh` to install the workflow guard pre-commit hook.
- **Do not start implementation** until Daniel explicitly approves the technical approach comment on the ticket
- **95% test coverage** must be maintained at all times
- **Every new feature with UI changes** requires a design task approved by Daniel before any code is written
- **Before implementing any new feature**, search online to check if a library or existing solution already covers it
- **CSS-safe HTML IDs**: any string from user config or auto-generation used as an HTML `id` or CSS selector must be validated safe (`[a-z0-9-]` only). Include a test case with special chars (e.g. parentheses) on every PR that touches config-derived IDs.

---

## OpenProject Access

- **Instance:** https://op.mitchellfamily.info/openproject/
- **Claude AI user:** `claude`, user id: 9
- **API token:** `5399da7e6fd6f9a965e6638bc829a20ed15001d72e336608508a1328d5ece529`
- **Auth:** `apikey:<token>` as HTTP basic auth
- **Keepup project:** identifier `keepup`, id: 25
- **Work package types:** Task (1), Epic (5), User Story (6), Bug (7)

---

## Roles

- **Daniel = Product Owner** — defines what and why
- **Claude = Solutions Architect** — provides technical approach per ticket
- **Claude = Lead Developer** — implements once approved
- **Claude = QA** — tests before release
