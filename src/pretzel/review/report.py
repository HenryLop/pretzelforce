"""Merge what code computed with what the model judged, then render it.

Code is the authority on facts: which components changed, how, and the manifests. The
model is the authority on judgment: risk, reasons, findings. The merge enforces that
split. If the model names a component code didn't find, it's dropped. If it skips one,
that component shows "not assessed" instead of a guessed risk. A finding that points
at a file that doesn't exist is dropped and counted, never shown.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

from .components import UNMAPPED, ComponentChange
from .manifest import Manifests
from .references import ComponentReferences

NOT_ASSESSED = "not assessed"
_SEVERITY_ORDER = {"block": 0, "warn": 1, "info": 2}
_SEVERITY_ICON = {"block": "🛑", "warn": "⚠️", "info": "ℹ️"}
_RISK_ORDER = {"high": 0, "medium": 1, "low": 2, NOT_ASSESSED: 3}


@dataclass
class ComponentRow:
    path: str
    type: str
    name: str
    change: str
    risk: str
    reason: str
    references: int


@dataclass
class Finding:
    severity: str
    file: str
    line: int | None
    message: str
    fix: str


@dataclass
class Review:
    mode: str  # "offline" | "canned" | "live"
    base: str
    head: str
    summary: str
    components: list[ComponentRow]
    findings: list[Finding]
    manifests: Manifests
    unmapped: list[str] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)  # what the merge threw away, and why
    cost_usd: float = 0.0
    model_calls: int = 0

    @property
    def blocking(self) -> bool:
        return any(f.severity == "block" for f in self.findings)

    def to_json(self) -> dict[str, Any]:
        """The output contract from the brief: summary, components, findings, manifests."""
        return {
            "summary": self.summary,
            "components": [asdict(c) for c in self.components],
            "findings": [asdict(f) for f in self.findings],
            "manifests": {
                "package.xml": self.manifests.package_xml,
                "destructiveChanges.xml": self.manifests.destructive_xml,
            },
            "meta": {
                "mode": self.mode,
                "base": self.base,
                "head": self.head,
                "unmapped": self.unmapped,
                "dropped": self.dropped,
                "cost_usd": round(self.cost_usd, 4),
                "model_calls": self.model_calls,
            },
        }


def merge(
    *,
    mode: str,
    base: str,
    head: str,
    changes: list[ComponentChange],
    refs: list[ComponentReferences],
    manifests: Manifests,
    model_output: dict[str, Any] | None,
    file_exists: Callable[[str], bool],
    line_count: Callable[[str], int | None],
    cost_usd: float = 0.0,
    model_calls: int = 0,
) -> Review:
    ref_counts = {str(r.component.key): r.total_hits for r in refs}
    real = [c for c in changes if c.type != UNMAPPED]
    unmapped = sorted(f for c in changes if c.type == UNMAPPED for f in c.files)
    dropped: list[str] = []

    judged: dict[str, dict[str, Any]] = {}
    if model_output is not None:
        by_key = {f"{c.type}:{c.name}": c for c in real}
        by_path = {f: c for c in real for f in c.files}
        for item in model_output.get("components", []):
            match = by_key.get(f"{item.get('type')}:{item.get('name')}") or by_path.get(
                item.get("path", "")
            )
            if match is None:
                dropped.append(
                    f"component {item.get('type')}:{item.get('name')} is not in the diff"
                )
                continue
            judged[str(match.key)] = item

    rows = []
    for c in real:
        item = judged.get(str(c.key))
        rows.append(
            ComponentRow(
                path=c.path,
                type=c.type,
                name=c.name,
                change=c.change,
                risk=item["risk"] if item else NOT_ASSESSED,
                reason=item["reason"] if item else "",
                references=ref_counts.get(str(c.key), 0),
            )
        )
    rows.sort(key=lambda r: (_RISK_ORDER.get(r.risk, 9), r.type, r.name))

    findings: list[Finding] = []
    for raw in (model_output or {}).get("findings", []):
        f = Finding(
            severity=raw["severity"],
            file=raw["file"],
            line=raw.get("line"),
            message=raw["message"],
            fix=raw["fix"],
        )
        if not file_exists(f.file):
            dropped.append(f"finding on {f.file}: file does not exist at head or base")
            continue
        if f.line is not None:
            n = line_count(f.file)
            if n is not None and not 1 <= f.line <= n:
                dropped.append(f"finding on {f.file}:{f.line}: line out of range, kept without a line")
                f.line = None
        findings.append(f)
    findings.sort(key=lambda f: (_SEVERITY_ORDER.get(f.severity, 9), f.file, f.line or 0))

    if model_output is None:
        summary = (
            "AI review not run (offline mode). Components and manifests below are "
            "computed by code; risks and findings need a canned or live model response."
        )
    else:
        summary = model_output["summary"]

    return Review(
        mode=mode,
        base=base,
        head=head,
        summary=summary,
        components=rows,
        findings=findings,
        manifests=manifests,
        unmapped=unmapped,
        dropped=dropped,
        cost_usd=cost_usd,
        model_calls=model_calls,
    )


def render_markdown(review: Review) -> str:
    """The PR comment. Step 4 posts this; step 2 prints it."""
    out: list[str] = []
    verdict = "🛑 Blocking findings" if review.blocking else "✅ No blocking findings"
    if review.mode == "offline":
        verdict = "⏸️ Manifest only (AI review not run)"
    out.append(f"## PretzelForce review: {verdict}")
    out.append("")
    out.append(review.summary)
    out.append("")

    if review.findings:
        out.append(f"### Findings ({len(review.findings)})")
        out.append("")
        for f in review.findings:
            where = f"`{f.file}`" + (f" line {f.line}" if f.line else "")
            out.append(f"- {_SEVERITY_ICON.get(f.severity, '')} **{f.severity}** {where}: {f.message}")
            out.append(f"  - **Fix:** {f.fix}")
        out.append("")
    elif review.mode != "offline":
        out.append("### Findings")
        out.append("")
        out.append("None.")
        out.append("")

    out.append(f"### Components ({len(review.components)})")
    out.append("")
    out.append("| Risk | Change | Type | Name | Refs | Why |")
    out.append("| --- | --- | --- | --- | --- | --- |")
    for c in review.components:
        reason = c.reason.replace("|", "\\|").replace("\n", " ")
        out.append(f"| {c.risk} | {c.change} | {c.type} | `{c.name}` | {c.references} | {reason} |")
    out.append("")

    if review.unmapped:
        out.append("### Not in the manifest")
        out.append("")
        out.append("These changed files don't map to a known metadata type:")
        out.extend(f"- `{p}`" for p in review.unmapped)
        out.append("")

    out.append("<details><summary>package.xml</summary>")
    out.append("")
    out.append("```xml")
    out.append(review.manifests.package_xml.rstrip())
    out.append("```")
    out.append("</details>")
    if review.manifests.destructive_xml:
        out.append("")
        out.append("<details><summary>destructiveChanges.xml</summary>")
        out.append("")
        out.append("```xml")
        out.append(review.manifests.destructive_xml.rstrip())
        out.append("```")
        out.append("</details>")
    out.append("")

    foot = f"base `{review.base[:10]}` · head `{review.head[:10]}` · mode {review.mode}"
    if review.mode == "live":
        foot += f" · {review.model_calls} model call(s) · US${review.cost_usd:.4f}"
    if review.dropped:
        foot += f" · {len(review.dropped)} model item(s) discarded by validation"
    out.append(f"<sub>{foot}</sub>")
    return "\n".join(out) + "\n"


def dumps(review: Review) -> str:
    return json.dumps(review.to_json(), indent=2, ensure_ascii=False)
