# Deletion compliance

What eBay requires, what touchstone does about it, and what is still open.

Source: eBay's *Marketplace User Account Deletion* guide. Quotes below are from it.

## The obligation

> Every eBay Developers Program application that is making API calls that use/store
> eBay user data must be subscribed to eBay marketplace account deletion/closure
> notifications. It is the responsibility of each developer to remove all user data
> associated with the eBay user specified in the notification.

touchstone stores seller usernames against listings, so it subscribes. The
alternative — the "Not persisting eBay data" exemption toggle — would be a false
attestation, and the guide notes that "failure to provide correct information may
result in penalties or having their account disabled."

Non-compliance is not a small thing: "Failure to comply with this requirement will
result in termination of your access to the Developer Tools, and/or reduced access
to all or some APIs."

## Settled

**Endpoint validation.** `sha256(challengeCode + verificationToken + endpoint)`, in
that order, returned as `{"challengeResponse": "<hex>"}` with
`Content-Type: application/json`. Built with a JSON library, never string
concatenation — the guide warns a hand-written response often gets a BOM prepended,
which is invalid JSON and fails the subscription. Verification token is 32–80 chars,
alphanumeric plus `_` and `-`.

**Acknowledgement.** `200`, `201`, `202`, and `204` are all acceptable. touchstone
returns `200`.

**Retry and markdown.** Unacknowledged notifications are resent. After ~24h of
unacknowledged notifications the callback is marked down and an alert email is sent;
the developer then has 30 days to fix it before being marked non-compliant. This is
why a failed purge answers `500` rather than acking: staying inside eBay's retry loop
is the mechanism that eventually gets the data deleted, and a persistent failure
*should* escalate to a marked-down endpoint rather than hide behind a green 200.

**Identifier matching — a cross-product, not paired by field.** The guide describes
`data.username` as "the publicly known eBay user ID", with the note that effective
2025-09-26 "select developers will not receive username data for U.S. users through
this field. Instead, an immutable user ID will be returned in its place."

The field is not emptied for affected users; it is populated with a *different kind
of identifier*. So `WHERE seller_username = notification.username` does not fail
loudly — it compares a user id against stored usernames, matches nothing, and looks
exactly like holding no data. `sink/purge.py` therefore matches every identifier the
notification carries against every identifier column stored.

**Unmatched is a successful purge.** touchstone samples a narrow slice of eBay and
will hold nothing for the overwhelming majority of deleting users. There is nothing
to delete, the obligation is met, and the notification is acked. The outcome is
recorded (`deletion_receipt.unmatched`) but not alarmed on per event, because one
unmatched notification is indistinguishable from a systematic linkage failure. Only
`unmatched_rate` separates them.

**Known residual gap.** Browse's `seller` object exposes only `username` — never
`userId` or `eiasToken`. If Browse gives a real username while the notification
carries only an immutable user id, there is nothing to join on and we retain data we
were told to erase. Unfixable from the data eBay exposes; pinned by
`test_known_gap_username_stored_against_userid_notification` so it stays visible.

**Volume.** "Be prepared to acknowledge up to 1500 notifications on any given day",
bursty, with many days at zero. Trivial for a Postgres-backed purge, but it is why
the notification public key is cached for an hour — the guide warns that fetching it
per notification "can result in exceeding API call limits."

## Open — needs a decision before Plan 004 activates the endpoint

### Backups can reverse a deletion

> Deletion should be done in a manner such that even the highest system privilege
> cannot reverse the deletion.

A `DELETE` in Postgres satisfies this for the live database. It does **not** satisfy
it for backups. Restoring mvmpostgres01 from a snapshot taken before a purge
resurrects the deleted seller's rows, and nothing currently re-applies the deletion.
Given the cluster runs a daily Longhorn backup, this is a live path by which purged
personal data comes back, and it is exactly the reversal the guide names.

Options, in rough order of preference:

1. **Replay purges after any restore.** `deletion_receipt` already holds the
   identifiers, so a `replay_purges()` that re-runs every receipt against a restored
   database closes the loop. Cheap, and it gives the receipt a second purpose beyond
   audit. Requires the restore runbook to actually call it — a step that is easy to
   forget, which is the weakness.
2. **Hash the identifiers in the receipt.** Store a salted hash rather than the
   plaintext username/userId/eiasToken. Replay still works (hash the restored rows
   and compare), and the receipt stops being a retained copy of the personal data we
   were told to erase. Strictly better on privacy; costs the ability to read a
   receipt and see who it was about.
3. **Bound backup retention** so pre-purge snapshots age out inside a defined window.
   Does not prevent reversal, only limits it in time. Probably a complement, not a
   solution.

Recommendation: (1) plus (2) — hashed receipts, replayed after restore, with the
replay step written into the restore runbook rather than left to memory. Not yet
implemented; the schema currently stores plaintext identifiers.

### Ack-then-verify vs verify-then-412

The guide's prose says to acknowledge immediately and verify the signature
*afterwards*. Its own description of the official SDKs says the opposite: verify
first, return `200` on success and `412 Precondition Failed` on a signature failure.
touchstone follows the SDKs, since that is the behavior eBay actually ships and the
predecessor sink implemented. Worth revisiting only if eBay's validation tooling
objects.
