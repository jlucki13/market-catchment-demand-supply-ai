"""Adversarial review -- a second model trying to break the first one's memo.

Provenance checking catches fabricated numbers. It cannot catch a real number
used to support a conclusion it does not support, which is the subtler and more
dangerous failure: every figure traces, every citation resolves, and the
inference between them does not hold.

Runs on Opus 5, deliberately a different model from the one that wrote the memo.
A model reviewing its own output tends to ratify it.
"""

from __future__ import annotations

from .client import RoutingConfig, cached_system, call_structured

SYSTEM_PROMPT = """\
You are reviewing a draft market memo before it reaches an acquisition buyer. \
Assume the numbers are arithmetically correct -- that has been checked \
mechanically. Your job is the reasoning between them.

For each flag, ask:
  - Does the cited evidence actually support the stated conclusion, or merely \
sit near it?
  - Would the opposite conclusion survive the same evidence?
  - Does a warning in the scorecard undercut this flag in a way the flag does \
not acknowledge?
  - Is the confidence level defensible given the margins of error involved?
  - Is the falsifier real -- something a buyer could go and check -- or is it \
unfalsifiable in practice?

Look hardest for the failure that matters most here: a confident conclusion \
resting on a proxy that cannot bear it. Review count standing in for revenue. \
Five reviews standing in for business age. A benchmark describing the county's \
current equilibrium standing in for a viability threshold. Each of those is a \
legitimate signal and none supports a strong claim on its own.

Also check what is missing. A memo that flags four risks and omits the one \
visible in the data is worse than one that flags nothing, because it reads as \
thorough.

Return specific challenges tied to flag indices. Do not rewrite the memo. Where \
a flag holds up, say so briefly and move on -- an inflated challenge list is as \
unhelpful as an empty one.
"""


REVIEW_SCHEMA = {
    "type": "object",
    "required": ["challenges", "omissions", "sustained"],
    "additionalProperties": False,
    "properties": {
        "challenges": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["flag_index", "challenge", "severity"],
                "additionalProperties": False,
                "properties": {
                    "flag_index": {"type": "integer"},
                    "challenge": {"type": "string", "maxLength": 600},
                    "severity": {
                        "type": "string",
                        "enum": ["undermines", "weakens", "nitpick"],
                    },
                },
            },
        },
        "omissions": {
            "type": "array",
            "description": "Findings visible in the data that the memo missed.",
            "items": {"type": "string"},
        },
        "sustained": {
            "type": "array",
            "description": "Flag indices that hold up under challenge.",
            "items": {"type": "integer"},
        },
    },
}


def review(
    findings: dict,
    scorecard: dict,
    routing: RoutingConfig,
    *,
    api_key: str | None = None,
) -> tuple[dict, list[str]]:
    """Challenge the memo's reasoning with an independent model.

    Challenges are surfaced in the memo's own review section rather than
    silently applied. A buyer benefits from seeing that a flag was contested and
    why; a memo quietly edited to survive review has lost the disagreement that
    made it worth reading.
    """
    import json

    payload = {
        "findings": findings,
        "scorecard_warnings": scorecard.get("warnings", []),
        "balance": scorecard.get("balance"),
        "demand": scorecard.get("demand"),
        "supply": scorecard.get("supply"),
    }
    result = call_structured(
        "review", routing,
        system=cached_system(SYSTEM_PROMPT),
        user="Review this draft.\n\n" + json.dumps(payload, indent=2, default=str),
        schema=REVIEW_SCHEMA, api_key=api_key,
    )

    warnings: list[str] = []
    undermining = [
        c for c in result.get("challenges", []) if c.get("severity") == "undermines"
    ]
    if undermining:
        warnings.append(
            f"The review stage judged {len(undermining)} flag(s) to be "
            f"undermined by the evidence they cite. Those challenges appear in "
            f"the memo and were not silently resolved."
        )
    if result.get("omissions"):
        warnings.append(
            f"The review stage identified {len(result['omissions'])} finding(s) "
            f"visible in the data that the memo did not raise."
        )
    return result, warnings
