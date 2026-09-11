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
    authoritative_input={
        "current_player_room_id": "eval_foyer",
        "clock_tick": 0,
        "facts": [],
        "npcs": [],
        "player_action": {
            "action": "observe",
            "parse_status": "ok",
            "succeeded": True,
            "result_summary": "The player looks around.",
        },
    },
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

Director `authoritative_input` must validate as the real, production
`app.schemas.director.DirectorInput` contract (`current_player_room_id`,
`clock_tick`, `facts`, `npcs: list[DirectorNPCContext]`, and
`player_action: DirectorPlayerActionContext`). Narrator `authoritative_input`
must validate as the real, production
`app.agents.narrator.NarratorAgentInput` contract (`player_message`,
`scene_context`, and optionally `recent_turns`, `campaign_summary`,
`relevant_memories`, `parsed_action`, `tool_result`). The harness does not
define a second, competing input schema; scenario loading validates the
target-specific contract for every checked-in and custom scenario file, and
tests assert every checked-in scenario validates against these production
contracts.

Room, NPC, and item identifiers in the checked-in corpus are synthetic
fixture-local IDs (for example `eval_foyer`, `eval_gallery`, `eval_npc_a`,
`eval_lantern`) rather than shipped campaign content. Production schema
validation must still succeed against these synthetic values; the corpus is
never required to reference `app/game/world.py` or `app/game/npcs.py`
content.

Run the corpus offline:

```sh
python -m evals.runner
python -m evals.runner --agent director --tag movement --json
python -m evals.runner --scenario-id narrator-observable-item-state
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

Live mode requires both `AI_ENABLED=true` and a non-empty, non-whitespace
`OPENAI_API_KEY`. Missing, empty, or whitespace-only configuration fails
closed with a developer-facing error. It reuses the existing `DirectorAgent`
and `NarratorAgent` contracts and the same `ModelPolicy` the production agents
use (`ModelPolicy.director_model()` / `ModelPolicy.narrator_model()`), and
performs no gameplay or persistence mutation. Credentials alone never enable
live mode; `--live` is always required explicitly. Automated tests must mock
provider calls; normal CI never runs live mode and must not make network
calls.

A failed live call raises a sanitized `LiveEvalError` carrying only a safe
failure-type category, with no chained `__cause__`/`__context__` back to the
raw provider/agent exception. This sanitization is scoped to the eval
harness's own exception and report boundary; it does not alter or suppress
any logging `DirectorAgent`/`NarratorAgent`/the model client perform
internally, which is existing, out-of-scope production behavior.

Live results populate `ScenarioResult.model_metadata` with the target agent,
the selected model, and available provider token/usage counts (never raw
provider response content). This metadata is surfaced in both the JSON report
and the human-readable report. Offline results leave `model_metadata` empty.

## Extending the harness

Add a synthetic JSON scenario first, then add or reuse a focused deterministic
grader. Keep grader names and details explicit about what they prove. Add
model-judged checks only behind the future interface and explicit live opt-in.
Live evals can incur provider cost and can expose fixture content to a
provider, so use the smallest synthetic, reviewable fixtures possible.
