"""From changed files to changed *components*.

A deploy doesn't ship files. It ships metadata components: `CustomField
Account.Region__c`, `LightningComponentBundle myTable`. One component can be several
files (an Apex class is `.cls` + `.cls-meta.xml`; an LWC is a whole folder), and one
object can be many components (each field, record type and validation rule is its
own file and its own component). This module does that mapping, with no AI.

The mapping is table-driven: the folder a file sits in says its metadata type, the way
the Salesforce CLI's source registry does. File names are used as member names
*verbatim*: `Force%2Ecom - App Subscription User.profile-meta.xml` is the profile whose
Metadata API fullName is `Force%2Ecom - App Subscription User`. Decoding it would
produce a member the org doesn't know (checked against `sf project generate manifest`). A path the table doesn't know is reported
as `unmapped` instead of guessed, so a gap shows up in the review rather than as a
silently incomplete manifest.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import PurePosixPath

from .project import FileChange, SfdxRepo

# folder name -> (metadata type, file suffix). A file is `<name>.<suffix>-meta.xml`,
# optionally next to a content file `<name>.<suffix>` (classes, triggers, pages...).
SIMPLE_TYPES: dict[str, tuple[str, str]] = {
    "applications": ("CustomApplication", "app"),
    "approvalProcesses": ("ApprovalProcess", "approvalProcess"),
    "assignmentRules": ("AssignmentRules", "assignmentRules"),
    "autoResponseRules": ("AutoResponseRules", "autoResponseRules"),
    "cachePartitions": ("PlatformCachePartition", "cachePartition"),
    "classes": ("ApexClass", "cls"),
    "components": ("ApexComponent", "component"),
    "connectedApps": ("ConnectedApp", "connectedApp"),
    "contentassets": ("ContentAsset", "asset"),
    "cspTrustedSites": ("CspTrustedSite", "cspTrustedSite"),
    "customMetadata": ("CustomMetadata", "md"),
    "customPermissions": ("CustomPermission", "customPermission"),
    "duplicateRules": ("DuplicateRule", "duplicateRule"),
    "externalClientApps": ("ExternalClientApplication", "eca"),
    "externalCredentials": ("ExternalCredential", "externalCredential"),
    "extlClntAppOauthPolicies": ("ExtlClntAppOauthConfigurablePolicies", "ecaOauthPlcy"),
    "extlClntAppOauthSettings": ("ExtlClntAppOauthSettings", "ecaOauth"),
    "extlClntAppPolicies": ("ExtlClntAppConfigurablePolicies", "ecaPlcy"),
    "flexipages": ("FlexiPage", "flexipage"),
    "flowDefinitions": ("FlowDefinition", "flowDefinition"),
    "flows": ("Flow", "flow"),
    "globalValueSets": ("GlobalValueSet", "globalValueSet"),
    "groups": ("Group", "group"),
    "labels": ("CustomLabels", "labels"),
    "layouts": ("Layout", "layout"),
    "matchingRules": ("MatchingRules", "matchingRule"),
    "messageChannels": ("LightningMessageChannel", "messageChannel"),
    "namedCredentials": ("NamedCredential", "namedCredential"),
    "notificationtypes": ("CustomNotificationType", "notiftype"),
    "pages": ("ApexPage", "page"),
    "pathAssistants": ("PathAssistant", "pathAssistant"),
    "permissionsetgroups": ("PermissionSetGroup", "permissionsetgroup"),
    "permissionsets": ("PermissionSet", "permissionset"),
    "profiles": ("Profile", "profile"),
    "queues": ("Queue", "queue"),
    "quickActions": ("QuickAction", "quickAction"),
    "remoteSiteSettings": ("RemoteSiteSetting", "remoteSite"),
    "reportTypes": ("ReportType", "reportType"),
    "roles": ("Role", "role"),
    "samlssoconfigs": ("SamlSsoConfig", "samlssoconfig"),
    "settings": ("Settings", "settings"),
    "sharingRules": ("SharingRules", "sharingRules"),
    "standardValueSets": ("StandardValueSet", "standardValueSet"),
    "staticresources": ("StaticResource", "resource"),
    "tabs": ("CustomTab", "tab"),
    "translations": ("Translations", "translation"),
    "triggers": ("ApexTrigger", "trigger"),
    "useraccesspolicies": ("UserAccessPolicy", "useraccesspolicy"),
    "workflows": ("Workflow", "workflow"),
}

# A whole folder is one component: lwc/<name>/..., aura/<name>/...
BUNDLE_TYPES: dict[str, str] = {
    "lwc": "LightningComponentBundle",
    "aura": "AuraDefinitionBundle",
    "experiences": "ExperienceBundle",
    "objectTranslations": "CustomObjectTranslation",
}

# Content in folders: reports/<Folder>/<Name>.report-meta.xml -> Report "Folder/Name".
# The folder itself is `<Folder>.<folderSuffix>-meta.xml` and is listed under the same
# type with member "Folder".
FOLDER_TYPES: dict[str, tuple[str, str, str]] = {
    "reports": ("Report", "report", "reportFolder"),
    "dashboards": ("Dashboard", "dashboard", "dashboardFolder"),
    "email": ("EmailTemplate", "email", "emailFolder"),
    "documents": ("Document", "document", "documentFolder"),
}

# Decomposed objects: objects/<Object>/<childFolder>/<Name>.<suffix>-meta.xml
OBJECT_CHILD_TYPES: dict[str, tuple[str, str]] = {
    "businessProcesses": ("BusinessProcess", "businessProcess"),
    "compactLayouts": ("CompactLayout", "compactLayout"),
    "fieldSets": ("FieldSet", "fieldSet"),
    "fields": ("CustomField", "field"),
    "indexes": ("Index", "index"),
    "listViews": ("ListView", "listView"),
    "recordTypes": ("RecordType", "recordType"),
    "sharingReasons": ("SharingReason", "sharingReason"),
    "validationRules": ("ValidationRule", "validationRule"),
    "webLinks": ("WebLink", "webLink"),
}

UNMAPPED = "unmapped"


@dataclass(frozen=True, order=True)
class ComponentKey:
    type: str
    name: str

    def __str__(self) -> str:
        return f"{self.type}:{self.name}"


@dataclass(frozen=True)
class Located:
    """Where a path lives: its component, and the path that 'owns' the component.

    `anchor` is what we test for existence at a ref to decide added vs deleted: the
    bundle folder for an LWC or Aura component, the `-meta.xml` file for everything
    else (including `<Object>.object-meta.xml` for a CustomObject).
    """

    key: ComponentKey
    anchor: str
    bundle: bool = False


@dataclass
class ComponentChange:
    key: ComponentKey
    change: str  # "added" | "modified" | "deleted"
    files: list[str] = field(default_factory=list)

    @property
    def type(self) -> str:
        return self.key.type

    @property
    def name(self) -> str:
        return self.key.name

    @property
    def path(self) -> str:
        """A representative file for this component (for display and findings)."""
        metas = [f for f in self.files if f.endswith("-meta.xml")]
        content = [f for f in self.files if not f.endswith("-meta.xml")]
        return (content or metas or self.files)[0]


def locate(path: str, package_dirs: tuple[str, ...]) -> Located | None:
    """Map one repo path to its component, or None if it isn't under a package dir."""
    p = PurePosixPath(path)
    for pkg in package_dirs:
        try:
            rel = p.relative_to(pkg)
        except ValueError:
            continue
        return _locate_rel(rel, pkg)
    return None


