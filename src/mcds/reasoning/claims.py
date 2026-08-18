"""Turn the seller's story into propositions that can be checked against data.

v1 takes claims already structured (examples/sample_deal.yaml). This stage
exists for two jobs: normalising a hand-entered claim into the `parsed` shape
the reconciliation reads, and -- in v2 -- extracting claims from a CIM PDF.

The v2 form is why this is a Sonnet 5 stage with a 1M context rather than a
regex: a CIM buries its testable assertions in prose across forty pages, and the
useful ones are rarely in a table.
"""

from __future__ import annotations

from .client import RoutingConfig

SYSTEM_PROMPT = """\
Extract testable claims about the customer base and market from the seller's \
material. A claim is testable when it asserts something a demographic or \
competitive dataset could confirm or contradict: who the customers are, where \
they come from, how many there are, how many competitors exist.

Quote the seller verbatim. A paraphrase loses the hedging, and the hedging is \
often the whole point -- "most of our customers are families" and "our customer \
is a family with $90k household income" are different claims with different \
burdens of proof.

Do not extract financial claims (revenue, SDE, margins). Those are checked \
against the books, not against census data, and mixing them in produces verdicts \
this tool has no basis to give.

Where the seller states a catchment ("we draw from a 5-mile radius"), capture it \
as a catchment_extent claim. It is the most directly checkable claim in most \
listings and the one most often wrong.
"""


def extract_claims(
    document_text: str,
    routing: RoutingConfig,
    *,
    api_key: str | None = None,
) -> dict:
    """v2: pull claims from CIM text into schemas/seller_claims.schema.json shape."""
    raise NotImplementedError


def reconcile(claims: dict, scorecard: dict, demographics: dict) -> list[dict]:
    """Deterministic pre-computation of what each claim should be tested against.

    Runs BEFORE synthesis and in code, not in a model: mapping a claim of kind
    `customer_income` to the income distribution and computing the share of the
    catchment inside the claimed band is arithmetic, and arithmetic belongs here.
    The model's job is deciding what the gap means, not measuring it.

    Includes the implied-capture-rate inversion for volume claims:

        implied_capture_rate = claimed_customers_per_period
                               / qualified_households_in_catchment

    A claimed volume needing 40% of every qualified household in the catchment to
    be a regular customer is not impossible, but it is a question for management,
    and it is invisible until the two numbers are put side by side.
    """
    raise NotImplementedError
