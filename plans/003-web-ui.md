# Plan 003 — Web UI

> **Completed 2026-08-31.** All seven work items landed. The honesty constraints are
> tests, not review notes: a forbidden-label grep over the rendered HTML of every
> page, suppression checked at the n=4/n=5 boundary, both discontinuities asserted
> present *and* asserted absent when nothing changed, capped points marked, and the
> aggregate rule checked from the outside by deleting every listing row and
> confirming the chart does not move. Each of those was mutation-checked — the guard
> was shown to fail against a deliberately broken implementation before it was kept.
>
> Exit criterion met live: two real scans ran against the production keyset (596
> listings, 111 cohorts, 1 disappearance, 5 flagged), `touchstone extract` specced
> 488 of 500 distinct titles by regex, and every page was rendered and checked
> against that data.
>
> **Two things the plan did not anticipate**, both found on live data:
> - `correct_spec()` refused to correct a title with no spec row — which is exactly
>   the case the worklist surfaces most. It now creates the row when given the title.
> - Most real cohorts have **no spread**: p10 and the median coincide, because forty
>   listings of the same module often sit at one price. The price band rendered as a
>   zero-width hairline that said nothing. It now says so explicitly, and notes that
>   the score fell back to a fraction of the price level.
>
> Deferred deliberately: auth (Plan 004, with the ingress) and the UI Deployment
> itself (also Plan 004). The image already carries the templates and static assets.

**Goal:** manage tracked queries, read the trends, triage the deal feed, and correct
bad specs — without any of it lying about what the numbers are.

Plans 001 and 002 are complete and on `main`. The database is live on mvmpostgres01
and migrated to `59e51e06e599`. `touchstone queries|scan|extract|deals|scans` already
drive everything from the CLI; the web face is an alternative front end over the same
calls, never a privileged one.

## Read first

- `docs/measurement-model.md` — the design spine. It governs what each number may be
  called. Most of the risk in this plan is presentational, and it is all in there.
- `AGENTS.md` — hard rules. The two that bite here: the core never imports the web
  layer (an architecture test enforces it), and no seller identifier may be stored
  (a structural test enforces that, and it will fire on a well-meaning
  "show me who's selling it" feature).

## The honesty constraints — these are the point

The UI is where a careful measurement model gets casually undone by a chart title.

1. **Never render the asking-price index as "market price", "value", or "worth".**
   It is what people are *asking*, and it is biased upward because overpriced stock
   does not sell and therefore accumulates in the only pool we can see. Label it
   "asking".

2. **The disappearance series is an inference and must be visibly separate.** A
   listing that vanished may have sold, been ended by the seller, or expired. Never
   plot it on the same axis as the asking index as though both were observations, and
   never call it "sold".

3. **Suppress aggregates with `n < 5`.** They are stored (the count is a fact) but an
   aggregate over one listing is that listing's asking price wearing a costume. Show
   the `n`, grey the figure.

4. **Draw the discontinuities.** Two stored fields exist purely so the UI can show
   them, and omitting them turns an artifact into an apparent market move:
   - `scan.min_seller_feedback` — the seller-feedback floor applied. Changing it
     changes the sampled population.
   - `cohort_key` format changed between Plan 001 and 002. Old `scan_aggregate` rows
     keep the old keys and are never rewritten, so a cohort series may simply stop
     and a new one begin.
   A vertical rule with a tooltip is enough. Silence is not.

5. **Show `scan.capped`.** A capped scan (>10,000 results) sampled a moving window,
   and its disappearances were deliberately not recorded.

## Work items

### WI-011 — App skeleton
`src/touchstone/web/app.py`, `web/templates/`, `web/static/`

FastAPI + Jinja2 + `itsdangerous` sessions, matching openbia/dossier. `create_app()`
factory taking a session factory, so tests drive it without a live server.

Extend `tests/test_architecture.py`: `web/` may import the core; nothing in
`ebay/`, `scan/`, `extract/`, `db/`, `sink/` may import `web`.

### WI-012 — Design system
`src/touchstone/web/static/css/tokens.css`

Vendor `tokens.css` from `/projects/patina` (see its `sync.sh`); do not hand-roll the
token names — the coherence contract across cert-watch/gpo-lens/sluice *is* the token
names. Pick an accent not already taken (bronze, verdigris, aqua are used);
**cupric green** was proposed. IBM Plex Mono for figures with `tabular-nums`, dark
default via `data-theme` set before first paint.

Signature element (each family tool has one, built from its subject): the **cohort
price band** — the p10–median spread drawn as a horizontal gauge with the flagged
listing marked against it. It makes "how far below its cohort" legible at a glance
and is the one view the CLI cannot give you.

### WI-013 — Queries
List, create, edit, enable/disable. Fields: `q`, `category_ids`, `filter_expr`,
`cadence_minutes`, `max_pages`, `min_seller_feedback`.

"Scan now" sets `query.scan_requested_at`; the scanner CronJob honours it on its next
pass (Plan 004). Do not invoke a scan from a request handler — a web request must
never spend API budget or block on eBay.

Surface the budget from `BudgetGuard.state()`, including whether the figure is
authoritative or the ledger fallback. Show `max_pages × cadence` as a projected daily
call cost so an expensive query is obvious before it is saved, not after.

### WI-014 — Trends
Per query and per cohort, over time: `n`, p10, p25, median, mean of price and $/GB,
read from `scan_aggregate` only. **Never recompute from `listing` rows** — there is
no code path for it and adding one destroys the property that keeps history stable.

Server-rendered inline SVG is enough and avoids a JS build; no CDN (the family CSP is
strict). Constraints 1–5 above all land here.

### WI-015 — Deal feed
Open deals by score, with cohort context (p10, n, the price band), a link out to
eBay, and dismiss. Show the spec that produced the $/GB and how it was derived
(`regex` / `llm` / `manual` + confidence) — a deal is only as trustworthy as its
capacity parse, and that is the field most likely to be wrong.

### WI-016 — Spec correction
Edit an `item_spec` by title hash; `correct_spec()` already exists and validates via
`plausible()`. A correction sets `method=MANUAL`, `confidence=1.0`, and lifts the
confidence gate on deal scoring for every listing sharing that title.

Worth surfacing: unspecced listings and low-confidence specs, sorted by how many
listings share the title, so an hour of correcting is spent where it moves the most
data.

### WI-017 — Watchlist
Pin a listing, see its full observation history as a price line.

## Not in this plan

Auth. The UI is internal-only behind `traefik-internal` and the cluster runs
authentik if it is wanted later — but decide that in Plan 004 with the ingress, not
by half-building a login here.

## Verification

- `httpx` + the FastAPI test client against the real Postgres fixture in
  `tests/conftest.py`; no new test database machinery.
- **A test that greps rendered HTML for forbidden labels** — "market price", "market
  value", "sold for", "worth" — across the trend and deal templates. The honesty
  constraints are the deliverable, so assert them rather than trusting review.
- A cohort with `n < 5` renders suppressed.
- A series spanning a `min_seller_feedback` change renders the discontinuity marker.
- Architecture test extended for the web boundary.
- Mutation-check anything load-bearing: it must fail before it passes.
- `ruff`, `mypy --strict`, full suite green.

## Exit criteria

Add a query, run `touchstone scan` and `touchstone extract` from the CLI, and see the
result appear correctly in the UI: trend, cohorts, and any flagged deal — with every
figure labelled for what it actually is.
