"""Filesystem access for the agent: path safety, protected-file snapshots, tracked writes, diffs.

The workspace does not depend on git so it works on any checkout.
"""

from __future__ import annotations

import difflib
import hashlib
import logging
import os
import re
from pathlib import Path

log = logging.getLogger(__name__)

IGNORED_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".pytest_cache", ".testloop", "dist", "build", ".mypy_cache", ".ruff_cache"}


class PathOutsideRepo(ValueError):
    pass


def glob_to_regex(pattern: str) -> re.Pattern[str]:
    """Translate a gitignore-style glob to a regex over '/'-separated relative paths.

    `**` matches across directories; `*` and `?` do not cross '/'. A pattern without '/'
    (other than a leading `**/`) matches only at the repo root, mirroring how test-runner
    config files live at the top level.
    """
    out = ""
    i = 0
    while i < len(pattern):
        ch = pattern[i]
        if pattern.startswith("**/", i):
            out += "(?:.*/)?"
            i += 3
        elif pattern.startswith("/**", i):
            out += "(?:/.*)?"
            i += 3
        elif pattern.startswith("**", i):
            out += ".*"
            i += 2
        elif ch == "*":
            out += "[^/]*"
            i += 1
        elif ch == "?":
            out += "[^/]"
            i += 1
        else:
            out += re.escape(ch)
            i += 1
    return re.compile("^" + out + "$")


def matches_any(rel_path: str, patterns: list[re.Pattern[str]]) -> bool:
    return any(p.match(rel_path) for p in patterns)


class Workspace:
    def __init__(self, repo_path: Path, protected_globs: list[str]) -> None:
        self.repo_path = Path(repo_path).resolve()
        self.protected_patterns = [glob_to_regex(g) for g in protected_globs]
        self._protected_hashes: dict[str, str] = {}
        self._iteration_originals: dict[str, bytes | None] = {}
        self._run_originals: dict[str, bytes | None] = {}

    # ---- path safety -------------------------------------------------------------------

    def resolve(self, rel_path: str) -> Path:
        candidate = (self.repo_path / rel_path).resolve() if not os.path.isabs(rel_path) else Path(rel_path).resolve()
        try:
            candidate.relative_to(self.repo_path)
        except ValueError as exc:
            raise PathOutsideRepo(f"{rel_path!r} resolves outside the repository") from exc
        # resolve() follows symlinks, but a symlink *inside* the repo pointing outside has
        # already been caught above. Also reject paths whose parents are symlinks that escape.
        return candidate

    def relative(self, path: Path) -> str:
        return path.resolve().relative_to(self.repo_path).as_posix()

    def is_protected(self, rel_path: str) -> bool:
        return matches_any(rel_path, self.protected_patterns)

    # ---- walking -----------------------------------------------------------------------

    def walk(self) -> list[str]:
        files: list[str] = []
        for root, dirs, names in os.walk(self.repo_path):
            dirs[:] = sorted(d for d in dirs if d not in IGNORED_DIRS)
            rel_root = os.path.relpath(root, self.repo_path)
            for name in sorted(names):
                rel = name if rel_root == "." else f"{rel_root}/{name}"
                files.append(rel.replace(os.sep, "/"))
        return files

    def list_files(self, glob: str = "**/*") -> list[str]:
        pattern = glob_to_regex(glob)
        return [f for f in self.walk() if pattern.match(f)]

    def grep(self, regex: str, glob: str = "**/*", max_hits: int = 200) -> list[tuple[str, int, str]]:
        compiled = re.compile(regex)
        hits: list[tuple[str, int, str]] = []
        for rel in self.list_files(glob):
            try:
                text = (self.repo_path / rel).read_text(errors="replace")
            except (OSError, UnicodeDecodeError):
                continue
            for lineno, line in enumerate(text.splitlines(), start=1):
                if compiled.search(line):
                    hits.append((rel, lineno, line.rstrip()))
                    if len(hits) >= max_hits:
                        return hits
        return hits

    # ---- protected snapshot ------------------------------------------------------------

    def _protected_files(self) -> list[str]:
        return [f for f in self.walk() if self.is_protected(f)]

    def snapshot_protected(self) -> None:
        self._protected_hashes = {f: self._hash(f) for f in self._protected_files()}
        log.debug("[DEBUG] snapshot_protected files=%d", len(self._protected_hashes))

    def verify_protected_unchanged(self) -> list[str]:
        """Return protected files that were modified, added or deleted since the snapshot."""
        current = {f: self._hash(f) for f in self._protected_files()}
        changed = sorted(set(current) ^ set(self._protected_hashes) | {f for f in current if current[f] != self._protected_hashes.get(f)})
        if changed:
            log.error("protected files changed: %s", changed)
        return changed

    def _hash(self, rel_path: str) -> str:
        return hashlib.sha256((self.repo_path / rel_path).read_bytes()).hexdigest()

    # ---- tracked writes ----------------------------------------------------------------

    def begin_iteration(self) -> None:
        self._iteration_originals = {}

    def read_text(self, rel_path: str) -> str:
        return self.resolve(rel_path).read_text()

    def write_text(self, rel_path: str, content: str) -> None:
        target = self.resolve(rel_path)
        rel = self.relative(target) if target.exists() else Path(rel_path).as_posix()
        original = target.read_bytes() if target.exists() else None
        self._iteration_originals.setdefault(rel, original)
        self._run_originals.setdefault(rel, original)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        log.debug("[DEBUG] write_text path=%s bytes=%d", rel, len(content))

    def revert_iteration(self) -> None:
        for rel, original in self._iteration_originals.items():
            target = self.repo_path / rel
            if original is None:
                if target.exists():
                    target.unlink()
            else:
                target.write_bytes(original)
            # The run-level original stays as it was; if this file was first touched this
            # iteration the run-level entry now matches disk again.
        log.info("reverted %d file(s) from this iteration", len(self._iteration_originals))
        self._iteration_originals = {}

    def changed_files(self) -> list[str]:
        out = []
        for rel, original in self._run_originals.items():
            target = self.repo_path / rel
            current = target.read_bytes() if target.exists() else None
            if current != original:
                out.append(rel)
        return sorted(out)

    def _diff(self, originals: dict[str, bytes | None]) -> str:
        chunks: list[str] = []
        for rel in sorted(originals):
            original = originals[rel]
            target = self.repo_path / rel
            current = target.read_bytes() if target.exists() else None
            if current == original:
                continue
            before = original.decode(errors="replace").splitlines(keepends=True) if original is not None else []
            after = current.decode(errors="replace").splitlines(keepends=True) if current is not None else []
            chunks.append(
                "".join(
                    difflib.unified_diff(
                        before,
                        after,
                        fromfile=f"a/{rel}" if original is not None else "/dev/null",
                        tofile=f"b/{rel}" if current is not None else "/dev/null",
                    )
                )
            )
        return "\n".join(chunks)

    def iteration_diff(self) -> str:
        return self._diff(self._iteration_originals)

    def run_diff(self) -> str:
        return self._diff(self._run_originals)
