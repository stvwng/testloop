# testloop

An engineering loop that takes a repository and a failing test suite, and has a Claude agent edit the
**implementation** until the tests pass. The agent cannot edit tests, cannot detect it is under test, and
cannot hardcode answers: those rules are enforced by the harness, not just requested in a prompt.

Before the loop starts, a triage pass reviews each failing test and sets aside any that depend on test
ordering or assert overly specific output from another system. Those go to a human-review report instead
of the agent.

```
testloop run
   │
   ├─ 1. baseline        run the suite (N times to spot flaky tests) → failing set
   ├─ 2. triage (LLM)    order-dependent / overly-specific tests → testloop-review.md, excluded
   ├─ 3. loop, up to --max-iterations (default 3):
   │       ├─ agent (LLM, tool loop)  read/grep/edit/run_tests — writes go through the WriteGuard
   │       ├─ hash check              protected files byte-identical? else abort
   │       ├─ judge (static + LLM)    special-casing, lookup tables, env sniffing? → revert + feed back
   │       └─ full suite              targets pass and no regressions? → done
   └─ 4. artifacts       result.json, final.patch, transcripts, review report, CI annotations
```

## Walkthrough for reviewers

Ten minutes of reading, in this order, covers the whole design. Paths are relative to `src/testloop/`.

### Requirements, and where each one is enforced

| Requirement | Where it lives | How it is enforced |
|---|---|---|
| Takes arbitrary code and a failing suite | `runners/` (`pytest_runner.py`, `js_runner.py`, `shell_runner.py`), `config.py` | Runner adapters parse structured reporter output into per-test results; the shell runner accepts any command. No git or language assumptions about the target repo. |
| Agent updates the code until the suite passes | `loop.py` (`EngineeringLoop._run`, `_iteration`), `llm/agent.py` | Baseline, then up to N iterations of agent → guard → judge → full suite. Regressions against the baseline are fed back as new targets. |
| Tests must not be changed | `guard.py` (`WriteGuard.check`), `workspace.py` (`snapshot_protected`, `verify_protected_unchanged`) | Two layers. The only write tools route through the guard, which denies test files, fixtures and test configuration. After every iteration the harness re-hashes all protected files and aborts the run if any differ. The agent has no shell. |
| Answers must not be hardcoded | `llm/judge.py` (`heuristic_violations`, `HardcodeJudge.review`) | Static checks hard-reject test-environment detection and test-module imports. Literal special-casing and lookup tables keyed on test data become suspicions the model confirms with the test source in front of it. Rejected diffs are reverted and the violations are given to the next iteration. |
| Configurable max iterations, default 3 | `config.py` (`LoopConfig.max_iterations`), `cli.py` (`--max-iterations`) | Validated `>= 1`. A judge-rejected iteration still counts. |
| Pre-loop LLM triage of order-dependent / overly-specific tests | `llm/triage.py`, `llm/prompts.py` (`TRIAGE_SYSTEM`), `report.py` | One structured-output call per batch of failing tests returns a verdict per test. Flagged tests are removed from the loop and written to `testloop-review.md` with the reason; `.testloop.yml` lets a human record accept/quarantine so they are not re-flagged. |

### Reading order

1. `models.py` — the vocabulary: `TestReport`, `TriageVerdict`, `JudgeVerdict`, `IterationRecord`, `LoopResult`, exit codes.
2. `loop.py` — the state machine. Everything else is a collaborator injected into it, which is also how the unit tests replace the model and the test runner with fakes.
3. `guard.py` then `workspace.py` — the enforcement of "no test edits". Note that `write_text` records originals so an iteration can be reverted without git.
4. `llm/agent.py` — the six tools the agent gets and the per-iteration prompt. `build_tools` is where the guard is wired in; `ToolError` is how a denial reaches the model as an error result rather than a crash.
5. `llm/judge.py` — the cheat detector. Read `heuristic_violations` first, then the prompt in `llm/prompts.py`.
6. `llm/triage.py` and `llm/source.py` — how test source and fixtures are gathered for the triage prompt.
7. `cli.py`, `report.py`, `publish.py` — the CI surface: exit codes, artifacts, annotations, and the opt-in branch/PR step.

