"""The SFDX repo under review: `sfdx-project.json` plus read-only git access.

Everything here reads *git objects*, never the working tree. A review compares two
refs; the checkout may be on a third branch entirely, or dirty. `git show ref:path`
and `git grep ... ref` give the exact content at each side of the diff, whatever is
checked out.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

# quotepath=off keeps non-ASCII paths (Spanish accents in API names) as real UTF-8
# instead of octal escapes.
_GIT = ("git", "-c", "core.quotepath=off")


class GitError(RuntimeError):
    def __init__(self, message: str, returncode: int) -> None:
        super().__init__(message)
        self.returncode = returncode


@dataclass(frozen=True)
class FileChange:
    """One line of `git diff --name-status`: A, M, D, or R (rename) with the old path."""

    status: str  # "A" | "M" | "D" | "R"
    path: str
    old_path: str | None = None


@dataclass
class SfdxRepo:
    root: Path
    package_dirs: tuple[str, ...]
    api_version: str

    @classmethod
    def open(cls, root: Path | str, ref: str | None = None) -> "SfdxRepo":
        """Read `sfdx-project.json` at `ref` (or the working tree when ref is None)."""
        root = Path(root).resolve()
        if ref is None:
            raw = (root / "sfdx-project.json").read_text(encoding="utf-8")
        else:
            raw = _git(root, "show", f"{ref}:sfdx-project.json")
        project = json.loads(raw)
        dirs = tuple(
            str(PurePosixPath(d["path"].replace("\\", "/")))
            for d in project.get("packageDirectories", [])
        )
        if not dirs:
            raise ValueError("sfdx-project.json has no packageDirectories")
        version = project.get("sourceApiVersion")
        if not version:
            raise ValueError("sfdx-project.json has no sourceApiVersion")
        return cls(root=root, package_dirs=dirs, api_version=str(version))

    # -- git reads ---------------------------------------------------------

    def resolve(self, ref: str) -> str:
        return _git(self.root, "rev-parse", "--verify", f"{ref}^{{commit}}").strip()

    def merge_base(self, base: str, head: str) -> str:
        return _git(self.root, "merge-base", base, head).strip()

    def diff(self, base: str, head: str) -> list[FileChange]:
        """`git diff base...head`, limited to the package dirs.

        Three dots: compare head against the merge base, which is exactly what a PR
        shows. Two dots would also count everything merged into base since the branch
        was cut, as if this PR had reverted it.
        """
        out = _git(
            self.root,
            "diff",
            "--name-status",
            "-z",
            "-M",
            "--no-color",
            f"{base}...{head}",
            "--",
            *self.package_dirs,
        )
        return _parse_name_status_z(out)

    def diff_text(self, base: str, head: str, path: str, old_path: str | None = None) -> str:
        paths = [path] if old_path is None else [old_path, path]
        return _git(
            self.root, "diff", "--no-color", "-M", f"{base}...{head}", "--", *paths
        )

    def show(self, ref: str, path: str) -> str | None:
        """File content at `ref`, or None if the path does not exist there."""
        try:
            return _git(self.root, "show", f"{ref}:{path}")
        except GitError:
            return None

    def exists(self, ref: str, path: str) -> bool:
        """True if `path` (file or directory) exists at `ref`."""
        try:
            _git(self.root, "cat-file", "-e", f"{ref}:{path}")
            return True
        except GitError:
            return False

    def ls_files(self, ref: str, path: str) -> list[str]:
        out = _git(self.root, "ls-tree", "-r", "--name-only", "-z", ref, "--", path)
        return [p for p in out.split("\0") if p]

    def grep(self, ref: str, pattern: str, *, word: bool = True, fixed: bool = True) -> list[tuple[str, int, str]]:
        """`git grep` at `ref` inside the package dirs: (path, line, text) triples."""
        args = ["grep", "-n", "-I", "--no-color", "--full-name"]
        if word:
            args.append("-w")
        args.append("-F" if fixed else "-E")
        args += ["-e", pattern, ref, "--", *self.package_dirs]
        try:
            out = _git(self.root, *args)
        except GitError as exc:
            if exc.returncode == 1:
                return []  # git grep exits 1 for "no matches"
            raise
        hits = []
        prefix = f"{ref}:"
        for line in out.splitlines():
            if line.startswith(prefix):
                line = line[len(prefix):]
            path, _, rest = line.partition(":")
            num, _, text = rest.partition(":")
            if num.isdigit():
                hits.append((path, int(num), text))
        return hits


def _git(root: Path, *args: str) -> str:
    proc = subprocess.run(
        [*_GIT, *args],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        raise GitError(
            f"git {' '.join(args[:3])}... failed (exit {proc.returncode}): "
            f"{proc.stderr.strip()[:300]}",
            proc.returncode,
        )
    return proc.stdout


def _parse_name_status_z(out: str) -> list[FileChange]:
    """Parse `--name-status -z`: status\\0path\\0, and for renames status\\0old\\0new\\0."""
    tokens = [t for t in out.split("\0")]
    changes: list[FileChange] = []
    i = 0
    while i < len(tokens) and tokens[i]:
        status = tokens[i]
        kind = status[0]
        if kind in ("R", "C"):
            old, new = tokens[i + 1], tokens[i + 2]
            if kind == "R":
                changes.append(FileChange("R", new, old))
            else:  # a copy is just an add as far as a deploy is concerned
                changes.append(FileChange("A", new))
            i += 3
        else:
            path = tokens[i + 1]
            if kind == "T":  # type change (e.g. file <-> symlink): treat as modify
                kind = "M"
            changes.append(FileChange(kind, path))
            i += 2
    return changes
