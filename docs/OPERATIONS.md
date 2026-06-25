# Operations

How the commute optimizer actually behaves at runtime, what data it
freshens when, and how to keep Google Routes API costs under control.

## Three layers of data freshness

### Layer 1 — Daily 06:00 batch (the forecast)

Runs once at the configured `SCHEDULER_HOUR:SCHEDULER_MINUTE` in the
container's `TZ`. By default this is 04:00 UTC; in this codebase we
default to `SCHEDULER_HOUR=4` but most personal deployments set it to
match local morning (e.g., 06:00 Europe/Berlin).

What it does:

- Refreshes **only today's weekday column** for each active route
  (`recompute_all_active_routes(only_today=True)`). It re-samples that one
  day's slots and replaces just that day in SQLite, leaving the rest of the
  week's snapshot intact.
- Hits Google Routes API once per slot with that slot's next-occurrence
  datetime as `departureTime`
- Stores the predicted drive time in SQLite as the "snapshot"

Over a Mon–Fri week each day's column is refreshed on its own morning, so the
heatmap stays a rolling, always-fresh 5-day window. The manual
`POST /api/recompute` (the **Save & Recompute now** button) still samples the
**whole week** at once — use it to seed or reset all columns.

The snapshot is what the **heatmap UI** reads, and what later layers
use as the **baseline for incident detection** (live-vs-forecast delta).

By default the cron is restricted to `mon-fri`. Sampling on weekends
adds calls without adding signal — the route's own `weekdays` config
typically already excludes Saturday/Sunday.

Cost per daily batch: ~today's slots only. With both directions at a 15-min
interval that's roughly **~20 calls/day** (vs. ~145/day when it re-sampled
the entire week every morning — the old behavior that dominated monthly
usage). A full-week seed via `/api/recompute` is ~100 calls.

### Layer 2 — Live re-rank on every `/api/commute/today/...` call

Whenever a client calls one of the today endpoints, the server:

1. Reads the snapshot for today's weekday
2. Filters to slots that are still in the future, and (if a deadline is
   set) that arrive on or before the deadline
3. Sorts: deadline set → latest-departure first; no deadline → shortest
   duration first
4. Takes the top 3 as the probe batch
5. Fires Google Routes API calls **in parallel** for: leave-now + each
   of the 3 probe candidates (4 calls total)
6. Re-filters with live durations against the deadline
7. If 0 candidates remain after re-filter AND a deadline is set, expands to
   earlier batches — gated by `MAX_PROBE_BATCHES` (default **2** → up to 7
   live calls; set to 1 to disable expansion, 3 for up to 9)
8. Fires **one** `computeAlternativeRoutes` call for the chosen departure to
   populate `route_options` (the competing crossings/detours) — +1 call
9. Records every successful live probe into the `observations` history table
10. Returns top-1 as `best_departure_time`, top-3 as `alternatives`

So a normal `/today` request now costs **5** Routes API calls (leave-now + 3
probes + 1 alternatives), up from 4 before route options were added; the
worst-case incident morning is **8** (7 + alternatives).

This is what makes the dashboard react to incidents inside ~5 min
without requiring a separate incident API. The recorded observations also
accumulate into the typical/p90 baselines described below.

### Layer 3 — Consumer polling cadence

HA polls REST sensors on a configurable interval. We recommend driving
polling via time-window automations rather than always-on `scan_interval`
(see [INTEGRATIONS.md](INTEGRATIONS.md) for the pattern).

The iPhone HA Companion widget refreshes on its own ~15-min schedule
plus whenever the Companion app is opened.

## Incident detection

For each top-3 candidate the live re-rank produces, the server compares the
live duration against a **baseline** and computes `delta = live − baseline`.

The baseline is the slot's **typical** drive — the trailing median over recent
observations (filtered by `baseline_since`) — once at least
`MIN_SAMPLES_FOR_STATS` observations exist for that slot. Before then it falls
back to this morning's snapshot forecast. This matters on a chronically
congested corridor: the snapshot already bakes in the standing jam, so
"live vs snapshot" stops firing; "live vs typical-for-this-slot" still flags the
genuinely worse-than-usual days.

| Condition | Severity |
|---|---|
| `delta < 10 min` and ratio < 1.5× | `clear` |
| `delta` in `[10, 20)` min | `watch` |
| `delta ≥ 20 min` OR ratio `≥ 1.5×` | `alert` |

