"""The honesty rail: no number reaches the memo unless the scorecard produced it.

A language model asked to write a market memo will, if left alone, produce
numbers that read as computed but were inferred from the shape of the sentence.
In a diligence document that is the worst possible failure, because a fabricated
figure is indistinguishable from a real one at the point of reading.

The defence is structural rather than instructional. The synthesis stage may
only cite fields by their dotted path into the scorecard, and before anything
renders, this module re-derives the set of numbers the scorecard actually
contains and checks the memo against it. A memo containing a figure with no
computed origin does not render.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable

#: Bare integers a memo may use without a scorecard origin: small counts,
#: list ordinals, and the like. Kept tight on purpose.
SMALL_INTEGER_ALLOWANCE = 12

#: Relative and absolute tolerance for matching a memo figure to a computed one,
#: so rounding a value for readability is not treated as fabrication.
REL_TOLERANCE = 0.005
ABS_TOLERANCE = 0.5

_NUMBER_RE = re.compile(
    r"""
    (?<![\w.])                 # not mid-identifier
    \$?                        # optional currency
    (\d{1,3}(?:,\d{3})+        # 12,400
     |\d+(?:\.\d+)?)           # 12400 or 3.2
    \s?(%|x|k|K|M)?            # optional unit suffix
    """,
    re.VERBOSE,
)


@dataclass(frozen=True)
class Violation:
    kind: str          # "unresolved_ref" | "untraceable_number" | "missing_falsifier"
    detail: str
    location: str | None = None


def flatten(obj: Any, prefix: str = "") -> dict[str, Any]:
    """Dotted-path view of a nested structure.

    Lists are indexed (`supply.strongest.0.weight`) so a memo can cite a specific
    competitor's weight and the citation still resolves.
    """
    out: dict[str, Any] = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.update(flatten(v, f"{prefix}.{k}" if prefix else str(k)))
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            out.update(flatten(v, f"{prefix}.{i}"))
    else:
        out[prefix] = obj
    return out


def numeric_values(flat: dict[str, Any]) -> set[float]:
    """Every number the scorecard contains, plus the readable forms of each.

    A memo will legitimately write 0.084 as "8.4%" and 12,431 as "about 12,400".
    Both are the same computed value, so percentage and rounded variants are
    admitted; a figure that matches none of them had no computed origin.
    """
    values: set[float] = set()
    for v in flat.values():
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            continue
        f = float(v)
        values.add(f)
        values.add(f * 100.0)          # share rendered as a percentage
        for places in (0, 1, 2):
            values.add(round(f, places))
            values.add(round(f * 100.0, places))
        for unit in (100, 1000):        # "about 12,400" / "roughly 12,000"
            values.add(round(f / unit) * unit)
    return values


def _matches(candidate: float, allowed: Iterable[float]) -> bool:
    for a in allowed:
        if abs(candidate - a) <= max(ABS_TOLERANCE, abs(a) * REL_TOLERANCE):
            return True
    return False


def check_source_refs(findings: dict, scorecard: dict) -> list[Violation]:
    """Every evidence citation and claim verdict must resolve to a real field."""
    flat = flatten(scorecard)
    violations: list[Violation] = []

    for i, flag in enumerate(findings.get("flags", []) or []):
        for j, ev in enumerate(flag.get("evidence", []) or []):
            ref = ev.get("source_ref")
            if ref not in flat:
                violations.append(
                    Violation(
                        "unresolved_ref",
                        f"source_ref '{ref}' does not exist in the scorecard",
                        f"flags.{i}.evidence.{j}",
                    )
                )
        if not (flag.get("falsifier") or "").strip():
            violations.append(
                Violation(
                    "missing_falsifier",
                    "flag has no falsifier; every flag must state what would overturn it",
                    f"flags.{i}",
                )
            )

    for i, cv in enumerate(findings.get("claim_verdicts", []) or []):
        ref = cv.get("tested_against")
        if cv.get("verdict") != "untestable" and ref not in flat:
            violations.append(
                Violation(
                    "unresolved_ref",
                    f"claim verdict cites '{ref}', which is not a scorecard field",
                    f"claim_verdicts.{i}",
                )
            )
    return violations


def check_memo_numbers(memo_markdown: str, scorecard: dict) -> list[Violation]:
    """Flag any figure in the memo prose with no computed origin."""
    allowed = numeric_values(flatten(scorecard))
    violations: list[Violation] = []
    seen: set[str] = set()

    for match in _NUMBER_RE.finditer(memo_markdown or ""):
        raw, suffix = match.group(1), match.group(2)
        token = match.group(0).strip()
        if token in seen:
            continue
        seen.add(token)

        value = float(raw.replace(",", ""))
        candidates = [value]
        if suffix == "%":
            candidates.append(value / 100.0)
        elif suffix in ("k", "K"):
            candidates.append(value * 1_000)
        elif suffix == "M":
            candidates.append(value * 1_000_000)

        if value == int(value) and 0 <= value <= SMALL_INTEGER_ALLOWANCE:
            continue
        if value == int(value) and 1900 <= value <= 2100:  # years
            continue
        if any(_matches(c, allowed) for c in candidates):
            continue

        start = max(0, match.start() - 45)
        violations.append(
            Violation(
                "untraceable_number",
                f"'{token}' appears in the memo but matches no computed value",
                f"...{memo_markdown[start:match.end() + 25].strip()}...",
            )
        )
    return violations


def verify(findings: dict, scorecard: dict, *, strict: bool = True) -> list[Violation]:
    """Run every check. With strict=True a violation stops the render."""
    violations = check_source_refs(findings, scorecard)
    violations += check_memo_numbers(findings.get("memo_markdown", ""), scorecard)
    if violations and strict:
        detail = "\n".join(f"  [{v.kind}] {v.detail}" for v in violations)
        raise ProvenanceError(
            f"{len(violations)} provenance violation(s); memo not rendered:\n{detail}"
        )
    return violations


class ProvenanceError(RuntimeError):
    """Raised when a memo cites something the scorecard cannot back."""
