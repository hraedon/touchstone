# Publication review

**Status:** PASS — reviewed 2026-08-30 before changing the GitHub repository from
private to public.

## Scope and result

The complete reachable git history and the Plan 004a implementation working tree
were scanned with the estate's canonical forbidden-identifier denylist. The review
covered tracked and untracked source files, historical paths and blobs, commit
messages and metadata, and a full-history patch. It found **zero forbidden-identifier
matches**.

A separate credential and key-material scan found no private keys, GitHub or cloud
tokens, JWTs, eBay production credentials, credential files, or sensitive filename
artifacts in the working tree or history. No file beneath `samples/` or `data/` has
ever been tracked. The local `.env`, future raw API captures, and operator-generated
Kubernetes Secrets remain gitignored.

The synthetic notification, public key, and signature in `tests/test_sink_crypto.py`
are eBay's published interoperability fixture, not an observed marketplace event or
a private key. Its source and license are recorded in `THIRD_PARTY_NOTICES.md`.

## Standing controls

- `.github/workflows/identifier-gate.yml` scans tracked content and incoming commit
  messages on every push and pull request.
- The repository's `TOUCHSTONE_FORBIDDEN_IDENTIFIERS` Actions secret is populated
  from the canonical denylist. Because `publication.toml` declares `public`, CI fails
  closed if that secret is missing or empty.
- The always-on half of the gate refuses tracked runtime-data directories, local
  `.env*` files, and operator-generated Kubernetes Secret manifests even when the
  denylist secret is unavailable in a fork.
- `publication.toml` constrains the expected GitHub owner and commit identities.

## License and conditions

Touchstone is released under the MIT License. The eBay interoperability fixture's
Apache-2.0 notice is preserved separately.

Publication remains safe while raw marketplace responses, local configuration, and
operator Secrets stay untracked. Re-run this review before publishing to another
owner or importing any real marketplace capture into history.
