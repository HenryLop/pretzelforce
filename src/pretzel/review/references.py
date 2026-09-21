"""Who else mentions a changed component? A plain-text search, done by code.

This is the evidence the model needs to judge dependency risk. "Is it safe to delete
`Account.Legacy_Code__c`?" can't be answered from the diff alone. It needs "a Flow at
line 42 still reads it". The search is deliberately dumb (git grep for the API name at
head) and deliberately generous. False positives are cheap because the model sorts
them out. A missed reference is the expensive failure, so code errs on the side of
handing over too much and ranks it so the likeliest breakages come first.

Searches run at *head*: for a deletion, the question is what still points at the
component after this PR merges.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .components import UNMAPPED, ComponentChange, locate
from .project import SfdxRepo

MAX_REFS_PER_COMPONENT = 30

# Nothing references these by API name in a way a text search can use.
_NOT_SEARCHED = {"CustomLabels", "Settings", "ValidationRule", "Translations"}

# Nothing that references these can break when they are merely *modified* (a profile
# gaining a permission doesn't affect whoever names it), and their names are common
# words ("Sales", "Admin"), so searching them only adds noise. Deletions still count.
_DELETE_ONLY = {
    "Profile",
    "PermissionSet",
    "PermissionSetGroup",
    "Layout",
    "FlexiPage",
    "CustomApplication",
    "CustomTab",
    "ListView",
    "CompactLayout",
}

# Lower rank = shown first. Code that executes breaks at runtime; config breaks at deploy.
_RANK = {
    "ApexClass": 0,
    "ApexTrigger": 0,
    "Flow": 0,
    "LightningComponentBundle": 1,
    "AuraDefinitionBundle": 1,
    "ApexPage": 1,
    "ApexComponent": 1,
    "CustomField": 2,  # formulas, lookups
    "ValidationRule": 2,
    "FlexiPage": 3,
    "Layout": 3,
    "QuickAction": 3,
    "FlowDefinition": 3,
    "PermissionSet": 4,
    "PermissionSetGroup": 4,
    "Profile": 5,
}


@dataclass(frozen=True)
class Reference:
    path: str
    line: int
    text: str
    from_component: str  # "Flow:Lead_Router", the component the referencing file is in
    in_change_set: bool  # True if that component is also changed by this PR


@dataclass
class ComponentReferences:
    component: ComponentChange
    tokens: list[str]
    references: list[Reference]
    total_hits: int  # before the cap; > len(references) means some were dropped

    @property
    def truncated(self) -> bool:
        return self.total_hits > len(self.references)


def search_tokens(change: ComponentChange) -> list[str]:
    """The strings that would appear in a file that depends on this component."""
    if change.type in _NOT_SEARCHED or change.type == UNMAPPED:
        return []
    if change.type in _DELETE_ONLY and change.change != "deleted":
        return []
    name = change.name
    if change.type == "LightningComponentBundle":
        # <c-my-table> in markup, c:myTable in FlexiPages/Aura, myTable in JS imports.
        kebab = re.sub(r"([A-Z])", lambda m: "-" + m.group(1).lower(), name)
        return [name, f"c-{kebab}"]
    if change.type == "CustomMetadata":
        return [name.split(".", 1)[1]] if "." in name else [name]
    if change.type in ("Report", "Dashboard", "EmailTemplate", "Document"):
        return [name.rsplit("/", 1)[-1]]
    if "." in name:  # object children: CustomField Account.Region__c -> Region__c
        return [name.split(".", 1)[1]]
    return [name]


def find_references(
    repo: SfdxRepo, head: str, changes: list[ComponentChange]
) -> list[ComponentReferences]:
    changed_keys = {str(c.key) for c in changes}
    results: list[ComponentReferences] = []
    for change in changes:
        tokens = search_tokens(change)
        own_files = set(change.files)
        hits: dict[tuple[str, int], Reference] = {}
        for token in tokens:
            # Layout names contain spaces and dashes; -w would never match them.
            word = change.type != "Layout"
            for path, line, text in repo.grep(head, token, word=word):
                if path in own_files or (path, line) in hits:
                    continue
                loc = locate(path, repo.package_dirs)
                owner = str(loc.key) if loc else path
                if owner == str(change.key):
                    continue  # another file of the same bundle
                hits[(path, line)] = Reference(
                    path=path,
                    line=line,
                    text=text.strip()[:240],
                    from_component=owner,
                    in_change_set=owner in changed_keys,
                )
        ranked = sorted(hits.values(), key=lambda r: _sort_key(r, change))
        results.append(
            ComponentReferences(
                component=change,
                tokens=tokens,
                references=ranked[:MAX_REFS_PER_COMPONENT],
                total_hits=len(ranked),
            )
        )
    return results


def _sort_key(ref: Reference, change: ComponentChange) -> tuple[int, int, str, int]:
    ref_type = ref.from_component.split(":", 1)[0]
    # For an object child, a hit that also names the object is much more likely to be
    # a real dependency than one that merely shares the field name.
    same_object = 1
    if "." in change.name and change.type not in ("CustomMetadata",):
        obj = change.name.split(".", 1)[0]
        ref_obj = ref.from_component.split(":", 1)[-1].split(".", 1)[0]
        if obj in ref.text or f"/objects/{obj}/" in ref.path or ref_obj == obj:
            same_object = 0
    return (same_object, _RANK.get(ref_type, 6), ref.path, ref.line)
