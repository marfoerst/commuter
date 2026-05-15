# Changelog

All notable changes to the Commute Optimizer.

## [Unreleased]

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
```

Migrations: `name` and `arrival_deadline` columns added idempotently. Old
single-route deployments migrate automatically (existing row gets
`name = 'morning'`, `arrival_deadline` stays NULL).
