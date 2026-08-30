# The measurement model

This document dictates the data model. Everything in `src/touchstone/scan/` and the
schema exists to serve the distinctions drawn here. Change this document first, and
the schema second.

## What eBay lets us see

One fact governs the entire design: **we can observe active listings and nothing
else.**

- The Marketplace Insights API is the only official source of sold/completed data.
  It is a Limited Release gated on business-level approval and is routinely refused
  to individual developers.
- The legacy Finding API's `findCompletedItems` was retired in February 2025.
- Browse API `search` returns currently-listed items.

There is no supported path to sold prices for an application like this one. Any
claim touchstone makes about "what things sell for" would be fabricated. So it does
not make that claim.

## Why active asking prices are not market prices

The active pool is **survivorship-biased upward**, and the mechanism is worth stating
plainly because it determines how every number here must be labelled:

> A listing priced above what anyone will pay does not sell. It stays listed. A
> listing priced correctly sells and leaves the pool. Therefore the set of listings
> we can see is systematically enriched in overpriced inventory, and depleted of
> correctly-priced inventory, in direct proportion to how wrong the price is.

The median asking price is consequently **higher** than the median sale price, by an
unknown and category-dependent margin. This is not a defect to be corrected in a
later version; it is a property of the only data that exists. touchstone's job is to
be accurate about what it measured, not to guess at the number it cannot see.

## The three series

### 1. Asking-price index — a fact

Per cohort, per scan: `n`, `p10`, `p25`, `median`, `mean`, `min`, over both absolute
price and normalized `$/GB`.

This is a true statement about what was being asked at a point in time. It is always
labelled "asking" in the UI and in every export. It is never called market value,
fair value, or worth.

Its legitimate uses: direction and magnitude of change over time, and a reference
distribution to compare an individual listing against. Both are *relative* uses,
where the upward bias largely cancels because it applies to numerator and reference
alike.

### 2. Proxy sold series — an inference, labelled as one

A listing present in scan *N* and absent from scan *N+1* has ended. It ended because:

- a buyer bought it, **or**
- the seller ended or revised it, **or**
- it expired unsold, **or**
- it fell out of our query's result window without ending at all.

Only the first is a sale. We cannot distinguish them, so `listing_disappearance`
records the event and the last observed price, and the UI presents it as *"listings
that left the market at this price"* — never as a sold comp.

The fourth case is the one that quietly corrupts this series, and it is the reason
`listing_disappearance` stores `cohort_key` and the scan's `result_count`: a listing
that vanishes because the query hit the 10,000-item cap and re-sorted is not a market
event at all. Disappearances from a capped scan are recorded with `capped=true` and
excluded from the series.

This series costs nothing — it falls out of diffing snapshots we already store.

### 3. Deal score — conservative by construction

A listing is interesting when its `$/GB` sits below the `p10` of its cohort's current
active distribution.

The reference is biased high, so "below the 10th percentile of asking" is a *weaker*
claim than "below market" — it errs toward not flagging things. That is the correct
direction for the error: a missed deal costs nothing, a false deal costs a purchase.

Guards:
- Cohort must have `n >= 5`, or no score is computed. A percentile over three
  listings is noise.
- The spec must have `confidence >= 0.8` or come from a human correction. A
  mis-parsed capacity produces a spectacular fake bargain, and that is the single
  most likely way this system embarrasses itself.
- A listing is flagged once. Re-flagging every scan trains you to ignore it.

## Cohorts

Comparing a 32GB 2Rx4 PC4-2400 RDIMM against an 8GB UDIMM produces a number with no
meaning. A price is only comparable within a cohort of substitutable goods.

`cohort_key` for memory is the tuple:

```
(ddr_gen, form_factor, ecc, registered, capacity_per_module_gb, speed_mt, rank_org, condition)
```

Condition is in the key, not a modifier: "new" and "pulled from a decommissioned
server" are different goods at different prices, and averaging them describes
neither.

