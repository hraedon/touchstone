# AGENTS.md — conventions for touchstone

Read `docs/measurement-model.md` before touching the schema or anything in
`src/touchstone/scan/`. It is the design spine; the data model exists to serve the
distinctions it draws.

## Hard rules

**No model in the truth path.** Observed price, shipping, currency, timestamps,
presence and absence are recorded exactly as the API returned them. No LLM, no
heuristic, no inference touches them. The extractor proposes structured attributes
from free-text titles — that is its entire remit. If you find yourself wanting a
model to fill in a price, stop.

**Read-only against eBay.** touchstone issues GETs. It never bids, buys, lists,
messages, or writes anything to eBay. The only deletion it performs is of its own
data, on eBay's instruction.

**`scan_aggregate` is written once and never recomputed.** There must be no code path
that regenerates it from `listing` rows. This is what keeps a deletion from
retroactively rewriting history. A "let's just recompute the aggregates" refactor
silently destroys the property; if one is ever needed, it is a schema migration with
a written rationale, not a background job.

**Never label an inference as a fact.** The proxy sold series is a disappearance
series. Call it that in code, in the schema, in the UI, and in exports. Do not add a
`sold_price` column.

**No work-domain identifiers in committed files.** Placeholders only. Homelab
identifiers (`hraedon`, `mvm*`, `ad.hraedon.com`) are fine. The work-domain set is
not, and is blocked mechanically by the identifier gate — `scripts/install-git-hooks.sh`
installs it, CI re-checks it. Describe the denylist; never quote an entry from it.

**Never commit observed listing data.** `samples/` and `data/` are gitignored. A
committed snapshot contains seller identifiers, which the purge cannot reach — it
would put personal data permanently beyond the deletion endpoint's ability to erase
it. That is precisely the failure the endpoint exists to prevent.

**Secrets come from the environment.** No credential in a tracked file, including
`deploy/k8s/secret-*.yaml` (gitignored, operator-generated). Do not copy the umans
key out of `~/.config/opencode/opencode.json`.

## Correctness by construction

- `mypy --strict` in CI from day one, alongside ruff and pytest.
- `typing.assert_never()` in the default branch of every dispatch over a closed set
  (scan status, extraction method, buying option, disposition). This is the
  deliberate substitute for a compiler's exhaustive-match check, and it matters more
  under agent authorship: a human reviewer is least likely to spot the one site out
  of N that a refactor forgot.
- A new test must be shown to fail against the code it tests before the fix lands.
  The predecessor repo's suite passed 9/9 while the sink was acking deletions it had
  never performed — a green suite that cannot fail manufactures confidence in the
  next reviewer.

## Architecture

```
src/touchstone/
  ebay/       API client — OAuth, Browse search, rate budget. httpx only.
  scan/       The truth path: snapshot → observations → aggregates. Deterministic.
  extract/    Title → spec. Regex fast path, umans fallback. Never inline in a scan.
  sink/       eBay account-deletion endpoint + the purge.
  web/        FastAPI + Jinja2 UI. Imports the core; the core never imports web.
```

The import direction is enforced by an architecture test. `scan/` must remain
importable and runnable with no web framework and no model provider configured.

**httpx, never urllib.** A `urllib` TLS fingerprint draws a Cloudflare 1010 that is
indistinguishable from the provider being down — it has cost this estate two wrong
diagnoses. Probe with the library the app actually uses.

## The extractor

- Model must be a non-`-lab` umans id. `-lab` tiers are not reliable and config
  validation rejects them at startup — the rule is enforced, not documented.
- Extraction is a separate CronJob. A scan completes and stores observations whether
  or not the model is reachable. Unextracted listings queue with `method=null`.
- Cache by normalized title hash, not by listing. Cost is bounded to distinct titles.
- Umans fails transiently at any tier. Retry; do not blacklist a model in code.
- A human correction supersedes the model permanently and re-cohorts affected
  listings.

## Rate budget

5,000 Browse calls/day and 1,000 OAuth token calls/day, application-wide. The
scheduler reads remaining quota from the Developer Analytics `getRateLimits` endpoint
and treats that as truth, falling back to the local ledger only when that call fails
— and a failure degrades to the *ledger*, never to "unlimited". A local counter that
silently drifts is a check whose failure mode is silence.

## Deletion endpoint

The purge is a compliance claim, so it is proven rather than asserted. The test seeds
a listing for a synthetic seller, posts a signed notification, and asserts: the
listing and its observations are gone, `scan_aggregate` is **unchanged**, and the
sink answered 200 only after the delete committed. A failed purge answers 500 so
eBay retries — never ack a purge that did not happen.
