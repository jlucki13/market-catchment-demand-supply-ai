# Running inside the free tiers

Short answer: **yes, for a realistic deal pipeline** — with two catches worth
knowing before you start.

---

## The catches

**1. Google requires a billing account even to use the free tier.** A card has
to be on file. Free means "not charged", not "no card". Set a budget alert in
the Cloud console at, say, $5, so a bug in a loop cannot quietly run up a bill.

**2. Anthropic has no free tier.** The reasoning stages cost money. But they are
*optional* — see [Running with no model at all](#running-with-no-model-at-all)
below. The scoring engine, which is most of the machinery, never calls a model.

## What each service gives you

Google replaced its old flat $200/month credit on 1 March 2025 with a **per-SKU**
monthly free allowance. Allowances reset monthly, do not roll over, and no longer
pool across APIs — each SKU has its own bucket.

| Service | Tier | Free per month | Per deal | Deals/month |
|---|---|---|---|---|
| Geocoding | Essentials | 10,000 | 1 | 10,000 |
| Route Matrix | Essentials | 10,000 | 1 | 10,000 |
| Places Aggregate | Pro | 5,000 | 1–4 | 1,250+ |
| **Place Details** | **Pro or Enterprise** | **5,000 / 1,000** | **~60** | **~16–83** |
| Mapbox Isochrone | free tier | 100,000 | 1 | 100,000 |
| Census ACS + CBP | free, no billing | unlimited | ~10 | unlimited |

One Route Matrix request covers up to 625 destinations, so a whole catchment's
drive times cost a single call.

**Place Details is the only binding constraint.** It is the one call that scales
with competitor count rather than staying at one per deal.

## Field masks are the cost lever

Google bills a Places request at the **highest tier of any field requested**. One
expensive field pulls the whole call up. So the mask is the lever, and
`supply/places.py` names three:

| Mask | Adds | Tier |
|---|---|---|
| `DETAIL_MASK_ESSENTIALS` | id, name, address, location, types | Essentials |
| `DETAIL_MASK_STANDARD` **(default)** | rating, **review count**, price level, hours | Pro or Enterprise |
| `DETAIL_MASK_WITH_REVIEWS` | review **bodies** | Enterprise |

`DETAIL_MASK_STANDARD` is the default because it is the smallest mask that still
supports the Supply Index: entrenchment needs the rating and the review *count*,
not the review *text*.

`reviews` is opt-in and should usually stay off. It exists only to guess business
age from the oldest of at most five relevance-ranked reviews — the weakest signal
in the whole system, already barred from being a headline flag on its own. It is
also the field that drops your free allowance by 5×. Paying the highest tier for
the least reliable signal is a bad trade.

> **Still unverified:** whether `rating` and `userRatingCount` bill as Pro or
> Enterprise. Sources conflict and `developers.google.com` was unreachable from
> the build environment.
>
> Billing reports cannot settle it at low volume — 99 Place Details calls billed
> $0.00, which is consistent with both tiers (2% of a Pro allowance, 10% of an
> Enterprise one). The place to look is **Google Maps Platform → Quotas**, which
> shows consumption against each named allowance, or **Metrics** with Granularity
> set to *Per Day* and *Grouped by → SKU* if that grouping is offered.
>
> Not worth chasing until a batch run makes it matter. With primary-type
> filtering the range is now 26–130 deals/month, and either end is comfortable
> for a pipeline.

## Running with no model at all

```bash
mcds examples/sample_deal.yaml --no-llm
```

Zero Anthropic spend. Two things change:

- **Substitutability** comes from the vertical config's category priors rather
  than from a model.
- **Synthesis and review are skipped.** You get the scorecard and a memo built
  from it — no written flags, no seller-claim verdicts, no adversarial review.

Chain detection still works: it is name-based and deterministic.

What you give up is the judgment the classification stage exists for. Priors see
a category label; a model sees the business. Priors cannot separate a $10/month
high-volume gym from a $200/month boutique studio when Google types both as
`gym`, or a self-service laundromat from a drop-off dry cleaner when both file
under `laundry`. Every priors-based call is therefore tagged **low confidence**
and the memo carries a warning saying the supply read is category-level only.

That is a usable first pass, not the analysis the PRD describes. It is a good way
to validate the data plumbing before spending on reasoning.

### How much the priors path costs you, measured

A live 10-minute catchment around a Denver address returned **33 places typed
`laundry`**. Reading the names:

| What they actually were | Count |
|---|---|
| Self-service laundromats | 9 |
| Drop-off dry cleaners | 10 |
| Carpet cleaners, janitorial, commercial services, one appliance retailer | 14 |

The priors path scores all 33 as direct competitors, because Google's type is
all it can see. Running the Supply Index both ways on that data:

| | Supply Index |
|---|---|
| Priors — all 33 scored `direct` | 22.0 |
| Correctly classified — 9 direct, 10 adjacent, 14 excluded | 6.9 |

**A 3.2× overstatement of supply**, which is more than enough to flip a balance
verdict from underserved to oversupplied and kill a deal that was fine.

Worse, the single most-reviewed business in that catchment — 581 reviews, more
than any real laundromat — was a commercial cleaning company. Entrenchment would
rank it the strongest competitor in the market. It competes for nothing.

So on a category like this, `--no-llm` is not a cheaper version of the analysis;
it is a different and misleading one. Use it to validate plumbing. For a real
deal read, classification at roughly $0.35 is the cheapest high-value spend in
the whole pipeline.

### Primary-type filtering cuts Details calls 36%

Place Details is the only call that scales with competitor count, so it is the
binding free-tier constraint. Switching the census from `includedTypes` (primary
**or** secondary types) to `includedPrimaryTypes` (primary only) cut the same
Denver catchment from 33 places to 21 — and dropped **zero** genuine
laundromats. What it removed was carpet cleaners, janitorial firms, commercial
services, and two dry cleaners carrying `laundry` as a secondary tag.

Supply Index against a hand-classified ground truth:

| | Supply Index | vs truth |
|---|---|---|
| `includedTypes` + priors | 22.0 | 3.26× |
| `includedPrimaryTypes` + priors | 13.2 | 1.96× |
| `includedTypes` + classification | 6.8 | 1.00× |
| **`includedPrimaryTypes` + classification** | **6.5** | **0.96×** |

`primary` is now the default. It costs 4% understatement from the two dropped
dry cleaners, and buys 36% fewer Place Details calls — roughly **130 deals a
month instead of 83**, or 26 instead of 16, depending on the SKU tier.

It is **not** a substitute for classification. Five false positives survived
into the kept 21, including MSS Cleaning — still the most-reviewed business in
the catchment, still a commercial cleaner. Primary-type filtering halves the
error; only classification closes it.

## What the paid stages actually cost

If you do turn the model stages on, per deal:

| Stage | Model | Approx. |
|---|---|---|
| Classification | Sonnet 5 | $0.35 |
| Synthesis | Fable 5 | $1.60 |
| Review | Opus 5 | $0.35 |

A middle path worth considering: **classification on, synthesis off.** That buys
the real substitutability judgment — the thing priors cannot do — for about $0.35
a deal, while leaving the expensive stage off until the pipeline has earned it.

## The fully-free alternative, and why it is probably not worth it

You could replace Google and Mapbox entirely:

- **OpenStreetMap via the Overpass API** for the competitor census — free, no
  key, no billing account.
- **Openrouteservice** for isochrones and matrices — free tier, requires a key.

The reason not to: **OSM has no ratings and no review counts.** Entrenchment is
one of the three factors in the Supply Index, and without it every competitor
weighs the same regardless of whether it is a busy 20-year institution or a
struggling newcomer. OSM business coverage also varies enormously by area —
excellent in some cities, sparse in others — and you cannot tell which you are
in without checking against a market you know.

Google's free tier already covers 16–83 deals a month. Trading away the
entrenchment signal to avoid putting a card on file is a bad deal unless you
genuinely cannot use Google at all.

## Setting the keys

Copy `.env.example` to `.env` and fill in what you have. It is gitignored, and
the CLI and probe read it automatically — no need to set anything in your shell
each session, which matters on Windows where `$env:VAR="..."` lasts only until
you close the terminal.

```
MAPBOX_ACCESS_TOKEN=pk.eyJ1...
CENSUS_API_KEY=abc123...
```

A key exported in your shell always wins over the file, so you can override one
temporarily without editing anything.

Check whatever you have so far:

```bash
python scripts/probe.py --check
```

It reports per service, so a key can be verified the moment you get it rather
than waiting for the whole set. Every call it makes lands inside a free tier.

## Recommended setup

1. Google Cloud project, billing enabled, **budget alert at $5**.
2. Enable exactly four APIs: Geocoding, Places API (New), Places Aggregate, Routes.
3. Restrict the key to those four APIs.
4. Mapbox account — free tier, no card.
5. Census key — free, instant, no billing.
6. Run `scripts/probe.py` on one address. Check the billing report afterwards.
7. Run `--no-llm` first to validate the plumbing. Turn on classification once
   the data looks right.
