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
| `INTERVAL_MINUTES`          | `10`                   | Sampling step                                 |
| `DEFAULT_WEEKDAYS`          | `Mon,Tue,Wed,Thu,Fri`  | Days to sample                                |
| `SCHEDULER_HOUR`            | `4`                    | Daily recompute hour (in `TZ`)                |
| `SCHEDULER_MINUTE`          | `0`                    | Daily recompute minute                        |
| `CONCURRENT_REQUESTS`       | `10`                   | Max parallel Routes API calls per recompute   |
| `API_KEY`                   | _(empty)_              | If set, all `/api/*` calls need `X-API-Key`   |
| `TZ`                        | `UTC`                  | Container time zone                           |
| `DATA_DIR`                  | `/app/data`            | SQLite + persistence path                     |

## REST API

All endpoints return JSON. If `API_KEY` is set, send `X-API-Key: <key>` on
every request.

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

### `GET /api/commute/today/{direction}`

Same shape but flat. `direction` is `morning` or `evening`. Handy when an
upstream consumer wants only one direction per poll (e.g. it polls each
endpoint with its own cadence).

### `GET /api/commute/heatmap`

`{ "morning": [...], "evening": [...] }` where each list element is
`{ "day": "Mon", "time": "07:00", "duration": 35.0 }`.

### `GET /api/commute/heatmap/{direction}`

The flat list for a single direction.

## Cost considerations

A daily recompute does `len(weekdays) × slots` Routes API calls per
direction, with `routingPreference: TRAFFIC_AWARE_OPTIMAL`. With the defaults
(5 days × ~13 slots × 2 directions ≈ 130 calls/day) you're well inside
Google's free tier for personal use, but verify against current Routes API
pricing.

Every dashboard load also does 2 live Routes API calls (one for "leave now",
one for "best remaining slot today"). For lower cost, consumers should poll
`scan_interval >= 300` seconds.

## Architecture notes

- **Sampling logic** picks the next future occurrence of each weekday/time
  slot so Google Routes API always gets a valid future `departureTime`.
- **Two-tier freshness**: the daily 06:00 batch fills the heatmap (the
  weekly pattern); the live dashboard endpoint queries Google again at
  request time for "leave now" and today's best slot, so per-view numbers
  reflect current traffic.
- **Schema migrations** are idempotent: existing single-route deployments
  auto-migrate to the named-route model on next start.

## License

MIT — see [LICENSE](LICENSE).
