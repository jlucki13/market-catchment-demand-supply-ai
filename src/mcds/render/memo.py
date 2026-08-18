"""Render the scorecard and findings into a memo a buyer can act on.

Templated in plain Python rather than Jinja: the memo's structure is fixed, and
the parts that vary are the model's prose, which arrives already written. Adding
a template engine here would buy nothing and add a dependency to the render path.

Ordering is a deliberate choice. Caveats come before conclusions, not after. A
reader who stops halfway through a market memo has read the part that says the
supply census was incomplete, rather than the part that says the market looks
underserved.
"""

from __future__ import annotations

from typing import Any

VERDICT_LABEL = {
    "oversupplied": "Oversupplied",
    "balanced": "Balanced",
    "underserved": "Underserved",
    "suppressed": "No verdict (benchmark unsourced)",
}

SEVERITY_ORDER = {"critical": 0, "material": 1, "watch": 2, "favorable": 3}
SEVERITY_LABEL = {
    "critical": "CRITICAL",
    "material": "MATERIAL",
    "watch": "WATCH",
    "favorable": "FAVORABLE",
}


def _interval(est: dict[str, Any] | None, unit: str = "") -> str:
    """Format a value with its interval. Never prints a bare point estimate."""
    if not est:
        return "n/a"
    value = est.get("value")
    lo, hi = est.get("lo"), est.get("hi")
    if value is None:
        return "n/a"
    base = f"{value:,.0f}{unit}"
    if lo is None or hi is None:
        return base
    return f"{base} (90% CI {lo:,.0f}–{hi:,.0f})"


def render_markdown(
    scorecard: dict,
    findings: dict | None = None,
    *,
    deal: dict | None = None,
    catchment: dict | None = None,
    review: dict | None = None,
) -> str:
    d, s, b = scorecard["demand"], scorecard["supply"], scorecard["balance"]
    lines: list[str] = []
    a = lines.append

    title = (deal or {}).get("label") or scorecard["deal_id"]
    a(f"# Market Read: {title}")
    a("")
    if deal:
        a(f"**Address:** {deal.get('address', 'n/a')}  ")
    if catchment:
        traffic = "traffic-aware" if catchment.get("traffic_aware") else "free-flow (no live traffic)"
        a(f"**Catchment:** {catchment['minutes']}-minute {catchment['profile']} isochrone, {traffic}  ")
    a(f"**Vertical:** {scorecard['vertical']}  ")
    a(f"**Computed:** {scorecard['computed_at']}")
    a("")

    # Caveats first, on purpose.
    if scorecard.get("warnings"):
        a("## Read this first")
        a("")
        a("These limits bound everything below.")
        a("")
        for w in scorecard["warnings"]:
            a(f"- {w}")
        a("")

    a("## Headline")
    a("")
    if findings and findings.get("headline"):
        a(findings["headline"])
    else:
        a(
            f"{_interval(d['qualified_households'])} qualified households against "
            f"{s['index']:.1f} effective competitors."
        )
    a("")

    a("## Scorecard")
    a("")
    a("| Measure | Value |")
    a("| --- | --- |")
    a(f"| Total households in catchment | {_interval(d['total_households'])} |")
    a(f"| Qualified households (demand) | {_interval(d['qualified_households'])} |")
    a(f"| Qualified share | {d['qualified_share']:.1%} |")
    a(f"| Businesses examined | {s['competitor_count']} |")
    a(f"| Effective competitors (supply) | {s['index']:.2f} |")
    a(
        f"| Households per effective competitor | "
        f"{_interval(b['households_per_effective_competitor'])} |"
    )
    bench = b.get("benchmark") or {}
    if bench.get("households_per_location"):
        a(
            f"| Benchmark (households per location) | {bench['households_per_location']:,.0f} "
            f"({bench.get('confidence', 'unknown')} confidence) |"
        )
    if b.get("ratio") is not None:
        a(f"| Saturation ratio | {b['ratio']:.2f} |")
    a(f"| **Verdict** | **{VERDICT_LABEL.get(b['verdict'], b['verdict'])}** |")
    a("")
    if b.get("verdict_reason"):
        a(b["verdict_reason"])
        a("")

    a("### Supply composition")
    a("")
    for level in ("direct", "partial", "adjacent", "none"):
        n = (s.get("by_substitutability") or {}).get(level, 0)
        if n:
            a(f"- **{level}**: {n}")
    a("")
    if s.get("strongest"):
        a("Strongest competitors by weighted strength:")
        a("")
        a("| Competitor | Weight | Drive time |")
        a("| --- | --- | --- |")
        for c in s["strongest"][:5]:
            t = c.get("drive_time_minutes")
            a(f"| {c.get('name') or c['place_id']} | {c['weight']:.2f} | {t if t is None else f'{t:.1f} min'} |")
        a("")

    geo = scorecard.get("geography") or {}
    if geo.get("clusters"):
        a("### Clustering")
        a("")
        for c in geo["clusters"]:
            mean = c.get("mean_drive_time_minutes")
            mean_s = "n/a" if mean is None else f"{mean:.1f} min"
            a(
                f"- Cluster {c['cluster_id']}: {c['size']} locations, combined weight "
                f"{c['total_weight']:.2f}, mean drive time {mean_s}"
            )
        if geo.get("unclustered"):
            a(f"- {len(geo['unclustered'])} location(s) outside any cluster")
        a("")

    if findings and findings.get("flags"):
        a("## Flags")
        a("")
        for flag in sorted(findings["flags"], key=lambda f: SEVERITY_ORDER.get(f.get("severity", "watch"), 9)):
            a(f"**[{SEVERITY_LABEL.get(flag['severity'], flag['severity'].upper())}]** {flag['statement']}")
            a("")
            a(f"*Confidence: {flag['confidence']}.* Evidence: " + ", ".join(
                f"`{e['source_ref']}` = {e['value']}" for e in flag.get("evidence", [])
            ))
            a("")
            a(f"> Would be overturned by: {flag['falsifier']}")
            a("")

    if findings and findings.get("claim_verdicts"):
        a("## Seller's claims")
        a("")
        a("| Claim | Verdict | Computed | Notes |")
        a("| --- | --- | --- | --- |")
        for cv in findings["claim_verdicts"]:
            a(
                f"| {cv['claim_id']} | {cv['verdict'].replace('_', ' ')} | "
                f"{cv.get('computed_actual', '-')} | {cv['rationale']} |"
            )
        a("")

    if findings and findings.get("underserved_pockets"):
        a("## Underserved pockets")
        a("")
        for p in findings["underserved_pockets"]:
            a(f"- {p['description']} *(confidence: {p['confidence']})*")
        a("")

    if findings and findings.get("memo_markdown"):
        a("## Analysis")
        a("")
        a(findings["memo_markdown"])
        a("")

    if review and review.get("challenges"):
        a("## Review challenges")
        a("")
        a("An independent model reviewed the findings above. Unresolved challenges:")
        a("")
        for ch in review["challenges"]:
            a(f"- {ch}")
        a("")

    if findings and findings.get("data_gaps"):
        a("## What this analysis cannot see")
        a("")
        for gap in findings["data_gaps"]:
            a(f"- {gap}")
        a("")

    a("---")
    a("")
    a(
        "Competitor data from Google Maps Platform, retrieved live at run time and "
        "not warehoused. Demographics from the U.S. Census Bureau American Community "
        "Survey 5-year estimates; margins of error are at 90% confidence. Drive-time "
        "catchment from Mapbox. Every figure in this memo is computed from those "
        "sources and traceable to a scorecard field."
    )
    return "\n".join(lines)
