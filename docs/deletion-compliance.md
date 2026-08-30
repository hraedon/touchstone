# Deletion compliance

What eBay requires, and why touchstone's answer is "there is nothing to delete."

Source: eBay's *Marketplace User Account Deletion* guide. Quotes are from it.

## The obligation

> Every eBay Developers Program application that is making API calls that use/store
> eBay user data must be subscribed to eBay marketplace account deletion/closure
> notifications. It is the responsibility of each developer to remove all user data
> associated with the eBay user specified in the notification.

touchstone persists eBay data — listings, titles, prices — so it **subscribes**. The
"Not persisting eBay data" exemption toggle would be a false attestation, and the
guide notes that "failure to provide correct information may result in penalties or
having their account disabled." Non-compliance costs access: "termination of your
access to the Developer Tools, and/or reduced access to all or some APIs."

## The design: store no user identifier at all

**touchstone stores no eBay seller username, user id, or eiasToken.** The API client
drops `seller.username` from the Browse response before it reaches the database layer,
and no table has a column for it. A deletion notification is acknowledged and
recorded; nothing is erased, because nothing attributable to a person was ever kept.

A listing row stripped of its seller is a *product offer* — an item id, a title, a
price, a timestamp. That is market data, not personal data about a user. It is the
same reasoning that lets `scan_aggregate` stand on its own, applied one level down.

### Why this beats deleting correctly

An earlier design did store the seller and purged on notification. It worked, and
every problem it created came from the storing:

1. **Matching was fragile.** eBay documents `data.username` as "the publicly known
   eBay user ID", noting that since 2025-09-26 select developers receive "an
   immutable user ID returned in its place" for U.S. users. The field is never empty
   — it is filled with a *different kind of identifier* — so a paired
   `username → seller_username` match silently returns nothing and is
   indistinguishable from holding no data. Correct matching required a cross-product
   of every notification identifier against every stored column, and could *still*
   fail when eBay's two identifier spaces diverged for one person, because Browse
   exposes only `seller.username` and never the userId or eiasToken.

2. **Backups reversed it.** The guide requires deletion "in a manner such that even
   the highest system privilege cannot reverse the deletion." A `DELETE` does not
   cover backups, and this cluster's Longhorn volumes carry
   `recurring-job-group.longhorn.io/default: enabled`, so every volume is backed up
   nightly with 21-day retention. A restore resurrected purged data.

3. **The fix needed a register of the erased.** Replaying purges after a restore
   required keeping each notification's identifiers — a permanent, growing list of
   the people who had asked to be forgotten, retained in order to prove they had
   been forgotten. That is a worse privacy posture than the problem it solved.

Not storing the seller removes all three at once. There is no match to get wrong, no
purge to replay, and no register to keep.

### It is also a checkable claim

"We deleted their data" can only be evidenced by logs we wrote about ourselves.
"There is no column in which their data could be" is verifiable from the schema, by
anyone, at any time. Three tests enforce it:

- no table in the model metadata has a seller/username/user_id/eias_token column;
- the *live* database schema agrees, so the models and the database cannot drift
  apart into a column nobody noticed;
- the API client does not parse `seller` even when the response contains it — asserted
  against a fixture that definitely has one, so the test proves we decline to read it
  rather than that eBay withheld it.

### What it costs

- **No seller-level deduplication.** One seller listing forty identical DIMMs
  contributes forty rows to a cohort and pulls its distribution toward their price.
  Mitigation if it becomes a problem: collapse on `(title_hash, price)`, which needs
  no seller data — with the caveat that two genuine sellers at the same price is
  exactly the case where the price *is* market consensus, so collapsing would hide
  real signal. Currently accepted and unmitigated.
- **No seller reputation as a deal-quality signal.** A flagged bargain from a new
  account and one from a 99.9% seller look identical here. The feed is a "worth
  looking at" list, not an auto-buyer, so the judgement happens on the listing page.

### The judgement call, stated plainly

This rests on reading a de-attributed listing as non-personal data. The alternative
reading — that any record of a user's listing is data "associated with" them,
whether or not it names them — would require deleting the listings too, which is
impossible without storing the identifier that makes it possible. The position taken
here is that the identifier is the personal data, and that removing the ability to
attribute a listing to a person is a stronger privacy outcome than retaining that
ability in order to exercise it later. Recorded so the reasoning can be revisited if
eBay says otherwise.

## Settled mechanics

**Endpoint validation.** `sha256(challengeCode + verificationToken + endpoint)`, in
that order, returned as `{"challengeResponse": "<hex>"}` with
`Content-Type: application/json`. Built with a JSON library, never string
concatenation — the guide warns a hand-written response often gets a BOM prepended,
which is invalid JSON and fails the subscription. Verification token: 32–80 chars,
alphanumeric plus `_` and `-`.

**Acknowledgement.** `200`, `201`, `202`, `204` all acceptable. touchstone returns
`200` after recording the receipt.

**Retry and markdown.** Unacknowledged notifications are resent. After ~24h of
silence the callback is marked down and an alert email sent; 30 days to fix before
being marked non-compliant.

**Signature verification.** ECDSA P-256 over SHA-1 of the payload re-serialized as
eBay's Go SDK marshals it; public key from `getPublicKey`, cached ~1h. The guide's
prose says to acknowledge first and verify afterwards; its description of the
official SDKs says the opposite (verify, then `200` or `412 Precondition Failed`).
touchstone follows the SDKs, since that is the behaviour eBay actually ships.

**Volume.** "Be prepared to acknowledge up to 1500 notifications on any given day",
bursty, many days at zero. The public key is cached because the guide warns that
fetching it per notification "can result in exceeding API call limits."

## Residual note

`DROP COLUMN` in Postgres is a catalog change; the bytes stay in the heap until those
pages are rewritten. The migration that removed the seller columns carries a note: if
it is ever applied to a database that really held seller values, follow it with
`VACUUM FULL` and let the WAL rotate. It did not apply when written — touchstone had
never connected to eBay, so no real seller value was ever stored.
