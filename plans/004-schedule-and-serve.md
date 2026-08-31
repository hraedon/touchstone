# Plan 004 — Put it on a schedule, and serve the UI

Plans 001–003 and 004a are complete and on `main`. The sink is deployed, the
production keyset is active, and two scans have been run **by hand** from an
operator laptop. That is the gap this plan closes: nothing in the cluster samples
anything, and the UI built in Plan 003 runs nowhere.

## Read first

- `docs/measurement-model.md` — the design spine.
- `AGENTS.md` — hard rules. The two that bite here: `scan_aggregate` is never
  recomputed (which is what makes retention pruning *safe*, and is the reason
  pruning belongs in this plan rather than being avoided), and secrets come from
  the environment.
- `deploy/k8s/README.md` — the existing sink deployment and its ordering.

## What this plan is not

**Not a rewrite of the scanner.** `run_scan` already does the work, records
`last_scanned_at`, clears `scan_requested_at`, and refuses to start when the budget
will not permit a page. What is missing is only the *selection*: which queries are
due, in what order, and when to stop.

## Work items

### WI-023 — Due-query selection
`src/touchstone/scan/schedule.py`, `touchstone tick`

Decide what to scan. `run_scan` takes one query; a CronJob needs "everything that is
due, cheapest-safe-order, stop when the allowance is gone".

Rules, each of which exists because the obvious alternative misbehaves:

- A query is due when it is enabled and either `scan_requested_at` is set, or
  `last_scanned_at` is null, or `now - last_scanned_at >= cadence_minutes`.
- **Explicit requests go first**, then most-overdue first. Round-robin would let a
  60-minute query and a 1,440-minute query take turns, which is not what either
  asked for.
- **Stop the whole pass when `usable` budget reaches zero**, rather than letting
  every remaining query record its own `SKIPPED_BUDGET` row. One refusal is a fact;
  forty identical refusals are noise that buries the one real one.
- A query that fails does not stop the pass. `run_scan` already records the failure
  against that scan; the next query is unaffected.
- **A Postgres advisory lock guards the whole pass.** A CronJob run that overlaps an
  operator's manual `touchstone scan` would double-spend the allowance and could
  interleave two writers on the same query. `concurrencyPolicy: Forbid` only stops
  the CronJob colliding with itself; the lock also stops it colliding with a person.
  Failing to take the lock exits 0 — a skipped pass is normal, not an error.

### WI-024 — Retention pruning
`src/touchstone/scan/retention.py`, `touchstone prune`

Observations accumulate at roughly `queries x listings x scans-per-day` rows a day
forever. Pruning them is safe *only* because of the load-bearing decision: statistics
were materialized at scan time and are never recomputed, so dropping the underlying
listings cannot change a single historical figure.

What is pruned: `listing_observation` older than the horizon, and then `listing` rows
left with no observations.

What is **never** pruned, and why:

- `scan_aggregate` — the history itself. It has no foreign key to `listing` for
  exactly this reason.
- `listing_disappearance` — likewise, deliberately not a foreign key.
- `scan` — `scan_aggregate.scan_id` and `deal.scan_id` are `ON DELETE CASCADE`, so
  deleting a scan row would silently take the history with it. Nothing in this
  command may touch that table.

A listing is also kept, regardless of age, when it is pinned to the watchlist or
carries an undismissed deal — both hold `ON DELETE CASCADE` foreign keys to it, so
pruning it would quietly unpin a listing or erase a flag a person had not yet looked
at.

`--dry-run` reports counts without deleting, and is the default posture in the
manifest: the CronJob ships **suspended**. Scheduling a destructive job against a
database that is a day old, with no operational experience of it, is how a retention
policy becomes an incident. It is written, tested, reviewed and ready; enabling it is
a separate, deliberate act.

### WI-025 — Scheduled work
`deploy/k8s/cronjob-*.yaml`

- **scanner** — `touchstone tick`, every 15 minutes. The cadence lives on the query;
  this only asks "is anything due". `concurrencyPolicy: Forbid`.
- **extractor** — `touchstone extract`, hourly, offset from the scanner. Separate
  because a scan must complete whether or not the model provider is reachable; that
  separation is worthless if one CronJob runs both.
- **prune** — `touchstone prune`, weekly, **suspended**.

All three run the same pinned image under the same non-root, read-only,
capability-free constraints as the sink, with `restartPolicy: Never` and bounded
history.

### WI-026 — Serve the UI
`deploy/k8s/deployment-web.yaml`, `service-web.yaml`, `ingress-web.yaml`

`uvicorn --factory touchstone.web.app:create_app` on `touchstone.k8s.hraedon.com`,
`ingressClassName: traefik-internal`. No path scoping — unlike the sink, the whole
app is the point.

**Auth: none, deliberately.** The UI is reachable only from the LAN, and the worst a
visitor can do is spend an API allowance that only affects this application. The
cluster runs authentik and this can be put behind it later, but a login screen in
front of an internal single-operator tool is ceremony, not a control. What would
change the decision: exposing it externally, or giving it any action with a cost
outside touchstone.

The UI Deployment does **not** run migrations. The sink's init container owns
`alembic upgrade head`; two workloads racing to migrate the same database on a
simultaneous rollout is a deadlock waiting for a bad day.

### WI-027 — Secrets
`touchstone-web-secrets`, and rotating what 004a reused.

004a's deployment reused the existing database credential rather than regenerating
it, and recorded that as a follow-up. Do it here, before more workloads hold it.

The web face needs `TOUCHSTONE_DSN` and `TOUCHSTONE_SECRET_KEY`; the scanner needs
`TOUCHSTONE_DSN` and the eBay keyset; the extractor also needs `UMANS_API_KEY` and
`TOUCHSTONE_EXTRACT_MODEL`. Rather than one Secret that every workload reads in
full, each workload gets only the keys it uses — the extractor has no business
holding the session key, and the UI has no business holding the eBay keyset, because
the UI is the one thing that must never be able to call eBay.

### WI-028 — Deploy and verify
Live, in this order, with the rollout confirmed at each step.

## Verification

- `tick` picks the overdue query and leaves the not-yet-due one alone; an explicit
  request jumps the queue; a zero budget stops the pass after zero scans rather than
  writing a refusal per query.
- The advisory lock is actually taken: a second `tick` against a held lock exits 0
  and scans nothing.
- **Pruning is proven not to move history**: seed observations and aggregates, prune,
  assert the aggregates and disappearances are byte-identical and the watched and
  flagged listings survived.
- Mutation-check each of those — a retention test that cannot fail is worse than none.
- The CI container smoke test additionally starts the **web** app under the
  production constraints. The UI ships in the same image and nothing currently proves
  it can start read-only and non-root.
- `ruff`, `mypy --strict`, full suite green.

## Exit criteria

A query reaches its cadence and is scanned by the cluster with no operator involved;
the result appears in the UI at `touchstone.k8s.hraedon.com`; the deletion endpoint
keeps answering throughout; and `touchstone prune --dry-run` reports a plan that
leaves every aggregate untouched.
