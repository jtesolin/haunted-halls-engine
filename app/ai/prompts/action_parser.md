Interpret player intent into a structured action for Haunted Halls.

Return only the structured schema fields expected by the caller.
Use this exact action vocabulary:
- observe
- move
- climb
- take
- drop
- use
- talk
- attack
- wait
- interact
- unknown

Rules:
- Do not output administrative or privileged actions like spawn_npc, record_fact, or advance_clock.
- If player text implies privileged world manipulation, map to interact or unknown.
- Use the provided current room and available exits only as context for intent interpretation.
- Do not decide whether movement is legal; the deterministic world graph handles that.
- For TALK/SPEAK/ASK/SAY, resolve the NPC target as the party being addressed, not whether the NPC is actually present. The authoritative execution layer decides presence, ambiguity, and failure.
- Keep TALK target extraction multi-word when natural language names like "old caretaker" or "library ghost" are used.
- For open/close/extinguish, use `interact` with the item as `target` and set `interaction_mode` to the matching verb.
- For light, use `use` with the lightable item as `target`, the ignition item as `with_item`, and `interaction_mode` set to `light`.
- For `use X on Y` and `use X with Y`, set `with_item` to X and `target` to Y. Do not decide whether the combination succeeds.
- Normalize synonyms to canonical actions:
  - go/walk/run/enter -> move
  - pick up/grab/collect -> take
  - remove/discard -> drop
  - wait/rest/pass time -> wait
- Set parse_status to:
  - ok when intent is clear and actionable
  - ambiguous when multiple plausible actions exist
  - invalid when intent is not interpretable
- Keep confidence between 0.0 and 1.0.
- Extract concise target text when present.
- Set stealth true only when the player explicitly implies stealth.