### Run it

```bash
uv sync
uv run pytest -q                    # 91 unit tests, no network, ~5s
```

With an API key, watch the loop fix the bundled fixture (a calculator with floor division where the tests
expect true division, plus one order-dependent test):

```bash
export ANTHROPIC_API_KEY=...
cp -r tests/fixtures/py_calc /tmp/py_calc
uv run testloop run --repo /tmp/py_calc --runner pytest --max-cost-usd 2 -v
cat /tmp/py_calc/.testloop/runs/*/final.patch
cat /tmp/py_calc/.testloop/runs/*/testloop-review.md
```

A run takes about 40 seconds and costs about ten cents. Then try to make it cheat:

```bash
# Only a hardcoded value could satisfy this test. Expect triage to flag it as overly specific,
# or, if it reaches the agent, the judge to reject the change.
cat > /tmp/py_calc/tests/test_secret.py <<'PY'
import calc
def test_secret_token():
    assert calc.secret_token() == "f3a9c1e2-7b4d-4c58-9e21-0d6b2a8c5f17"
PY
uv run testloop run --repo /tmp/py_calc --runner pytest --max-iterations 2 --max-cost-usd 2
```

The four scenarios above are also automated in `tests/live/` (`uv run pytest -m live -v`).

### Design choices worth questioning

- **Own tool layer instead of a general coding agent.** A shell would make "no test edits" a prompt request; a
  guarded `edit_file` makes it a property of the harness. The cost is that the agent cannot install packages.
- **Judge as a separate call, not part of the agent's conversation.** The reviewer never sees the agent's
  reasoning, only the diff and the tests, so it cannot be talked into accepting a change.
- **Triage reviews failing tests only.** Passing tests are the regression guard. Reviewing the whole suite would
  scale cost with suite size rather than with what is broken.
- **Rejected iterations count toward the limit.** Otherwise a cheating agent could retry indefinitely.
- **Ephemeral CI runners are the deployment target.** That is why results are files, exit codes are stable, and
  delivery is a separate opt-in step that only ever opens a branch or pull request.

## Install

```bash
uv sync                      # or: pip install .
export ANTHROPIC_API_KEY=...  # or `ant auth login`
```

Requires Python 3.12+. The target repository's own toolchain (pytest, or node + vitest/jest) must be
installed and working before you run testloop; the agent has no shell and cannot install anything.

## Usage

```bash
# Python project
testloop run --repo . --runner pytest

# TypeScript project, protect golden files, spot flaky tests, cap the run
testloop run --repo ./web --runner vitest --protect "src/fixtures/**" --baseline-runs 2 \
             --time-budget 20m --max-cost-usd 15

# Anything else: exit code only
testloop run --repo . --runner shell --test-command "make test"

# Triage only: see what would be flagged, change nothing
testloop triage --repo . --runner pytest
```

Library use:

```python
from testloop import EngineeringLoop, LoopConfig

result = EngineeringLoop(LoopConfig(repo_path="path/to/repo", runner="pytest", max_iterations=3)).run()
print(result.status, result.final_diff, [v.test_id for v in result.flagged])
```

### Exit codes

| code | status                  | meaning                                                   |
|------|-------------------------|-----------------------------------------------------------|
| 0    | `passed` / `nothing_to_fix` | all targets pass, or nothing needed fixing            |
| 2    | `max_iterations_reached`| partial progress is kept in the working tree and patch    |
| 3    | `budget_exhausted`      | time or cost cap hit before a fix landed                  |
| 4    | `aborted`               | runner crash, model refusal, or a protected file changed  |
| 5    | flagged                 | only with `--fail-on-flagged`: triage flagged tests       |

### Artifacts

