"""Matching user-typed references (item codes, location names, PO numbers) against known records.

Two levels, deliberately different:

- same_key: a reference that equals exactly one record once case and punctuation are ignored
  ("bo-mc-0172", "PO 2026 0057") names that record unambiguously, so it is resolved.
- close_matches: anything else is never resolved. Similar records are only suggested, so the
  user (through the assistant) chooses; a typo can never silently select a different record.
"""

import difflib
import re
from collections.abc import Iterable, Sequence

# difflib ratio of the normalized strings. At 0.75 three quarters of the characters line up in order:
# "BO-MC-172" / "BO-MC-0172" (0.93) and "PO 0057" / "PO-2026-0057" (0.75) qualify, while neighbouring
# numbers such as "PO-2026-0056" for "PO 0057" (0.63) do not.
MIN_SIMILARITY = 0.75
# Suggestions clearly worse than the best one are noise: only those within this margin of it are kept,
# so "BO-MC-172" suggests BO-MC-0172 alone, while "bearing 600" offers both 6004 and 6905.
MAX_BEHIND_BEST = 0.1
MAX_SUGGESTIONS = 3
MAX_LABEL_DETAIL = 40

_NOISE = re.compile(r"[^0-9A-Z]+")
_DIGITS = re.compile(r"\d+")


def key(text: str) -> str:
    return _NOISE.sub("", str(text).upper())


def same_key(ref: str, codes: Iterable[str]) -> str | None:
    """The one code equal to `ref` ignoring case and punctuation, or None if none or several are."""
    wanted = key(ref)
    found = [code for code in codes if wanted and key(code) == wanted]
    return found[0] if len(found) == 1 else None


def close_matches(ref: str, candidates: Sequence[tuple[str, str | None]], *, number_suffix: bool = False) -> list[str]:
    """Up to MAX_SUGGESTIONS labels of candidates similar to `ref`, best first.

    A candidate is (code, detail); code and detail are both compared, and the label is "code (detail)".
    With number_suffix, a code whose last number equals the reference's last number also qualifies
    ("PO 57" -> "PO-2026-0057").
    """
    wanted = key(ref)
    last = _DIGITS.findall(ref)
    scored = []
    for code, detail in candidates:
        score = max(
            difflib.SequenceMatcher(None, wanted, key(text)).ratio() for text in (code, detail) if text
        )
        if number_suffix and last:
            code_numbers = _DIGITS.findall(code)
            if code_numbers and int(code_numbers[-1]) == int(last[-1]):
                score = max(score, MIN_SIMILARITY)
        if score >= MIN_SIMILARITY:
            scored.append((-score, code, detail))
    scored.sort()
    best = -scored[0][0] if scored else 0
    return [_label(code, detail) for score, code, detail in scored[:MAX_SUGGESTIONS] if -score >= best - MAX_BEHIND_BEST]


def suggestion(labels: list[str]) -> str:
    """Text appended to a not-found message."""
    if not labels:
        return ""
    if len(labels) == 1:
        return f". Did you mean {labels[0]}? Confirm with the user before using it"
    return f". Close matches: {'; '.join(labels)}. Ask the user which one they meant"


def _label(code: str, detail: str | None) -> str:
    if not detail or key(detail) == key(code):
        return code
    detail = detail if len(detail) <= MAX_LABEL_DETAIL else detail[: MAX_LABEL_DETAIL - 1] + "…"
    return f"{code} ({detail})"
