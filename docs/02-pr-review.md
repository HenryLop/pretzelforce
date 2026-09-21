# 02: PR review

> **Step 2.** Built: `src/pretzel/review/`, `pretzel review`, two harness additions.
> Verified: `pytest` (59 offline tests) and a manifest cross-check against the `sf` CLI on a
> real 110-file diff from the pilot org's repo. No live model call has been made yet.

## What we built

`pretzel review --base <target> --head <pr-branch>` runs against a local SFDX checkout and
prints the PR comment it *would* post. Step 4 posts it to Bitbucket.

```
git diff base...head ─▶ components ─▶ package.xml + destructiveChanges.xml     (code)
                            │
                            └─▶ references ─▶ prompt ─▶ review agent ─▶ JSON   (model)
                                                                          │
                          facts (code) + judgment (model), validated ◀────┘   (code)
```

| Piece | File | What it does |
| --- | --- | --- |
| Repo access | [project.py](../src/pretzel/review/project.py) | `sfdx-project.json`, and git reads at a ref (never the working tree) |
| Components | [components.py](../src/pretzel/review/components.py) | Changed files → metadata components, with added / modified / deleted |
| Manifests | [manifest.py](../src/pretzel/review/manifest.py) | Deterministic `package.xml` and `destructiveChanges.xml` |
| References | [references.py](../src/pretzel/review/references.py) | Where else each changed component is named, ranked |
| Review agent | [reviewer.py](../src/pretzel/review/reviewer.py) | System prompt, JSON schema, two read-only tools, prompt builder |
| Merge + report | [report.py](../src/pretzel/review/report.py) | Checks the model's answer against code's facts; renders Markdown |
| CLI | [cli.py](../src/pretzel/cli.py) | `pretzel review`, with the cost estimate |
| Harness: cap | [harness/cost.py](../src/pretzel/harness/cost.py) | Tokens → US$, and `max_cost_usd` enforced in the loop |
| Harness: offline client | [harness/scripted.py](../src/pretzel/harness/scripted.py) | Replays canned responses and records every request |

There are three modes, and only the last one costs money:

| Mode | Flag | Model | Use |
| --- | --- | --- | --- |
| offline | (none) | none | Manifests, components, references, and the cost estimate |
| canned | `--canned answer.json` | recorded answer, replayed through the real loop | Tests and demos |
| live | `--live --max-usd 0.50` | `claude-opus-5`, one run, hard cap | Real reviews, only after Henry's yes |

## Why it exists architecturally

### Code computes, AI judges, and the merge enforces the line

The brief's first design rule becomes an actual boundary in the code. Every fact that can
be computed is computed, and computed the same way every time: which files changed, what
metadata type they are, whether a component was added or deleted, what the manifest says,
and which files mention it. The model gets those facts as input and is asked only for what
code can't do: *is this risky, and what would you change?*

`report.merge()` enforces the split on the way back:

- Components come from code. If the model names a component that isn't in the diff, it's
  dropped. If it skips one, that row says **not assessed** rather than a guessed risk.
- A finding that points at a file that doesn't exist is dropped. A line number past the end
  of the file is removed, and the finding is kept.
- Every dropped item is counted in the report footer, so a model that invents things shows
  up instead of disappearing quietly.

This is why the manifest can be trusted even when the model has a bad day. The model never
touches it.

### Structured outputs turn the answer into data

The agent answers in a strict JSON schema (`output_config.format`), so the reply comes back
as fields the merge can check, not prose to parse. Tools and structured outputs coexist: the
model can call `read_file` / `search_repo` for as many turns as it needs, and only its
*final* text is constrained to the schema. `AgentSpec.output_schema` is how any stage asks
for that.

Enums (`block|warn|info`, `low|medium|high`) are the schema doing the calibration work. The
model can't invent a fourth severity that the report doesn't know how to render.

### Front-load the evidence, keep tools for the gaps

The obvious agent design gives the model `git diff` and `grep` tools and lets it explore.
This one does the exploration in code, puts the results in the first message, and keeps two
read-only tools for the cases where that isn't enough. The reasons:

1. **Cost and speed.** One call that already has the evidence is cheaper than five calls that
   discover it. The system prompt says most reviews need no tools.
2. **Determinism where it's free.** The reference search is dumb on purpose. It's a `git grep -w`
   for the API name at head. It's generous (false positives are cheap, because the model sorts
   them out) and ranked (Apex and Flows first, profiles last).
3. **The prompt is testable.** Tests assert that the evidence reached the model: the Flow line
   that uses a deleted field, the numbered SOQL line. That's the half of "does it catch the
   problem" that code owns, and it can be checked offline.

### Three dots, not two

`git diff base...head` compares head with the **merge base**, which is what a PR shows.
`base..head` compares the two tips, so anything merged into `main` after the branch was cut
looks like this PR deleted it. With `destructiveChanges.xml` in the picture, that isn't
cosmetic: it would delete someone else's hotfix from the org. A fixture tests exactly this
(`main` gains `Hotfix.cls` after the branches are cut). Swapping to two dots fails 8 tests.

### The target branch owns the rules

`.pretzel/rules.md` is read from **base**, not head. Otherwise a PR could delete the rule it
breaks and review itself as clean. A fixture PR does exactly that, and the review still applies
the rule.

### Everything reads git objects, never the working tree

`git show ref:path`, `git grep ... ref`, `git cat-file -e ref:path`. The checkout can be on a
third branch, or dirty, and the review is still of exactly `base...head`. That will matter in
Pipelines too, where the clone is shallow and on a detached head.

### A hard US$ cap in the loop

