"""`package.xml` and `destructiveChanges.xml` from component changes.

Pure code, byte-for-byte deterministic: types sorted, members sorted, no timestamps.
That matters for two reasons. The same diff must always give the same manifest, or a
reviewer can't trust it. And step 5 compares the manifest validated on the PR with the
one about to be quick-deployed on merge, so it has to be stable to be comparable.

Added and modified components go in `package.xml`. Deleted ones go in
`destructiveChanges.xml`, which Salesforce only accepts next to a `package.xml`, so
a deletions-only PR still gets a `package.xml` with no types.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from xml.sax.saxutils import escape

from .components import UNMAPPED, ComponentChange

NS = "http://soap.sforce.com/2006/04/metadata"


@dataclass(frozen=True)
class Manifests:
    package_xml: str
    destructive_xml: str | None  # None when nothing is deleted

    @property
    def has_deletions(self) -> bool:
        return self.destructive_xml is not None


def build_manifests(changes: list[ComponentChange], api_version: str) -> Manifests:
    additive = [c for c in changes if c.change in ("added", "modified")]
    deleted = [c for c in changes if c.change == "deleted"]
    return Manifests(
        package_xml=render_package(additive, api_version),
        destructive_xml=render_package(deleted, api_version) if deleted else None,
    )


def render_package(changes: list[ComponentChange], api_version: str) -> str:
    by_type: dict[str, set[str]] = defaultdict(set)
    for c in changes:
        if c.type != UNMAPPED:
            by_type[c.type].add(c.name)

    lines = ['<?xml version="1.0" encoding="UTF-8"?>', f'<Package xmlns="{NS}">']
    for mtype in sorted(by_type):
        lines.append("    <types>")
        for member in sorted(by_type[mtype]):
            lines.append(f"        <members>{escape(member)}</members>")
        lines.append(f"        <name>{mtype}</name>")
        lines.append("    </types>")
    lines.append(f"    <version>{api_version}</version>")
    lines.append("</Package>")
    return "\n".join(lines) + "\n"
