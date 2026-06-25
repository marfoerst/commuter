# Architecture

Quick tour of how the pieces fit together. For "how the data refreshes"
and "what triggers when," see [OPERATIONS.md](OPERATIONS.md).

## File layout

```
commuter/
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── .env.example
├── README.md
├── CHANGELOG.md
├── docs/
│   ├── ARCHITECTURE.md     ← you are here
│   ├── OPERATIONS.md       ← runtime behavior + cost management
│   └── INTEGRATIONS.md     ← Home Assistant, AI assistant, etc.
└── app/
    ├── main.py             ← FastAPI app + lifespan (init DB, seed, start scheduler)
    ├── config.py           ← env var reading
    ├── api/
    │   ├── routes.py       ← commute REST endpoints (live re-rank), user-scoped
    │   ├── auth_routes.py  ← login/logout/me + admin user management endpoints
    │   └── deps.py         ← auth dependencies (require_user / require_admin)
    ├── services/
    │   ├── google_routes.py    ← Google Routes API v2 client (+ alternatives, polyline)
    │   ├── bonn_traffic.py     ← Bonn realtime feed: fetch/cache, polyline match, status
    │   ├── auth.py             ← password hashing (PBKDF2) + token helpers
    │   ├── sampling.py         ← batch sampler (parallel, semaphore-bounded), per-user
    │   ├── stats.py            ← pure stats: typical/p90, incident, window edge
    │   └── notify.py           ← per-user push (ntfy / webhook)
    ├── scheduler/
    │   └── jobs.py         ← APScheduler: daily recompute + in-window push check
    ├── db/
    │   ├── database.py     ← SQLite schema + lightweight migrations (users, routes…)
    │   ├── models.py       ← route/observation DAOs (user-scoped), stats queries
    │   └── users.py        ← users / sessions / api_usage DAOs
    └── static/
        ├── index.html      ← dashboard UI
        ├── styles.css
        └── app.js          ← heatmap rendering, fetch logic
```

## Components

### `app.services.google_routes`

Thin async wrapper over Google Routes API v2 `computeRoutes`. Each call
sends a single origin/destination/departureTime triple and returns
predicted duration in minutes. Uses `TRAFFIC_AWARE_OPTIMAL` routing
preference — the highest-quality, traffic-aware tier.

`compute_route_alternatives()` asks for multiple competing routes in one call
(`computeAlternativeRoutes`) and returns them fastest-first with a road label.
Alternatives aren't supported with `TRAFFIC_AWARE_OPTIMAL`, so this one uses
`TRAFFIC_AWARE` (one tier down, still real-time traffic).

Failure mode: returns `None` (single) / `[]` (alternatives) on HTTP error or
empty response. Callers handle it (fallback to snapshot duration, or omit the
best-effort enrichment).

### `app.services.stats`

Dependency-free (stdlib only) so it's unit-testable without a DB or network.
`summarize()` (typical/p90/spread), `classify_incident()` (live vs a baseline →
`clear`/`watch`/`alert`), and `window_edge_hint()` (best slot at the window
edge). All degrade to None/"clear" when history is thin.

### `app.services.notify`

Best-effort push delivery to an ntfy topic and/or a JSON webhook. Failures are
logged, never raised, so a flaky notifier can't break the dashboard or scheduler.

### `app.services.sampling`

The daily batch. For each active route, iterates `weekdays × time_slots`,
fires Google calls in parallel through an `asyncio.Semaphore` (default
limit 10 concurrent). Each call computes the "next occurrence" of that
weekday+slot — Google Routes API only accepts future `departureTime`
values, so we project each slot forward to its next future occurrence.

### `app.scheduler.jobs`

APScheduler async scheduler. One job: `daily_job` that runs at
`SCHEDULER_HOUR:SCHEDULER_MINUTE` on Mon-Fri only. Fires
`recompute_all_active_routes()` which sample + persist for each active
route. Misfire grace time: 1 hour (so if the host was off at 06:00 but
came up at 06:30, the batch still runs).

### `app.api.routes`

All REST endpoints. The "smart" logic lives here:

- `_compute_candidates()` — turns raw snapshot rows into ranked candidate
  dicts (filtered to future + deadline-feasible, sorted appropriately)
