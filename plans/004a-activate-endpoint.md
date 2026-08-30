# Plan 004a — Expose the sink and activate the keyset

**Do this before Plan 003, not after.**

## Why it moves first

eBay activates a production keyset only once the application is subscribed to
Marketplace Account Deletion notifications, and subscription succeeds the moment the
endpoint answers eBay's validation challenge. Until then **no production API call
works at all** — touchstone cannot scan, so every number in the UI Sol is about to
build would come from hand-authored fixtures.

Doing it now also closes the deferred item in `tests/fixtures/RECORDED.md`: with a
live keyset we can capture one real `item_summary/search` response and correct the
guessed field shapes, rather than discovering them wrong at the end.

## What activation actually requires

Very little, and that is the point. The validation handshake is:

```
challengeResponse = sha256(challenge_code + verification_token + endpoint_url)
```

A pure hash. **No database, no eBay keyset, no scanner, no extractor, no UI.** The
whole thing gating production access is a few lines plus a public HTTPS route.

The POST path (the "Send Test Notification" button, and real notifications
afterwards) needs more — signature verification and the receipt write — but the
keyset is already active by then, so there is no ordering problem.

## Scope

Split from Plan 004. **In:** the sink service, its image, and its external ingress.
**Out:** scanner and extractor CronJobs, the internal UI ingress, retention — all of
which stay in Plan 004 proper, after Plan 003.

## Work items

### WI-018 — Port the crypto
`src/touchstone/sink/crypto.py`

From `hraedon/ebay-deletion-sink` PR #1 (reviewed and fixed; merge it first). Needs:
challenge response; Go-parity canonical serialization of the notification (compact,
struct field order, HTML-escaped `&<>`); ECDSA P-256 over SHA-1; `getPublicKey` with
a ~1h cache.

Two changes on the way in: **httpx, not urllib** (estate rule), and the PEM
normalizer stays — eBay has returned the key both split and inline, and Python's
`cryptography` is stricter than Go's decoder about it.

Test the challenge hash against eBay's own worked example from the guide, not only
against our own implementation. A hash function agreeing with itself proves nothing.

### WI-019 — The service
`src/touchstone/sink/app.py`

- `GET /?challenge_code=…` → `200 {"challengeResponse": "<hex>"}`, built with a JSON
  library. The guide warns a hand-written string response often gets a BOM prepended,
  which is invalid JSON and fails subscription outright.
- `GET /healthz`
- `POST /` → verify signature, `handle_deletion()`, `200`. Invalid signature → `412`,
  matching the official SDKs.

### WI-020 — Image and CI
Dockerfile, non-root, read-only rootfs, `.github/workflows` publishing to
`ghcr.io/hraedon/touchstone`.

### WI-021 — Deploy
`deploy/k8s/` — namespace, Deployment (replicas 1), Service, and an external Ingress
copying the `usage-dashboard-readings-ext` pattern: `ingressClassName: traefik-external`,
`cert-manager.io/cluster-issuer: letsencrypt-prod-porkbun`,
`traefik.ingress.kubernetes.io/router.entrypoints: websecure`, **path-scoped** so only
the sink routes are exposed.

Secret (operator-generated, gitignored): `TOUCHSTONE_DSN`, `VERIFICATION_TOKEN`,
`ENDPOINT_URL`, `EBAY_CLIENT_ID`, `EBAY_CLIENT_SECRET`. The DSN password is
regenerated here rather than rotated earlier.

### WI-022 — Register and activate
Developer Portal → Alerts and Notifications: alert email, endpoint URL, verification
token, Save. eBay sends the challenge immediately. Then **Send Test Notification** to
exercise the POST path, and confirm a `deletion_receipt` row lands.

Then capture one live search response into `samples/` (gitignored — it contains
`seller.username` even though we never store it) and reconcile
`tests/fixtures/RECORDED.md`.

## Operator prerequisites

- **DNS:** `ebdel.hraedon.com` CNAME → `ingress-ext.hraedon.com`, matching
  `usage.hraedon.com` and `vitrine.hraedon.com`. Created and resolving 2026-08-30.
- Port 443 reachable from the internet at that ingress — same path the existing
  external hosts use.
- **Image:** change the linked `ghcr.io/hraedon/touchstone` package visibility to
  Public. GHCR visibility is separate from repository visibility and GitHub exposes
  this change only through Package settings.
- The production eBay keyset, for the POST path.

## The commitment this creates

Once validated, eBay sends real deletion notifications to this endpoint indefinitely.
Unacknowledged ones are resent, and ~24h of silence marks the callback down, with 30
days to fix before non-compliance. Exposing it is an ongoing uptime commitment, not a
one-off — modest, since the handler only writes a receipt, but real. The
`EBAY_CLIENT_*` values must be in the Secret from the first deploy: without them
signature verification fails, the endpoint returns 412 to everything, and the
markdown clock starts.

## Verification

- Challenge hash matches eBay's published worked example.
- A signed notification round-trips to `200` and one receipt; a tampered one → `412`;
  a redelivery does not create a second receipt.
- The container serves a correct challenge under the manifest's own constraints
  (read-only rootfs, non-root, all capabilities dropped).
- Live: eBay's Save succeeds, the keyset flips to active, and Send Test Notification
  produces a receipt row.
