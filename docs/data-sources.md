# Data sources

Every external dependency, what it can and cannot do, and the constraint that
shapes how this system uses it.

---

## Google Places API (New) — competitor discovery

### The constraint that shapes the design

`places.searchNearby` returns **at most 20 results with no pagination**. A
catchment holding 40 laundromats silently returns 20, and the analysis proceeds
as though that were the market. Radius-tiling around it is fragile and expensive.

### What we use instead: Places Aggregate API

`POST https://areainsights.googleapis.com/v1:computeInsights` takes a **custom
polygon** — the isochrone itself, not a circle approximating it — and returns:

| Insight | Returns | Limit |
|---|---|---|
| `INSIGHT_COUNT` | How many matching places the area holds | Uncapped |
| `INSIGHT_PLACES` | Their place IDs | **Only when count ≤ 100** |

That split is load-bearing. The count is always trustworthy; the named list is
not always complete. When count > 100 the Supply Index is a known undercount,
`census_complete` goes false, and the memo says so rather than presenting a
partial census as a full one.

Other limits: area must be between 1,556.86 m² and ~2×10¹² m². Polygons need ≥4
coordinates with the first equal to the last and ≥3 unique. Default rate limit
1,200 QPM.

### Place Details enrichment

`GET https://places.googleapis.com/v1/places/{place_id}` for the shortlist.
Field masks select the SKU tier, so the census pass uses a cheap mask and only
the shortlist pays Enterprise+Atmosphere rates.

**Reviews cap at 5 per place**, selected by relevance rather than date. This is
why "three competitors opened in the last 18 months per review history" is a
weak signal rather than a finding: the oldest of five relevance-ranked reviews is
a loose lower bound on business age and nothing more.

**`openingDate` does not solve this.** It populates only for businesses expected
to open in the *future*. There is no field giving the age of an existing business.

`priceLevel` is frequently absent, especially for services. Absence is not
"inexpensive" — leave it null.

### Terms of service — the design constraint

| Data | May we store it? |
|---|---|
| `place_id` | **Yes, indefinitely** |
| Latitude/longitude | Up to **30 days** |
| Names, ratings, reviews, photos, phone numbers | **No.** Fetch live, display with Google attribution |

So this tool does not accumulate a competitor database. It persists place IDs
and *our own derived judgments* — substitutability, chain grouping, computed
weights — and re-fetches content each run. Every memo is a point-in-time artifact
stamped with its fetch time. `supply/places.strip_transient()` is the only
sanctioned path to durable storage.

## Google Routes API — drive times

`POST https://routes.googleapis.com/distanceMatrix/v2:computeRouteMatrix`

| Limit | Value |
|---|---|
| Elements (origins × destinations) | 625 |
| Elements with `TRAFFIC_AWARE_OPTIMAL` | 100 |
| `TRANSIT` mode | Not supported by computeRouteMatrix |

One origin against any realistic catchment fits in a single request. Batching
only matters for `TRAFFIC_AWARE_OPTIMAL`, which is worth it on the small
peak/off-peak sample.

Implementation notes: responses arrive out of order and must be keyed by
`originIndex`/`destinationIndex`, never by position. `condition: ROUTE_NOT_FOUND`
is a real outcome (an island, a gated community, a bad geocode on the
competitor's side) and returns a null duration rather than dropping the row.

## Mapbox Isochrone — the catchment

`GET https://api.mapbox.com/isochrone/v1/mapbox/{profile}/{lng},{lat}`

**Google has no isochrone endpoint.** This is a hard third-party dependency.

| Limit | Value |
|---|---|
| Coordinates per request | 1 |
| Contours per request | 4 |
| Max contour | 60 minutes / 100 km |
| Rate limit | 300 req/min |
| Live traffic | **No** on the `driving` profile |

That last row matters most. A free-flow 10-minute contour is not a 10-minute
contour at 8am, so both the household count and the competitor set inside it are
upper bounds. Every memo generated from a non-traffic-aware profile says so, and
`spot_check_traffic_divergence()` routes a small sample at peak and off-peak to
bound the error.

Alternatives considered: **TravelTime** (higher-vertex polygons, up to 4 hours,
transit mode, flat monthly pricing) and **self-hosted Valhalla/Openrouteservice**
(free per call, OSM speed limits, no traffic, your ops burden). Mapbox was chosen
for v1 on simplicity and cost. `catchment/isochrone.py` is the single module a
swap would touch.

## U.S. Census — demand

### ACS 5-year (`api.census.gov/data/{vintage}/acs/acs5`)

The **only** ACS product published at block-group level. 1-year has better
currency but stops at 65,000-population geographies, which no drive-time
catchment resembles.

The cost is currency: a 2023 5-year estimate pools 2019–2023, so a neighbourhood
that turned over in 2024 does not show it. The vintage belongs in every memo.

| Dimension | Table |
|---|---|
| Household income | B19001 |
| Age (**person-level**) | B01001 |
| Tenure (owner/renter) | B25003 |
| Household size | B11016 |
| Total households | B11001 |

Request `E` (estimate) and `M` (margin) together — fetching them separately
invites drift. Suppressed values return as `-666666666`; mapping that to a number
produces catastrophic garbage.

Margins are published at **90% confidence** and are large at block-group level,
routinely ±30%. They propagate through every stage.

### TIGERweb — geography

`https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/tigerWMS_ACS{vintage}/MapServer/{layer}/query`

Block groups are layer 8. Query with `geometryType=esriGeometryPolygon` and
`spatialRel=esriSpatialRelIntersects`, returning geometry so overlap fractions
can be computed. Max record count 100,000.

### County Business Patterns — benchmarks

`https://api.census.gov/data/{year}/cbp` — establishment counts by NAICS by
county. Divided by ACS households, this yields a locally-calibrated, fully
sourced sustainability benchmark. See
[scoring-methodology.md](scoring-methodology.md#where-the-benchmark-comes-from),
including the caveat that it measures the current equilibrium rather than a
healthy one.

## Anthropic API

See [model-routing.md](model-routing.md).

## What none of these can see

Stated in every memo, because a reader most needs to know the shape of the gap:

- **Municipal and employer facilities.** A city recreation centre or an
  apartment-complex gym competes with a fitness studio and appears in no Places
  category.
- **In-home substitutes.** In-unit laundry is a laundromat's largest competitor
  and has no listing.
- **Businesses too new to be listed**, or listed without reviews.
- **Competitors' revenue, floor space, equipment age, or lease terms.**
- **Daytime population.** For lunch trade, coffee, and convenience, residential
  households are the wrong denominator. LODES/LEHD would fix it — see PRD §4 v2.
- **Recent neighbourhood change**, invisible inside a 5-year ACS pool.
