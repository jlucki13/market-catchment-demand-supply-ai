# Scoring methodology

Every formula here is implemented in `src/mcds/scoring/indices.py` and pinned by
golden-number tests in `tests/test_scoring.py`. No language model participates in
any calculation on this page.

---

## Demand Index

```
DI = total_households × Π over filters d of share_d
```

Read as **qualified households**: how many households in the real drive-time
catchment plausibly belong to this business's market.

**Inputs.** ACS 5-year detail tables at block-group level, interpolated onto the
isochrone polygon (see *Interpolation* below). Which dimensions gate demand is a
per-vertical decision — laundromats key on tenure and an income *ceiling*,
fitness studios on an income *floor* and age.

**Band edge normalisation.** ACS brackets print as "$50,000 to $99,999" but are
contiguous with the next bracket at $100,000. The true span is 50,000, not the
printed 49,999. Apportioning against the printed width biases every partial band
low, and the bias compounds across a distribution — on the sample deal, fixing
it moved qualified households by 10%. Band upper edges are therefore normalised
to the next band's floor before any apportionment.

**Open-topped bands.** A filter floor landing inside "$200,000 or more" has no
honest answer: including the band whole overstates, apportioning it invents a
shape for the tail. The band is dropped and a note records that the share
understates in a known direction.

**Combination.** `multiplicative` multiplies filter shares, which assumes they
are independent. Demographic dimensions rarely are — income and age correlate,
income and tenure correlate. Two responses:

- `independence_penalty: true` widens the interval by
  `1 + 0.25 × (filters − 1)`. This is a judgment-based inflation, not a derived
  statistic, and it says so wherever it is applied.
- `combination: primary_only` uses the single most selective filter and ignores
  the rest — the conservative option when correlation is known to be strong.

**Unit mismatch.** B01001 (age) is person-level; households are not. Applying a
person-level share to a household count assumes uniform household composition
across the catchment. Configs declare `unit: person` and the mismatch produces a
warning rather than being silently absorbed.

## Supply Index

```
for each competitor i:
    s_i = substitutability   direct 1.0 · partial 0.5 · adjacent 0.15 · none 0
    e_i = entrenchment       log1p(reviews)/log1p(150) × clamp((rating−3.0)/1.5, 0.4, 1.3)
    a_i = accessibility      exp(−drive_minutes / τ),  τ = catchment_minutes / ln 2

SI = Σ (s_i × e_i × a_i)
```

Read as **effective competitor equivalents**: how many full-strength, doorstep,
direct competitors this market's supply is equivalent to. Nine listed businesses
can be an SI of 2.5 when most are weak, distant, or only adjacent substitutes.

**Substitutability** comes from the classification stage, not from Google's
`types`. A fast-casual salad counter and a steakhouse are both `restaurant`.
`adjacent` is deliberately small but non-zero: a bakery does take some coffee
trips, and rounding that to zero is as wrong as counting it in full.

**Entrenchment** uses review count as a proxy for how long and how busily a place
has operated. It is a poor proxy for revenue and is never presented as one — it
is biased by business age, category norms, and whether the operator asks for
reviews. The reference point is 150 reviews at 4.5 stars = exactly 1.0, chosen as
a solidly established local business. Log compression matters: without it a
2,000-review chain would outweigh eight independents combined, which is not how
competitive pressure works. A 10× review count produces under 1.6× the weight.

Missing rating and review count default to 0.6 — just below a typical established
competitor, because absence of reviews is not absence of business — and always
emit a warning.

**Accessibility** decays in drive-time space, calibrated so a competitor at the
catchment edge counts exactly half as much as one at the front door. Straight-line
distance is never used: a competitor two miles away across a river is not two
miles away. The `detour_ratio` (routed distance ÷ straight-line distance) is
surfaced above 1.6 as the clearest evidence that a radius-based read of this
market would be wrong.

**Chain damping.** Locations sharing an operator are damped by `k^-0.5` each, so
a chain's total weight grows as `√k` rather than `k`. Six locations of one brand
are more competitive pressure than one and much less than six.

## Balance

```
households_per_effective_competitor = DI / (SI + 1)
ratio = households_per_effective_competitor / benchmark.households_per_location
```

