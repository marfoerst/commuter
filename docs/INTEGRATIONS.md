# Integrations

The commute optimizer exposes a small REST API. Anything that can fetch
JSON over HTTP can consume it. Below are working integration patterns
for Home Assistant and AI chat assistants (Hermes, Claude, or any agent
framework that supports markdown "skills").

Throughout this doc, replace `<COMMUTE-OPTIMIZER-IP>` with the host where
your commute-optimizer container is reachable on your LAN (e.g.,
`10.0.0.50`, `192.168.1.100`, or `commute-optimizer.local`).

---

## Home Assistant

Works for both HAOS and HA Container. Tested on HA Core 2025.x.

### Step 1 — REST sensors in `configuration.yaml`

The simplest pattern is to expose each useful field of the response as
its own sensor. Add to `configuration.yaml` (top-level):

```yaml
rest:
  - resource: http://<COMMUTE-OPTIMIZER-IP>:8088/api/commute/today/morning
    scan_interval: 86400  # auto-poll disabled; refreshed by automation below
    sensor:
      - name: "Commute Morning Best Departure"
        unique_id: commute_morning_best_departure
        value_template: "{{ value_json.best_departure_time }}"
      - name: "Commute Morning Optimal Duration"
        unique_id: commute_morning_optimal_duration
        value_template: "{{ value_json.optimal_duration }}"
        unit_of_measurement: "min"
      - name: "Commute Morning Current Duration"
        unique_id: commute_morning_current_duration
        value_template: "{{ value_json.current_duration }}"
        unit_of_measurement: "min"
      - name: "Commute Morning Time Savings"
        unique_id: commute_morning_time_savings
        value_template: "{{ value_json.time_savings }}"
        unit_of_measurement: "min"
      - name: "Commute Morning Arrival Time"
        unique_id: commute_morning_arrival_time
        value_template: "{{ value_json.arrival_time }}"
      - name: "Commute Morning Buffer Minutes"
        unique_id: commute_morning_buffer_minutes
        value_template: "{{ value_json.buffer_minutes | default(0, true) }}"
        unit_of_measurement: "min"
      - name: "Commute Morning Incident Severity"
        unique_id: commute_morning_incident_severity
        value_template: "{{ value_json.incident_severity }}"
      - name: "Commute Morning Incident Note"
        unique_id: commute_morning_incident_note
        value_template: "{{ value_json.incident_note }}"
      - name: "Commute Morning Incident Delta"
        unique_id: commute_morning_incident_delta
        value_template: "{{ value_json.incident_delta_minutes | default(0, true) }}"
        unit_of_measurement: "min"

  # Duplicate the above block for evening, substituting "evening" everywhere.

  - resource: http://<COMMUTE-OPTIMIZER-IP>:8088/api/commute/today/morning/next?minutes=60
    scan_interval: 86400
    sensor:
      - name: "Commute Morning Next 60"
        unique_id: commute_morning_next_60
        value_template: "{{ value_json.best.departure_time if value_json.best else 'no slot' }}"
      - name: "Commute Morning Next 60 Duration"
        unique_id: commute_morning_next_60_duration
        value_template: "{{ value_json.best.duration_minutes if value_json.best else 0 }}"
        unit_of_measurement: "min"
      - name: "Commute Morning Next 60 Arrival"
        unique_id: commute_morning_next_60_arrival
        value_template: "{{ value_json.best.arrival_time if value_json.best else '—' }}"
      - name: "Commute Morning Next 60 Buffer"
        unique_id: commute_morning_next_60_buffer
        value_template: "{{ value_json.best.buffer_minutes if value_json.best else 0 }}"
        unit_of_measurement: "min"
      - name: "Commute Morning Next 60 Severity"
        unique_id: commute_morning_next_60_severity
        value_template: "{{ value_json.best.incident_severity if value_json.best else 'unknown' }}"

  # And the corresponding evening /next block.
```

**Why per-field sensors instead of `json_attributes`?** HA's REST
integration `json_attributes` feature silently fails to extract on some
versions for nested paths. Per-field sensors are bulletproof.

### Step 2 — Time-window refresh automations

Add to `automations.yaml` (or via the UI):

