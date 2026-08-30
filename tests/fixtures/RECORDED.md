# Fixture provenance

These fixtures are **hand-authored from eBay's documented response schema**, not
recorded from a live account — touchstone has a production keyset but the endpoint
is not yet activated, so no real response has been observed.

That makes them a shaped guess, and a guess in a fixture is exactly the kind of thing
that produces a confidently green suite over a client that cannot parse reality. Every
assumed field is listed here so it can be checked against the first live response
rather than quietly trusted.

## Assumed present on `ItemSummary`

| Field | Assumption | Confidence |
| --- | --- | --- |
| `itemId` | Present, string, `v1\|<id>\|0` form | High — documented, used as the PK |
| `title` | Present, string | High |
| `price.value` / `price.currency` | Present; `value` is a decimal **string**, not a number | High |
| `seller.username` | Present | Medium — assumed always populated |
| `condition` / `conditionId` | Present for most items; both nullable in our schema | Medium |
| `buyingOptions` | Array of strings | High |
| `shippingOptions[0].shippingCost` | Present when shipping is known; **absent for freight/local-pickup** | **Low** |
| `itemWebUrl`, `image.imageUrl`, `categories[0].categoryId` | Present | Medium |

## Not assumed, and deliberately unused

- `itemCreationDate` — may or may not be on `ItemSummary`. Nothing depends on it. If
  it is present it would let us distinguish a genuinely new listing from one that
  merely entered our result window, which would materially improve the disappearance
  series. Worth re-checking against a live response.
- `estimatedAvailabilities` / any sold-quantity field — believed to be on `getItem`
  only, not `ItemSummary`. A per-item `getItem` call costs one unit of the 5,000/day
  budget, so this is only affordable for the watchlist, never for a full scan.

## The one that will bite first

`shippingOptions` absence. `total_cost = price + shipping`, and the client treats a
missing shipping cost as `None` (recorded) rather than `0.0` (assumed). A listing with
free shipping and a listing with unknown shipping must not both record `0.00` — one is
a fact and the other is missing data, and conflating them biases every `$/GB` figure
downward for the affected cohort.

## On first live response

1. Capture one raw `item_summary/search` response to `samples/` (gitignored — it
   contains seller identifiers, which the purge must be able to reach).
2. Diff its field set against this table.
3. Correct the fixtures, then re-run the suite and confirm it still passes for the
   right reasons.
