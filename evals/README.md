# Haunted Halls evaluation harness

This package provides a lightweight evaluation harness for the engine's
model-backed agents. Evals complement unit tests: unit tests prove a known
function or contract, while scenarios compare agent outputs against bounded
authoritative fixtures and report regressions across multiple cases.

Offline mode is the safe default. It makes no provider calls, creates no
campaigns or turns, writes no database telemetry, and never executes a
Director proposal through `WorldAuthorityExecutor`.

## Contents

- `schemas.py` — typed scenario and grading contracts.
- `graders.py` — deterministic evaluation checks for Director and Narrator outputs.
- `runner.py` — scenario execution helpers.
- `report.py` — simple aggregate reporting.
- `scenarios/` — version-controlled example scenarios stored as JSON.

## Example

```python
from evals.schemas import Scenario, ScenarioTarget
from evals.runner import EvalRunner

scenario = Scenario(
    scenario_id="director-baseline-noop",
    description="No action when nothing needs changing.",
    target=ScenarioTarget.DIRECTOR,
    authoritative_input={"current_player_room_id": "entry_hall", "legal_destinations": ["grand_corridor"]},
    deterministic_expectations={"require_none": True},
    actual_output={"decision": "none"},
)

result = EvalRunner().run(scenario)
print(result.passed)
```

## Scenario format

Scenarios are JSON files under `evals/scenarios/`. Each has a stable ID,
description, `director` or `narrator` target, tags, bounded synthetic
authoritative input, deterministic expectations, and an optional fixture
output. Duplicate IDs are rejected when the corpus is loaded. Do not add
secrets, real users, production campaign IDs, or conversation dumps.

Run the corpus offline:

```sh
python -m evals.runner
python -m evals.runner --agent director --tag grounding --json
python -m evals.runner --scenario-id narrator-observable-item
```

The JSON report is stable (`sort_keys=True`, compact separators) and excludes
raw actual output so it is suitable for comparison without persisting provider
content. Use `--tag`, `--agent`, or `--scenario-id` to control scope.

## Graders and limitations

Deterministic Director graders reuse the production Pydantic proposal and
WorldAction schemas, then check bounded entity and destination references.
Narrator checks are intentionally fixture-specific `contains` and
`must_not_contain` assertions. They are transparent regression checks, not a
general semantic-groundedness or narrative-quality metric.

`SubjectiveGrader` is the explicit future model-judge boundary. A later
milestone may add a separate, live-only grading call with its own result type;
it must never be silently mixed into deterministic pass/fail assertions.

## Explicit live mode

Provider-backed execution is opt-in only:

```sh
python -m evals.runner --live --scenario-id director-legal-adjacent-move
```

Live mode requires both `AI_ENABLED=true` and `OPENAI_API_KEY`. Missing
configuration fails closed with a developer-facing error. It reuses the
existing `DirectorAgent` and `NarratorAgent` contracts and performs no
gameplay or persistence mutation. Credentials alone never enable live mode.
Automated tests must mock provider calls; normal CI never runs live mode and
must not make network calls.

## Extending the harness

Add a synthetic JSON scenario first, then add or reuse a focused deterministic
grader. Keep grader names and details explicit about what they prove. Add
model-judged checks only behind the future interface and explicit live opt-in.
Live evals can incur provider cost and can expose fixture content to a
provider, so use the smallest synthetic, reviewable fixtures possible.
