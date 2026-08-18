# Market Catchment Demand/Supply Analyzer

**Status:** Draft v1 · **Owner:** jlucki13 · **Last updated:** 2026-08-18

---

## 1. The problem

Screening a small-business acquisition means answering one question about the
location: **how much demand exists here, and how much of it is already spoken
for?**

Today that question gets answered two separate ways, both by hand, and both
worse than they look:

- **Competitor research** ends up as a map with pins on it. A pin does not tell
  you whether that business competes for the same customer, how entrenched it
  is, or whether the six pins clustered on one corridor leave half the catchment
  unserved.
- **Demographic research** ends up as a radius circle around the address with
  census numbers attached. A two-mile circle drawn around a business on the far
  side of a river includes thousands of households that will never walk in.

Each is weak alone, and for the same underlying reason: neither is measured
against the market the business actually serves.

## 2. Why the two halves belong together

They are one question viewed from two sides, and the interesting findings live
in the interaction:

- "12,000 households earning $75k+" reads as a strong market until you see nine
  direct competitors already splitting it.
- Two competitors reads as a thin field until you see the catchment holds 3,000
  relevant households total.
- A seller's claimed customer profile is hard to dispute in isolation. Set it
  against a catchment that is *both* thin on that demographic *and* crowded with
  entrenched competitors, and the earnings assumptions behind the asking price
  need explaining.

None of those is visible in either analysis run separately. That is the product.

## 3. Users and jobs

**Primary user:** a buyer screening SMB acquisition opportunities, working a
pipeline of deals rather than one at a time.

| Job | Today | With this |
|---|---|---|
| Screen a new listing fast | An hour of manual Maps and Census lookups | One command, one memo |
| Test the seller's story | Gut feel against the CIM | Claim-by-claim verdicts against catchment data |
| Compare deals | Not really possible — each was researched differently | A comparable score per address |
| Find off-market opportunities | Ad hoc | Underserved pockets surfaced from the gap grid |

**Explicit non-user:** anyone needing a site-selection tool with foot-traffic
telemetry, or a market-sizing product. This screens deals.

## 4. Scope

### v1 (this build)

1. One address in, one memo and scorecard out
2. Real drive-time catchment (Mapbox isochrone), not a radius circle
3. Competitor census over that exact polygon, enriched with drive time, rating,
   review volume, price level
4. ACS demographics interpolated onto that same polygon, with margins of error
5. Substitutability classification — which listed businesses actually compete
6. Deterministic Demand Index, Supply Index, and balance ratio
7. Cluster and gap geometry
8. Seller-claim reconciliation from structured input
9. Written memo with flags, confidence levels, and falsifiers
10. Config-driven verticals, three seeded (coffee shop, laundromat, fitness studio)

### v2 (specced, not built)

1. **CIM PDF ingestion** — extract testable claims from the deal document
   instead of hand-entering them. The reconciliation engine is built to accept
   them today; only the extraction stage is missing.
2. **Batch screening** — score 20 candidate addresses, triage cheaply, run full
   synthesis on the shortlist only.
3. **Map rendering** — the scored map. The scorecard already carries cluster
   centroids and gap cells; this is a rendering job.
4. **Peak/off-peak catchment comparison** as a first-class output rather than a
   spot check.
5. **Additional demand signals** — daytime population (LODES/LEHD) matters far
   more than residential households for a lunch-trade business.

### Non-goals

- Financial diligence. This tool does not touch the books. It flags where a
  market read makes the earnings story worth questioning; it does not value the
  business.
- A competitor database. Google's terms forbid it (§9), and the design is built
  around that rather than against it.
- Precision. The inputs do not support it, and the output is honest about that.

## 5. How it works

```
deal input (address, vertical, seller claims)
  │
  ├─1 geocode ────────────────── Google Geocoding
  ├─2 catchment ──────────────── Mapbox Isochrone → drive-time polygon
  │
  ├─3 DEMAND                                 ├─4 SUPPLY
  │   TIGERweb: block groups ∩ polygon       │   Places Aggregate computeInsights
  │   ACS 5-yr detail tables per BG          │     over the SAME polygon
  │   population-weighted interpolation      │   Place Details on the shortlist
  │   → households, mix, margins of error    │   Routes matrix → drive times
  │                                          │
  ├─5 classify substitutes ───── Sonnet 5, one batched call
  ├─6 score ──────────────────── deterministic Python, no model
  ├─7 synthesize ─────────────── Fable 5 → flags + memo
  ├─8 adversarial review ─────── Opus 5 → challenges the flags
  └─9 render ─────────────────── markdown memo + scorecard JSON
```