The top-level `incident_severity` returned in `/today` responses is the
**worst** signal across all top candidates. Rationale: even if your
single best slot looks fine, you want to know when nearby alternatives
are degrading — that's how you tell "evolving congestion" from "isolated
blip on one slot."

Note: this is not a labeled incident feed from Google. It does not
tell you what or where the incident is — only that your route is taking
materially longer than typical. For specifics, fall back to Google Maps.

### Bonn local-traffic signal (free, zero Google cost)

When `BONN_TRAFFIC_ENABLED` is on (default), the app also reads the free, CC-BY
[Bonn realtime traffic feed](https://opendata.bonn.de/dataset/strassenverkehrslage-realtime)
— per-segment status (`Staugefahr` / `erhöhte Verkehrsbelastung` / `normal` /
`nicht ermittelbar`) and speed for Bonn's Rhine bridges and main arterials,
refreshed every 5 minutes.

- **Matching is one-time, not per-request.** When a route's addresses change
  (config save) or on recompute, the app fetches the route polyline once and
  matches Bonn segments to it geographically, storing the matched `strecke_id`s
  on the route (`bonn_segment_ids`). The match is stable because it depends on
  the corridor, not on live traffic.
- **The live path costs nothing extra from Google.** A `/today` request does one
  cached GET of the Bonn feed (shared across both directions, TTL
  `BONN_CACHE_SECONDS`, default 300 s) and looks up the stored segments — no
  additional Routes API calls.
- **It folds into incidents.** Bonn severity (`Staugefahr` → alert,
  `erhöhte Verkehrsbelastung` → watch) is combined worst-of with the
  Google-derived severity, so proactive push fires on Bonn-side congestion too.
- **Graceful + opt-out.** Routes outside Bonn match nothing and the
  `local_traffic` field is simply absent; any feed/parse failure is ignored.
  Set `BONN_TRAFFIC_ENABLED=false` to disable entirely.
- **Attribution.** The CC-BY licence requires showing
  "Datenquelle: Bundesstadt Bonn, Amt 66" — the app includes it in the payload
  and renders it under the dashboard panel.

### Observation history, typical/p90, and baseline reset

`commute_data` holds only the latest forecast per slot (it's replaced each day).
The `observations` table is append-only: every daily-batch forecast and every
live probe lands there with a timestamp. From it the server computes, per slot:

- **typical** (`typical_duration`) — trailing median
- **p90** (`p90_duration`) — the bad-day duration; `reliability_minutes` is the
  median→p90 spread you should pad for
- the **incident baseline** above

By default stats use a rolling `RECENT_DAYS` (35) window. Setting a route's
`baseline_since` to the date a disruption began clamps the lower bound to that
date instead, so the pre-event traffic pattern is excluded entirely. History
accumulates from first run after upgrade; until it's thick enough every feature
degrades to the previous snapshot-only behaviour.

## Multi-user

The app is multi-user (invite-only). An admin is seeded on first start from
`ADMIN_USERNAME`/`ADMIN_PASSWORD`; on upgrade from the single-user schema the
existing routes/history are migrated onto that admin (routes with a NULL
`user_id` are adopted at startup). Each user is fully isolated — routes,
history, Bonn matching, and notifications are all scoped by `user_id`.

The scheduler iterates **per user**: the daily recompute and the in-window push
check loop over every user and operate on that user's routes and push settings.
The shared Google key is protected by `USER_DAILY_API_BUDGET` — each user's
daily Routes API calls are counted in `api_usage`, and once over the cap their
live `/today` lookups serve the stored snapshot (`live: false`) instead of
spending more quota. So total monthly cost scales with *active* users; size the
budget and your Google billing alert accordingly.

## Proactive push

Per-user, opt-in. Each user sets their own `ntfy` topic and/or webhook (Account
tab); the legacy global `NTFY_TOPIC_URL`/`WEBHOOK_URL` env vars only seed the
admin's settings on first start. A scheduler job runs every
`PUSH_CHECK_MINUTES` (default 15) and, **only when a commute window is open on a
configured day**, runs the live `/today` computation per route and pushes when
`incident_severity` crosses `PUSH_MIN_SEVERITY` (default `alert`). Pushes are
deduped per day and only re-fire on escalation (`watch` → `alert`).

Cost: each in-window check costs a full `/today` (~5 calls) per active route.
A 2.5h morning + 3h evening window at 15-min cadence is roughly
`(10 + 12) checks × 5 calls × ~21 weekdays ≈ 2,300 calls/month` **in addition**
to dashboard polling — so enable it deliberately and watch your budget alert.
If you already poll from Home Assistant, you likely don't need this too; it's
for setups with no always-on consumer.

## Expand-on-failure (incident edge case)

Without this logic: at 07:15 with a major accident, if the snapshot's
top 3 latest slots (e.g., 08:10, 08:00, 07:50) all violate the deadline
under live conditions, the response would be `"no slot"` — even though
earlier slots (07:40, 07:30) would still be feasible. That's a cliff
exactly when you most need an answer.

With this logic: the system iteratively probes earlier batches of 3
candidates until either it finds feasible options or exhausts the
batch budget (`MAX_PROBE_BATCHES`).

**Default is `MAX_PROBE_BATCHES = 2`** — one extra batch beyond the first, so
up to 7 live calls on a heavy-incident morning while the normal no-incident
case still costs only 4. Set it to 1 to disable expansion entirely (max 4
calls; `/today` may then report "no slot" instead of surfacing an earlier
feasible departure when all 3 latest slots blow the deadline), or 3 to restore
the original behavior (up to 9 calls).

## Cost management

`TRAFFIC_AWARE_OPTIMAL` routing bills under the **Routes: Compute Routes Pro**
SKU at roughly **$0.008–0.010 per request**, with a **5,000 free
requests/month** allowance (the **Pro** tier — *not* the 10,000 that
Essentials SKUs get; an earlier version of this doc wrongly assumed 10,000).
The free tier resets on the 1st of each calendar month.

> **Verified the hard way (June 2026):** with the old "re-sample the whole
> week every morning" batch, steady usage ran ~5,500 calls/month — just over
> the 5,000 Pro free tier. It crossed 5,000 around the 27th and billed the
> remainder of the month (~€4). The "today only" batch above brings steady
> usage to ~3,000/month, comfortably under the free tier.

### Three reference architectures

All figures assume the **today-only** daily batch (~440 calls/month) and the
**5,000/month Pro free tier**.

| Architecture | Polling cadence | Daily batch | Expected calls/month |
|---|---|---|---|
| **A. Free-tier safe** | 15-min during your real commute window only (e.g. 06:30–09:00 + 15:30–18:30 weekdays) | today-only, Mon-Fri | ~3,000–4,000 |
| **B. Manual only** | No HA polling; live calls only when you open the dashboard | today-only, Mon-Fri | ~600–1,500 |
| **C. Real-time** | 2–5 min cadence, 24/7 | every day | ~20,000–30,000 |

Architecture A is the recommended default and now fits inside the 5,000 free
tier with headroom. Architecture B sacrifices the proactive incident push
notification (you'd find out at next manual check). Architecture C blows
through the free tier and costs roughly $100–200/month at current pricing.

### Always set a Google Cloud billing alert

Cloud Console → Billing → **Budgets & alerts → Create budget**:

- Scope to this project only
- Amount: small enough to notice early (€10/month is a useful tripwire)
- Alert thresholds: 50%, 90%, 100% via email

Reasoning: if anything misbehaves (HA gets into a poll loop, the API key
leaks and someone hammers it, a misconfigured `scan_interval` of 2
seconds instead of 200) you'll know within hours rather than at month-end.

### Other cost dials

- Tighten the morning sampling window in your route config (fewer slots
  per batch). Each slot dropped saves 5 calls/day (one per weekday).
- Restrict the daily batch's `weekdays` to your actual commute days.
- Set HA's `scan_interval` to something high (e.g., `86400`) and gate
  refreshes entirely via time-window automations.
- `MAX_PROBE_BATCHES` in `app/api/routes.py` defaults to **2** (up to 7 live
  calls per `/today` request on incident mornings; 4 normally). Set it to 1 to
  cap at 4 and skip expand-on-failure, or 3 for the original up-to-9 behavior.
- Live `/today` and `/next` calls on a day not in the route's `weekdays`
  (e.g. weekends) now short-circuit **without** any Google call.
