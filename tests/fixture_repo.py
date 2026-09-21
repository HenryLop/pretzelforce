"""Builds a small SFDX git repo with one branch per known problem.

Built fresh in a temp dir for each test session instead of committed as a nested repo:
the scenarios stay readable as Python, and git doesn't have to store a repo in a repo.

    main                         the base org config
    feature/delete-field         deletes Account.Legacy_Code__c; a Flow still reads it
    feature/bulk-trigger         SOQL inside a loop in AccountTrigger
    feature/widen-perms          Sales_User permission set gains Modify All on Account
    feature/hardcoded-id         AccountService hardcodes a record type Id
    feature/new-class-no-test    DiscountCalc added without a test (breaks rules.md)
    feature/mixed                bundle edits, renames, a deleted object, odd names

After the branches are cut, main moves on (a hotfix class), so a two-dot diff would
wrongly show the hotfix as "deleted" by every branch. The three-dot diff must not.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

D = "force-app/main/default"

META = '<?xml version="1.0" encoding="UTF-8"?>\n'


def _cls_meta(kind: str = "ApexClass") -> str:
    return (
        META
        + f'<{kind} xmlns="http://soap.sforce.com/2006/04/metadata">\n'
        + "    <apiVersion>65.0</apiVersion>\n    <status>Active</status>\n"
        + f"</{kind}>\n"
    )


def _field(name: str, label: str) -> str:
    return (
        META
        + '<CustomField xmlns="http://soap.sforce.com/2006/04/metadata">\n'
        + f"    <fullName>{name}</fullName>\n    <label>{label}</label>\n"
        + "    <length>40</length>\n    <type>Text</type>\n</CustomField>\n"
    )


BASE_FILES: dict[str, str] = {
    "sfdx-project.json": (
        '{\n  "packageDirectories": [{"path": "force-app", "default": true}],\n'
        '  "name": "fixture",\n  "sourceApiVersion": "65.0"\n}\n'
    ),
    "README.md": "Fixture org.\n",
    ".pretzel/rules.md": (
        "# Team rules\n\n"
        "1. Every new Apex class ships with a test class named `<Class>Test`.\n"
        "2. No hardcoded record IDs anywhere. Use Custom Metadata or a query.\n"
    ),
    f"{D}/objects/Account/Account.object-meta.xml": (
        META + '<CustomObject xmlns="http://soap.sforce.com/2006/04/metadata">\n'
        "    <sharingModel>Private</sharingModel>\n</CustomObject>\n"
    ),
    f"{D}/objects/Account/fields/Legacy_Code__c.field-meta.xml": _field(
        "Legacy_Code__c", "Legacy Code"
    ),
    f"{D}/objects/Account/fields/Region__c.field-meta.xml": _field("Region__c", "Region"),
    f"{D}/objects/Account/validationRules/Region_Required.validationRule-meta.xml": (
        META + '<ValidationRule xmlns="http://soap.sforce.com/2006/04/metadata">\n'
        "    <fullName>Region_Required</fullName>\n    <active>true</active>\n"
        "    <errorConditionFormula>ISBLANK(Region__c)</errorConditionFormula>\n"
        "    <errorMessage>Region is required.</errorMessage>\n</ValidationRule>\n"
    ),
    f"{D}/objects/Invoice__c/Invoice__c.object-meta.xml": (
        META + '<CustomObject xmlns="http://soap.sforce.com/2006/04/metadata">\n'
        "    <label>Invoice</label>\n    <sharingModel>ReadWrite</sharingModel>\n"
        "</CustomObject>\n"
    ),
    f"{D}/objects/Invoice__c/fields/Total__c.field-meta.xml": _field("Total__c", "Total"),
    f"{D}/flows/Account_Sync.flow-meta.xml": (
        META + '<Flow xmlns="http://soap.sforce.com/2006/04/metadata">\n'
        "    <apiVersion>65.0</apiVersion>\n    <label>Account Sync</label>\n"
        "    <processType>AutoLaunchedFlow</processType>\n"
        "    <recordUpdates>\n        <name>Copy_Legacy</name>\n"
        "        <inputAssignments>\n"
        "            <field>Legacy_Code__c</field>\n"
        "            <value><elementReference>$Record.AccountNumber</elementReference></value>\n"
        "        </inputAssignments>\n    </recordUpdates>\n"
        "    <status>Active</status>\n</Flow>\n"
    ),
    f"{D}/classes/AccountService.cls": (
        "public with sharing class AccountService {\n"
        "    public static void tagRegion(List<Account> accounts) {\n"
        "        for (Account a : accounts) {\n"
        "            if (a.Region__c == null) {\n"
        "                a.Region__c = 'LATAM';\n"
        "            }\n"
        "        }\n"
        "    }\n"
        "}\n"
    ),
    f"{D}/classes/AccountService.cls-meta.xml": _cls_meta(),
    f"{D}/classes/AccountServiceTest.cls": (
        "@IsTest\nprivate class AccountServiceTest {\n"
        "    @IsTest static void tagsRegion() {\n"
        "        Account a = new Account(Name = 'x');\n"
        "        AccountService.tagRegion(new List<Account>{ a });\n"
        "        System.assertEquals('LATAM', a.Region__c);\n"
        "    }\n}\n"
    ),
    f"{D}/classes/AccountServiceTest.cls-meta.xml": _cls_meta(),
    f"{D}/triggers/AccountTrigger.trigger": (
        "trigger AccountTrigger on Account (before insert, before update) {\n"
        "    AccountService.tagRegion(Trigger.new);\n"
        "}\n"
    ),
    f"{D}/triggers/AccountTrigger.trigger-meta.xml": _cls_meta("ApexTrigger"),
    f"{D}/permissionsets/Sales_User.permissionset-meta.xml": (
        META + '<PermissionSet xmlns="http://soap.sforce.com/2006/04/metadata">\n'
        "    <label>Sales User</label>\n"
        "    <objectPermissions>\n"
        "        <allowCreate>true</allowCreate>\n"
        "        <allowDelete>false</allowDelete>\n"
        "        <allowEdit>true</allowEdit>\n"
        "        <allowRead>true</allowRead>\n"
        "        <modifyAllRecords>false</modifyAllRecords>\n"
        "        <object>Account</object>\n"
        "        <viewAllRecords>false</viewAllRecords>\n"
        "    </objectPermissions>\n"
        "</PermissionSet>\n"
    ),
    f"{D}/lwc/accountCard/accountCard.js": (
        "import { LightningElement, api } from 'lwc';\n"
        "export default class AccountCard extends LightningElement {\n"
        "    @api recordId;\n}\n"
    ),
    f"{D}/lwc/accountCard/accountCard.html": "<template><p>{recordId}</p></template>\n",
    f"{D}/lwc/accountCard/accountCard.js-meta.xml": (
        META + '<LightningComponentBundle xmlns="http://soap.sforce.com/2006/04/metadata">\n'
        "    <apiVersion>65.0</apiVersion>\n    <isExposed>true</isExposed>\n"
        "</LightningComponentBundle>\n"
    ),
    f"{D}/lwc/oldBanner/oldBanner.js": (
        "import { LightningElement } from 'lwc';\n"
        "export default class OldBanner extends LightningElement {}\n"
    ),
    f"{D}/lwc/oldBanner/oldBanner.html": "<template>Old</template>\n",
    f"{D}/lwc/oldBanner/oldBanner.js-meta.xml": (
        META + '<LightningComponentBundle xmlns="http://soap.sforce.com/2006/04/metadata">\n'
        "    <apiVersion>65.0</apiVersion>\n    <isExposed>false</isExposed>\n"
        "</LightningComponentBundle>\n"
    ),
    f"{D}/layouts/Account-Account Layout.layout-meta.xml": (
        META + '<Layout xmlns="http://soap.sforce.com/2006/04/metadata">\n'
        "    <layoutSections><label>Info</label></layoutSections>\n</Layout>\n"
    ),
    f"{D}/profiles/Force%2Ecom - App Subscription User.profile-meta.xml": (
        META + '<Profile xmlns="http://soap.sforce.com/2006/04/metadata">\n'
        "    <custom>false</custom>\n</Profile>\n"
    ),
}


def _edit(files: dict[str, str | None], path: str, old: str, new: str) -> None:
    text = files[path]
    assert text is not None and old in text, f"fixture edit anchor missing in {path}"
    files[path] = text.replace(old, new)


def scenario_changes() -> dict[str, dict[str, str | None]]:
    """Branch name -> {path: new content, or None to delete}."""
    s: dict[str, dict[str, str | None]] = {}

    s["feature/delete-field"] = {
        f"{D}/objects/Account/fields/Legacy_Code__c.field-meta.xml": None,
    }

    trig = dict(BASE_FILES)
    _edit(
        trig,
        f"{D}/triggers/AccountTrigger.trigger",
        "    AccountService.tagRegion(Trigger.new);\n",
        "    for (Account a : Trigger.new) {\n"
        "        List<Contact> cs = [SELECT Id FROM Contact WHERE AccountId = :a.Id];\n"
        "        a.Description = String.valueOf(cs.size());\n"
        "    }\n",
    )
    s["feature/bulk-trigger"] = {
        f"{D}/triggers/AccountTrigger.trigger": trig[f"{D}/triggers/AccountTrigger.trigger"]
    }

    perm = dict(BASE_FILES)
    p = f"{D}/permissionsets/Sales_User.permissionset-meta.xml"
    _edit(perm, p, "<modifyAllRecords>false</modifyAllRecords>", "<modifyAllRecords>true</modifyAllRecords>")
    _edit(perm, p, "<viewAllRecords>false</viewAllRecords>", "<viewAllRecords>true</viewAllRecords>")
    s["feature/widen-perms"] = {p: perm[p]}

    svc = dict(BASE_FILES)
    c = f"{D}/classes/AccountService.cls"
    _edit(
        svc,
        c,
        "                a.Region__c = 'LATAM';\n",
        "                a.Region__c = 'LATAM';\n"
        "                a.RecordTypeId = '0125e000000AbCdAAK';\n",
    )
    s["feature/hardcoded-id"] = {c: svc[c]}

    s["feature/new-class-no-test"] = {
        f"{D}/classes/DiscountCalc.cls": (
            "public with sharing class DiscountCalc {\n"
            "    public static Decimal apply(Decimal amount, Decimal pct) {\n"
            "        return amount * (1 - pct / 100);\n"
            "    }\n}\n"
        ),
        f"{D}/classes/DiscountCalc.cls-meta.xml": _cls_meta(),
        # The PR also tries to delete the rule it breaks. Rules are read from the
        # target branch, so this must not weaken the review.
        ".pretzel/rules.md": "# Team rules\n\n2. No hardcoded record IDs anywhere.\n",
    }

    flow_v1 = BASE_FILES[f"{D}/flows/Account_Sync.flow-meta.xml"]
    s["feature/mixed"] = {
        # one file of a bundle removed -> the bundle is modified, not deleted
        f"{D}/lwc/accountCard/accountCard.html": None,
        f"{D}/lwc/accountCard/accountCard.js": BASE_FILES[f"{D}/lwc/accountCard/accountCard.js"]
        + "// renders without a template now\n",
        # whole bundle removed -> deleted
        f"{D}/lwc/oldBanner/oldBanner.js": None,
        f"{D}/lwc/oldBanner/oldBanner.html": None,
        f"{D}/lwc/oldBanner/oldBanner.js-meta.xml": None,
        # rename -> old deleted, new added
        f"{D}/flows/Account_Sync.flow-meta.xml": None,
        f"{D}/flows/Account_Sync_V2.flow-meta.xml": flow_v1,
        # a whole object removed -> only CustomObject in destructiveChanges
        f"{D}/objects/Invoice__c/Invoice__c.object-meta.xml": None,
        f"{D}/objects/Invoice__c/fields/Total__c.field-meta.xml": None,
        # decomposed child added
        f"{D}/objects/Account/recordTypes/Partner.recordType-meta.xml": (
            META + '<RecordType xmlns="http://soap.sforce.com/2006/04/metadata">\n'
            "    <fullName>Partner</fullName>\n    <active>true</active>\n"
            "    <label>Partner &amp; Reseller</label>\n</RecordType>\n"
        ),
        # names with spaces and URL-encoded characters
        f"{D}/layouts/Account-Account Layout.layout-meta.xml": BASE_FILES[
            f"{D}/layouts/Account-Account Layout.layout-meta.xml"
        ].replace("Info", "Information"),
        f"{D}/profiles/Force%2Ecom - App Subscription User.profile-meta.xml": BASE_FILES[
            f"{D}/profiles/Force%2Ecom - App Subscription User.profile-meta.xml"
        ].replace("<custom>false</custom>", "<custom>false</custom>\n    <userLicense>Salesforce</userLicense>"),
        # not metadata: tooling file in lwc/, and a file outside the package dir
        f"{D}/lwc/jsconfig.json": '{"compilerOptions": {}}\n',
        "README.md": "Fixture org, now with a README change.\n",
    }
    return s


HOTFIX = {
    f"{D}/classes/Hotfix.cls": "public class Hotfix {}\n",
    f"{D}/classes/Hotfix.cls-meta.xml": _cls_meta(),
}


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "core.quotepath=off", *args],
        cwd=root,
        check=True,
        capture_output=True,
    )


def _write(root: Path, files: dict[str, str | None]) -> None:
    for rel, text in files.items():
        path = root / rel
        if text is None:
            path.unlink()
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))  # bytes: no CRLF translation on Windows


def build(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "fixture@example.com")
    _git(root, "config", "user.name", "Fixture")
    _git(root, "config", "core.autocrlf", "false")
    _write(root, BASE_FILES)
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base org")

    for branch, changes in scenario_changes().items():
        _git(root, "checkout", "-q", "-b", branch, "main")
        _write(root, changes)
        _git(root, "add", "-A")
        _git(root, "commit", "-q", "-m", branch)
        _git(root, "checkout", "-q", "main")

    _write(root, HOTFIX)
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "hotfix on main after the branches were cut")
    return root
