# Deployment

Ten tracked manifests. Nothing here contains a secret; every credential comes from an
operator-created Secret in namespace `touchstone`, and `tests/test_deploy.py` asserts
that none of these files carries one.

| Workload | What it is | Exposure |
| --- | --- | --- |
| `deployment.yaml`, `service.yaml`, `ingress.yaml` | the eBay deletion sink, plus the migration init container | **external**, `ebdel.hraedon.com`, exactly one path |
| `deployment-web.yaml`, `service-web.yaml`, `ingress-web.yaml` | the UI | internal, `touchstone.k8s.hraedon.com` |
| `cronjob-scanner.yaml` | `touchstone tick`, every 15 minutes | none |
| `cronjob-extractor.yaml` | `touchstone extract`, hourly | none |
| `cronjob-prune.yaml` | `touchstone prune`, weekly, **suspended** | none |

The Deployments intentionally have no registry credential. GitHub keeps a GHCR
package's visibility separate from its linked repository, so `ghcr.io/hraedon/touchstone`
must be set to **Public** in Package settings. Making the repository public does not
perform that second change.

## Secrets

Five Secrets, split by purpose rather than by workload, so **every value lives in
exactly one place**. That matters more than it looks: a database password duplicated
across two Secrets is a rotation that half-succeeds, and a sink that starts failing
its signature checks begins a 30-day compliance clock.

| Secret | Keys | Read by |
| --- | --- | --- |
| `touchstone-db-secrets` | `TOUCHSTONE_DSN` | everything |
| `touchstone-ebay-secrets` | `EBAY_CLIENT_ID`, `EBAY_CLIENT_SECRET` | sink, scanner |
| `touchstone-sink-secrets` | `VERIFICATION_TOKEN`, `ENDPOINT_URL` | sink |
| `touchstone-extract-secrets` | `UMANS_API_KEY` | extractor |
| `touchstone-web-secrets` | `TOUCHSTONE_SECRET_KEY` | UI |

**The UI is deliberately not given the eBay keyset.** A page load must never spend
from a 5,000-a-day allowance. That rule is enforced in code by an architecture test;
withholding the credentials makes it a property of the deployment too, so it survives
a route module that imports something it should not.

`ENDPOINT_URL` must be `https://ebdel.hraedon.com/` byte for byte, including the
trailing slash — it is hashed into eBay's challenge response and a mismatch fails
with no useful error. `VERIFICATION_TOKEN` is 32–80 characters from `A-Za-z0-9_-` and
must equal the value in eBay's Developer Portal. `TOUCHSTONE_SECRET_KEY` is 32+
characters (`openssl rand -hex 32`) and signs the UI's session cookie; changing it
logs everyone out, nothing more.

An operator-generated manifest may be saved as `secret-*.yaml`; that pattern is
gitignored.

## First application, in order

1. `namespace.yaml`.
2. Create all five Secrets.
3. `deployment.yaml`, `service.yaml`, `ingress.yaml` — wait for the rollout and the
   `ebdel-hraedon-com-tls` certificate. The init container runs `alembic upgrade head`.
4. Verify the external challenge route before saving the endpoint in eBay.
5. `deployment-web.yaml`, `service-web.yaml`, `ingress-web.yaml`.
6. `cronjob-scanner.yaml`, `cronjob-extractor.yaml`, `cronjob-prune.yaml`.

## Migrations

**Only the sink migrates.** Its init container owns `alembic upgrade head`; the UI and
the CronJobs assume the schema is current. Two workloads racing to migrate one
database during a simultaneous rollout is a deadlock waiting for a bad day, and a test
asserts that no other container invokes alembic.

Consequence for ordering: a release carrying a migration must roll the **sink first**,
and only then the UI and the CronJobs.

## Releasing

Every container in every workload is pinned to one immutable digest, and a test
asserts they are all the same one. Never deploy the mutable `main` tag —
`imagePullPolicy: Always` only applies at pod creation, so CI can be green for weeks
while a pod serves a stale build.

To release: wait for CI and its two constrained-container smoke tests (the sink's
challenge route, and the UI starting non-root on a read-only rootfs), then replace
every image field with the published manifest digest in one commit.

```bash
docker buildx imagetools inspect ghcr.io/hraedon/touchstone:main \
  --format '{{ .Manifest.Digest }}'
```

## Rotating the database credential

Replacing a Secret does **not** update the environment of a running pod. Rotate in
this order, and verify each step:

```bash
# 1. New password on the server, then update the one Secret that holds the DSN.
kubectl -n touchstone create secret generic touchstone-db-secrets \
  --from-literal=TOUCHSTONE_DSN='postgresql+psycopg://touchstone:NEW@HOST:5432/touchstone' \
  --dry-run=client -o yaml | kubectl apply -f -

# 2. Restart every workload that holds it. The CronJobs pick it up on their next run.
kubectl -n touchstone rollout restart deployment/touchstone-sink
kubectl -n touchstone rollout status  deployment/touchstone-sink
kubectl -n touchstone rollout restart deployment/touchstone-web
kubectl -n touchstone rollout status  deployment/touchstone-web
```

The sink is the one that matters: it must be answering before the next eBay
notification, because ~24h of silence marks the callback down and starts a 30-day
clock. Verify it end to end, not just that the pod is Running:

```bash
curl -s 'https://ebdel.hraedon.com/?challenge_code=rotation-check'
```

## Enabling retention

`cronjob-prune.yaml` ships **suspended**, and that is the safety property rather than
an oversight — it deletes observations, and scheduling a destructive job against a
database with no operational history is how a retention policy becomes an incident.

Pruning cannot change a historical figure: per-cohort statistics are materialized when
a scan runs and are never recomputed. Verify that rather than assume it — run the dry
run, which is the default, and read the plan:

```bash
touchstone prune --days 365          # reports; deletes nothing
kubectl -n touchstone patch cronjob touchstone-prune -p '{"spec":{"suspend":false}}'
```

Watched listings and undismissed deals are kept regardless of age, with their full
observation history: both hold cascading foreign keys to `listing`, so pruning one
would silently unpin a listing or erase a flag nobody had looked at yet. Every one of
those predicates is evaluated by the database at the moment of the delete, not read
into memory beforehand — a weekly prune routinely overlaps several fifteen-minute
scanner passes, and a listing re-observed or flagged mid-prune must survive it.

`prune --apply` also takes the same writer lock the scanner takes, so the two cannot
run at once at all. If a scan holds it, the prune reports so and deletes nothing; the
next weekly run picks it up. A dry run reads only and never waits.

## Scanning

The scanner CronJob asks "is anything due" every 15 minutes; the cadence itself lives
on each query. `concurrencyPolicy: Forbid` stops the job overlapping itself, and a
Postgres advisory lock stops it overlapping an operator running `touchstone scan` by
hand — Kubernetes cannot see that second case. A pass that cannot take the lock exits
0, because a skipped pass is normal operation and a job that reports failure for it
would train you to ignore its alerts.

The lock opens its own database connection rather than riding the scanner's session.
A Postgres advisory lock belongs to the backend that took it, and a pass commits many
times, returning that session's connection to the pool each time; a dedicated
connection makes the lock correct by construction instead of by luck.