- `_build_live_candidate()` — combines a snapshot candidate + live duration
  (+ the slot's trailing stats) into the output shape, classifying the incident
  via `stats.classify_incident()` and attaching typical/p90; returns None if
  live conditions make it deadline-infeasible
- `_today_payload()` — the live re-rank for `/today/{direction}`, including
  expand-on-failure, route-option enrichment, live-observation recording, and
  the window-edge hint
- `commute_today_next()` — same algorithm but pre-filtered to a rolling
  N-minute window

### `app.db`

SQLite with WAL mode. Three tables: `routes`, `commute_data` (latest forecast
per slot, replaced daily), and `observations` (append-only history feeding the
typical/p90 and incident baselines). Migrations applied idempotently on startup
via `PRAGMA table_info()` introspection.

### `app.main`

FastAPI app with a lifespan handler that on startup:
1. Runs `init_db()` (creates tables + applies migrations)
2. Seeds default morning and evening routes from env if no active routes
   exist (auto-reverses evening addresses)
3. Starts the APScheduler (daily recompute, plus the push check when push is
   configured)

And on shutdown stops the scheduler gracefully.

## Data flow — a single dashboard request

```
Browser GET /api/commute/today/morning
        │
        ▼
FastAPI route handler  ──► get_route_by_name("morning")
        │                        │
        │                        ▼
        │                  SQLite SELECT … WHERE name='morning' AND is_active=1
        │
        ▼
_today_payload(client, route)
        │
        ├── get_day_data(route.id, today_weekday)        ──► SQLite SELECT
        ├── get_day_slot_stats(route.id, weekday, baseline_since) ──► observations
        ├── _compute_candidates(day_data, now, deadline)
        │       └── filter future + deadline-feasible, sort latest-first
        ├── parallel: 4 × compute_route_duration()       ──► Google Routes API
        ├── build live_candidates with _build_live_candidate() (incident vs typical)
        ├── if empty + deadline set: probe next batch (expand-on-failure)
        ├── insert_observations(live probes, source='live') ──► SQLite INSERT
        ├── compute_route_alternatives(best slot)        ──► Google Routes API (+1)
        ├── re-rank by departure_time desc (deadline) or duration asc
        └── return payload with best, alternatives, route_options,
                window_hint, typical/p90, incident_*
        │
        ▼
JSON response back to browser
```

## Data flow — daily batch

```
06:00 cron fires
        │
        ▼
recompute_all_active_routes()
        │
        ▼
for each active route:
    sample_route(route)
        │
        ├── generate_time_slots(start, end, interval)
        ├── for each weekday in route.weekdays:
        │       for each slot:
        │             create task: compute_route_duration(next_occurrence(weekday, slot))
        │
        ├── asyncio.gather all tasks (semaphore-bounded, default 10 concurrent)
        │
        ├── insert results into commute_data (replacing old rows for this route)
        └── append the same results to observations (source='batch')
```

## Routing-preference choice

We use `TRAFFIC_AWARE_OPTIMAL` for every call. Rationale:

- **Without traffic awareness**, the API just returns historical average
  duration — useless for "is there an accident on my route" detection
- `TRAFFIC_AWARE` (a tier below) gives traffic data but uses route
  preferences that may not match real driving
- `TRAFFIC_AWARE_OPTIMAL` blends traffic into route choice itself,
  which is closest to what a navigation app would do

The trade-off is cost: TRAFFIC_AWARE_OPTIMAL is the most expensive tier.
See [OPERATIONS.md](OPERATIONS.md) for cost management strategies.

## Two-tier freshness model

The repo intentionally separates "weekly pattern" (snapshot) from
"right-now answer" (live):

- **Snapshot (06:00 daily)** drives the heatmap and serves as the
  baseline for incident detection. It's stable, cached, and cheap to
  read — perfect for showing "which days/times tend to be slow."
- **Live (on every dashboard call)** drives the recommendation card,
  the alternatives strip, and the incident signal. It's expensive
  (Google Routes API call per request) but always reflects current
  conditions.

Combining the two gives both context (heatmap) and immediacy
(recommendation) without either falling stale. The cost of live calls
is bounded because:

1. Live calls happen only on actual consumer requests (no idle polling
   in the server itself)
2. Top-N candidates are queried in parallel, not sequentially (latency
   stays under ~500 ms)
3. Expand-on-failure adds calls only when needed (incident scenarios)
