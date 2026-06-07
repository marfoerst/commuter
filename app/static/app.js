const WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
const DIRECTIONS = ["morning", "evening"];

const form = document.getElementById("config-form");
const statusEl = document.getElementById("status");

document.querySelectorAll(".tab").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((b) =>
      b.classList.toggle("active", b === btn)
    );
    document.querySelectorAll(".panel").forEach((p) =>
      p.classList.toggle("active", p.id === btn.dataset.tab)
    );
  });
});

function setStatus(msg) {
  statusEl.textContent = msg || "";
}

async function loadConfig() {
  const r = await fetch("/api/config");
  if (!r.ok) return;
  const cfg = await r.json();
  if (!cfg) return;
  const morning = cfg.morning || {};
  const evening = cfg.evening || {};
  form.origin.value = morning.origin || "";
  form.destination.value = morning.destination || "";
  form.baseline_since.value = morning.baseline_since || "";
  form.m_start.value = morning.time_window_start || "07:00";
  form.m_end.value = morning.time_window_end || "09:00";
  form.m_interval.value = morning.interval_minutes || 10;
  form.m_deadline.value = morning.arrival_deadline || "";
  form.e_start.value = evening.time_window_start || "16:00";
  form.e_end.value = evening.time_window_end || "18:30";
  form.e_interval.value = evening.interval_minutes || 10;
  form.e_deadline.value = evening.arrival_deadline || "";
  const weekdays = (morning.weekdays || "Mon,Tue,Wed,Thu,Fri")
    .split(",")
    .map((s) => s.trim());
  document.querySelectorAll("input[name='weekdays']").forEach((cb) => {
    cb.checked = weekdays.includes(cb.value);
  });
}

function readForm() {
  const fd = new FormData(form);
  const weekdays = Array.from(
    form.querySelectorAll("input[name='weekdays']:checked")
  )
    .map((cb) => cb.value)
    .join(",");
  return {
    origin: fd.get("origin"),
    destination: fd.get("destination"),
    baseline_since: fd.get("baseline_since") || null,
    morning: {
      time_window_start: fd.get("m_start"),
      time_window_end: fd.get("m_end"),
      interval_minutes: parseInt(fd.get("m_interval"), 10),
      weekdays,
      arrival_deadline: fd.get("m_deadline") || null,
    },
    evening: {
      time_window_start: fd.get("e_start"),
      time_window_end: fd.get("e_end"),
      interval_minutes: parseInt(fd.get("e_interval"), 10),
      weekdays,
      arrival_deadline: fd.get("e_deadline") || null,
    },
  };
}

async function saveConfig() {
  const body = readForm();
  if (!body.origin || !body.destination) {
    setStatus("Home and Office addresses are required.");
    return false;
  }
  if (!body.morning.weekdays) {
    setStatus("Select at least one weekday.");
    return false;
  }
  setStatus("Saving…");
  const r = await fetch("/api/config", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!r.ok) {
    const err = await r.json().catch(() => ({}));
    setStatus("Save failed: " + (err.detail || r.statusText));
    return false;
  }
  setStatus("Saved.");
  return true;
}

async function triggerRecompute() {
  setStatus("Recomputing both directions… (15–60 seconds)");
  const r = await fetch("/api/recompute", { method: "POST" });
  if (!r.ok) {
    const err = await r.json().catch(() => ({}));
    setStatus("Recompute failed: " + (err.detail || r.statusText));
    return;
  }
  const data = await r.json();
  const m = data.samples?.morning ?? 0;
  const e = data.samples?.evening ?? 0;
  setStatus(`Done. Morning: ${m} samples · Evening: ${e} samples.`);
  await loadAll();
}

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  if (await saveConfig()) loadAll();
});

document.getElementById("recompute-btn").addEventListener("click", async () => {
  if (!(await saveConfig())) return;
  await triggerRecompute();
});

document.getElementById("recompute-top").addEventListener("click", async () => {
  await triggerRecompute();
});

function colorFor(value, min, max) {
  if (max === min) return "hsl(120, 60%, 38%)";
  const t = (value - min) / (max - min);
  const hue = 120 * (1 - t);
  return `hsl(${hue}, 65%, 40%)`;
}

