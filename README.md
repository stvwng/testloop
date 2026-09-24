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
