# Generated traversal (8F4)

## Bounded mechanics

Each explicit invocation crosses **one authored local route** and ends at its
stable destination. The player ToolExecutor owns eligibility and location
mutation; neither the provider, Director, nor Narrator adjudicates success.
Definitions persist their finite method and (for jumps only) fixed three-metre
reach. No ongoing status, durations, horizontal free flight, teleportation,
phasing, swimming, underwater access, or hazard immunity is granted.

Routes declare origin/destination, unique ID, discoverable name/description,
kind, integer crossing length (1–12 metres), explicit takeoff/landing validity,
and finite path properties. Vertical length means rise/drop; other lengths
mean the authored crossing distance, not coordinate physics. Both endpoints
must exist and differ. Unsupported combinations and malformed content fail
authored validation. At most six routes are exposed per origin.

| Method | Eligible authored constraints |
| --- | --- |
| Levitation | Vertical ascent/descent with a clear path and stable endpoints |
| Spider climb | Vertical/surface route with a continuous supporting surface |
| Supernatural jump | Gap or clear vertical route, stable endpoints, length ≤3 m |
| Water walking | Water route with a water surface and stable endpoints |

All methods use the same route/capability validator. Unknown, ambiguous,
nonlocal, incompatible, unstable, out-of-reach, or unowned requests cannot
move the player. A grounded incompatible request is a typed gameplay failure,
not a schema/HTTP failure. Destination references matching multiple routes
require disambiguation by route ID/name.

## Generation and compatibility

The real Structured Outputs contract has two choice slots, each accepting
sensing, object, or traversal variants. Conversion derives mechanics; exactly
two mechanically distinct choices pass new-generation validation. The model
is not promised to select uniformly. Cosmetic names/descriptions and track
labels do not distinguish identical effects. Existing collision rules and
baseline availability remain enforced.

Saved 8F2/8F3 definitions load unchanged, without regeneration or backfill.
Generic legacy effects retain their unsupported behavior. New signatures and
thematic-name requirements are not retroactively applied to old saves.
No database migration or additional ordinary-turn mechanics call is needed.
Public campaign/chat response contracts are unchanged.

## Demonstration

Entry Hall → north → Grand Corridor → west → Dining Room → south → Rain Court.
Five restricted routes are discoverable here, independently of owned starters:

| Route | Destination | Length | Eligible methods |
| --- | --- | --- | --- |
| Gallery Ascent | Upper Gallery | 2 m vertical | Levitation, climb, jump |
| Ivy Wall | Upper Gallery | 5 m supported surface | Climb |
| Channel Gap | Far Bank | 3 m gap | Jump |
| Water Crossing | Far Bank | 6 m water surface | Water walking |
| Wide Gap | Far Bank | 5 m gap | None of the starters; jump is out of reach |

The ordinary `stairs` and `bridge` exits reach the same destinations and lead
back. Route names/IDs are not walking exits. Special routes never add sensing
or NPC/Director adjacency. Library access and its quest remain unchanged;
no starter pair blocks essential progress or strands the player.

## Provider-free validation

- `tests/test_traversal.py`: finite method matrix, endpoint/path/reach failures,
  unknown/ambiguous/remote/unowned requests, explicit occurrence grounding and
  route collisions, separate walking/NPC/sensing adjacency, compatibility,
  conversion/reload, summaries, and actual executor movement.
- `tests/test_starter_ability_provider_contract.py`: real OpenAI SDK Structured
  Outputs with mocked HTTP for every new method and existing object variants.
- `tests/test_orchestrator_story_progression.py`: new-campaign persistence,
  actual parser/executor turns for every method, room-entry Library progression,
  final Director/Narrator context, failure without movement/progression,
  Director/Narrator rollback, same-key retry, and completed-request replay.
- `tests/test_generated_abilities.py`: existing 8F2/8F3 and item grounding
  regressions. `tests/test_evals.py`: focused success/failure narrator fixtures
  and representative contradictory-output negatives.

## Manual staging walkthrough — UNPERFORMED

1. In staging, create a **new** campaign; record its two generated names and
   engine-derived meanings. Do not rewrite an existing campaign or force a
   provider choice. Generation may select no traversal or only some methods.
2. Follow the ordinary path above; ask to look around. Confirm route names,
   destinations, and limitations are discoverable separately from exits.
3. For any owned traversal method, explicitly name the ability and route in
   natural language, e.g. “Could my ability NAME carry me along Gallery Ascent?”
   Choose an eligible row above. Confirm arrival/landing and destination scene;
   refresh/reload the campaign to confirm persisted location.
4. Return by ordinary stairs/bridge. Try a locally grounded incompatible row;
   confirm in-world failure and no arrival. With a jump, try Wide Gap and
   confirm its reach limit. Try an ambiguous destination such as Upper Gallery
   and confirm no crossing; then name the route to disambiguate.
5. Confirm ordinary stairs/bridge access and escape without abilities; return
   north/east from the court/dining room to the corridor and continue the
   Library quest normally.
6. Where the staging caller supports an idempotency key, resend a completed
   crossing with the **same** key: expect the same response, not another move.
   Do not induce live provider failures to test rollback.

Methods not selected in that live campaign, bad endpoints, rollback/retry,
and exact-once room-entry behavior are covered deterministically by the tests
above. These instructions are not evidence that staging was exercised.
Crafting/alchemy belongs to a later phase; #52 remains open.
