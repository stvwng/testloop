"""Pre-loop review: which failing tests are safe to hand to the agent."""

from __future__ import annotations

import logging
from pathlib import Path

from testloop.llm.common import MAX_TOKENS, describe_api_error, truncate, usage_from
from testloop.llm.prompts import TRIAGE_SYSTEM
from testloop.llm.source import extract_test_source
from testloop.models import TestResult, TriageBatch, TriageVerdict, Usage
from testloop.runners.base import TestRunner

log = logging.getLogger(__name__)

FILE_CHARS = 15000
CONFTEST_CHARS = 4000


def lang_for(file: str | None) -> str:
    return "py" if (file or "").endswith(".py") else "ts"


def gather_test_context(repo_path: Path, runner: TestRunner, result: TestResult) -> tuple[str, str, str]:
    """(test source, whole test file, fixtures) for a single test, each possibly empty."""
    file, name = runner.locate(result.test_id)
    file = file or result.file
    if not file:
        return "", "", ""
    path = repo_path / file
    if not path.is_file():
        return "", "", ""
    text = path.read_text(errors="replace")
    snippet = extract_test_source(text, name, lang_for(file)) if name else ""
    fixtures: list[str] = []
    if lang_for(file) == "py":
        # conftest.py files on the path from the test up to the repo root shape setup and ordering.
        for parent in [path.parent, *path.parent.parents]:
            if parent == repo_path.parent:
                break
            conftest = parent / "conftest.py"
            if conftest.is_file():
                fixtures.append(f"# {conftest.relative_to(repo_path).as_posix()}\n{truncate(conftest.read_text(errors='replace'), CONFTEST_CHARS)}")
            if parent == repo_path:
                break
    return snippet, truncate(text, FILE_CHARS), "\n\n".join(fixtures)


class TestTriager:
    __test__ = False  # keep pytest from collecting this
    def __init__(self, client, model: str, effort: str, repo_path: Path, runner: TestRunner, batch_size: int = 10) -> None:
        self.client = client
        self.model = model
        self.effort = effort
        self.repo_path = Path(repo_path)
        self.runner = runner
        self.batch_size = batch_size

    def triage(self, failing: list[TestResult]) -> tuple[list[TriageVerdict], Usage]:
        verdicts: list[TriageVerdict] = []
        total = Usage()
        for start in range(0, len(failing), self.batch_size):
            batch = failing[start : start + self.batch_size]
            got, usage = self._triage_batch(batch)
            total.add(usage)
            by_id = {v.test_id: v for v in got}
            for result in batch:
                verdict = by_id.get(result.test_id)
                if verdict is None:
                    log.warning("triage returned no verdict for %s; treating as usable", result.test_id)
                    verdict = TriageVerdict(test_id=result.test_id, usable=True, explanation="no verdict returned by the model; treated as usable")
                verdicts.append(verdict)
        return verdicts, total

    def _triage_batch(self, batch: list[TestResult]) -> tuple[list[TriageVerdict], Usage]:
        prompt = self._build_prompt(batch)
        log.debug("[DEBUG] _triage_batch tests=%d prompt_chars=%d", len(batch), len(prompt))
        try:
            response = self.client.messages.parse(
                model=self.model,
                max_tokens=MAX_TOKENS,
                system=TRIAGE_SYSTEM,
                thinking={"type": "adaptive"},
                output_config={"effort": self.effort},
                messages=[{"role": "user", "content": prompt}],
                output_format=TriageBatch,
            )
        except Exception as exc:  # noqa: BLE001 - re-raised as a typed harness error
            raise describe_api_error("triage", exc) from exc
        if getattr(response, "stop_reason", None) == "refusal":
            raise RuntimeError("triage: the model refused the request")
        parsed: TriageBatch = response.parsed_output
        return list(parsed.verdicts), usage_from(response)

    def _build_prompt(self, batch: list[TestResult]) -> str:
        sections = [f"Review these {len(batch)} failing tests. Return exactly one verdict per test_id.\n"]
        seen_files: set[str] = set()
        for result in batch:
            snippet, whole_file, fixtures = gather_test_context(self.repo_path, self.runner, result)
            file, _ = self.runner.locate(result.test_id)
            sections.append(f"<test id=\"{result.test_id}\">")
            sections.append(f"<failure>\n{truncate(result.message or '', 1500)}\n{truncate(result.traceback or '', 3000)}\n</failure>")
            if snippet:
                sections.append(f"<source>\n{snippet}</source>")
            if file and file not in seen_files and whole_file:
                seen_files.add(file)
                sections.append(f"<test_file path=\"{file}\">\n{whole_file}\n</test_file>")
            if fixtures:
                sections.append(f"<fixtures>\n{fixtures}\n</fixtures>")
            sections.append("</test>\n")
        return "\n".join(sections)
