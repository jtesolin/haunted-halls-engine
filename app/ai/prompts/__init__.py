from __future__ import annotations

from pathlib import Path


_PROMPTS_DIR = Path(__file__).resolve().parent

narrator_prompt = (_PROMPTS_DIR / "narrator.md").read_text(encoding="utf-8")
