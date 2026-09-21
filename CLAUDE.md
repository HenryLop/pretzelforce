# PretzelForce

## What this is

A Salesforce code-to-production agent pipeline, built as a **learning project** for agent
architecture. A git diff of Salesforce metadata goes in; a deploy comes out. Five stages, each
its own agent with its own loop, tools, and system prompt.

Because it's a learning project, explanations are a deliverable: every step gets a page in
[`docs/`](docs/README.md). Keep that wiki current — it is the point of the repo, not a byproduct.

## Environment

- Windows 11, PowerShell 5.1 is the primary shell.
  - No `&&` / `||` chaining. Use `;` or `if ($?) { ... }`.
  - No ternary, `??`, or `?.` operators.

## Stack

- Language / runtime: Python 3.12
- Package manager: `pip` + `pyproject.toml` (editable install, src layout)
- Model: `claude-opus-5` via `anthropic` >= 1.4.0 (adaptive thinking, effort per stage)
- Salesforce: `sf` CLI 2.149.9
- Storage: SQLite (stdlib `sqlite3`)
- Test runner: `pytest` (offline, canned model responses); `demo_harness` is the harness smoke test
- Lint / format: none yet

## Commands

| Task            | Command                                |
| --------------- | -------------------------------------- |
| Install         | `pip install -e .`                     |
| Smoke (offline) | `python -m pretzel.demo_harness`        |
| Smoke (live)    | `python -m pretzel.demo_harness --live` |
| Tests (offline) | `python -m pytest`                     |
| Review (offline)| `pretzel review --repo <sfdx> --base main --head <branch>` |
| List orgs       | `sf org list`                          |

## Conventions

- Keep `README.md` accurate as the stack lands.
- Secrets go in `.env` (gitignored); commit an `.env.example` with keys but no values.
- Every build step ends by writing its `docs/` page: what we built → why it exists
  architecturally → the code → what to remember.
- `main` requires a PR (branch protection). Branch, push, open a PR — don't push to `main`.
- Prefer explaining a mechanism over hiding it. This repo optimizes for understanding, not
  for the shortest path to a working deploy.