`AgentSpec.max_cost_usd` is checked after every model call, *before* the loop looks at what
the model wants next. Once the running cost passes the cap, `BudgetExceeded` is raised and
nothing more is sent. The cap can only be checked after a call returns, so the real worst
case is `cap + one call`. Live mode refuses to start without a cap, and the CLI prints an
estimate on every run, offline included, so a live run can be priced before anyone says yes.

## The code

### Mapping files to components

It's table-driven, like the `sf` CLI's source registry. The folder says the type:

| Shape | Example path | Component |
| --- | --- | --- |
| simple | `classes/Foo.cls` + `Foo.cls-meta.xml` | `ApexClass:Foo` |
| bundle | `lwc/accountCard/*` | `LightningComponentBundle:accountCard` |
| decomposed child | `objects/Account/fields/Region__c.field-meta.xml` | `CustomField:Account.Region__c` |
| folder content | `reports/Sales/Pipeline.report-meta.xml` | `Report:Sales/Pipeline` |
| unknown | `lwc/jsconfig.json` | **unmapped**: listed in the report, never guessed |

**Added, modified or deleted** is decided per *component*, not per file. Code asks git
whether the component's anchor (the bundle folder, or the `-meta.xml`) exists at the merge
base and at head. Both means modified, head only means added, base only means deleted. That
single rule gets bundles right: deleting one file of an LWC modifies the bundle, and deleting
the folder deletes it. A rename is the old component deleted plus the new one added.

Deleting a `CustomObject` drops its child deletions from `destructiveChanges.xml`, because
listing both fails the deploy.

### Checked against the Salesforce CLI

On a real diff from the pilot org's repo (110 changed files, 93 components), the `package.xml` was compared
member by member with `sf project generate manifest` run on the same files:

- **Identical**, except for one expected difference: given `Campaign.object-meta.xml`, `sf`
  also lists every Campaign field in the source. A delta manifest lists only
  `CustomObject:Campaign`. Both deploy the same thing (see *What to remember*).
- The check caught a real bug. I had been URL-decoding file names
  (`Force%2Ecom` → `Force.com`). `sf` keeps them verbatim, because the file name *is* the
  Metadata API fullName. Decoding would have produced members the org doesn't have.

### The prompt budget

The first message is capped at about 160k characters (~45k tokens). It's filled in priority
order: deletions first, then executable code (Apex, Flows, LWC), then everything else, with
profiles and permission sets last and as diffs only. A component past the budget gets one
line naming it, and the model can `read_file` it if it's worth the tokens. The whole first
message carries a `cache_control` breakpoint, because every loop iteration resends it.

### Tests

```
tests/fixture_repo.py          builds an SFDX git repo with one branch per known problem
tests/fixtures/canned/*.json   the answer a correct review gives for each branch
tests/test_components.py       path → component, one row per source-format shape
tests/test_review_fixtures.py  manifests, evidence-in-prompt, canned answers end to end
tests/test_harness_additions.py  cost math, the cap, schema wiring, a real two-turn tool run
```

| Branch | Known problem | What the test checks |
| --- | --- | --- |
| `feature/delete-field` | deletes a field an active Flow writes | destructive manifest; the Flow line is in the prompt |
| `feature/bulk-trigger` | SOQL in a loop over `Trigger.new` | numbered SOQL line in the prompt |
| `feature/widen-perms` | View All + Modify All on Account | the `true`/`false` diff lines in the prompt |
| `feature/hardcoded-id` | record type Id in Apex | numbered Id line in the prompt |
| `feature/new-class-no-test` | new class, no test; PR also deletes that rule | rule still in the prompt (read from base) |
| `feature/mixed` | bundle edits, rename, deleted object, odd names | full manifest; merge drops invented items |

## What to remember

- **Code computes, AI judges, and the merge enforces it.** The model never writes the manifest,
  never adds a component, and can't point a finding at a file that doesn't exist.
- **Structured outputs + tools:** tools run normally, and only the final text is schema-bound.
  Put the calibration in enums.
- **Front-load evidence** into the first message and keep tools for gaps. It's cheaper, faster,
  and the prompt becomes testable offline.
- **`base...head`, never `base..head`.** Two dots makes a PR "delete" everything merged into
  the target since the branch was cut.
- **Rules from base.** A PR must not be able to edit the rules it's judged by.
- **Member names are file names, verbatim.** Don't URL-decode `%2E` or `%26`.
- **A delta manifest listing `CustomObject:X` deploys all of X's fields** that are in source,
  because `sf project deploy --manifest` resolves the object from source with its children.
  Fine for a sandbox validation, and worth knowing when a PR only meant to touch the object's
  settings.
- **A spending cap is checked after the call.** Worst case is `cap + one call`. Size it that way.
- **The offline test proves the plumbing, not the judgment.** It shows that the evidence reached
  the model and that the answer is handled correctly. Whether `claude-opus-5` actually catches
  the problems needs a live eval over the same fixture branches.

## Known limits

- `.forceignore` isn't read yet. An LWC Jest test change counts as a bundle change.
- The reference search is name-based. A field referenced only through dynamic Apex
  (`record.get(fieldName)`) is invisible to it.
- `CustomLabels` is shipped whole, not per label.

## Verification

```sh
pip install -e ".[dev]"
python -m pytest                                  # 59 offline tests, ~8s
python -m pretzel.demo_harness                    # step 1 checks still pass
pretzel review --repo <sfdx-repo> --base main --head <branch>   # offline, prints the estimate
```

## Next

- **Live eval (needs Henry's yes):** run the six fixture branches live and compare the findings
  with the canned answers. Estimate: about US$0.16 per branch as a one-call review, so under
  US$1.00 for all six, with the cap at US$2.00.
- **Step 3:** test selection with sub-agents, so every changed class clears 75%.
