# Plan 002 — Signals: specs, cohorts, deals

**Goal:** turn free-text titles into structured specs, group listings into
substitutable cohorts, normalize to `$/GB`, and flag listings below their cohort —
without letting any of it into the truth path.

Plan 001 recorded prices. Prices are not comparable until you know what was being
priced, so this plan is what makes the numbers mean anything.

## The thing most likely to go wrong

`"Lot of 4 x 32GB PC4-2400"` at $200 is **$1.56/GB**. Read as a single 32GB module it
is **$6.25/GB** — a 4× error that both manufactures a nonexistent bargain and drags
the cohort statistics that every *other* listing is scored against. One mis-parsed lot
can produce several false deals.

So the lot multiplier gets: dedicated tests over real title shapes, a consistency
check (when a title states total, count, and per-module capacity, they must
multiply out), and a confidence score that gates deal flagging.

Traps the regex must survive:
- `2Rx4` contains an `x` and is **not** a multiplier. Rank patterns are consumed first.
- `PC4-2400` / `PC4-19200` are two notations for the same speed — one is MT/s, the
  other is bandwidth in MB/s (`19200 / 8 = 2400`).
- `128GB (4x32GB)` states the total *and* the breakdown; `128GB` alone states only a
  total and the module count is unknown.

## Work items

### WI-006 — Regex spec extractor
`src/touchstone/extract/specs.py`

Deterministic, no network. Extracts `capacity_per_module_gb`, `module_count`,
`total_gb`, `ddr_gen`, `speed_mt`, `form_factor`, `rank_org`, `ecc`, `registered`.

Emits a confidence in [0, 1]. Confidence drops when the title states a total that
does not equal `count × per-module`, when capacity is absent, or when the form factor
is ambiguous. Anything below the LLM threshold is queued for WI-007 rather than
guessed at.

### WI-007 — umans fallback
`src/touchstone/extract/llm.py`

OpenAI-compatible client against `https://api.code.umans.ai/v1`, model `umans-flash`
with `umans-deepseek-v4-flash-0731` as secondary. **Any model id ending in `-lab` is
rejected at construction** — the operator's rule, enforced rather than documented.

Returns the same `SpecCandidate` shape as the regex path. Structured output is
validated before it is trusted: a model that returns `capacity_per_module_gb: 3200`
(having read the speed as a capacity) must be rejected by range checks, not stored.

Failures are transient and expected. Retry, do not blacklist a model, and never let a
failure block or alter an observation.

### WI-008 — Extraction runner
`src/touchstone/extract/runner.py`

Walks listings whose `title_hash` has no `item_spec`, tries regex, escalates to the
model below the confidence threshold, writes one `item_spec` per distinct title.

A separate entry point (`touchstone extract`) run by its own CronJob. Never called
from `scan/`; the architecture test enforces it.

### WI-009 — Cohorts and $/GB
`src/touchstone/extract/cohort.py`, changes to `scan/aggregate.py`

`cohort_key` becomes the full tuple: `ddr_gen`, `form_factor`, `ecc`, `registered`,
`capacity_per_module_gb`, `speed_mt`, `rank_org`, `condition_id`. Listings with no
usable spec fall into an `unspecced` cohort rather than polluting a real one.

`provisional_cohort_key` from Plan 001 is retired. **Historic `scan_aggregate` rows
keep their old keys and are not rewritten** — the no-recompute rule holds, so the
cohort change is a discontinuity in the series, and the UI must show it as one rather
than pretending the two key spaces are the same.

### WI-010 — Deal scoring
`src/touchstone/scan/deals.py`

A listing is flagged when its `$/GB` is below its cohort's current `p10`, subject to:
- cohort `n >= 5` (a percentile over three listings is noise),
- spec `confidence >= 0.8` or `method == MANUAL`,
- not already flagged (one alert per listing, ever).

Score is how far below p10, in units of the cohort's own spread — comparable across
cohorts with different price levels.

## Verification

- **Lot multiplier corpus**: a table of real-shaped titles with expected
  `(per_module, count, total)`, including every trap above. Mutation-check that
  removing the rank-pattern guard makes `2Rx4` parse as a multiplier and the tests go
  red.
- **Speed notation**: `PC4-2400`, `PC4-19200`, `DDR4-2400`, `2400MHz` all resolve to
  `2400`.
- **Confidence gates the damage**: a listing with an inconsistent title must not be
  flagged as a deal, however cheap it computes.
- **The model never enters the truth path**: with the extractor pointed at a dead
  endpoint, a scan still completes and stores observations; only `item_spec` coverage
  drops.
- **`-lab` rejection**: constructing the client with a `-lab` model raises.
- Cohort membership: two listings differing only in condition are in different
  cohorts; differing only in seller are in the same one.

## Exit criteria

Deal detection runs over a recorded corpus and flags the planted bargain and nothing
else. The lot-multiplier tests fail against a naive parser. `ruff`, `mypy --strict`,
and the full suite green.
