# Fixture provenance and live verification

The committed fixtures are synthetic and were originally hand-authored from eBay's
documented response schema. They contain no observed listing or seller value.

After production activation on 2026-08-30, two ten-result Browse responses were
inspected only in memory: the first for field presence and types, the second through
touchstone's real `parse_item_summary()` boundary. The response bodies and all field
values were discarded. Only the aggregate, identifier-free report below was retained.

This deliberately tightens the original plan, which proposed keeping a raw response
under gitignored `samples/`. A raw `ItemSummary` carries `seller.username`; retaining
it was unnecessary once the same compatibility check could be done in memory.

## Live field report

The counts below describe one ten-item production response. A second ten-item
response independently parsed 10/10 through touchstone. They verify compatibility,
not an API guarantee that optional fields will always have the same presence rate.

| Field | Production observation | Client treatment |
| --- | --- | --- |
| `itemId` | 10/10 strings; 10/10 matched the `v1\|<id>\|<number>` form | Required and retained as the listing key |
| `title` | 10/10 strings | Retained verbatim |
| `price.value` / `price.currency` | 10/10 strings | Required; numeric value parsed for storage |
| `seller.username` | 10/10 strings | Deliberately discarded before `ParsedListing` |
| `seller.feedbackScore` | 10/10 integers | Read only for filtering, then discarded |
| `condition` / `conditionId` | 10/10 strings | Retained when present; remains nullable |
| `buyingOptions` | 10/10 lists with string elements | Retained as a tuple of strings |
| `shippingOptions` | Non-empty on 10/10; first `shippingCost.value` present on 8/10 and a string | Missing cost remains `None`, never inferred as free |
| `itemWebUrl` | 10/10 strings | Retained when present |
| `image.imageUrl` | 10/10 strings | Retained when present |
| `categories[0].categoryId` | 10/10 strings | Retained when present |
| `itemCreationDate` | 10/10 strings | Observed but deliberately unused |
| `estimatedAvailabilities` | 0/10 | Deliberately unused |

The top-level response fields were `href`, `itemSummaries`, `limit`, `next`, `offset`,
and `total`. The live item union included additional documented presentation and
location fields, but touchstone does not parse them because they are outside the
measurement model.

## Shipping is optional at the value boundary

The first shipping option existed for all ten sampled items, but two lacked a nested
`shippingCost`. That is the distinction the client already models:
`shipping_cost=None` means unknown, while an explicit `"0.00"` means free shipping.
Conflating those states would bias every affected total and `$/GB` figure downward.

## Reconciliation result

- All ten items in the parser-validation response produced a `ParsedListing`.
- `ParsedListing` contains item, price, shipping, condition, buying-option, URL,
  image, and category fields only. It has no seller username, user id, eiasToken, or
  seller feedback field.
- The synthetic values and types already matched every field the client consumes, so
  no observed value was copied. The fixture builder was extended to reproduce one
  structural variant seen live: a non-empty shipping option with no `shippingCost`.
- `itemCreationDate` is available on the sampled summaries. Using it to distinguish a
  genuinely new listing from one entering the result window remains a future
  measurement-model decision, not an opportunistic parser change.
