"""Synthesis -- the judgment the tool exists to produce.

Everything upstream is measurement. This stage decides what the measurements
mean together, which is where a buyer actually needs help: raw demographics mean
nothing without knowing how many competitors split that demand, and competitor
density means nothing without knowing whether the catchment can support anyone.

Runs on Fable 5 at high or xhigh effort. This is long-horizon reasoning across
four sources with an expensive failure mode -- the output feeds an acquisition
decision -- which is exactly the tier's case. The prompt is deliberately
declarative rather than step-by-step: over-prescribing the procedure to this
model measurably degrades the result, so it gets the full task, the constraints,
and the output contract, and works out the path itself.

The hard constraint is the provenance rule. The model may cite only fields that
exist in the scorecard, by dotted path. provenance.verify() re-checks every
figure in the rendered prose against the computed set before anything is
written, and a memo that fails does not render.
"""

from __future__ import annotations

import json
from pathlib import Path

from ..provenance import ProvenanceError, verify
from .client import RoutingConfig, cached_system, call_structured

SCHEMA_PATH = Path(__file__).resolve().parents[3] / "schemas" / "findings.schema.json"

SYSTEM_PROMPT = """\
You are writing the market section of an acquisition diligence memo for a buyer \
evaluating a specific small business. You will receive a scorecard of computed \
figures, the competitor set with substitutability already assigned, the \
catchment demographics with margins of error, and the seller's claims.

Your job is the reasoning across those sources that none of them contains alone. \
The buyer can read a table; what they cannot do is see that a thin catchment and \
an entrenched competitor cluster and an optimistic customer-profile claim are \
the same finding viewed three ways.

Absolute constraint on figures: every number you write must come from the \
scorecard, cited by its dotted path in an evidence entry. You may round for \
readability. You may not estimate, infer, extrapolate, or introduce a figure \
from your own knowledge -- not revenue, not market size, not a national average, \
not a plausible-sounding benchmark. If a number you want does not exist in the \
scorecard, say what is missing instead of supplying it. This is checked \
mechanically after you finish and a memo containing an unsourced figure is \
rejected outright.

Read the warnings array before you write anything. It carries the caveats that \
determine how much weight the figures can bear: an undercounted supply census, a \
suppressed benchmark, margins of error wide enough to flip a verdict, a \
free-flow catchment that overstates peak reach. A memo that states a conclusion \
the warnings undercut is worse than no memo. Where a warning materially limits a \
finding, say so in the finding, not only in a caveats section at the end.

On the seller's claims: test each one against the specific field that bears on \
it and give a verdict. Where a claim is untestable with this data, say so \
plainly rather than stretching. The useful output is often not "the seller is \
wrong" but "the seller's claimed customer profile describes a small share of the \
real catchment, so either the business draws from further out than stated or the \
volume assumptions behind the earnings need explaining" -- a question for \
management, not a verdict.

Every flag needs a falsifier: what a buyer could observe that would overturn it. \
A flag nobody can check is an opinion wearing a finding's clothes.

Set confidence honestly and low where the data is thin. A buyer acting on a \
high-confidence flag that rested on five reviews and a wide margin of error is a \
worse outcome than one who was told the market read was inconclusive.

Use data_gaps for what this analysis structurally cannot see. Some of it is \
known: at most five reviews per competitor, so business age is a weak lower \
bound only; no visibility into competitors' revenue, floor space, or lease \
terms; municipal and employer facilities absent from the source data; ACS \
estimates pooled over five years, so recent neighbourhood change is invisible.
"""


def load_schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text())


def build_prompt(
    scorecard: dict,
    competitors: list[dict],
    demographics: dict,
    seller_claims: dict,
    vertical: dict,
) -> tuple[list[dict], str]:
    """Return (system_blocks, user_message).

    The rubric and vertical config cache; the per-deal payload does not. The
    scorecard goes in whole, because the model is only permitted to cite fields
    that appear in it and needs to see the full set of paths available.
    """
    stable = (
        SYSTEM_PROMPT
        + f"\n\nVertical under evaluation: {vertical.get('label')}\n"
        + f"Catchment convention: {(vertical.get('catchment') or {}).get('default_minutes')} "
        "minutes' drive, on the reasoning that trip friction at this frequency "
        "and ticket size caps how far customers realistically travel.\n"
    )
    payload = {
        "scorecard": scorecard,
        "competitors": competitors,
        "demographics": demographics,
        "seller_claims": seller_claims,
    }
    user = (
        "Produce the findings object for this deal.\n\n"
        + json.dumps(payload, indent=2, default=str)
    )
    return cached_system(stable), user


def synthesize(
    scorecard: dict,
    competitors: list[dict],
    demographics: dict,
    seller_claims: dict,
    vertical: dict,
    routing: RoutingConfig,
    *,
    api_key: str | None = None,
    max_repairs: int = 1,
) -> tuple[dict, list[str]]:
    """Produce findings conforming to schemas/findings.schema.json.

    Runs the provenance check before returning and, on violation, re-prompts
    once with the specific violations quoted. A repair round is far cheaper than
    failing a deal run, and the failure mode being repaired -- a figure with no
    computed origin -- is exactly the one a reader cannot detect unaided.
    """
    system, user = build_prompt(
        scorecard, competitors, demographics, seller_claims, vertical
    )
    schema = load_schema()
    warnings: list[str] = []

    for attempt in range(max_repairs + 1):
        findings = call_structured(
            "synthesize", routing, system=system, user=user,
            schema=schema, api_key=api_key, stream=True,
        )
        violations = verify(findings, scorecard, strict=False)
        if not violations:
            if attempt:
                warnings.append(
                    "The first synthesis draft cited figures with no computed "
                    "origin and was rejected; the memo here is a corrected "
                    "second pass."
                )
            return findings, warnings

        if attempt == max_repairs:
            detail = "; ".join(f"{v.kind}: {v.detail}" for v in violations[:5])
            raise ProvenanceError(
                f"Synthesis still cited unverifiable figures after "
                f"{max_repairs} repair attempt(s): {detail}"
            )

        user += (
            "\n\nYour previous response was rejected by the provenance check. "
            "Each item below is a figure or citation with no computed origin in "
            "the scorecard. Rewrite the findings using only values that appear "
            "in the scorecard, cited by dotted path. Where a number you wanted "
            "does not exist, say what is missing instead of supplying it.\n\n"
            + "\n".join(f"- [{v.kind}] {v.detail}" for v in violations)
        )

    raise ProvenanceError("unreachable")
