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

- Iterates `len(weekdays) × number_of_slots` for each active route
- Hits Google Routes API once per slot with that slot's next-occurrence
  datetime as `departureTime`
- Stores the predicted drive time in SQLite as the "snapshot"

The snapshot is what the **heatmap UI** reads, and what later layers
use as the **baseline for incident detection** (live-vs-forecast delta).

By default the cron is restricted to `mon-fri`. Sampling on weekends
adds calls without adding signal — the route's own `weekdays` config
typically already excludes Saturday/Sunday.

Cost per batch: 5 × ~29 slots × 1 direction = ~145 calls. Both directions
included = ~145 total (since morning has fewer slots than evening on
average).

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
7. If 0 candidates remain after re-filter AND a deadline is set,
   expands to the next earlier batch (max 3 batches → max 9 live calls)
8. Returns top-1 as `best_departure_time`, top-3 as `alternatives`

This is what makes the dashboard react to incidents inside ~5 min
without requiring a separate incident API.

### Layer 3 — Consumer polling cadence

HA polls REST sensors on a configurable interval. We recommend driving
polling via time-window automations rather than always-on `scan_interval`
(see [INTEGRATIONS.md](INTEGRATIONS.md) for the pattern).

The iPhone HA Companion widget refreshes on its own ~15-min schedule
plus whenever the Companion app is opened.

## Incident detection

For each top-3 candidate the live re-rank produces, the server has both
the snapshot duration (from 06:00) and the live duration (from Google
right now). `delta = live − snapshot`.

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
materially longer than the morning forecast expected. For specifics,
fall back to Google Maps.

## Expand-on-failure (incident edge case)

Without this logic: at 07:15 with a major accident, if the snapshot's
top 3 latest slots (e.g., 08:10, 08:00, 07:50) all violate the deadline
under live conditions, the response would be `"no slot"` — even though
earlier slots (07:40, 07:30) would still be feasible. That's a cliff
exactly when you most need an answer.

With this logic: the system iteratively probes earlier batches of 3
candidates until either it finds feasible options or exhausts the
batch budget (`MAX_PROBE_BATCHES = 3`). Worst case it does 9 live Google
calls instead of 3. Best case (no incident) it stays at 3.

## Cost management

Routes API "Advanced" tier (which `TRAFFIC_AWARE_OPTIMAL` falls into)
is **$0.010 per request** with **10,000 free requests/month** under the
current pricing model. The free tier resets monthly.

### Three reference architectures

| Architecture | Polling cadence | Daily batch | Expected calls/month |
|---|---|---|---|
| **A. Free-tier safe** | 15-min during your real commute window only (e.g. 06:30–09:00 + 15:30–18:30 weekdays) | Mon-Fri | ~9,000–10,000 |
| **B. Manual only** | No HA polling; live calls only when you open the dashboard | Mon-Fri | ~5,000–6,000 |
| **C. Real-time** | 2–5 min cadence, 24/7 | every day | ~20,000–30,000 |

Architecture A is the recommended default. Architecture B sacrifices
the proactive incident push notification (you'd find out at next manual
check). Architecture C costs roughly $100–200/month at current pricing.

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
- Lower the `MAX_PROBE_BATCHES` constant in `app/api/routes.py` from 3
  to 1 to skip expand-on-failure (loses graceful incident handling but
  caps live calls at 4 per `/today` request).