function renderSummary(direction, d) {
  const target = document.querySelector(`.summary[data-direction="${direction}"]`);
  const metaTarget = document.querySelector(`.meta[data-direction="${direction}"]`);
  if (!d || d.error) {
    target.innerHTML = `<div class='card'><div class='label'>No data yet</div><div class='value'>—</div></div>`;
    metaTarget.textContent = d?.error || "";
    return;
  }
  const hasDeadline = d.arrival_deadline != null && d.arrival_deadline !== "";
  const bestLabel = hasDeadline
    ? `Latest safe departure (${d.day_of_week})`
    : `Best departure (${d.day_of_week})`;
  let fourthCard;
  if (hasDeadline) {
    const buf = d.buffer_minutes;
    let bufCls = "";
    let bufText;
    if (buf == null) bufText = "—";
    else if (buf <= 0) { bufCls = "loss"; bufText = `${buf} min`; }
    else if (buf < 10) { bufCls = "save"; bufText = `${buf} min`; }
    else { bufCls = "save"; bufText = `${buf} min`; }
    fourthCard = `
      <div class="card">
        <div class="label">Buffer vs ${d.arrival_deadline} deadline</div>
        <div class="value delta ${bufCls}">${bufText}</div>
      </div>`;
  } else {
    const savings = d.time_savings ?? 0;
    let deltaCls = savings > 0 ? "save" : savings < 0 ? "loss" : "";
    let deltaText;
    if (savings > 0) deltaText = `Save ${savings} min`;
    else if (savings < 0) deltaText = `+${Math.abs(savings)} min slower`;
    else deltaText = `On par`;
    fourthCard = `
      <div class="card">
        <div class="label">Wait-to-leave benefit</div>
        <div class="value delta ${deltaCls}">${deltaText}</div>
      </div>`;
  }
  let driveSub = "";
  if (d.typical_duration != null) {
    let s = `typical ${d.typical_duration} min`;
    if (d.p90_duration != null) s += ` · p90 ${d.p90_duration} min`;
    driveSub = `<div class="sub">${s}</div>`;
  }
  target.innerHTML = `
    <div class="card">
      <div class="label">${bestLabel}</div>
      <div class="value">${d.best_departure_time}</div>
    </div>
    <div class="card">
      <div class="label">Drive time</div>
      <div class="value">${d.optimal_duration} min</div>
      ${driveSub}
    </div>
    <div class="card">
      <div class="label">${hasDeadline ? "Predicted arrival" : "If you leave now"}</div>
      <div class="value">${hasDeadline ? d.arrival_time : d.current_duration + " min"}</div>
    </div>
    ${fourthCard}
  `;
  if (d.reliability_minutes != null && d.reliability_minutes >= 3) {
    const rel = document.createElement("p");
    rel.className = "reliability-note";
    rel.textContent = `Unreliable slot: pad ${d.reliability_minutes} min to cover the bad days (p90).`;
    target.appendChild(rel);
  }
  const note = d.note ? ` · ${d.note}` : "";
  metaTarget.textContent = `${d.origin}  →  ${d.destination}${note}`;
}

function renderRouteOptions(direction, d) {
  const target = document.querySelector(`.route-options[data-direction="${direction}"]`);
  if (!target) return;
  const opts = d?.route_options;
  if (!opts || opts.length < 2) {
    target.innerHTML = "";
    return;
  }
  const fastest = opts[0].duration_minutes;
  const rows = opts
    .map((o, i) => {
      const delta = o.duration_minutes - fastest;
      const tag =
        i === 0
          ? `<span class="route-tag fastest">fastest</span>`
          : `<span class="route-tag">+${delta} min</span>`;
      const dist = o.distance_km != null ? ` · ${o.distance_km} km` : "";
      return `
        <div class="route-row ${i === 0 ? "top" : ""}">
          <div class="route-label">${o.label}</div>
          <div class="route-dur">${o.duration_minutes} min${dist} ${tag}</div>
        </div>`;
    })
    .join("");
  target.innerHTML = `<h3>Route options now (leave at ${d.best_departure_time})</h3><div class="route-grid">${rows}</div>`;
}

function renderWindowHint(direction, d) {
  const el = document.querySelector(`.window-hint[data-direction="${direction}"]`);
  if (!el) return;
  const hint = d?.window_hint;
  if (!hint || !hint.message) {
    el.style.display = "none";
    return;
  }
  el.style.display = "block";
  el.innerHTML = `<span class="icon">↔</span>${hint.message}`;
}

