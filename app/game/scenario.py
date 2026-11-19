"""Persistent campaign scenario: the narrative-goal seam for a future Story Director.

Ownership: this module is the single owner of scenario state (`state["scenario"]`
inside the authoritative campaign state JSON, separate from the authored
quest `state["story"]` namespace), its validation, the bounded Narrator context
projection, and progress updates. Nothing here calls a model. A future Story
Director would replace `build_story_context` / `extract_story_update` /
`apply_story_update` behind the same narrow interface.

Player-known vs hidden: `premise`, `objective`, `stakes`, revealed beats and a
concluded outcome are player-known. `hidden_truth`, unrevealed beat clues and
the outcome catalogue are hidden; they are only ever given to the Narrator as
guidance and are never returned by any API.

Progress is reported by the Narrator in a trailing `[[STORY ...]]` marker that
is always stripped from player-visible text and applied only if the whole turn
commits (the caller writes it with the turn's single atomic state update).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from app.game.campaign_state import InvalidCampaignStateError
from app.schemas.scenario import ScenarioProviderOutput

SCENARIO_VERSION = 1
STATUS_ACTIVE = "active"
STATUS_CONCLUDED = "concluded"

_ID = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
_MAX_TEXT = 200
_MIN_BEATS, _MAX_BEATS = 2, 4
_MIN_OUTCOMES, _MAX_OUTCOMES = 2, 3
# One line; closed by `]]` (extra `]` swallowed) or, if truncated, by end of line.
_MARKER = re.compile(r"\[\[\s*STORY\b[^\n]*?(?:\]\]+|$)", re.IGNORECASE | re.MULTILINE)
_BEAT_LIST = re.compile(r"beats\s*=\s*([A-Za-z0-9_]+(?:\s*,\s*[A-Za-z0-9_]+)*)", re.IGNORECASE)
_OUTCOME = re.compile(r"outcome\s*=\s*([A-Za-z0-9_]+)", re.IGNORECASE)
_FORGED = re.compile(r"\[\[(\s*STORY)", re.IGNORECASE)


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > _MAX_TEXT:
        raise ValueError(f"Scenario {name} is empty or too long.")
    return value.strip()


def _ident(value: Any, name: str) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError(f"Scenario {name} is not a valid id.")
    return value


def _flag(value: Any) -> bool:
    if not isinstance(value, bool):
        raise ValueError("Scenario beat revealed flag is not a boolean.")
    return value


def _validate(raw: Any) -> dict[str, Any]:
    """Return a normalized scenario dict or raise ValueError."""
    if not isinstance(raw, dict):
        raise ValueError("Scenario is not an object.")
    beats_raw, outcomes_raw = raw.get("beats"), raw.get("outcomes")
    if not isinstance(beats_raw, list) or not _MIN_BEATS <= len(beats_raw) <= _MAX_BEATS:
        raise ValueError("Scenario beat count is out of range.")
    if not isinstance(outcomes_raw, list) or not _MIN_OUTCOMES <= len(outcomes_raw) <= _MAX_OUTCOMES:
        raise ValueError("Scenario outcome count is out of range.")
    beats = [
        {
            "id": _ident(b.get("id"), "beat id"),
            "clue": _text(b.get("clue"), "beat clue"),
            "revealed": _flag(b.get("revealed", False)),
        }
        for b in beats_raw
        if isinstance(b, dict)
    ]
    outcomes = [
        {"id": _ident(o.get("id"), "outcome id"), "summary": _text(o.get("summary"), "outcome")}
        for o in outcomes_raw
        if isinstance(o, dict)
    ]
    if len(beats) != len(beats_raw) or len(outcomes) != len(outcomes_raw):
        raise ValueError("Scenario entries must be objects.")
    if len({b["id"] for b in beats}) != len(beats) or len({o["id"] for o in outcomes}) != len(outcomes):
        raise ValueError("Scenario ids must be unique.")
    status = raw.get("status", STATUS_ACTIVE)
    outcome_id = raw.get("outcome_id")
    if status not in (STATUS_ACTIVE, STATUS_CONCLUDED):
        raise ValueError("Scenario status is invalid.")
    if outcome_id is not None and not isinstance(outcome_id, str):
        raise ValueError("Scenario outcome id is not a string.")
    if (status == STATUS_CONCLUDED) != (outcome_id is not None) or (
        outcome_id is not None and outcome_id not in {o["id"] for o in outcomes}
    ):
        raise ValueError("Scenario outcome is inconsistent with status.")
    hook = raw.get("opening_hook")  # optional: absent on scenarios persisted before #125
    return {
        "version": SCENARIO_VERSION,
        "status": status,
        "premise": _text(raw.get("premise"), "premise"),
        "objective": _text(raw.get("objective"), "objective"),
        "stakes": _text(raw.get("stakes"), "stakes"),
        **({"opening_hook": _text(hook, "opening hook")} if hook is not None else {}),
        "hidden_truth": _text(raw.get("hidden_truth"), "hidden truth"),
        "beats": beats,
        "outcomes": outcomes,
        "outcome_id": outcome_id,
    }


def scenario_from_provider(output: ScenarioProviderOutput) -> dict[str, Any]:
    """Validate generated output into persisted form; raises ValueError if unusable."""
    scenario = _validate(
        {
            "premise": output.premise,
            "objective": output.objective,
            "stakes": output.stakes,
            "opening_hook": output.opening_hook,  # required for new scenarios; blank -> fallback
            "hidden_truth": output.hidden_truth,
            "beats": [{"id": b.beat_id, "clue": b.clue} for b in output.beats],
            "outcomes": [{"id": o.outcome_id, "summary": o.summary} for o in output.outcomes],
        }
    )
    _reject_leaky_hook(scenario)
    return scenario


def _reject_leaky_hook(scenario: dict[str, Any]) -> None:
    """The hook is shown verbatim to the player, so it must not copy hidden text.

    shortcut: lexical (verbatim copy or >=3 shared distinctive words beyond the public
    fields), not semantic; upgrade only if staging shows paraphrased leaks. Runs only on
    provider output, never on persisted reads.
    """
    hook = scenario["opening_hook"]
    own = _words(hook) - _words(" ".join(scenario[k] for k in ("premise", "objective", "stakes")))
    hidden = [scenario["hidden_truth"]]
    hidden += [b["clue"] for b in scenario["beats"]] + [o["summary"] for o in scenario["outcomes"]]
    for text in hidden:
        copied = text.lower().rstrip(". ")
        if (len(copied) >= 15 and copied in hook.lower()) or len(own & _words(text)) >= 3:
            raise ValueError("Scenario opening hook leaks hidden content.")


def build_fallback_scenario() -> dict[str, Any]:
    """Deterministic scenario used for legacy campaigns and failed generation."""
    return _validate(
        {
            "premise": "A cold draught has woken the sleeping house, and something within it wants to be found.",
            "objective": "Find what the wronged spirit lost in these halls and return it before the house claims you.",
            "stakes": "If the spirit's loss stays unanswered, the house will keep you and its restless dead forever.",
            "opening_hook": "A faded note pinned to the door asks you to return what the house took from someone, before the dark claims you both.",
            "hidden_truth": "A wronged former resident lingers here, bound to an unfinished matter that the halls remember.",
            "beats": [
                {"id": "first_sign", "clue": "Someone has recently disturbed the house's dust and left a trace of their passing."},
                {"id": "buried_record", "clue": "A record or keepsake names the wronged resident and what was taken from them."},
                {"id": "unfinished_matter", "clue": "The haunting repeats a single act that only the player's choice can finish or end."},
            ],
            "outcomes": [
                {"id": "peace_restored", "summary": "The wrong is righted and the spirit rests."},
                {"id": "house_claims_you", "summary": "The player fails or flees and the house keeps its hold."},
                {"id": "uneasy_truce", "summary": "A bargain or compromise leaves the haunting bound but quiet."},
            ],
        }
    )


def read_scenario(state: dict[str, Any]) -> dict[str, Any] | None:
    """Validated scenario, None if absent.

    Structurally malformed data raises InvalidCampaignStateError; absent optional
    fields (status, revealed) take defaults and `version` is normalized.
    """
    raw = state.get("scenario")
    if raw is None:
        return None
    try:
        return _validate(raw)
    except ValueError:
        raise InvalidCampaignStateError("Persisted scenario state is malformed.") from None


def ensure_scenario(state: dict[str, Any]) -> bool:
    """Lazily give a legacy campaign a scenario. True if state changed."""
    if read_scenario(state) is not None:
        return False
    state["scenario"] = build_fallback_scenario()
    return True


def build_story_context(state: dict[str, Any]) -> str | None:
    """Bounded Narrator guidance (hidden facts included; the Narrator must not disclose them)."""
    scenario = read_scenario(state)
    if scenario is None:
        return None
    lines = [
        f"Premise: {scenario['premise']}",
        f"Player objective: {scenario['objective']}",
        f"Stakes: {scenario['stakes']}",
    ]
    revealed = [b for b in scenario["beats"] if b["revealed"]]
    if revealed:
        lines.append("Already discovered by the player: " + " | ".join(b["clue"] for b in revealed))
    if scenario["status"] == STATUS_CONCLUDED:
        summary = next(o["summary"] for o in scenario["outcomes"] if o["id"] == scenario["outcome_id"])
        lines.append(f"STORY CONCLUDED ({scenario['outcome_id']}): {summary}")
        lines.append("Do not report story progress. Play on as a quiet epilogue; do not reopen the main conflict.")
        return "\n".join(lines)
    lines.append(f"HIDDEN truth (never state it; let discovery earn it): {scenario['hidden_truth']}")
    pending = [b for b in scenario["beats"] if not b["revealed"]]
    if pending:
        lines.append("HIDDEN undiscovered beats (id: clue):")
        lines += [f"- {b['id']}: {b['clue']}" for b in pending]
    lines.append("HIDDEN possible outcomes (id: summary); others that fit the player's choices are fine:")
    lines += [f"- {o['id']}: {o['summary']}" for o in scenario["outcomes"]]
    return "\n".join(lines)


_WORD = re.compile(r"[a-z]{4,}")


# Generic words that say nothing about a specific objective.
_STOP = frozenset(
    "find what these this that with from your have been will before after into over "
    "return learn house halls hall home room rooms them they their there where which "
    "when while must keep bring".split()
)


def _words(text: str) -> set[str]:
    return set(_WORD.findall(text.lower())) - _STOP


def build_opening_directive(state: dict[str, Any]) -> str:
    """Player-known setup the creation-time opening must dramatize in-world."""
    scenario = read_scenario(state)
    if scenario is None:
        return ""
    hook = scenario.get("opening_hook")
    delivery = (
        f" Build the opening around this in-world hook, using only people and props present in "
        f"the authoritative scene (otherwise deliver it as a found note or a witnessed event): {hook}"
        if hook
        else " Have the player learn it through a plausible in-world cause (a request, message or "
        "witnessed event) from someone or something in the scene."
    )
    return (
        " The opening must make the player aware of their concrete objective "
        f"({scenario['objective']}) and what is at stake ({scenario['stakes']}) as part of the "
        "scene, never as a narrator instruction like 'your task is'; do not script steps."
        + delivery
        + " Never reveal hidden truths, undiscovered clues or possible outcomes."
    )


def _covered(opening: str, text: str) -> bool:
    words = _words(text)
    return bool(words) and len(words & _words(opening)) >= min(2, len(words))


def ground_opening(opening: str, state: dict[str, Any]) -> str:
    """Deterministic backstop: if the opening conveys neither objective nor hook, insert the hook.

    The hook is authored as in-world narration, so it is inserted before the closing
    paragraph (the player invitation) rather than spliced on as a meta directive; a
    single-paragraph opening gets it prepended so the invitation stays last. Scenarios
    without a hook (legacy) are left unchanged.

    shortcut: lexical overlap check (>=2 shared words), not semantic, and zero extra model
    calls; upgrade to a validating/regenerating model call only if staging shows
    incoherent or false-pass openings.
    """
    scenario = read_scenario(state)
    hook = scenario.get("opening_hook") if scenario else None
    if scenario is None or not hook or _covered(opening, scenario["objective"]) or _covered(opening, hook):
        return opening
    head, sep, tail = opening.rstrip().rpartition("\n\n")
    return f"{head}\n\n{hook}\n\n{tail}" if sep else f"{hook}\n\n{opening.strip()}"


@dataclass
class StoryUpdate:
    beats: list[str] = field(default_factory=list)
    outcome: str | None = None


def extract_story_update(reply: str) -> tuple[str, StoryUpdate]:
    """Strip every STORY marker from the reply and parse the last one."""
    update = StoryUpdate()
    matches = list(_MARKER.finditer(reply))
    if matches:
        body = matches[-1].group(0)
        beats = _BEAT_LIST.search(body)
        outcome = _OUTCOME.search(body)
        if beats:
            update.beats = [b.strip().lower() for b in beats.group(1).split(",") if b.strip()]
        if outcome:
            update.outcome = outcome.group(1).lower()
    return _MARKER.sub("", reply).strip(), update


def apply_story_update(state: dict[str, Any], update: StoryUpdate) -> bool:
    """Apply known beat ids / a known outcome id; unknown ids are ignored. True if changed."""
    scenario = read_scenario(state)
    if scenario is None or scenario["status"] == STATUS_CONCLUDED:
        return False
    changed = False
    for beat in scenario["beats"]:
        if beat["id"] in update.beats and not beat["revealed"]:
            beat["revealed"] = True
            changed = True
    if update.outcome in {o["id"] for o in scenario["outcomes"]}:
        scenario["status"] = STATUS_CONCLUDED
        scenario["outcome_id"] = update.outcome
        changed = True
    if changed:
        state["scenario"] = scenario
    return changed




def neutralize_story_markers(text: str) -> str:
    """Defang `[[STORY` in untrusted text so a player cannot forge progress."""
    return _FORGED.sub(r"[ [\1", text)


def redact_state_for_memory(campaign_state: str) -> str:
    """Campaign state JSON with only player-known scenario fields.

    Memory agents persist text that is later fed back to the Narrator without
    HIDDEN labels, so hidden truth, undiscovered clues and the outcome
    catalogue must never reach them. Unparseable input becomes `{}` and an
    invalid scenario is dropped entirely.
    """
    try:
        state = json.loads(campaign_state)
    except (TypeError, ValueError):
        return "{}"  # unparseable: nothing safe to forward
    if not isinstance(state, dict) or "scenario" not in state:
        return campaign_state
    try:
        scenario = _validate(state["scenario"])
    except ValueError:
        scenario = None
    if scenario is None:
        del state["scenario"]
    else:
        known: dict[str, Any] = {
            "premise": scenario["premise"],
            "objective": scenario["objective"],
            "stakes": scenario["stakes"],
            "discovered_clues": [b["clue"] for b in scenario["beats"] if b["revealed"]],
        }
        if scenario["status"] == STATUS_CONCLUDED:
            known["outcome"] = next(
                o["summary"] for o in scenario["outcomes"] if o["id"] == scenario["outcome_id"]
            )
        state["scenario"] = known
    return json.dumps(state)
