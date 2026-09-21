# PretzelForce

> A Salesforce code-to-production agent pipeline, built to learn agent architecture.

A git diff of Salesforce metadata goes in. A deploy comes out. Five stages, each one its own
agent with its own loop, tools, and system prompt.

```
git diff ──▶ 1. change analysis ──▶ 2. test selection + run ──▶ 3. validation deploy
                                                                       │
                                          5. final deploy + log ◀── 4. human approval
```

This is a learning project: the explanations are the deliverable. Start at
**[docs/](docs/README.md)**.

## Status

Step 2 of 7: `pretzel review` (PR change analysis, delta manifests, AI findings) is built and
tested offline. Step 1, the reusable agent harness, is in PR #1.
See the [build status table](docs/README.md#build-status).

## Getting started

```sh
git clone https://github.com/HenryLop/pretzelforce.git
cd pretzelforce
pip install -e .
cp .env.example .env          # then add your ANTHROPIC_API_KEY
python -m pretzel.demo_harness # offline checks, no API key needed
pip install -e ".[dev]" && python -m pytest   # offline test suite
pretzel review --repo ../my-sfdx-repo --base main --head feature/x   # offline review
```

Add `--live` to exercise the loop against the real API (costs a few cents).

## Layout

```
.
├── CLAUDE.md                 # notes for Claude Code sessions
├── docs/                     # the wiki — one page per build step
├── pyproject.toml
└── src/pretzel/
    ├── harness/              # the reusable agent loop every stage runs on
    ├── review/               # step 2: pretzel review
    ├── cli.py                # the `pretzel` command
    └── demo_harness.py       # smoke test
tests/                        # offline tests; builds a fixture SFDX repo per run
```

## Requirements

Python 3.12+, an Anthropic API key, and the `sf` CLI authed to a sandbox for stages 2–5.

## License

TBD