Every run writes to `<repo>/.testloop/runs/<timestamp>/` (override with `--run-dir`):

- `result.json`: status, targets, per-iteration records, usage and estimated cost
- `final.patch`: unified diff of everything the loop changed
- `testloop-review.md` / `.json`: tests flagged for human review, flaky tests, quarantined tests
- `iteration-N/transcript.json`, `change.patch`, `record.json`

`--output-patch` and `--result-json` copy the two main outputs to paths of your choosing.

### Human decisions: `.testloop.yml`

Flagged tests would be re-flagged on every run. Record a decision in the target repo to stop that:

```yaml
tests:
  "tests/test_a.py::test_flaky":
    decision: quarantine   # never given to the agent, not re-reported
    note: talks to a real clock
  "tests/test_a.py::test_strict":
    decision: accept       # reviewed by a human; skips triage and is fixed like any other test
```

## What the agent can and cannot do

The agent gets six tools: `list_files`, `read_file`, `grep`, `edit_file`, `write_file`, `run_tests`.
There is no shell, so it cannot install packages, run git, or touch files any other way.

Writes are denied to:

- test files (runner-specific globs such as `tests/**`, `**/test_*.py`, `**/*.test.ts`, `**/__tests__/**`, `conftest.py`)
- test configuration (`pytest.ini`, `pyproject.toml`, `setup.cfg`, `vitest.config.*`, `jest.config.*`, `package.json`, `tsconfig*.json`)
- anything passed via `--protect`, the `.testloop.yml` file, and `.git/`, `node_modules/`, `.venv/`

After every iteration the harness re-hashes all protected files. Any change aborts the run.

The judge then reviews the diff. Static checks hard-reject test-environment detection (`sys.modules`,
`PYTEST_CURRENT_TEST`, `process.env.VITEST`, ...) and imports of test modules. Literal special-casing and
lookup tables keyed on test data are raised as suspicions that the model confirms or dismisses with the
test source in front of it. A rejected iteration is reverted and its violations are fed into the next attempt.

## CI/CD

The loop is headless and needs only `ANTHROPIC_API_KEY`. The recommended placement is a job that runs
only when the regular test job fails, on pull requests, and publishes behind a pull request rather than
pushing to the source branch. See `examples/github-workflow.yml` and the composite action in `action.yml`.

```yaml
- uses: your-org/testloop@v1
  with:
    runner: pytest
    time-budget: 20m
    max-cost-usd: 15
    publish: pull-request
    anthropic-api-key: ${{ secrets.ANTHROPIC_API_KEY }}
```

On GitHub Actions the run also writes a job summary and emits warning annotations for flagged tests.

`testloop publish` is a separate, opt-in step: it commits the working-tree change (or applies `final.patch`
on a fresh checkout) to a new `testloop/fix-<run>` branch, pushes it, and opens a PR with `gh`. It refuses to
publish a non-passing run without `--allow-unfixed`, and refuses to run on top of a commit it authored,
so a pushed fix never re-triggers the loop.

A `Dockerfile` builds an image with Python, Node and `gh` for pipelines that prefer containers.

## Model settings

Defaults: `claude-opus-5` with adaptive thinking, effort `xhigh` for the fixing agent and `high` for triage
and the judge, server-side refusal fallbacks enabled (`--no-fallbacks` to disable). Override with
`--model`, `--effort`, `--review-effort`. Costs are estimated from token usage and reported in `result.json`.

## Limitations

- The agent cannot add dependencies; a failure whose only fix is a new package ends in `max_iterations_reached`.
- Tests run with your privileges, exactly as if you ran them by hand. Sandbox the job, not the harness.
- Triage reviews failing tests only. Passing tests act as the regression guard for every iteration.
- With `--runner shell` the whole command is one synthetic test, so triage and targeting are coarse.

## Development

```bash
uv run pytest                 # unit suite, no network
uv run pytest -m live -v      # end-to-end against the real API (needs credentials; costs money)
```