def _strip_suffix(filename: str, suffix: str) -> str | None:
    for ending in (f".{suffix}-meta.xml", f".{suffix}"):
        if filename.endswith(ending):
            return filename[: -len(ending)]
    return None


def _locate_rel(rel: PurePosixPath, prefix: str) -> Located:
    parts = rel.parts

    def anchor(*segs: str) -> str:
        return "/".join([prefix, *segs]) if prefix else "/".join(segs)

    def unmapped() -> Located:
        return Located(ComponentKey(UNMAPPED, str(rel)), anchor(*parts))

    # Find the metadata folder: the first segment we recognize. SFDX lets you nest
    # anything above it (force-app/main/default/, force-app/sales/, ...).
    for i, seg in enumerate(parts):
        rest = parts[i + 1:]
        if not rest:
            break
        base = parts[: i + 1]

        if seg == "objects":
            obj = rest[0]
            if len(rest) == 2 and rest[1] == f"{obj}.object-meta.xml":
                return Located(ComponentKey("CustomObject", obj), anchor(*base, obj, rest[1]))
            if len(rest) == 3 and rest[1] in OBJECT_CHILD_TYPES:
                ctype, suffix = OBJECT_CHILD_TYPES[rest[1]]
                name = _strip_suffix(rest[2], suffix)
                if name:
                    return Located(
                        ComponentKey(ctype, f"{obj}.{name}"),
                        anchor(*base, *rest),
                    )
            return unmapped()

        if seg in BUNDLE_TYPES:
            if len(rest) == 1:
                # lwc/jsconfig.json, lwc/.eslintrc.json: tooling files, not a bundle
                return unmapped()
            return Located(
                ComponentKey(BUNDLE_TYPES[seg], rest[0]),
                anchor(*base, rest[0]),
                bundle=True,
            )

        if seg in FOLDER_TYPES:
            ftype, suffix, folder_suffix = FOLDER_TYPES[seg]
            if len(rest) == 1:
                name = _strip_suffix(rest[0], folder_suffix)
                if name:
                    return Located(ComponentKey(ftype, name), anchor(*base, rest[0]))
                return unmapped()
            # Document content keeps its own extension; everything else is <name>.<suffix>
            leaf = rest[-1]
            name = _strip_suffix(leaf, suffix)
            if name is None and ftype == "Document":
                name = leaf.split(".")[0]
            if name is None:
                return unmapped()
            member = "/".join([*rest[:-1], name])
            meta = anchor(*base, *rest[:-1], f"{name}.{suffix}-meta.xml")
            return Located(ComponentKey(ftype, member), meta)

        if seg in SIMPLE_TYPES:
            mtype, suffix = SIMPLE_TYPES[seg]
            if mtype == "StaticResource" and len(rest) > 1:
                # An unzipped resource folder: staticresources/<name>/... is one component
                name = rest[0]
                return Located(
                    ComponentKey(mtype, name),
                    anchor(*base, f"{name}.{suffix}-meta.xml"),
                )
            if len(rest) != 1:
                return unmapped()
            name = _strip_suffix(rest[0], suffix)
            if name is None:
                return unmapped()
            return Located(
                ComponentKey(mtype, name),
                anchor(*base, f"{name}.{suffix}-meta.xml"),
            )

    return unmapped()


