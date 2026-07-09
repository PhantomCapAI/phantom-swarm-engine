"""Lightweight input sanitization and prompt-injection mitigation.

User specs flow straight into LLM prompts, so this hardens that boundary without
being heavy-handed (the spec is *meant* to be free-form natural language):

* length-capping to bound cost and abuse,
* stripping control characters and zero-width/bidi tricks used to hide payloads,
* neutralizing the most common "ignore previous instructions / you are now …"
  injection openers by tagging them, while leaving legitimate text intact,
* wrapping the untrusted text in a clearly delimited block the system prompts
  can be told to treat as data, not instructions.

This is defense-in-depth, not a guarantee — the pipeline prompts also frame the
spec as data.
"""

from __future__ import annotations

import re
import unicodedata

import config

# Zero-width, bidi-override and other invisible characters used to smuggle text.
_INVISIBLE = re.compile(r"[​-‏‪-‮⁠-⁯﻿]")
# Control chars except tab/newline/carriage-return.
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

# Common injection openers. We don't delete the text (it may be legitimate) but
# defang the imperative so it reads as quoted content rather than a command.
_INJECTION_PATTERNS = [
    re.compile(r"(?i)\bignore\s+(all\s+)?(previous|prior|above)\s+instructions?\b"),
    re.compile(r"(?i)\bdisregard\s+(all\s+)?(previous|prior|above)\b"),
    re.compile(r"(?i)\byou\s+are\s+now\b"),
    re.compile(r"(?i)\bsystem\s*prompt\s*[:>]"),
    re.compile(r"(?i)\b(reveal|print|show)\s+(your\s+)?(system\s+prompt|instructions)\b"),
]


class InputError(ValueError):
    """Raised when input fails validation (too long, empty, etc.)."""


def sanitize_spec(spec: str, max_length: int | None = None) -> str:
    """Clean a user-provided spec. Raises :class:`InputError` if unusable."""
    if not isinstance(spec, str):
        raise InputError("spec must be a string")

    limit = max_length or config.MAX_SPEC_LENGTH
    text = unicodedata.normalize("NFKC", spec)
    text = _INVISIBLE.sub("", text)
    text = _CONTROL.sub("", text)
    text = text.strip()

    if not text:
        raise InputError("spec is empty after sanitization")
    if len(text) > limit:
        text = text[:limit]

    for pat in _INJECTION_PATTERNS:
        text = pat.sub(lambda m: f"[flagged-instruction: {m.group(0)}]", text)

    return text


def sanitize_text(text: str, max_length: int) -> str:
    """Sanitize a shorter free-text field (e.g. a swarm topic)."""
    if not isinstance(text, str):
        raise InputError("expected a string")
    cleaned = unicodedata.normalize("NFKC", text)
    cleaned = _INVISIBLE.sub("", cleaned)
    cleaned = _CONTROL.sub("", cleaned).strip()
    if not cleaned:
        raise InputError("text is empty after sanitization")
    return cleaned[:max_length]