Step 2 gating steps 3 and 4 is the load-bearing part. Both halves are measured
against the same polygon; the moment either falls back to a radius, they are
describing different markets and the ratio between them is meaningless.

## 6. The output

A memo per address. Structure, in order:

1. **Read this first** — every caveat that bounds what follows. Deliberately
   before the conclusions, not appended after them.
2. **Headline** — the one-sentence read.
3. **Scorecard** — demand, supply, balance, verdict, each with its interval.
4. **Supply composition** — direct/partial/adjacent counts, strongest competitors.
5. **Clustering** — corridors and what they leave uncovered.
6. **Flags** — severity, evidence by field reference, confidence, and a
   falsifier for each.
7. **Seller's claims** — verdict per claim against the specific field tested.
8. **Underserved pockets.**
9. **Analysis** — the written reasoning.
10. **Review challenges** — where the adversarial pass disagreed and was not
    resolved. Shown, not silently applied.
11. **What this analysis cannot see.**

Plus `scorecard.json`: every computed figure, machine-readable, for comparing
deals across a pipeline.

## 7. Design principles

These are the decisions that make the output trustworthy, and each is enforced
in code rather than left to discipline.

### Models produce judgment; code produces numbers

Nothing in `scoring/` calls a model. The synthesis stage may cite only fields
that exist in the scorecard, by dotted path, and `provenance.verify()` re-derives
the set of computed values and checks every figure in the rendered prose against
it before anything is written. **A memo containing a number with no computed
origin does not render.** A fabricated figure in a diligence document is
indistinguishable from a real one at the point of reading, so the defence has to
be structural.

### Intervals, not point estimates

ACS block-group margins of error routinely run ±30%. An analysis that reports a
point estimate and drops the interval is not more precise, it is less honest.
Margins propagate through every stage and the memo never prints a bare figure
where an interval exists.

### Suppress rather than guess

No sourced sustainability benchmark means **no verdict**, not a plausible one.
A confident "underserved" derived from an invented denominator is the single
most dangerous output this tool could produce. The demand-per-competitor figure
still stands on its own; it simply has nothing to be measured against yet.

### Caveats before conclusions

A reader who stops halfway has read that the supply census was incomplete,
rather than that the market looks underserved.

### Every flag carries a falsifier

What could a buyer observe that would overturn this? A flag nobody can check is
an opinion wearing a finding's clothes. Enforced by schema and by the
provenance checker.

### Approximations announce themselves

Chain damping, interpolated bands, defaulted entrenchment, free-flow catchments,
person-level filters applied to household counts — each emits a warning that
reaches the memo verbatim.

## 8. Model routing

Full rationale and cost model in [model-routing.md](model-routing.md).

| Stage | Model | Why this tier |
|---|---|---|
| Normalization, dedupe, review-date extraction | `claude-haiku-4-5` | Mechanical, high fan-out |
| Substitutability classification | `claude-sonnet-5` | Real judgment, bounded. One batched call over all competitors — cheaper than per-item *and* more consistent, since ranking is relative |
| Seller-claim parsing (and v2 CIM extraction) | `claude-sonnet-5` | Structured extraction over long documents |
| **Synthesis, flags, memo** | **`claude-fable-5`** @ effort `xhigh` | The judgment the tool exists for: long-horizon reasoning across four sources where being wrong means a bad acquisition |
| Adversarial review | `claude-opus-5` @ effort `high` | Independent model attacking the memo's reasoning. A model reviewing its own output ratifies it |
| Batch triage (v2) | `claude-sonnet-5` @ effort `low` | Shortlist cheaply; Fable runs only on survivors |
| Scoring arithmetic | **none** | Deterministic |

## 9. Scoring

Full derivations in [scoring-methodology.md](scoring-methodology.md).

- **Demand Index** — `DI = households × Π(demographic filter shares)`, read as
  *qualified households*, always with a 90% interval.
- **Supply Index** — `SI = Σ(substitutability × entrenchment × accessibility)`,
  read as *effective competitor equivalents*. Nine listed businesses can be an
  SI of 2.5 when most are weak, distant, or only adjacent substitutes.
- **Balance** — `ratio = [DI / (SI + 1)] / benchmark`. The `+1` is the subject
  business: a buyer entering an empty market becomes its competitor.
- **Geography** — DBSCAN corridors in drive-time-adjacent space; a gap grid over
  the polygon.

## 10. Constraints and known limits

