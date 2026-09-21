# 00 — Architecture

## What this project is

A five-stage pipeline that takes a git diff of Salesforce metadata and produces a deploy.
Each stage is its own agent: own system prompt, own tool surface, own loop.

```
git diff ──▶ 1. change analysis ──▶ 2. test selection + run ──▶ 3. validation deploy
                                                                       │
                                          5. final deploy + log ◀── 4. human approval
```

It is a **learning project**. The pipeline is the excuse; the point is to understand agent
architecture well enough to reason about it cold. Every design choice below was made to expose
a concept rather than to ship the shortest path to a working deploy.

## Concept map

| Concept | Where it shows up | Why here |
| --- | --- | --- |
| The agent loop | `harness/agent.py` | Five stages, one loop — build it once, correctly |
| Tool dispatch | `harness/tools.py` | Name → handler, with failure containment |
| Sub-agents | Stage 2 | Per-class test analysis fans out; keeps the parent's context clean |
| Human-in-the-loop | Stage 4 | The one place the loop is *supposed* to block |
| Memory across runs | SQLite (step 6) | Audit log + learned facts; distinct from in-session context |
| System prompt quality | Every stage | Same harness, different prompt, wildly different behavior |

## Decisions locked

**Hand-roll the loop, then port it.** The SDK ships `client.beta.messages.tool_runner`, which
drives the tool-call cycle for you. We are not using it first, because it hides exactly the
mechanisms the project exists to teach. Step 7 ports the harness to the runner as a
compare-and-contrast, once the manual version is understood.

**UAT sandbox stands in for production.** Stage 3 validates with `--dry-run`, Stage 5 really
deploys — both against a sandbox. No customer org is ever touched. The `sf` CLI calls are real,
so real failure modes and real output parsing still get exercised.

**Memory is two things in one database.** An *audit log* (runs, messages, tool calls, token
counts) that agents write but never read, and a *facts* table that agents read at start and
write at end. Conflating them is a common design mistake; keeping them apart makes the
distinction between "observability" and "memory" concrete.

**Docs live in the repo, not a GitHub Wiki.** A GitHub Wiki is a separate git repo
(`pretzelforce.wiki.git`). It bypasses the branch protection on `main` and cannot be reviewed
in a PR. In-repo `docs/` keeps an explanation and the code it explains in the same commit.

## Stack

| | |
| --- | --- |
| Language | Python 3.12 |
| Model | `claude-opus-5`, adaptive thinking, effort per stage |
| SDK | `anthropic` 1.4.0 (upgraded from 0.86.0 — 0.x predates `output_config` and `fallbacks`) |
| Salesforce | `sf` CLI 2.149.9 |
| Storage | SQLite (stdlib `sqlite3`) |
| Config | `python-dotenv`, `.env` gitignored |

## Roadmap

| Step | Builds | Concept it teaches | Status |
| --- | --- | --- | --- |
| 1 | Harness — the reusable loop | Agent loop, tool dispatch | ✅ [built](01-the-agent-loop.md) |
| 2 | Stage 1: change analysis | System prompt quality, structured outputs | ⬜ |
| 3 | Stage 2: test selection + `sf apex run test` | Sub-agents, context isolation | ⬜ |
| 4 | Stage 3+4: validation deploy, approval gate | Human-in-the-loop | ⬜ |
| 5 | Stage 5: real deploy + run log | Irreversible actions, audit trail | ⬜ |
| 6 | SQLite `runs` / `messages` / `tool_calls` / `facts` | Cross-run memory | ⬜ |
| 7 | Port to `tool_runner` | What the SDK does for you | ⬜ |

## Out of scope

No CI/CD integration, no webhooks, no scheduler, no web UI, no production org.
