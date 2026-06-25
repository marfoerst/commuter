# Commute Optimizer

Self-hosted commute optimizer. Samples the Google Routes API across your
weekly commute windows, stores results in SQLite, and shows the best time to
leave as a heatmap. Exposes a small REST API so anything you like — a
dashboard, a notification system, a chat assistant — can consume the data.

## Features

- FastAPI backend + a no-build HTML/JS UI
- Two named routes — `morning` (home → office) and `evening` (office → home),
  each with its own sampling window. Evening addresses are auto-reversed from
  morning.
- **Hard arrival deadline** (optional, per direction). When set, the system
  recommends the **latest safe departure** that still arrives in time, and
  surfaces a top-3 alternatives strip ranked by departure time.
- **Route-option comparison** — for the recommended departure, shows the fastest
  alternative crossings/detours (which bridge/road is quickest right now).
- **Reliability + typical baseline** — accumulates a history of observations and
  reports the *typical* and *p90* drive per slot, so you can pad for the bad days.
- **Baseline reset** for disruptions — set the date a major change took effect
  (e.g. a bridge closure) and stats ignore the pre-event traffic pattern.
- **Window-edge hint** — nudges you to widen the sampling window when the peak
  shifts earlier/later than your window covers.
- **Native proactive push** (opt-in) via ntfy or a webhook — no Home Assistant
  required.
- Google Routes API v2 (`computeRoutes`, traffic-aware) for live travel times
- APScheduler runs a full weekly recompute every day at a configurable hour
  (default 04:00)
- Live dashboard endpoint: every request makes two Routes API calls in
  parallel — "leave now" and the recommended slot — so the numbers are fresh
  on every page view
- SQLite persistence on a single bind-mounted volume
- Single Docker container; runs anywhere Docker runs (tested on Synology
  Container Manager and Docker Desktop)

## Project layout

```
commuter/
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── .env.example
└── app/
    ├── main.py            # FastAPI app + lifespan
    ├── config.py          # env config
    ├── api/routes.py      # REST endpoints
    ├── services/
    │   ├── google_routes.py   # Routes API client
    │   └── sampling.py        # parallel sampler
    ├── scheduler/jobs.py  # APScheduler daily job
    ├── db/                # SQLite schema + DAOs
    └── static/            # index.html, styles.css, app.js
```

## Quick start (Docker)

1. Get a Google Cloud API key with the **Routes API** enabled
   (Cloud Console → APIs & Services → Credentials). Restrict the key to the
   Routes API and your server's IP.
2. Copy `.env.example` to `.env` and fill in `GOOGLE_API_KEY`. Optionally set
   `DEFAULT_ORIGIN` / `DEFAULT_DESTINATION` to auto-seed both routes on first
   start.
3. Build and run:

   ```bash
   docker compose up -d --build
   ```

4. Open <http://localhost:8080>. On the **Setup** tab, enter origin and
   destination, save, then click **Save & Recompute now** to populate the
   heatmaps. The daily auto-recompute then keeps them current.

## Quick start (local, no Docker)

```bash
python -m venv .venv
.venv\Scripts\activate                  # or:  source .venv/bin/activate
pip install -r requirements.txt

# PowerShell:
$env:GOOGLE_API_KEY = "your_key"
$env:DATA_DIR = ".\data"
# bash:
# export GOOGLE_API_KEY=your_key
# export DATA_DIR=./data

uvicorn app.main:app --host 0.0.0.0 --port 8080
```

## Configuration (environment variables)