function renderAlternatives(direction, d) {
  const target = document.querySelector(`.alternatives[data-direction="${direction}"]`);
  if (!target) return;
  const alts = d?.alternatives;
  if (!alts || alts.length === 0) {
    target.innerHTML = "";
    return;
  }
  const hasDeadline = d.arrival_deadline != null && d.arrival_deadline !== "";
  const title = hasDeadline
    ? `Top departures arriving by ${d.arrival_deadline}`
    : "Next-best slots";

  const rows = alts.map((a, i) => {
    let bufHtml = "";
    if (a.buffer_minutes != null) {
      let cls = "";
      if (a.buffer_minutes <= 0) cls = "zero";
      else if (a.buffer_minutes < 10) cls = "tight";
      bufHtml = `<div class="alt-buffer ${cls}"><div class="alt-label">Buffer</div><div class="alt-value">${a.buffer_minutes} min</div></div>`;
    } else {
      bufHtml = `<div><div class="alt-label">Arrival</div><div class="alt-value">${a.arrival_time}</div></div>`;
    }
    let deltaTag = "";
    if (a.delta_minutes != null && a.delta_minutes !== 0) {
      const sign = a.delta_minutes > 0 ? "+" : "";
      const cls = a.incident_severity || "";
      deltaTag = `<span class="alt-delta ${cls}">${sign}${a.delta_minutes} vs forecast</span>`;
    }
    return `
      <div class="alt-row ${i === 0 ? "top" : ""}">
        <div class="alt-time">${a.departure_time}</div>
        <div><div class="alt-label">Drive</div><div class="alt-value">${a.duration_minutes} min${deltaTag}</div></div>
        <div><div class="alt-label">Arrival</div><div class="alt-value">${a.arrival_time}</div></div>
        ${bufHtml}
      </div>`;
  }).join("");

  target.innerHTML = `<h3>${title}</h3><div class="alt-grid">${rows}</div>`;
}

function renderHeatmap(direction, data) {
  const container = document.querySelector(`.heatmap[data-direction="${direction}"]`);
  if (!data || !data.length) {
    container.innerHTML = "<p class='hint'>No heatmap data yet.</p>";
    return;
  }
  const times = [...new Set(data.map((d) => d.time))].sort();
  const days = WEEKDAYS.filter((d) => data.some((x) => x.day === d));
  const lookup = {};
  data.forEach((d) => {
    lookup[`${d.day}|${d.time}`] = d.duration;
  });
  const values = data.map((d) => d.duration);
  const min = Math.min(...values);
  const max = Math.max(...values);
  const colStyle = `grid-template-columns: 56px repeat(${times.length}, minmax(40px, 1fr));`;

  const parts = [];
  parts.push(`<div class="heatmap-row" style="${colStyle}">`);
  parts.push(`<div class="heatmap-label"></div>`);
  times.forEach((t) => parts.push(`<div class="heatmap-label">${t}</div>`));
  parts.push(`</div>`);

  days.forEach((day) => {
    parts.push(`<div class="heatmap-row" style="${colStyle}">`);
    parts.push(`<div class="heatmap-label row">${day}</div>`);
    times.forEach((t) => {
      const v = lookup[`${day}|${t}`];
      if (v == null) {
        parts.push(`<div class="heatmap-cell empty"></div>`);
      } else {
        const c = colorFor(v, min, max);
        parts.push(
          `<div class="heatmap-cell" style="background:${c}" title="${day} ${t} — ${v} min">${Math.round(
            v
          )}</div>`
        );
      }
    });
    parts.push(`</div>`);
  });

  container.innerHTML = parts.join("");
}

async function loadToday() {
  const r = await fetch("/api/commute/today");
  if (!r.ok) {
    DIRECTIONS.forEach((dir) => renderSummary(dir, null));
    return;
  }
  const data = await r.json();
  DIRECTIONS.forEach((dir) => {
    renderIncidentBanner(dir, data[dir]);
    renderSummary(dir, data[dir]);
    renderRouteOptions(dir, data[dir]);
    renderAlternatives(dir, data[dir]);
    renderWindowHint(dir, data[dir]);
  });
}

function renderIncidentBanner(direction, d) {
  const el = document.querySelector(`.incident-banner[data-direction="${direction}"]`);
  if (!el) return;
  const sev = d?.incident_severity;
  if (!sev || sev === "clear") { el.style.display = "none"; return; }
  el.style.display = "block";
  el.className = `incident-banner ${sev}`;
  const icon = sev === "alert" ? "⚠" : "▲";
  el.innerHTML = `<span class="icon">${icon}</span>${d.incident_note || ""}`;
}

async function loadHeatmap() {
  const r = await fetch("/api/commute/heatmap");
  if (!r.ok) {
    DIRECTIONS.forEach((dir) => renderHeatmap(dir, null));
    return;
  }
  const data = await r.json();
  DIRECTIONS.forEach((dir) => renderHeatmap(dir, data[dir] || []));
}

async function loadAll() {
  await Promise.all([loadToday(), loadHeatmap()]);
}

loadConfig();
loadAll();
