# Market Catchment Demand/Supply Analyzer

One score and one memo per address, answering the question behind every SMB
acquisition screen: **how much demand exists here, and how much of it is already
spoken for?**

Not a map with pins on it, and not a radius circle with census numbers attached
— a real drive-time catchment with both the demand and the competition measured
against that same polygon, plus the seller's claims tested against it.

**Start with [docs/PRD.md](docs/PRD.md).**

## Try it without any API keys

Install once, editable, from the repo root. This works the same on macOS, Linux,
and Windows, and puts `mcds` on your path:

```bash
python -m venv .venv                       # optional but recommended
source .venv/bin/activate                  # Windows: .venv\Scripts\Activate.ps1

pip install -e ".[dev]"
mcds examples/sample_deal.yaml --dry-run
pytest -q                                  # 53 tests
```

`--dry-run` walks the full orchestration against `fixtures/` with no network
calls, no API keys, and no charges. It is how the scoring path is exercised in
tests and how a formula change is verified before spending on APIs.

<details>
<summary>Running without installing</summary>

The package lives under `src/`, so Python needs to be told where to find it.
The syntax differs by shell, which is why the editable install above is the
recommended path.

```bash
# macOS / Linux (bash, zsh)
PYTHONPATH=src python3 -m mcds.cli examples/sample_deal.yaml --dry-run
```

```powershell
# Windows PowerShell -- the VAR=value prefix is Unix syntax and fails here
$env:PYTHONPATH="src"; python -m mcds.cli examples/sample_deal.yaml --dry-run
```

```bat
:: Windows cmd.exe
set PYTHONPATH=src && python -m mcds.cli examples/sample_deal.yaml --dry-run
```

On Windows use `python`, not `python3` -- the latter is often a stub that opens
the Microsoft Store. Requires Python 3.11+ and `pip install pyyaml` (plus
`jsonschema` for schema validation, which is optional and skipped when absent).

</details>

## Before building connectors: run the probe

`scripts/probe.py` hits Mapbox and the Places Aggregate API for one real address
and answers the three questions that could invalidate the architecture — whether
a Mapbox isochrone ring is accepted as-is, whether a real catchment exceeds the
100-place cap, and what the SKUs actually cost. Costs about one coffee.

```powershell
$env:GOOGLE_MAPS_API_KEY="..."; $env:MAPBOX_ACCESS_TOKEN="..."
python scripts/probe.py "4820 Skillman Ave, Sunnyside, NY 11104" --vertical laundromat
```

Its request shapes come from documentation research and have not been executed
against the live APIs — discrepancies are the finding, so every call prints its
raw status and body on failure.

## Running it for free

Every external service except Anthropic has a free tier that covers a realistic
deal pipeline, and `--no-llm` removes the Anthropic dependency entirely:

```bash
mcds examples/sample_deal.yaml --no-llm    # zero model spend
```

Substitutability then comes from vertical-config category priors rather than a
model, and synthesis is skipped. See [docs/running-free.md](docs/running-free.md)
for per-service budgets, which field mask to use, and what the priors path gives
up.

## Status

Phase 0 is complete and tested: schemas, vertical configs, the full scoring
engine, the provenance rail, the memo renderer, and the model routing. The
external connectors and reasoning calls are stubs carrying their full signatures,
request shapes, and API constraints — see the build sequence in
[docs/PRD.md §13](docs/PRD.md).

## Layout

```
docs/PRD.md                  the product spec — read this first
docs/scoring-methodology.md  every formula and the reasoning behind each constant
docs/model-routing.md        which Claude model runs which stage, and why
docs/data-sources.md         per-API limits, terms of service, data vintages
docs/running-free.md         free-tier budgets and the zero-cost --no-llm path

config/verticals/*.yaml      per-category tuning. A new vertical is a new file here
config/settings.example.yaml credentials, model assignments, API defaults
schemas/*.schema.json        the data contracts every stage reads and writes

src/mcds/scoring/            Demand Index, Supply Index, balance, geography
src/mcds/provenance.py       the rail: no number reaches the memo uncomputed
src/mcds/moe.py              Census margin-of-error arithmetic
src/mcds/reasoning/          model routing and the stage prompts
src/mcds/catchment|supply|demand/   external connectors
src/mcds/render/memo.py      scorecard + findings → markdown

fixtures/                    a synthetic Queens laundromat catchment for --dry-run
tests/test_scoring.py        golden-number tests pinning every constant
```

## Design principles

Four decisions do most of the work, and each is enforced in code rather than
left to discipline:

1. **Models produce judgment; code produces numbers.** Nothing in `scoring/`
   calls a model. The synthesis stage may cite only fields that exist in the
   scorecard, and a memo containing a figure with no computed origin does not
   render.
2. **Intervals, not point estimates.** ACS block-group margins run ±30%.
   Reporting a bare figure is not more precise, it is less honest.
3. **Suppress rather than guess.** No sourced benchmark means no verdict. A
   confident "underserved" from an invented denominator is the most dangerous
   output this tool could produce.
4. **Caveats before conclusions.** A reader who stops halfway has read that the
   supply census was incomplete.

## Adding a vertical

A new file in `config/verticals/`. It declares the drive-time catchment, the
Google place types to census, substitutability priors and guidance for the
classifier, the demographic filters that gate demand, and the sustainability
benchmark. No code changes.