| Variable                    | Default                | Purpose                                       |
| --------------------------- | ---------------------- | --------------------------------------------- |
| `GOOGLE_API_KEY`            | _(required)_           | Google Cloud key with Routes API enabled      |
| `DEFAULT_ORIGIN`            | _(empty)_              | Auto-seed home address on first start         |
| `DEFAULT_DESTINATION`       | _(empty)_              | Auto-seed office address on first start       |
| `TIME_WINDOW_START`         | `07:00`                | Morning sampling window start (HH:MM)         |
| `TIME_WINDOW_END`           | `09:00`                | Morning sampling window end                   |
| `EVENING_TIME_WINDOW_START` | `16:00`                | Evening sampling window start                 |
| `EVENING_TIME_WINDOW_END`   | `18:30`                | Evening sampling window end                   |
| `INTERVAL_MINUTES`          | `15`                   | Sampling step                                 |
| `DEFAULT_WEEKDAYS`          | `Mon,Tue,Wed,Thu,Fri`  | Days to sample                                |
| `SCHEDULER_HOUR`            | `4`                    | Daily recompute hour (in `TZ`)                |
| `SCHEDULER_MINUTE`          | `0`                    | Daily recompute minute                        |
| `CONCURRENT_REQUESTS`       | `10`                   | Max parallel Routes API calls per recompute   |
| `ADMIN_USERNAME`            | `admin`                | Seeded admin account username (first start)   |
| `ADMIN_PASSWORD`            | _(default + warning)_  | Seeded admin password — set before first start |
| `SESSION_TTL_DAYS`          | `30`                   | Login session cookie lifetime                  |
| `COOKIE_SECURE`             | `false`                | Mark the session cookie Secure (HTTPS only)    |
| `USER_DAILY_API_BUDGET`     | `250`                  | Per-user daily Routes API call cap (0 = off)   |
| `NTFY_TOPIC_URL`            | _(empty)_              | Enable push via an ntfy topic URL             |
| `WEBHOOK_URL`               | _(empty)_              | Enable push via a generic JSON POST           |
| `PUSH_MIN_SEVERITY`         | `alert`                | Push threshold: `watch` or `alert`            |
| `PUSH_CHECK_MINUTES`        | `15`                   | In-window push check interval                 |
| `TZ`                        | `UTC`                  | Container time zone                           |
| `DATA_DIR`                  | `/app/data`            | SQLite + persistence path                     |
| `BONN_TRAFFIC_ENABLED`      | `true`                 | Enable the Bonn live local-traffic signal     |
| `BONN_TRAFFIC_URL`          | _(Bonn feed)_          | GeoJSON endpoint for the realtime feed         |
| `BONN_CACHE_SECONDS`        | `300`                  | Feed cache TTL (feed refreshes every 5 min)    |
| `BONN_MATCH_RADIUS_M`       | `40`                   | Segment↔route match radius (metres)            |
| `BONN_MATCH_MIN_FRACTION`   | `0.5`                  | Min fraction of a segment near the route       |

Push is opt-in: the periodic check only runs (and only spends Routes API calls)
when `NTFY_TOPIC_URL` or `WEBHOOK_URL` is set **and** a commute window is open.

## Multi-user & authentication

The app is **multi-user, invite-only**. Each user has their own home/office
pair, dashboard, history, and notifications — fully isolated from other users.
There is no public signup: an **admin** account is seeded on first start from
`ADMIN_USERNAME` / `ADMIN_PASSWORD`, and the admin creates other accounts from
the **Users** tab.

