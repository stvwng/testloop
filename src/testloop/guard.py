"""Write policy: the agent may only change ordinary source files inside the repo."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from testloop.workspace import IGNORED_DIRS, PathOutsideRepo, Workspace, glob_to_regex, matches_any

log = logging.getLogger(__name__)

HARNESS_FILES = [".testloop.yml", ".testloop/**"]


@dataclass(frozen=True)
class Denial:
    path: str
    rule: str
    message: str


class WriteGuard:
    def __init__(
        self,
        workspace: Workspace,
        test_file_globs: list[str],
        config_file_globs: list[str],
        extra_protected_globs: list[str],
    ) -> None:
        self.workspace = workspace
        self.test_file_globs = list(test_file_globs)
        self.config_file_globs = list(config_file_globs)
        self.extra_protected_globs = list(extra_protected_globs)
        self._test_patterns = [glob_to_regex(g) for g in self.test_file_globs + self.extra_protected_globs]
        self._config_patterns = [glob_to_regex(g) for g in self.config_file_globs + HARNESS_FILES]

    @property
    def protected_globs(self) -> list[str]:
        """Everything the workspace must hash-check after each iteration."""
        return self.test_file_globs + self.extra_protected_globs + self.config_file_globs + HARNESS_FILES

    def check(self, rel_path: str) -> Denial | None:
        try:
            resolved = self.workspace.resolve(rel_path)
        except PathOutsideRepo as exc:
            return self._deny(rel_path, "outside_repo", str(exc))
        rel = resolved.relative_to(self.workspace.repo_path).as_posix()
        top = rel.split("/", 1)[0]
        if top in IGNORED_DIRS:
            return self._deny(rel_path, "tool_directory", f"{rel_path} is inside {top}/, which the agent may not modify")
        if matches_any(rel, self._test_patterns):
            return self._deny(rel_path, "test_file", f"{rel_path} is a test file or protected fixture; tests must not be modified")
        if matches_any(rel, self._config_patterns):
            return self._deny(rel_path, "test_config", f"{rel_path} configures test collection or the harness and may not be modified")
        return None

    def _deny(self, path: str, rule: str, message: str) -> Denial:
        log.warning("write denied path=%s rule=%s", path, rule)
        return Denial(path=path, rule=rule, message=message)
