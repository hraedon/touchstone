# Sink deployment

The four tracked manifests expose only the eBay callback at the exact external path
`https://ebdel.hraedon.com/`. The health check remains cluster-internal.

Two operator-created Secrets must exist before the Deployment is applied. Do not
commit either one.

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

## `ghcr-pull`

The source repository and its GHCR package are private. Create a Docker registry
Secret named `ghcr-pull` in namespace `touchstone`, using a dedicated token with only
the package-read access needed for `ghcr.io/hraedon/touchstone`.

## Order

1. Apply `namespace.yaml`.
2. Create both Secrets in that namespace.
3. Apply `deployment.yaml`, `service.yaml`, and `ingress.yaml`.
4. Wait for the rollout and `ebdel-hraedon-com-tls` certificate.
5. Verify the external challenge route before saving the endpoint in eBay.

The init container runs `alembic upgrade head` under the same non-root, read-only,
capability-free constraints as the service. A database or migration failure prevents
the new pod becoming ready while RollingUpdate leaves the prior ready pod serving.