- **Login** is username + password with an HTTP-only session cookie. Set
  `ADMIN_PASSWORD` before first start; the seeded admin should change it (and can
  reset any user's password). When upgrading an existing single-user install,
  the previous routes/history are automatically migrated to the admin account.
- **Programmatic access** (Home Assistant, widgets, scripts) uses each user's
  personal **API token** (Account tab) sent as `X-API-Key: <token>`. The old
  global `API_KEY` is retired.
- **Cost**: all users share one Google key (you pay). `USER_DAILY_API_BUDGET`
  caps each user's daily Routes API calls; over the cap, that user's live
  lookups fall back to the stored snapshot instead of spending more quota.

## REST API

All endpoints return JSON and require authentication — either the session cookie
(after `POST /api/login`) or an `X-API-Key: <token>` header. Auth endpoints:
`POST /api/login`, `POST /api/logout`, `GET /api/me`, `POST /api/me/password`,
`PUT /api/me/notifications`, `POST /api/me/api-token`; admin-only:
`GET|POST /api/admin/users`, `POST /api/admin/users/{id}/password`,
`DELETE /api/admin/users/{id}`.

### `GET /api/health`
```json
{ "status": "ok" }
```

### `GET /api/config`
Returns `{ "morning": route|null, "evening": route|null }`.

### `POST /api/config`

One home/office pair plus per-direction sampling windows. The evening route
is stored automatically with the addresses reversed.

```json
{
  "origin": "Berliner Str. 1, 10115 Berlin",
  "destination": "Alexanderplatz, Berlin",
  "baseline_since": "2026-05-15",
  "morning": {
    "time_window_start": "07:00",
    "time_window_end":   "09:00",
    "interval_minutes":  10,
    "weekdays": "Mon,Tue,Wed,Thu,Fri",
    "arrival_deadline":  "09:00"
  },
  "evening": {
    "time_window_start": "16:00",
    "time_window_end":   "18:30",
    "interval_minutes":  10,
    "weekdays": "Mon,Tue,Wed,Thu,Fri",
    "arrival_deadline":  null
  }
}
```

`arrival_deadline` is optional. When set on a direction, that direction's
recommendation becomes "the latest departure that still arrives by this time."

`baseline_since` is optional and applies to both directions. Set it to the date
a major traffic change took effect (e.g. a bridge closure); typical/p90 and
incident baselines then ignore observations from before that date.

### `POST /api/recompute`

Resamples both directions. Returns
`{ "status": "ok", "samples": {"morning": n, "evening": m} }`.

### `GET /api/commute/today`

Live payload (Routes API queried at request time) for both directions:

```json
{
  "morning": {
    "name": "morning",
    "day_of_week": "Thu",
    "origin": "…",
    "destination": "…",
    "arrival_deadline": "09:00",
    "best_departure_time": "08:00",
    "optimal_duration": 59,
    "arrival_time": "08:59",
    "buffer_minutes": 1,
    "current_duration": 42,
    "time_savings": 14,
    "live": true,
    "alternatives": [
      { "departure_time": "08:00", "duration_minutes": 59, "arrival_time": "08:59", "buffer_minutes": 1 },
      { "departure_time": "07:50", "duration_minutes": 62, "arrival_time": "08:52", "buffer_minutes": 8 },
      { "departure_time": "07:40", "duration_minutes": 64, "arrival_time": "08:44", "buffer_minutes": 16 }
    ]
  },
  "evening": { "…": "same shape (no deadline → alternatives sorted by shortest drive)" }
}
```

`alternatives` is the top 3 candidate slots from today's snapshot, filtered
by deadline if set, sorted latest-departure-first when a deadline applies and
shortest-drive-first otherwise. `best_departure_time` is `alternatives[0]`.

The payload also carries, when data is available:

- `route_options` — fastest alternative routes for the recommended departure:
  `[{ "label": "A565", "duration_minutes": 38, "distance_km": 12.4 }, …]`.
- `typical_duration`, `p90_duration`, `reliability_minutes` — the recent typical
  and p90 drive for the chosen slot, and the median→p90 spread to pad for.
- `window_hint` — `{ "edge": "early"|"late", "slot": "07:00", "message": … }`
  or `null`, suggesting the sampling window be widened.
- `local_traffic` — present only for routes through Bonn that matched segments in
  the [Bonn realtime feed](https://opendata.bonn.de/dataset/strassenverkehrslage-realtime):

  ```json
  "local_traffic": {
    "source": "Bonn open-data",
    "evaluated_at": "2026-06-25T10:50:00Z",
    "severity": "alert",
    "worst_status": "Staugefahr",
    "min_speed_kmh": 5,
    "segment_count": 6,
    "congested": [ { "strecke_id": 72, "status": "Staugefahr", "speed_kmh": 5 } ],
    "attribution": "Datenquelle: Bundesstadt Bonn, Amt 66"
  }
  ```

Incident fields (`incident_severity`, `incident_delta_minutes`, `incident_note`)
compare live to the slot's *typical* drive once enough history has accumulated,
falling back to the morning forecast before then. When the Bonn feed is enabled,
its segment status (`Staugefahr` → alert, `erhöhte Verkehrsbelastung` → watch)
is combined worst-of into `incident_severity` (so push alerts fire on it too);
`incident_delta_minutes` remains the Google-derived value.

### Bonn local-traffic source

When `BONN_TRAFFIC_ENABLED` is on (default), routes passing through Bonn are
auto-matched to the free, CC-BY realtime traffic feed once at config/recompute
time (matched `strecke_id`s are persisted per route). Live requests then only do
one cached GET of the feed — **no extra Google Routes API cost**. Routes outside
Bonn match nothing and the panel/field stay absent. Attribution
("Datenquelle: Bundesstadt Bonn, Amt 66") is included in the payload and shown
in the UI, as the CC-BY licence requires.

### `GET /api/commute/today/{direction}`

Same shape but flat. `direction` is `morning` or `evening`. Handy when an
upstream consumer wants only one direction per poll (e.g. it polls each
endpoint with its own cadence).

### `GET /api/commute/today/{direction}/next?minutes=60`

Best departure within the next N minutes from now, with the same
live-rechecked top-3 alternatives. Designed for dashboard tiles and
homescreen widgets — the response's `best` object is exactly what you
want to display ("leave at HH:MM, drive N min, arrive HH:MM").

```json
{
  "name": "morning",
  "day_of_week": "Tue",
  "window_minutes": 60,
  "window_end_time": "07:30",
  "arrival_deadline": "09:00",
  "best": {
    "departure_time": "07:10",
    "duration_minutes": 61,
    "arrival_time": "08:11",
    "buffer_minutes": 49,
    "incident_severity": "clear",
    "delta_minutes": 0,
    "live": true
  },
  "candidates": [ … top 3 latest-feasible in the window … ]
}
```

### `GET /api/commute/heatmap`

`{ "morning": [...], "evening": [...] }` where each list element is
`{ "day": "Mon", "time": "07:00", "duration": 35.0 }`.

### `GET /api/commute/heatmap/{direction}`

The flat list for a single direction.

## Tests

```bash
pip install -r requirements-dev.txt
pytest            # offline — no API key or network needed
```

The suite covers the pure stats helpers, the observation/baseline queries, the
route-label parsing, and an offline end-to-end run of the live re-rank payload
(Google calls are monkeypatched).

## Documentation

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — file layout, components, data flow
- [docs/OPERATIONS.md](docs/OPERATIONS.md) — runtime behavior, refresh layers, incident detection, **cost management**
- [docs/INTEGRATIONS.md](docs/INTEGRATIONS.md) — recipes for Home Assistant, AI assistant skills, and shell scripts
- [CHANGELOG.md](CHANGELOG.md) — feature history

## License

MIT — see [LICENSE](LICENSE).