Normalization is `total_cost / total_gb`, where `total_cost` includes shipping and
`total_gb = capacity_per_module_gb × module_count`.

**The lot multiplier is the highest-consequence field in the system.** A listing
titled "Lot of 4 × 32GB PC4-2400" at $200 is $1.56/GB. Read as a single 32GB module
it is $6.25/GB — a 4× error that manufactures a nonexistent bargain *and* poisons the
cohort statistics that other listings are scored against. One mis-parsed lot can
create several false deals. This is why extraction confidence gates deal scoring, and
why lot parsing gets dedicated tests.

## Deletion, and why aggregates are materialized

`listing` rows carry **no seller identifier**. The API client drops
`seller.username` before it reaches the database, so a listing is a product offer
rather than a record about a person, and an account-deletion notification has nothing
to erase. See `docs/deletion-compliance.md` for why that is a stronger position than
deleting correctly would have been.

The aggregate rule below was originally justified by that purge: if statistics were
computed on demand from raw listing rows, honoring a deletion would retroactively
rewrite every historical chart. **That justification is gone** — but the rule stays,
because retention pruning will eventually drop old listing rows to bound table
growth, and derived statistics would silently rewrite themselves then instead. A
trend line drawn today must not differ from the same trend line drawn next year.

Therefore:

- `scan_aggregate` is computed and written **at scan time**.
- It holds no foreign key to `listing`.
- It is **never recomputed**. There is no code path that regenerates it.
- Dropping listing rows leaves aggregates untouched, so they remain a true record of
  what was observed at the time.

Aggregates with `n < 5` are stored (the count is itself a fact) but suppressed from
display: an aggregate over one listing is just that listing's asking price with a
statistical costume on.

## Which listings are sampled

The index does not measure "all active listings". It measures **active listings from
sellers with at least `min_seller_feedback` feedback**, default 1 — which excludes
zero-feedback accounts, where the obvious scam listings cluster. Those listings are
dropped in the API client, before anything is stored.

This is a noise filter, not a security control. It removes the crude fakes; it does
not stop a scammer who bought an aged account. And it excludes genuinely new,
legitimate sellers along with the bad ones. Both are accepted.

Excluding them arguably improves the index as well as the deal feed: scam listings
are typically priced far *below* market to bait, so leaving them in would drag a
cohort's floor — and the floor is exactly what deal scoring compares against.

Two things are recorded on every scan so this stays honest:

- `scan.min_seller_feedback` — the threshold actually applied. Changing it changes
  the population being sampled, so the series becomes discontinuous at that point.
  A discontinuity you cannot see is indistinguishable from a market move.
- `scan.excluded_low_feedback` — how many listings it removed. A filter that quietly
  eats most of a result set is otherwise invisible; the numbers just look calmer.

A listing whose seller has **no** feedback score reported (as opposed to a score of
zero) is kept. Unknown is not zero, and dropping on absence would silently discard
legitimate listings whenever eBay omits the field.

The seller's feedback score is read in the client to make this decision and discarded
there. It never reaches `ParsedListing` and so cannot reach the database — the same
boundary that keeps the username out.

## What is deliberately not stored

Seller identifiers, and nothing else is close. The cost is real and accepted: no
seller-level deduplication, so one seller with forty identical listings pulls a
cohort toward their price; and no seller reputation to weigh a flagged bargain by.
Both are recorded in `docs/deletion-compliance.md`.

## What the LLM may and may not touch

The truth path — observed price, shipping, timestamps, presence and absence — is
fully deterministic and never sees a model. Those are facts we recorded from an API.

The LLM's only role is proposing structured attributes from an unstructured title,
which is genuine natural-language work that regex handles badly. Every extraction is
stored with `method` and `confidence`, cached by title hash, and correctable by hand;
a correction supersedes the model permanently and re-cohorts the affected listings.

A model failure degrades extraction coverage. It must never alter, block, or delay a
recorded observation.
