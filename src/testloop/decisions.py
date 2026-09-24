"""Human triage decisions persisted in the repo so flagged tests are not re-reported every run.

.testloop.yml
  tests:
    "tests/test_a.py::test_flaky":
      decision: quarantine   # exclude from the loop, do not re-flag
      note: talks to a real clock
    "tests/test_a.py::test_ok":
      decision: accept       # human reviewed; skip triage and use in the loop
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

VALID = {"accept", "quarantine"}


@dataclass
class Decisions:
    accepted: set[str] = field(default_factory=set)
    quarantined: set[str] = field(default_factory=set)
    notes: dict[str, str] = field(default_factory=dict)

    def note(self, test_id: str) -> str:
        return self.notes.get(test_id, "")

    @staticmethod
    def template_for(test_ids: list[str], reason: str) -> str:
        lines = ["tests:"]
        for test_id in test_ids:
            lines += [f'  "{test_id}":', "    decision: quarantine  # or: accept", f"    note: flagged by testloop as {reason}"]
        return "\n".join(lines) + "\n"


def load_decisions(path: Path) -> Decisions:
    if not path.exists():
        return Decisions()
    raw = yaml.safe_load(path.read_text()) or {}
    decisions = Decisions()
    for test_id, entry in (raw.get("tests") or {}).items():
        entry = entry or {}
        decision = str(entry.get("decision", "")).strip()
        if decision not in VALID:
            raise ValueError(f"{path}: test {test_id!r} has unknown decision {decision!r}; expected one of {sorted(VALID)}")
        (decisions.accepted if decision == "accept" else decisions.quarantined).add(str(test_id))
        if entry.get("note"):
            decisions.notes[str(test_id)] = str(entry["note"])
    return decisions