The `+1` is the subject business itself: the buyer is not entering an empty
market, they are becoming one of its competitors.

| Ratio | Verdict |
|---|---|
| < 0.75 | oversupplied |
| 0.75 – 1.25 | balanced |
| > 1.25 | underserved |
| no sourced benchmark | **suppressed** |

The band is wide on purpose. The inputs do not support fine discrimination, and
a narrow band would imply a precision they cannot back.

**Suppression is a feature.** An unsourced benchmark yields no verdict rather
than a plausible one. A confident "underserved" derived from an invented
denominator is the most dangerous output this tool could produce. A static
benchmark missing `source` or `source_url` is treated as absent, not trusted.

### Where the benchmark comes from

**`cbp_derived`** (default) computes it at run time:

```
households_per_location = ACS households in the county (or CBSA)
                          ÷ CBP establishments for the vertical's NAICS code
```

County Business Patterns publishes establishment counts by NAICS by county.
Dividing local households by local establishments gives what this market
currently supports *here*, rather than a national average that may describe
nowhere. Confidence scales with the establishment count backing the ratio: ≥30
high, ≥10 medium, ≥5 low, below that no benchmark at all.

**The caveat that belongs in every memo using it:** this measures the *current*
equilibrium, not a healthy one. In an already-saturated county the derived
benchmark encodes saturation as normal. A "balanced" verdict from a cbp_derived
benchmark means "typical for this county", not "viable".

## Interpolation

The catchment boundary does not respect Census geography — a 10-minute contour
will cut a block group in half, and how that split is handled decides a
meaningful share of the demand estimate.

**`population_weighted`** (preferred) intersects the block group with the polygon,
then weights by 2020 Decennial blocks whose centroids fall inside. Blocks are
small enough to track where people actually live: half a block group by area can
hold 90% of its households if the other half is a golf course.

**`areal`** falls back to the intersected area fraction, assuming uniform density
inside the block group. That is wrong wherever land use varies, and land use
always varies. Used only when block geometry is unavailable, and it warns.

Block groups contributing under 2% overlap are dropped — they add margin of error
without adding signal.

**Known understatement:** the overlap fraction is treated as exact when scaling
the margin of error, so reported intervals cover sampling error in the ACS
estimate but not error in the apportionment itself.

## Geography

**Clusters** — DBSCAN over competitor locations, radius per vertical
(`clustering.eps_km`). DBSCAN rather than k-means because the question is "is
there a corridor", not "split these into k groups": corridors are dense,
arbitrarily shaped, and their number is what we are trying to discover.

Density sensitivity is real and handled explicitly. At 0.8 km every competitor in
a dense urban catchment lands in one cluster, which tells you nothing. A cluster
holding more than 70% of all competitors triggers a warning saying the radius is
too coarse for this market rather than reporting it as a finding.

**Gap grid** — the polygon is gridded at ~0.5 km and each cell scored:

```
underservice = 1 − max over competitors of (substitutability × exp(−km / 1.6))
```

A cell scores high only when every competitor that could serve it is far away or
a weak substitute.

Two honest limits, both stated in the memo: the grid uses straight-line distance
(a full cell-to-competitor route matrix would blow past the 625-element cap), and
it weights by substitutability but **not** by demand. A genuinely empty quarter
of the catchment may be empty because nobody lives there. Cross-reading gaps
against demographics is the synthesis stage's job, and the memo is required to do
it before calling a gap an opportunity.

## Margins of error

ACS publishes 90% margins. They propagate throughout using the Bureau's own
formulas:

| Operation | Formula |
|---|---|
| Sum | `MOE = √(Σ MOE_i²)` |
| Scaled by an exact constant | `MOE × |c|` |
| Proportion (numerator ⊂ denominator) | `(1/Y)·√(MOE_X² − p²·MOE_Y²)`, falling back to `+` when the radicand goes negative |
| Product of independents | `√(a²·MOE_b² + b²·MOE_a²)` |

A missing component margin propagates as unknown rather than as zero. Lower
bounds clamp at zero — an ACS count cannot be negative, and a bound below zero
means the estimate is not distinguishable from zero, which is worth showing.

Relative MOE above 30% triggers a warning that the point estimate should not be
quoted without its interval and that the balance verdict may flip inside it.
