# Sink deployment

The four tracked manifests expose only the eBay callback at the exact external path
`https://ebdel.hraedon.com/`. The health check remains cluster-internal.

One operator-created Secret must exist before the Deployment is applied. Do not
commit it.

## `touchstone-sink-secrets`

Create this Opaque Secret in namespace `touchstone` with exactly these keys:

- `TOUCHSTONE_DSN`
- `VERIFICATION_TOKEN`
- `ENDPOINT_URL`
- `EBAY_CLIENT_ID`
- `EBAY_CLIENT_SECRET`

`ENDPOINT_URL` must be `https://ebdel.hraedon.com/`, byte for byte, including the
trailing slash. The verification token must be 32–80 characters from
`A-Za-z0-9_-`, and must be the same value entered in eBay's Developer Portal.

An operator-generated manifest may be saved as `secret-sink.yaml`; that name is
gitignored.

## Order

1. Apply `namespace.yaml`.
2. Create `touchstone-sink-secrets` in that namespace.
3. Apply `deployment.yaml`, `service.yaml`, and `ingress.yaml`.
4. Wait for the rollout and `ebdel-hraedon-com-tls` certificate.
5. Verify the external challenge route before saving the endpoint in eBay.

The image is public and both containers are pinned to the same registry digest. To
release a new revision, wait for CI and its constrained-container smoke test to pass,
then replace both image fields with the published manifest digest in one commit.
Never deploy the mutable `main` tag.

The database hostname must be reachable from the cluster, and the DSN role must own
the schema (or otherwise have the DDL rights Alembic needs). The init container runs
`alembic upgrade head` under the same non-root, read-only, capability-free constraints
as the service. A migration failure blocks the first deployment; on later rolling
updates, the prior ready pod remains available.

Replacing a Kubernetes Secret does not update environment variables in an existing
pod. After rotating the database password, eBay credentials, or verification token,
restart the Deployment explicitly and verify the rollout before changing the value in
eBay's Developer Portal.
