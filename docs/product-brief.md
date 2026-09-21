# PretzelForce: product brief

Written 2026-09-21 as the handoff from the design session into the build. It is the
source of truth for *why, what and how* until the architecture page is rewritten.
Diagram page: https://claude.ai/artifact/9p1Bh79EkBKnFKDwfhbLYc (the stage table there still
says "five Claude agents"; the split below supersedes it).

## Why

PretzelForce started as Churro Force, a learning project for agent architecture. It's now a
real product. It puts Henry's detailed knowledge of Claude, Salesforce and Bitbucket into a
helper for every stage of a normal Salesforce deploy process, and it gets piloted at Xertica.

- **Study:** it covers the CCAR-F syllabus hands-on (agent loop, sub-agents, structured
  outputs, human-in-the-loop).
- **Side project:** it's the proof behind "Salesforce architect who ships agent systems".
- **Creative path:** Henry builds it because he wants to.

**Ownership:** the code is Henry's. He is not billing Xertica for it. The generic core lives
in this repo (github.com/HenryLop/pretzelforce, public). Xertica-specific config lives in
Xertica's repo, and Xertica installs a pinned tag.

## What

It's a release helper that runs inside Bitbucket. There's no new UI. Developers see
everything on their PR: a comment, a Code Insights report with line annotations, and a build
status.

**Scope v1: Bitbucket only.** Jira and production come later.

| # | Bitbucket event | Helper does | AI? |
| --- | --- | --- | --- |
| 1 | PR opened / updated | Reviews the change: risk, dependencies, team rules | Yes |
| 2 | Push to feature branch | Fast check: stray metadata, hardcoded IDs, profile noise | Same analyzer, light mode |
| 3 | PR ready | Picks tests, `sf project deploy validate`, explains failures | Tests + explanations yes; the validate itself is code |
| 4 | Merge to UAT branch | `sf project deploy quick --job-id`, then lists manual post-deploy steps | Only the steps list |
| 5 | Merge to main | Production | Later |

**Design rules:**

- **Code computes, AI judges.** Manifests (`package.xml`, `destructiveChanges.xml`), diff
  parsing and every `sf` command are deterministic code. The model is used only where
  judgment helps: risk, dependencies, test choice, and explaining failures.
- **Approval is the merge.** Bitbucket merge checks (green build, N approvals) replace the
  old "human approval" stage.
- **The job ID is the handoff.** The PR pipeline validates and gets a job ID; the merge
  pipeline quick-deploys it. If the ID expired (10 days) or the target branch moved since
  validation, it re-validates first.
- **Tests:** `RunSpecifiedTests` requires 75% or more coverage on every class and trigger
  in the deployment. Test selection must guarantee that.
- **Memory:** Pipelines containers are wiped after each run, so there's no SQLite there.
  Team rules live in `.pretzel/rules.md` in the target repo, reviewed by PR. The audit log
  is a pipeline artifact plus the PR comment.
- **v1 deploys to a sandbox only.**

## How: build order

| Step | Builds | Done when | Status |
| --- | --- | --- | --- |
| 1 | Harness (`src/pretzel/harness/`) | Offline checks pass | Built (PR #1) |
| 2 | **PR review**: change analysis + delta manifest | A real diff gives a correct manifest and useful findings | **Next** |
| 3 | Test selection with sub-agents | Chosen tests clear 75% on every changed class | |
| 4 | Validation + Bitbucket report (comment, Code Insights, status) | A PR shows all three | |
| 5 | Pipelines wiring + quick deploy on merge | A merge ships the validated package to the sandbox | |
| 6 | Push check (light mode of step 2) | Feedback on push in under a minute | |
| 7 | Pilot on one Xertica repo | Two weeks of real PRs against a sandbox | |

The `tool_runner` port is now optional. The "no CI/CD" line in `00-architecture.md` is
obsolete, because CI/CD is the product.

## Step 2 spec: PR review

**Command:** `pretzel review --base <target-branch> --head <source-branch>`, run locally
against an SFDX repo checkout. It prints the PR comment it *would* post. Posting to
Bitbucket comes in step 4.

**Input:**
- `git diff base...head`, limited to the package dirs in `sfdx-project.json`;
- the full text of each changed file;
- `sfdx-project.json` (API version, package dirs);
- `.pretzel/rules.md`, if present.

**Code does (no AI):**
1. Parses the diff into components: metadata type, API name, and added / modified /
   deleted. It handles bundles (LWC, Aura) and decomposed objects (fields, record types,
   validation rules as separate files).
2. Builds `package.xml` + `destructiveChanges.xml`.
3. Searches the repo for references to every changed or deleted component (Apex, Flows,
   layouts, LWC, permission sets, profiles) and hands them to the agent.

**AI does:** reads each change together with its references and judges the risk. It finds
real problems and gives a fix for each. Examples:
- a deleted or renamed field still referenced in a Flow;
- non-bulkified trigger logic;
- widened permissions;
- hardcoded record IDs;
- a broken rule from `rules.md`.

**Output (strict JSON schema, via structured outputs):**
- `summary`;
- `components[]`: path, type, name, change, risk (low / medium / high), reason;
- `findings[]`: severity (block / warn / info), file, line, message, fix;
- the two manifests (from code, not the model).

**Tests:** offline tests use canned model responses. Keep a set of fixture branches with
known problems, and check the review catches them.

## Constraints (hard)

- **Claude API calls cost money.** Every live run needs Henry's explicit yes first: key,
  model, number of calls, estimated US$ and a hard cap in code. None of Henry's personal
  keys is cleared for PretzelForce. At Xertica, it's Xertica's key (or Bedrock / Vertex),
  and that's still to be confirmed. The offline path needs no key.
- **Repo conventions** (`CLAUDE.md`): `main` needs a PR. Branch, push, open a PR. Every step
  ends with its `docs/` page: what was built, why it exists architecturally, the code, and
  what to remember.
- **Model:** `claude-opus-5` via `anthropic` >= 1.4.0, as the harness already does.

## Open

- Which Xertica Salesforce repo in Bitbucket is the pilot, and which sandbox it validates
  against.
- Model access at Xertica: direct Anthropic key, Bedrock or Vertex AI.
- Merging PR #1 (harness + rename) is Henry's call.