```yaml
- id: commute_refresh_morning
  alias: "Commute · Refresh morning sensors"
  description: >-
    Triggers REST sensor updates only during the morning commute window
    so we don't hammer Google Routes API 24/7.
  mode: single
  trigger:
    - platform: time_pattern
      minutes: "/15"
  condition:
    - condition: time
      after: "06:30:00"
      before: "09:00:00"
      weekday: [mon, tue, wed, thu, fri]
  action:
    - service: homeassistant.update_entity
      data:
        entity_id:
          - sensor.commute_morning_best_departure
          - sensor.commute_morning_next_60

- id: commute_refresh_evening
  alias: "Commute · Refresh evening sensors"
  mode: single
  trigger:
    - platform: time_pattern
      minutes: "/15"
  condition:
    - condition: time
      after: "15:30:00"
      before: "18:30:00"
      weekday: [mon, tue, wed, thu, fri]
  action:
    - service: homeassistant.update_entity
      data:
        entity_id:
          - sensor.commute_evening_best_departure
          - sensor.commute_evening_next_60

- id: commute_refresh_startup
  alias: "Commute · Initial refresh on startup"
  description: >-
    Refresh once when HA boots so sensors aren't stuck on stale post-restart state.
  mode: single
  trigger:
    - platform: homeassistant
      event: start
  action:
    - delay: "00:00:30"
    - service: homeassistant.update_entity
      data:
        entity_id:
          - sensor.commute_morning_best_departure
          - sensor.commute_morning_next_60
          - sensor.commute_evening_best_departure
          - sensor.commute_evening_next_60
```

This pattern keeps the daily Google Routes API call count predictable
and inside the free tier. See [OPERATIONS.md](OPERATIONS.md) for the
math.

### Step 3 — Incident push notification

```yaml
- id: commute_morning_incident_alert
  alias: "Commute · Morning route incident alert"
  description: >-
    Push when the morning route shows a likely incident (live drive much
    slower than this morning's forecast).
  mode: single
  trigger:
    - platform: state
      entity_id: sensor.commute_morning_incident_severity
      to: "alert"
  condition:
    - condition: time
      after: "06:00:00"
      before: "09:30:00"
      weekday: [mon, tue, wed, thu, fri]
  action:
    - service: notify.mobile_app_<YOUR_PHONE>     # replace
      data:
        title: "Commute alert"
        message: >-
          {{ states('sensor.commute_morning_incident_note') }}
          Best slot now: {{ states('sensor.commute_morning_best_departure') }} ·
          {{ states('sensor.commute_morning_optimal_duration') }} min drive.
```

Find your `notify.*` service in HA → Developer Tools → Services → type
`notify.` and pick the entry matching your phone.

