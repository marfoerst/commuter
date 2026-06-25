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
  // No actionable recommendation (e.g. weekend / not a commute day, or no
  // feasible slot left today). Show a friendly state instead of "null" cards.
  if (d.best_departure_time == null) {
    let msg = d.note || "No recommendation available right now.";
    if (/not a configured commute day/i.test(msg)) {
      msg = `No commute scheduled today (${d.day_of_week}). Recommendations resume on your next commute day.`;
    }
    target.innerHTML = `
      <div class="card wide">
        <div class="label">Nothing to recommend right now</div>
        <div class="value note">${msg}</div>
      </div>`;
    metaTarget.textContent = `${d.origin}  →  ${d.destination}`;
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
  if (r.status === 401) {
    showLogin("Session expired — please log in again.");
    return;
  }
  if (!r.ok) {
    DIRECTIONS.forEach((dir) => renderSummary(dir, null));
    return;
  }
  const data = await r.json();
  DIRECTIONS.forEach((dir) => {
    renderIncidentBanner(dir, data[dir]);
    renderLocalTraffic(dir, data[dir]);
    renderSummary(dir, data[dir]);
    renderRouteOptions(dir, data[dir]);
    renderAlternatives(dir, data[dir]);
    renderWindowHint(dir, data[dir]);
  });
}

function localTrafficClass(status) {
  if (status === "Staugefahr") return "stau";
  if (status === "erhöhte Verkehrsbelastung") return "erhoeht";
  if (status === "aktuell nicht ermittelbar") return "unknown";
  return "normal";
}

function renderLocalTraffic(direction, d) {
  const el = document.querySelector(`.local-traffic[data-direction="${direction}"]`);
  if (!el) return;
  const lt = d?.local_traffic;
  if (!lt || !lt.segment_count) {
    el.style.display = "none";
    return;
  }
  el.style.display = "block";
  const chipCls = localTrafficClass(lt.worst_status);
  const speed = lt.min_speed_kmh != null ? ` · slowest ${lt.min_speed_kmh} km/h` : "";
  let detail = "";
  if (lt.congested && lt.congested.length) {
    const rows = lt.congested
      .map((c) => {
        const s = c.speed_kmh != null ? `${c.speed_kmh} km/h` : "—";
        return `<li><span class="lt-dot ${localTrafficClass(c.status)}"></span>${c.status} · ${s}</li>`;
      })
      .join("");
    detail = `<ul class="lt-segments">${rows}</ul>`;
  }
  el.innerHTML = `
    <div class="lt-head">
      <span class="lt-chip ${chipCls}">${lt.worst_status}</span>
      <span class="lt-meta">${lt.segment_count} segment(s) on your route${speed}</span>
    </div>
    ${detail}
    <div class="lt-attr">${lt.attribution || ""}</div>`;
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

// --- Auth + views ---------------------------------------------------------
const loginView = document.getElementById("login-view");
const appView = document.getElementById("app-view");
const loginForm = document.getElementById("login-form");
const loginStatus = document.getElementById("login-status");
const whoami = document.getElementById("whoami");

let currentUser = null;

function showLogin(msg) {
  appView.style.display = "none";
  loginView.style.display = "flex";
  loginStatus.textContent = msg || "";
}

function showApp(user) {
  currentUser = user;
  loginView.style.display = "none";
  appView.style.display = "block";
  whoami.textContent = user.username + (user.is_admin ? " · admin" : "");
  document.querySelectorAll(".admin-only").forEach((el) => {
    el.style.display = user.is_admin ? "" : "none";
  });
  const nf = document.getElementById("notif-form");
  nf.ntfy_topic_url.value = user.ntfy_topic_url || "";
  nf.webhook_url.value = user.webhook_url || "";
  nf.push_min_severity.value = user.push_min_severity || "alert";
  document.getElementById("api-token").value = user.api_token || "";
  loadConfig();
  loadAll();
  if (user.is_admin) loadUsers();
}

async function checkAuth() {
  const r = await fetch("/api/me");
  if (r.ok) showApp((await r.json()).user);
  else showLogin();
}

loginForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  loginStatus.textContent = "";
  const fd = new FormData(loginForm);
  const r = await fetch("/api/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username: fd.get("username"), password: fd.get("password") }),
  });
  if (!r.ok) {
    const err = await r.json().catch(() => ({}));
    loginStatus.textContent = err.detail || "Login failed.";
    return;
  }
  loginForm.reset();
  showApp((await r.json()).user);
});

document.getElementById("logout-btn").addEventListener("click", async () => {
  await fetch("/api/logout", { method: "POST" });
  currentUser = null;
  showLogin("Logged out.");
});

document.getElementById("notif-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const fd = new FormData(e.target);
  const r = await fetch("/api/me/notifications", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      ntfy_topic_url: fd.get("ntfy_topic_url") || null,
      webhook_url: fd.get("webhook_url") || null,
      push_min_severity: fd.get("push_min_severity"),
    }),
  });
  document.getElementById("notif-status").textContent = r.ok ? "Saved." : "Save failed.";
});

document.getElementById("password-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const fd = new FormData(e.target);
  const r = await fetch("/api/me/password", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ password: fd.get("password") }),
  });
  if (r.ok) {
    e.target.reset();
    showLogin("Password changed — please log in again.");
  } else {
    document.getElementById("password-status").textContent = "Change failed (min 6 chars).";
  }
});

document.getElementById("regen-token").addEventListener("click", async () => {
  const r = await fetch("/api/me/api-token", { method: "POST" });
  if (r.ok) document.getElementById("api-token").value = (await r.json()).api_token;
});

async function loadUsers() {
  const r = await fetch("/api/admin/users");
  if (!r.ok) return;
  const users = (await r.json()).users;
  document.getElementById("user-list").innerHTML = users
    .map(
      (u) => `
      <div class="user-row">
        <span class="user-name">${u.username}${u.is_admin ? ' <span class="badge">admin</span>' : ""}</span>
        <span class="user-actions">
          <button class="ghost" data-act="reset" data-id="${u.id}" data-name="${u.username}">Reset password</button>
          <button class="ghost danger" data-act="delete" data-id="${u.id}" data-name="${u.username}">Delete</button>
        </span>
      </div>`
    )
    .join("");
}

document.getElementById("user-list").addEventListener("click", async (e) => {
  const btn = e.target.closest("button");
  if (!btn) return;
  const { act, id, name } = btn.dataset;
  if (act === "reset") {
    const pw = prompt(`New password for ${name} (min 6 chars):`);
    if (!pw) return;
    const r = await fetch(`/api/admin/users/${id}/password`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ password: pw }),
    });
    alert(r.ok ? "Password reset." : "Failed (min 6 chars).");
  } else if (act === "delete") {
    if (!confirm(`Delete user ${name} and all their data?`)) return;
    const r = await fetch(`/api/admin/users/${id}`, { method: "DELETE" });
    if (r.ok) loadUsers();
    else {
      const err = await r.json().catch(() => ({}));
      alert(err.detail || "Delete failed.");
    }
  }
});

document.getElementById("newuser-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const fd = new FormData(e.target);
  const r = await fetch("/api/admin/users", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      username: fd.get("username"),
      password: fd.get("password"),
      is_admin: fd.get("is_admin") === "on",
    }),
  });
  const st = document.getElementById("newuser-status");
  if (r.ok) {
    e.target.reset();
    st.textContent = "User created.";
    loadUsers();
  } else {
    const err = await r.json().catch(() => ({}));
    st.textContent = err.detail || "Create failed.";
  }
});

checkAuth();
