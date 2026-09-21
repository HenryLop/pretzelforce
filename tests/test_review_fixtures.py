"""Each fixture branch carries one known problem. For each, check three things offline:

1. the manifest code builds is exactly right;
2. the evidence a reviewer needs to catch the problem is in the prompt the model gets
   (that's the part code owns; whether the model *uses* it is what a live eval checks);
3. a canned model answer flows through the real loop, schema, merge and report.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from pretzel.harness import ScriptedClient, text_turn
from pretzel.review import analyze, render_markdown, run_review
from pretzel.review.reviewer import REVIEW_SCHEMA

D = "force-app/main/default"


def members(xml: str | None) -> set[tuple[str, str]]:
    if xml is None:
        return set()
    out = set()
    for block in re.findall(r"<types>(.*?)</types>", xml, re.S):
        name = re.search(r"<name>(.*?)</name>", block).group(1)
        out |= {(name, m) for m in re.findall(r"<members>(.*?)</members>", block)}
    return out


def review(repo: Path, branch: str, canned_answer: dict):
    analysis = analyze(str(repo), "main", branch)
    client = ScriptedClient([text_turn(canned_answer)])
    result = run_review(analysis, mode="canned", canned=canned_answer, client=client)
    return analysis, result, client


# -- 1. manifests --------------------------------------------------------------


def test_delete_field_manifest(fixture_repo: Path) -> None:
    a = analyze(str(fixture_repo), "main", "feature/delete-field")
    assert members(a.manifests.package_xml) == set()
    assert "<version>65.0</version>" in a.manifests.package_xml
    assert members(a.manifests.destructive_xml) == {("CustomField", "Account.Legacy_Code__c")}


def test_three_dot_diff_ignores_what_main_did_after_the_branch(fixture_repo: Path) -> None:
    # main gained Hotfix.cls after every branch was cut. A two-dot diff would list it
    # as deleted by this PR, and destructiveChanges would delete it from the org.
    a = analyze(str(fixture_repo), "main", "feature/bulk-trigger")
    assert members(a.manifests.package_xml) == {("ApexTrigger", "AccountTrigger")}
    assert a.manifests.destructive_xml is None


def test_mixed_manifest(fixture_repo: Path) -> None:
    a = analyze(str(fixture_repo), "main", "feature/mixed")
    assert members(a.manifests.package_xml) == {
        ("LightningComponentBundle", "accountCard"),  # one file deleted: still modified
        ("Flow", "Account_Sync_V2"),
        ("RecordType", "Account.Partner"),
        ("Layout", "Account-Account Layout"),
        ("Profile", "Force%2Ecom - App Subscription User"),
    }
    assert members(a.manifests.destructive_xml) == {
        ("LightningComponentBundle", "oldBanner"),  # whole bundle gone
        ("Flow", "Account_Sync"),  # renamed away
        ("CustomObject", "Invoice__c"),  # its field is implied, not listed
    }
    unmapped = [c for c in a.changes if c.change == "unmapped"]
    assert [c.files for c in unmapped] == [[f"{D}/lwc/jsconfig.json"]]
    assert not any("README.md" in f for c in a.changes for f in c.files)


def test_manifest_escapes_xml(fixture_repo: Path) -> None:
    a = analyze(str(fixture_repo), "main", "feature/mixed")
    assert "&" not in a.manifests.package_xml.replace("&amp;", "")


# -- 2. evidence reaches the model ----------------------------------------------


def test_deleted_field_prompt_shows_the_flow_that_uses_it(fixture_repo: Path) -> None:
    a = analyze(str(fixture_repo), "main", "feature/delete-field")
    [refs] = [r for r in a.references if r.component.name == "Account.Legacy_Code__c"]
    flow = [r for r in refs.references if r.from_component == "Flow:Account_Sync"]
    assert flow and flow[0].line == 9 and not flow[0].in_change_set
    assert f"{D}/flows/Account_Sync.flow-meta.xml:9 (Flow:Account_Sync)" in a.review_input.prompt


def test_trigger_prompt_has_numbered_soql_line(fixture_repo: Path) -> None:
    a = analyze(str(fixture_repo), "main", "feature/bulk-trigger")
    assert "3|         List<Contact> cs = [SELECT Id FROM Contact" in a.review_input.prompt


def test_permission_widening_is_in_the_diff(fixture_repo: Path) -> None:
    a = analyze(str(fixture_repo), "main", "feature/widen-perms")
    p = a.review_input.prompt
    assert "+        <modifyAllRecords>true</modifyAllRecords>" in p
    assert "-        <modifyAllRecords>false</modifyAllRecords>" in p
    # Permission sets go in as a diff only; the full file is not inlined.
    assert f"{D}/permissionsets/Sales_User.permissionset-meta.xml" in a.review_input.skipped_files


def test_hardcoded_id_line_is_numbered(fixture_repo: Path) -> None:
    a = analyze(str(fixture_repo), "main", "feature/hardcoded-id")
    assert "6|                 a.RecordTypeId = '0125e000000AbCdAAK';" in a.review_input.prompt


def test_rules_come_from_the_target_branch(fixture_repo: Path) -> None:
    # The PR deletes rule 1 from rules.md. The review must still apply it.
    a = analyze(str(fixture_repo), "main", "feature/new-class-no-test")
    assert a.review_input.rules is not None
    assert "Every new Apex class ships with a test class" in a.review_input.prompt
    assert "<team_rules>" in a.review_input.prompt


def test_repo_content_is_fenced_as_data(fixture_repo: Path) -> None:
    a = analyze(str(fixture_repo), "main", "feature/hardcoded-id")
    p = a.review_input.prompt
    assert p.index("<repo_content>") < p.index("AccountService.cls") < p.index("</repo_content>")


# -- 3. canned answers through the real loop ------------------------------------


@pytest.mark.parametrize(
    ("branch", "name", "severity", "file", "line"),
    [
        ("feature/delete-field", "delete-field", "block", f"{D}/flows/Account_Sync.flow-meta.xml", 9),
        ("feature/bulk-trigger", "bulk-trigger", "block", f"{D}/triggers/AccountTrigger.trigger", 3),
        ("feature/widen-perms", "widen-perms", "warn", f"{D}/permissionsets/Sales_User.permissionset-meta.xml", 9),
        ("feature/hardcoded-id", "hardcoded-id", "block", f"{D}/classes/AccountService.cls", 6),
        ("feature/new-class-no-test", "new-class-no-test", "block", f"{D}/classes/DiscountCalc.cls", 1),
    ],
)
def test_canned_review_surfaces_the_finding(
    fixture_repo: Path, canned, branch: str, name: str, severity: str, file: str, line: int
) -> None:
    _, result, client = review(fixture_repo, branch, canned(name))
    assert [(f.severity, f.file, f.line) for f in result.findings] == [(severity, file, line)]
    assert result.dropped == []
    assert all(c.risk != "not assessed" for c in result.components)
    assert result.blocking == (severity == "block")
    md = render_markdown(result)
    assert f"`{file}` line {line}" in md and "**Fix:**" in md
    assert client.remaining == 0


def test_request_carries_schema_cache_and_fallbacks(fixture_repo: Path, canned) -> None:
    _, _, client = review(fixture_repo, "feature/delete-field", canned("delete-field"))
    [req] = client.requests
    assert req["model"] == "claude-opus-5"
    assert req["output_config"]["format"] == {"type": "json_schema", "schema": REVIEW_SCHEMA}
    assert req["thinking"] == {"type": "adaptive"}
    first = req["messages"][0]["content"][0]
    assert first["cache_control"] == {"type": "ephemeral"}
    assert req["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert {t["name"] for t in req["tools"]} == {"read_file", "search_repo"}
    assert req["fallbacks"] == "default"


def test_merge_drops_what_the_model_made_up(fixture_repo: Path, canned) -> None:
    _, result, _ = review(fixture_repo, "feature/mixed", canned("mixed"))
    names = {c.name for c in result.components}
    assert "Imaginary" not in names
    assert not any(f.file.endswith("DoesNotExist.cls") for f in result.findings)
    past_end = [f for f in result.findings if "past the end" in f.message]
    assert past_end and past_end[0].line is None
    assert len(result.dropped) == 3
    # Components the model skipped are shown as not assessed, never guessed.
    risks = {c.name: c.risk for c in result.components}
    assert risks["accountCard"] == "not assessed"
    assert risks["Invoice__c"] == "high"


def test_offline_mode_makes_no_model_call(fixture_repo: Path) -> None:
    a = analyze(str(fixture_repo), "main", "feature/delete-field")
    result = run_review(a, mode="offline", client=ScriptedClient([]))
    assert result.mode == "offline" and result.findings == []
    assert "Manifest only" in render_markdown(result)


def test_live_mode_refuses_without_a_cap(fixture_repo: Path) -> None:
    a = analyze(str(fixture_repo), "main", "feature/delete-field")
    with pytest.raises(ValueError, match="max_cost_usd"):
        run_review(a, mode="live", client=ScriptedClient([]))
