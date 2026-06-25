# Changelog

All notable changes to the Commute Optimizer.

## [Unreleased]

### Added — multi-user

- **Multi-user, invite-only accounts.** Each user has their own isolated
  home/office pair, dashboard, history, Bonn matching, and notifications. An
  admin account is seeded on first start (`ADMIN_USERNAME`/`ADMIN_PASSWORD`) and
  creates/manages other users from a new **Users** tab. Upgrading an existing
  single-user install auto-migrates its routes/history onto the admin.
- **Username + password login** with a server-side session cookie (stdlib
  PBKDF2 hashing — no new dependencies). New `users`, `sessions`, and `api_usage`
  tables; `routes` gains a `user_id`. New endpoints: `login`, `logout`, `me`,
  `me/password`, `me/notifications`, `me/api-token`, and admin `admin/users…`.
- **Per-user API token** for programmatic access (`X-API-Key`) — replaces the
  retired global `API_KEY`. Home-Assistant/widget integrations now scope per user.
- **Per-user notifications.** ntfy/webhook/min-severity moved from global env to
  each user's settings; the scheduler recomputes and pushes per user.
- **Per-user daily Routes API budget** (`USER_DAILY_API_BUDGET`) on the shared
  Google key; over the cap, live lookups degrade to the stored snapshot.

### Added — Bonn real-time local traffic

