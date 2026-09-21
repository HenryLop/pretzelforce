# PretzelForce — project wiki

A Salesforce code-to-production agent pipeline, built as a **learning project** for agent
architecture. Five stages, each its own agent with its own loop, tools, and system prompt.

```
git diff ──▶ 1. change analysis ──▶ 2. test selection + run ──▶ 3. validation deploy
                                                                       │
                                          5. final deploy + log ◀── 4. human approval
```

## Pages

| # | Page | Covers |
| --- | --- | --- |
| 00 | [Architecture](00-architecture.md) | The pipeline, decisions locked, roadmap |
| 01 | [The agent loop](01-the-agent-loop.md) | The reusable harness every stage runs on — the *why* |
| 01a | [The harness, line by line](01a-harness-line-by-line.md) | Every meaningful line of `tools.py` and `agent.py`, the concept behind it, a worked message trace, and exercises |

## Build status

| Step | Component | Concept it demonstrates | Status |
| --- | --- | --- | --- |
| 1 | Harness (`src/pretzel/harness/`) | The agent loop, tool dispatch | ✅ built |
| 2 | Stage 1 — change analysis | System prompt quality, structured outputs | ⬜ not started |
| 3 | Stage 2 — test selection + run | Sub-agents, context isolation | ⬜ not started |
| 4 | Stage 3+4 — validation deploy, approval | Human-in-the-loop | ⬜ not started |
| 5 | Stage 5 — final deploy + log | Irreversible actions, audit trail | ⬜ not started |
| 6 | SQLite memory | Cross-run memory vs. in-session context | ⬜ not started |
| 7 | `tool_runner` port | What the SDK does for you | ⬜ not started |

## How these pages are written

Every page follows the same shape, so they work as revision material:

**what we built → why it exists architecturally → the code → what to remember.**

The *what to remember* section is the exam-facing part — read those alone for a fast pass.
