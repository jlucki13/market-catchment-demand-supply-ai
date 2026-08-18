"""Adversarial review -- a second model trying to break the first one's memo.

Provenance checking catches fabricated numbers. It cannot catch a real number
used to support a conclusion it does not support, which is the subtler and more
dangerous failure: every figure traces, every citation resolves, and the
inference between them does not hold.

Runs on Opus 5, deliberately a different model from the one that wrote the memo.
A model reviewing its own output tends to ratify it.
"""

from __future__ import annotations

from .client import RoutingConfig, build_request, cached_system, check_refusal, get_client

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


def review(
    findings: dict,
    scorecard: dict,
    routing: RoutingConfig,
    *,
    api_key: str | None = None,
) -> dict:
    """Return {"challenges": [...], "omissions": [...], "sustained": [...]}.

    Challenges are surfaced in the memo's own review section rather than silently
    applied. A buyer benefits from seeing that a flag was contested and why; a
    memo quietly edited to survive review has lost the disagreement that made it
    worth reading.
    """
    raise NotImplementedError