> **No Home Assistant?** The app can push on its own. Set `NTFY_TOPIC_URL`
> (an [ntfy](https://ntfy.sh) topic) and/or `WEBHOOK_URL`, plus
> `PUSH_MIN_SEVERITY` (`watch`/`alert`). A scheduler job checks live conditions
> every `PUSH_CHECK_MINUTES` **while a commute window is open** and pushes when
> the threshold is crossed (deduped per day, escalation-aware). The webhook
> receives `{title, message, severity, route, best_departure_time, delta_minutes}`.
> This spends extra Routes API calls — see the **Proactive push** section in
> [OPERATIONS.md](OPERATIONS.md) before enabling. If you already poll from HA,
> prefer the automation above rather than running both.

### Step 4 — Lovelace dashboard tile (Mushroom)

Requires the **Mushroom** custom card from HACS. Add as a Manual card:

```yaml
type: custom:mushroom-template-card
primary: >-
  {% set s = states('sensor.commute_morning_next_60') %}
  {% if s in ['no slot','unknown','unavailable'] %}Morning: no slot
  {% else %}Morning: {{ s }}{% endif %}
secondary: >-
  {% set s = states('sensor.commute_morning_next_60') %}
  {% if s in ['no slot','unknown','unavailable'] %}
    Live now {{ states('sensor.commute_morning_current_duration') }} min
  {% else %}
    {{ states('sensor.commute_morning_next_60_duration') }} min drive
    · arrive {{ states('sensor.commute_morning_next_60_arrival') }}
    · buffer {{ states('sensor.commute_morning_next_60_buffer') }} min
  {% endif %}
icon: mdi:car-clock
icon_color: >-
  {% set sev = states('sensor.commute_morning_next_60_severity') %}
  {% if sev == 'alert' %}red
  {% elif sev == 'watch' %}orange
  {% else %}green{% endif %}
fill_container: true
tap_action:
  action: url
  url_path: http://<COMMUTE-OPTIMIZER-IP>:8088/
```

Vanilla `tile` and `entities` cards work too if you don't have Mushroom.

### Step 5 — iPhone Homescreen widget

HA Companion app on iOS ships a **Sensors** widget. Long-press homescreen
→ `+` top-left → search "Home Assistant" → pick the Sensors widget →
add to homescreen → tap to configure → select
`sensor.commute_morning_next_60`. State is `HH:MM` (the time you should
leave), which is exactly what you want to glance at.

Medium widget can show up to 4 sensors — useful for showing morning +
evening + buffer + current-duration in one tile.

---

## AI assistant skill (Hermes / Claude Code / generic agent)

For agents that load markdown "skills" with YAML frontmatter (Claude
Code, Hermes Agent, etc.), drop this file into the agent's skills
directory:

````markdown
---
name: commute
description: "Live commute decisions for the user's morning and evening drive between home and office. Use when the user asks about traffic, drive time, when to leave for the office, when to leave the office, whether to wait out rush hour, whether there's a jam, or whether something happened on the route."
metadata:
  tags: [Smart-Home, Commute, Traffic, Travel, Routing, Incidents]
prerequisites:
  commands: [curl]
---

# Commute optimizer

Self-hosted commute service at `http://<COMMUTE-OPTIMIZER-IP>:8088`.
Daily 06:00 batch + live Google Routes API queries on every request.

## When to invoke

- "When should I leave for the office?" / "leave the office?"
- "How's traffic to/from work?"
- "Is there a jam on my commute?"
- "Did something happen on my route?"
- "Should I wait or go now?"
- "What's the latest I can leave to be at work by 9?"

## Endpoints

```bash
# Both directions, full payload
curl -s http://<COMMUTE-OPTIMIZER-IP>:8088/api/commute/today

# Single direction (cheaper)
curl -s http://<COMMUTE-OPTIMIZER-IP>:8088/api/commute/today/morning
curl -s http://<COMMUTE-OPTIMIZER-IP>:8088/api/commute/today/evening

# Best in next N minutes
curl -s "http://<COMMUTE-OPTIMIZER-IP>:8088/api/commute/today/morning/next?minutes=60"

# Weekly pattern
curl -s http://<COMMUTE-OPTIMIZER-IP>:8088/api/commute/heatmap/morning
```

## Key fields

| Field | Meaning |
|---|---|
| `best_departure_time` | Recommended departure. With a deadline set, this is the LATEST safe departure. |
| `optimal_duration` | Live drive time for the recommended slot. |
| `arrival_time` | When the user arrives if they leave at `best_departure_time`. |
| `buffer_minutes` | `arrival_deadline - arrival_time`. Positive = on time with margin. |
| `current_duration` | Live drive time if leaving right now. |
| `time_savings` | `current - optimal`. Positive = waiting saves time. |
| `alternatives` | Top 3 feasible options. With deadline: latest-departure first. |
| `incident_severity` | `clear` / `watch` / `alert`. Worst across top candidates. |
| `incident_delta_minutes` | Live drive minus the morning forecast prediction. |
| `incident_note` | Human-readable summary of the signal. |

## How to answer

**"When should I leave?"** — read `best_departure_time` and
`optimal_duration`. Report buffer to deadline if positive.

**"Now or wait?"** — use `time_savings`. If `> 5`, suggest waiting.
Otherwise say "just go."

**"Is there a jam?"** — use `incident_severity`:
- `clear` → "No, traffic looks normal."
- `watch` → "Heavier than usual, +N min vs forecast."
- `alert` → "Likely incident, +N min vs forecast. Check Google Maps for specifics."

**"How does Wednesday look?"** — pattern question, hit the heatmap
endpoint and filter by `day == "Wed"`.

## Failure modes

- `404 "No data for {day}"` → daily batch hasn't filled this weekday.
  Trigger one: `POST /api/recompute`, retry after ~30s.
- `503 "Routes API unavailable"` → Google quota or auth issue.
- `Connection refused` → optimizer container is down on the LAN.

## Don't

- Call Google Maps directly — always go through this local API.
- Tell the user where/what the incident is. Only the magnitude is
  available; for specifics they should open Google Maps.
- Edit route config unless explicitly asked. To change config:
  `POST /api/config` with the new JSON body.
````

The agent only needs to know `<COMMUTE-OPTIMIZER-IP>` — everything else
flows through the local API.

---

## n8n / Node-RED / shell scripts

The API is plain HTTP+JSON, so the same patterns apply: do a GET on
the appropriate endpoint, read the field you care about, take action.

Example shell one-liner that prints today's recommended morning
departure:

```bash
curl -s http://<COMMUTE-OPTIMIZER-IP>:8088/api/commute/today/morning \
  | jq -r '"Leave at \(.best_departure_time) — \(.optimal_duration) min, arrive \(.arrival_time)."'
```