- **Live local-traffic signal (Bonn open data)** — integrates the free, CC-BY
  [Straßenverkehrslage-Realtime](https://opendata.bonn.de/dataset/strassenverkehrslage-realtime)
  GeoJSON feed (Bonn's three Rhine bridges + major arterials, refreshed every
  5 min). The Bonn road segments that lie along each route are **auto-matched**
  to the Google route geometry once at config/recompute time and persisted
  (`routes.bonn_segment_ids`), so the live path adds **zero** Google cost — it
  only does one cached GET of the feed.
- **`local_traffic` in the today payload** — worst status, slowest measured
  speed, and the congested segments for the matched corridor, plus the required
  attribution string. Rendered as a per-direction panel on the dashboard.
- **Bonn folds into incident alerts** — `Staugefahr` → `alert`,
  `erhöhte Verkehrsbelastung` → `watch`; combined worst-of with the
  Google-derived severity, so existing push notifications fire on Bonn-side
  congestion too. Opt-out via `BONN_TRAFFIC_ENABLED=false`. Tunable with
  `BONN_TRAFFIC_URL`, `BONN_CACHE_SECONDS`, `BONN_MATCH_RADIUS_M`,
  `BONN_MATCH_MIN_FRACTION`.

### Added — traffic-disruption resilience

Motivated by a multi-year Bonn bridge closure: when a corridor's normal
degrades and gets less predictable, "what time" matters less than "which
route, how bad vs typical, and tell me without me looking".

- **Route-option comparison** — the live payload now includes `route_options`,
  the fastest alternative crossings/detours for the recommended departure
  (Google `computeAlternativeRoutes`, returned in a single billable call).
  Answers "which bridge is fastest right now", not just "what minute".
- **Observation history** — a new append-only `observations` table records every
  batch forecast **and** every live probe. `commute_data` still holds only the
  latest forecast per slot; history accumulates separately and powers the three
  features below.
- **Reliability (typical / p90)** — per-slot trailing median and p90 over recent
  observations. The dashboard shows "typical 38 · p90 58" and recommends padding
  for the bad days. Surfaced as `typical_duration`, `p90_duration`,
  `reliability_minutes`.
- **Incident detection re-anchored to *typical*** — incidents are now judged
  against the trailing median for the slot (when enough history exists), not
  this morning's forecast. On a chronically congested corridor the forecast
  already bakes in the jam; "vs typical" still flags the genuinely bad days.
  Falls back to the old snapshot comparison when history is thin.
- **Baseline reset** — `baseline_since` (date) on a route. Set it to the day a
  disruption began; typical/p90 and incident baselines ignore everything before
  it, so the pre-event traffic pattern stops skewing today's advice. Configurable
  from the Setup tab.
- **Window-edge hint** — when the fastest slot sits at the very start or end of
  the sampling window, `window_hint` suggests widening it (a closure shifts the
  peak earlier/later). Shown as a dismissible nudge on the dashboard.
- **Native proactive push** — opt-in `NTFY_TOPIC_URL` and/or `WEBHOOK_URL`. A
  periodic in-window check pushes when live conditions cross
  `PUSH_MIN_SEVERITY` (deduped per day, escalation-aware). No Home Assistant
  required. Spends Routes API calls only while a commute window is open.

### Added
- **Named routes** — `morning` (home → office) and `evening` (office → home),
  each with its own sampling window. Evening addresses auto-reverse from
  morning. Schema migration is idempotent.
- **Hard arrival deadline** per direction (e.g., `"09:00"` for morning).
  When set, the recommendation flips from "shortest drive" to **"latest safe
  departure that still arrives in time"**.
- **Live re-rank** — every `/today/...` request fires Google Routes API
  calls in parallel for the leave-now and top candidates, then re-ranks
  using live durations. Catches real-time conditions the morning forecast
  didn't predict.
- **`/api/commute/today/{direction}/next?minutes=N`** — best departure in
  the next N minutes. Designed for compact dashboard tiles and homescreen
  widgets.
- **Incident detection** — compares live vs the 06:00 forecast duration.
  Flags `clear` / `watch` / `alert` severity. Suitable for push
  notification triggers.
- **Expand-on-failure probing** — if the top 3 latest candidates all
  violate the deadline live (incident scenario), automatically probe
  earlier batches up to MAX_PROBE_BATCHES (3 → max 9 live calls). Avoids
  the "no slot" cliff when conditions degrade.
- **Daily batch restricted to weekdays** — cron now `mon-fri` instead of
  every day. Saves ~10% of monthly Google API calls.
- **MIT license + cleaner README** with two-direction model documented.

### API response shape (current)

`GET /api/commute/today/{direction}` returns:

```jsonc
{
  "name": "morning",
  "day_of_week": "Tue",
  "origin": "...",
  "destination": "...",
  "arrival_deadline": "09:00",            // null if not set
  "best_departure_time": "08:00",         // latest-safe when deadline set
  "optimal_duration": 59,                 // live minutes
  "arrival_time": "08:59",
  "buffer_minutes": 1,                    // null if no deadline
  "current_duration": 73,                 // live "leave now"
  "time_savings": 14,                     // current - optimal
  "live": true,
  "incident_severity": "clear",           // clear | watch | alert
  "incident_delta_minutes": 2,            // live - snapshot
  "incident_note": "Conditions normal.",
  "alternatives": [                       // top 3 feasible, live-rechecked
    { "departure_time": "08:00",
      "duration_minutes": 59,
      "snapshot_duration_minutes": 59,
      "arrival_time": "08:59",
      "buffer_minutes": 1,
      "delta_minutes": 0,
      "incident_severity": "clear",
      "live": true },
    /* ... */
  ]
}
```

`GET /api/commute/today/{direction}/next?minutes=60` returns the same shape
but filtered to slots departing within the next N minutes, wrapped under a
`best` object instead of being at the top level. See README.

### Schema

```
routes:
  id, name (NEW), origin, destination,
  time_window_start, time_window_end, interval_minutes, weekdays,
  arrival_deadline (NEW), is_active, created_at

commute_data:
  id, route_id, day_of_week, departure_time, duration_minutes, created_at

observations:                                    # NEW — append-only history
  id, route_id, day_of_week, departure_time,
  duration_minutes, source ('batch'|'live'), observed_at
```

Migrations: `name`, `arrival_deadline`, and `baseline_since` columns added
idempotently; the `observations` table is created if absent. Old single-route
deployments migrate automatically (existing row gets `name = 'morning'`,
`arrival_deadline` / `baseline_since` stay NULL). History simply starts
accumulating from the first batch/live call after upgrade — until it builds up,
reliability and typical-baseline features degrade gracefully to the previous
snapshot-only behaviour.