def components_from_diff(
    repo: SfdxRepo, changes: list[FileChange], base: str, head: str
) -> list[ComponentChange]:
    """Collapse file changes into component changes.

    The rule for each component: does it exist at the merge base, and does it exist at
    head? Both -> modified; only head -> added; only base -> deleted. Asking git about
    the component's anchor (not about the individual files) is what gets bundles right:
    deleting one file of an LWC is a *modification* of the bundle, while deleting the
    whole folder is a deletion.
    """
    merge_base = repo.merge_base(base, head)
    touched: dict[ComponentKey, tuple[Located, list[str]]] = {}

    def touch(path: str) -> None:
        loc = locate(path, repo.package_dirs)
        if loc is None:
            return
        entry = touched.setdefault(loc.key, (loc, []))
        if path not in entry[1]:
            entry[1].append(path)

    for fc in changes:
        touch(fc.path)
        if fc.old_path:
            touch(fc.old_path)  # a rename touches the old component too

    result: list[ComponentChange] = []
    for key, (loc, files) in touched.items():
        if key.type == UNMAPPED:
            result.append(ComponentChange(key, "unmapped", files))
            continue
        before = repo.exists(merge_base, loc.anchor)
        after = repo.exists(head, loc.anchor)
        if before and after:
            change = "modified"
        elif after:
            change = "added"
        elif before:
            change = "deleted"
        else:
            # Neither side has the anchor (e.g. a content file without its meta file).
            change = "modified" if any(repo.exists(head, f) for f in files) else "deleted"
        result.append(ComponentChange(key, change, sorted(files)))

    return _drop_children_of_deleted_objects(sorted(result, key=lambda c: c.key))


def _drop_children_of_deleted_objects(changes: list[ComponentChange]) -> list[ComponentChange]:
    """Deleting a CustomObject deletes its fields; listing both makes the deploy fail."""
    gone = {c.name for c in changes if c.type == "CustomObject" and c.change == "deleted"}
    if not gone:
        return changes
    child_types = {t for t, _ in OBJECT_CHILD_TYPES.values()}
    return [
        c
        for c in changes
        if not (
            c.type in child_types
            and c.change == "deleted"
            and c.name.split(".", 1)[0] in gone
        )
    ]
