# Model routing

The governing rule: **models produce judgment, code produces numbers.** Nothing
in `scoring/` calls a model, and no stage here returns a figure the memo quotes.

Routing lives in `src/mcds/reasoning/client.py` and is overridable in
`config/settings.yaml`.

---

## Stage assignment

| Stage | Model | Effort | Why this tier |
|---|---|---|---|
| Category normalization, dedupe, review-date extraction | `claude-haiku-4-5` | n/a | Mechanical, high fan-out, per-item. Nothing here needs reasoning depth. |
| Substitutability classification | `claude-sonnet-5` | `medium` | Real judgment ("is a bakery competing with this cafe?") but bounded and well-specified. |
| Seller-claim parsing · v2 CIM extraction | `claude-sonnet-5` | `medium` | Structured extraction over long documents; the 1M context absorbs a full CIM. |
| **Synthesis: flags, verdicts, memo** | **`claude-fable-5`** | `xhigh` | The judgment the tool exists for. Long-horizon reasoning across demand, supply, geography, and seller claims, where being wrong means a bad acquisition. |
| Adversarial review | `claude-opus-5` | `high` | An independent model attacking the memo's reasoning. Deliberately not the model that wrote it — a model reviewing its own output ratifies it. |
| Batch triage (v2) | `claude-sonnet-5` | `low` | Shortlist 20 addresses cheaply; Fable runs only on survivors. |
| Interactive follow-ups | `claude-opus-5` | `medium` | Fast enough to iterate against. |
| **All arithmetic** | **none** | — | Deterministic Python. |

## Why classification is one batched call, not N

The instinct is to classify each competitor separately on the cheapest model.
Batching all of them into a single Sonnet 5 call is better on both axes:

- **Cheaper.** ~200 competitors is roughly 40K input tokens in one call. Two
  hundred separate Haiku calls re-send the rubric two hundred times.
- **More consistent.** Substitutability is a *relative* judgment. A model seeing
  all thirty competitors at once grades them against each other; thirty separate
  calls grade each against thirty independent notions of "typical".

Haiku's place is genuinely mechanical fan-out — normalising category strings,
deduping records across queries, pulling the earliest review date out of a blob.

## Why synthesis is Fable

Four criteria decide whether a task justifies the top tier, and this stage meets
all four:

- **Complexity** — cross-referencing four data sources plus seller claims,
  where the finding is usually the *interaction* rather than any single figure.
- **Value** — the output feeds an acquisition decision.
- **Viability** — reasoning over structured data with an explicit rubric is
  squarely in scope.
- **Cost of error** — high, and errors are hard to catch downstream. The
  provenance rail catches fabricated numbers; it cannot catch a real number
  supporting a conclusion it does not support.

Effort `xhigh` rather than `max`: `max` is for correctness-over-cost work, and
the marginal gain here does not justify it on every deal in a pipeline. Escalate
to `max` for a deal at the IOI stage.

## Request shapes differ by tier — and getting them wrong is a 400

| Model | `thinking` | `output_config.effort` | `budget_tokens` |
|---|---|---|---|
| `claude-fable-5` | **Omit entirely.** Thinking is unconditionally on; sending the parameter at all is rejected | `low`–`max` | Rejected |
| `claude-opus-5` | `{"type": "adaptive"}` (on by default) | `low`–`max` | Rejected |
| `claude-sonnet-5` | `{"type": "adaptive"}` | `low`–`max` | Rejected |
| `claude-haiku-4-5` | `{"type": "enabled", "budget_tokens": N}` | **Rejected** | Required for thinking |

`build_request()` handles this per model and is unit-tested, so a stage
reassignment in settings cannot silently produce an invalid request.

Other cross-cutting requirements:

- **Sampling parameters** (`temperature`, `top_p`, `top_k`) are rejected on the
  current generation. Do not reintroduce them.
- **Assistant prefill** is rejected. Use structured outputs to control format.
- **Structured outputs** via `output_config.format` with a JSON schema, plus
  `strict: true` on tool definitions.
- **Streaming** for synthesis. At `xhigh` effort on a full deal payload a
  non-streaming request will hit the HTTP timeout. Use the SDK's
  `get_final_message()` rather than assembling events by hand.

## Refusals

Fable 5 and Opus 5 can end a turn with `stop_reason: "refusal"` — **HTTP 200,
not an exception**. Code reading `response.content` without checking gets an
empty or partial result and no error. Every call site checks first
(`check_refusal()`).

Server-side fallbacks are on by default
(`betas: ["server-side-fallback-2026-07-01"]`, `fallbacks: "default"`), so a
refusal reroutes by category instead of failing a deal run.

## Zero data retention

**Fable 5 is not available under zero data retention.** If the org enforces ZDR,
set `reasoning.org_requires_zdr: true` and the synthesis stage degrades to Opus 5
at config load time rather than failing at request time. This is tested.

## Prompt caching

The rubric, scoring definitions, and vertical config are identical across every
deal in a pipeline and are long. They form the cached prefix; per-deal payloads
go behind the breakpoint. A 20-address screening run becomes one full-price call
and nineteen cache reads.

The trap: anything volatile — a timestamp, a deal id — landing *before* the
breakpoint invalidates the prefix on every call, and the cache silently never
hits. Verify with `usage.cache_read_input_tokens`; a zero across repeated calls
means something volatile crept into the prefix.

## Cost per deal

Order of magnitude, one deal, ~200 competitors:

| Stage | Model | Approx. |
|---|---|---|
| Normalization | Haiku 4.5 | $0.10 |
| Classification | Sonnet 5 | $0.35 |
| Synthesis | **Fable 5** | $1.60 |
| Review | Opus 5 | $0.35 |
| **Claude total** | | **~$2.40** |

Google API cost is the larger and less certain half — Place Details on ~100
competitors at Enterprise+Atmosphere field masks is the biggest single line.
Field masks select Google's SKU tier, so the census pass runs on a cheap mask and
only the enriched shortlist pays the higher rate.

**Google and Mapbox figures are unverified** — `developers.google.com` was
unreachable from the build environment. Confirm current SKU pricing in the
console before committing to a per-deal budget.

Batch API halves Claude cost for v2 pipeline screening, where latency does not
matter.