| Constraint | Consequence | How it is handled |
|---|---|---|
| **Google ToS forbids warehousing Places content**; only `place_id` is storable indefinitely, coordinates ≤30 days | No competitor database can accumulate | Persist IDs and *our own* derived judgments; re-fetch content per run; every memo stamped with fetch time. `strip_transient()` is the only path to durable storage |
| **`searchNearby` returns max 20 results, no pagination** | Naive competitor discovery silently truncates a market | Places Aggregate `computeInsights` over the polygon instead — uncounted census, IDs when count ≤100 |
| **Aggregate API names places only when count ≤100** | Dense urban catchments can exceed it | `census_complete: false`, an explicit undercount warning, and the verdict read as an upper bound |
| **Place Details returns at most 5 reviews**; `openingDate` only populates for *future* openings | "Competitors opened recently" is a weak signal | Recorded as a lower bound on business age, confidence-tagged low, never a headline flag alone |
| **Google has no isochrone API** | Hard third-party dependency | Mapbox. 60-min contour cap, 4 per request, 1 origin per request, 300 req/min |
| **Mapbox `driving` is free-flow** | Catchment overstates peak reach | Flagged in every memo; peak/off-peak spot check via Routes API |
| **ACS 5-year is the only block-group product** | Estimates pool five years; recent turnover invisible | Vintage stated in the memo; MOEs propagated |
| **Review count is a poor revenue proxy** | Biased by age, category norms, solicitation practice | Log-compressed, labelled a proxy, never presented as volume |
| **Chains ≠ independents** | Six locations of one operator overstate pressure | Damped at `k^-0.5`, so a chain totals `√k` |
| **Municipal, employer, and in-home substitutes are invisible** | Supply understated for some verticals | Surfaced by the classifier as `coverage_concerns` |

## 11. Cost model

Per deal, order of magnitude:

- **Claude:** ~$2–3, dominated by Fable 5 synthesis. Prompt caching on the
  rubric and vertical config makes a batch of deals in one vertical roughly one
  full-price call plus cache reads.
- **Google:** likely the larger half. Place Details on ~100 competitors at
  Enterprise+Atmosphere field masks is the biggest single line. Field masks
  select the SKU tier, so the census pass stays on a cheap mask and only the
  shortlist pays.
- **Mapbox / Census:** negligible. Census API is free with a key.

**Google and Mapbox dollar figures are unverified** — `developers.google.com` was
unreachable from the build environment. Confirm current SKU pricing in the
console before committing to a per-deal budget.

## 12. Success criteria

1. A deal that took an hour to research takes one command.
2. Every figure in every memo traces to a computed field. Enforced, not aspired to.
3. On a market the user knows well, the memo's flags match their own read — and
   where they diverge, the memo shows its work well enough to settle which is right.
4. At least one flag per pipeline that the user would not have caught manually.
5. No memo ever states a confident conclusion that its own warnings undercut.

## 13. Build sequence

| Phase | Deliverable | Status |
|---|---|---|
| 0 | PRD, schemas, configs, scoring engine, provenance rail, tests | **done** |
| 1 | Live connectors: geocode, Mapbox, Places Aggregate, Details, Routes | stubs with contracts |
| 2 | Census: TIGERweb + ACS + population-weighted interpolation | stubs with contracts |
| 3 | CBP-derived benchmarks — unblocks the balance verdict | stub with contract |
| 4 | Reasoning stages wired to the API | prompts written, calls stubbed |
| 5 | First real deal end-to-end; calibrate constants against a known market | — |
| 6 | v2: CIM ingestion, batch screening, map rendering | specced |

Phase 0 is complete and tested. `--dry-run` exercises phases 0 and 5's
orchestration against fixtures with no network calls, so the scoring path can be
changed and verified without spending on APIs.

## 14. Open questions

1. **Benchmark calibration.** CBP-derived benchmarks measure the *current*
   equilibrium, not a healthy one. In an already-saturated county the derived
   benchmark encodes saturation as normal. Worth pairing with a published
   industry figure per vertical where one exists.
2. **Daytime vs residential population.** For lunch trade, coffee, and
   convenience, residential households are the wrong denominator entirely.
   LODES/LEHD workplace-area data would fix it and is free.
3. **Constant calibration.** `ENTRENCHMENT_REFERENCE_REVIEWS`, the
   substitutability weights, and the verdict bands are reasoned but not
   empirically fitted. The first few real deals in known markets should tune them.
4. **Where the user's own judgment enters.** Currently the classifier's
   substitutability calls are accepted as-is. An override file per deal is
   probably worth having.
