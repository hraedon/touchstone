# Plan 001 — Foundation: the truth path

**Goal:** a scan of an eBay search produces correct `listing_observation` rows and
correct, immutable `scan_aggregate` rows, provably, without a live eBay account.

Everything downstream (specs, cohorts, deals, UI) is built on these two tables. If
they are wrong the rest is decoration, so this plan ends with them proven against
recorded fixtures rather than with a running service.

## Scope

**In:** schema + migrations; the eBay client (OAuth token caching, Browse search
pagination, rate budget); the scanner; a fake eBay serving recorded fixtures; the
CLI entry point.

**Out:** title extraction, cohorts, $/GB, deal scoring (Plan 002); web UI (Plan 003);
deployment and endpoint activation (Plan 004). No live API calls in this plan.

## Work items

### WI-001 — Schema and migrations
`src/touchstone/db/models.py`, `migrations/`

Tables per `docs/measurement-model.md`: `query`, `scan`, `listing`,
`listing_observation`, `listing_disappearance`, `scan_aggregate`, `rate_budget`.
`item_spec`, `deal`, `watch` are declared now (Plan 002 populates them) so the
cohort FK shape is settled before data exists.

Load-bearing constraints, in the schema rather than in a docstring:
- `listing` is the only table with seller identifier columns. Nothing else may carry
  them; the purge finds personal data by deleting from exactly one place.
- `scan_aggregate` has **no** FK to `listing` and no identifier columns.
- `listing_observation` cascades on `listing` delete, so a purge cannot orphan rows.
- Unique `(listing_id, scan_id)` on observations — a scan sees a listing once.

### WI-002 — eBay client
`src/touchstone/ebay/client.py`, `ebay/auth.py`, `ebay/budget.py`

- httpx. One client, connection pooling, explicit timeouts.
- Application token via client-credentials, cached ~2h less a safety margin. The
  1,000/day token cap makes caching mandatory, not an optimization.
- `search()` paginating with `limit<=200`, `offset` a multiple of `limit`, stopping
  at the 10,000 result cap and returning `capped=True` when it hits it — a capped
  scan's disappearances are not market events.
- Budget: read remaining quota from Developer Analytics `getRateLimits` and treat it
  as truth. On failure fall back to the local `rate_budget` ledger. **Never fall
  back to unlimited** — assert this with a test.

### WI-003 — Fake eBay + fixtures
`tests/fake_ebay.py`, `tests/fixtures/`

A local HTTP server speaking the Browse search response shape, driven by recorded
JSON. Needs to reproduce: multi-page results, the 10k cap, a listing appearing then
disappearing across scans, price changes between scans, and OAuth token issuance.
Fixtures are hand-authored (no live account yet) but shaped from the documented
response schema; a `RECORDED.md` notes which fields are assumed and must be
re-verified against the first live response.

### WI-004 — Scanner
`src/touchstone/scan/runner.py`, `scan/aggregate.py`, `scan/diff.py`

One scan: resolve query → page through results within budget → upsert `listing`
(refresh `last_seen`) → insert one `listing_observation` per result → diff against
the previous scan to record `listing_disappearance` (skipped when `capped`) →
compute and write `scan_aggregate` → close out `scan`.

Aggregates are computed in this pass, from the rows just observed, and written once.
There is deliberately no recompute function.

Cohorting needs specs, which arrive in Plan 002. Until then aggregates are keyed on
`(query_id, condition)` — a coarse but honest cohort — and the `cohort_key` column is
sized for the full tuple.

### WI-005 — CLI
`src/touchstone/cli.py`

`touchstone scan --query <id>`, `touchstone queries list|add`, `touchstone budget`.
Enough to drive the system without a UI.

## Verification

- **Postgres, file-backed, per-test transaction rollback.** Not SQLite, not
  in-memory: `create_all` without `drop_all` hides cross-test bleed on in-memory
  SQLite, and the schema uses Postgres types.
- **Two-scan sequence** against the fake: assert observation counts, that a price
  change produces two observations with different prices for one listing, and that a
  vanished listing produces exactly one `listing_disappearance`.
- **Immutability of aggregates:** compute aggregates, delete a listing and its
  observations, assert `scan_aggregate` rows are byte-identical. This is the property
  the whole deletion story rests on, so it gets a direct test.
- **Capped scan:** assert a scan that hits the 10k cap records `capped=True` and
  produces **zero** disappearances.
- **Budget:** assert a scan is refused when reported quota is insufficient, and that
  a `getRateLimits` failure degrades to the ledger rather than to unlimited.
- **Mutation-check:** every test above must be shown failing against the code it
  guards before the implementation lands.
- `ruff check`, `mypy --strict`, and the architecture test (core imports no web, no
  model provider) all green.

## Exit criteria

`pytest` green against fixtures, `mypy --strict` clean, and a two-scan sequence whose
observations, disappearances, and aggregates are correct by inspection. No live eBay
call has been made.

## Deliberately deferred

- **Field-level verification against a real response.** Fixtures are shaped from the
  documented schema; the eBay docs site was timing out during planning. Whether
  `itemCreationDate` is present on `ItemSummary`, and the exact shipping-cost shape,
  must be confirmed against the first live response and the fixtures corrected. This
  is tracked, not forgotten — `tests/fixtures/RECORDED.md` lists every assumed field.
