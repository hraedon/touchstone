# touchstone

Tracks asking prices for sets of things on eBay over time, flags individual listings
well below their cohort, and runs a genuinely functional eBay Marketplace Account
Deletion endpoint rather than a false exemption attestation.

Named for the stone used to assay metal against a reference — which is the whole job:
a listing means nothing until you have something to compare it to.

## Why it exists

Tracking commodity hardware prices (the first case is ECC DDR4 server memory) needs
history, and eBay gives you none — the API shows you what is listed right now. So
touchstone builds its own history by sampling repeatedly and storing what it saw.

Persisting eBay data brings an obligation: any application storing it must subscribe
to Marketplace Account Deletion notifications and erase the user's data when one
arrives. The "not persisting eBay data" exemption would be a false attestation, so
touchstone subscribes.

It then has nothing to erase, by design. **No seller username, user id, or eiasToken
is stored anywhere** — the API client drops `seller.username` before it reaches the
database. A listing without its seller is a product offer, not a record about a
person. That turned out to be far stronger than deleting correctly: no fragile
identifier matching, no purge to replay after a restore, and no permanent register of
the people who asked to be forgotten kept in order to prove they were. The claim is
checkable from the schema instead of from our own logs, and three tests enforce it.

See `docs/deletion-compliance.md`, including what it costs (no seller-level
deduplication, no seller reputation signal).

## What it measures, and what it calls it

**It cannot see sold prices.** Marketplace Insights is a Limited Release refused to
individual developers, and `findCompletedItems` was retired in February 2025. Only
active listings are observable.

Active asking prices are biased *upward*: overpriced items don't sell, so they stay
listed and accumulate, while correctly-priced items sell and leave. The visible pool
is therefore enriched in exactly the listings that were wrong about price.

touchstone does not paper over this. It reports three separate things:

| | What it is |
| --- | --- |
| **Asking-price index** | Distribution of active asking prices per cohort per scan. A fact. Never labelled "market value". |
| **Proxy sold series** | Listings that left the market, at their last seen price. An *inference* — a disappearance may be a sale, a seller ending the listing, or an expiry. Labelled as such, never blended into the index. |
| **Deal score** | A listing below the 10th percentile of its cohort's active distribution. Conservative: the reference is biased high, so it errs toward not flagging. |

`docs/measurement-model.md` is the design spine and governs the schema.

## Scope

**In:** repeated sampling of user-defined eBay searches, excluding zero-feedback
sellers; per-cohort asking-price
statistics over time; title → structured spec normalization; $/GB comparison within
cohorts; below-cohort deal flagging; a watchlist of pinned listings; a web UI to
manage all of it; a compliant, functional deletion endpoint.

**Out:** buying, bidding, selling, or listing anything. Sold-price estimation.
Cross-marketplace arbitrage. Notifying anyone but the operator.

**Non-goals:** it is not a repricing tool, not a market oracle, and not a substitute
for looking at the listing before you buy.

## Design principles

- **Deterministic truth path.** Observed prices, timestamps, presence and absence
  never pass through a model. The LLM proposes structured attributes from free-text
  titles and nothing else.
- **Store no identifier you would later have to erase.** The cheapest way to honour a
  deletion request is to have nothing to delete.
- **Read-only against eBay.** touchstone only reads.
- **Aggregate at write time.** Per-scan statistics are materialized when the scan
  runs and never recomputed, so pruning old listings cannot retroactively rewrite
  history. See `docs/measurement-model.md`.
- **Name the uncertainty.** A number that is an inference is labelled an inference,
  everywhere it appears.
- **The budget is finite and known.** 5,000 Browse calls/day, application-wide. The
  scheduler reads its remaining quota from eBay rather than assuming.

## Boundary vs. siblings

Unlike `cert-watch` / `gpo-lens` / `adcs-lens`, touchstone is not a posture analyzer
over an estate you own — it samples a third-party marketplace on a schedule and keeps
the history. What it shares with them is the family shape: a deterministic core, a
read-only stance, an optional web face that imports the core and never the reverse,
and no work-domain identifiers in committed files.

## Status

Plans 001 (foundation) and 002 (specs, cohorts, deals) complete. Plan 003 is the
web UI, 004 is deployment and endpoint activation. Nothing has scanned live yet.

## Development

```bash
uv venv && . .venv/bin/activate
uv pip install -e '.[dev]'
pytest
ruff check . && mypy
```

## License

Private. Not published.
