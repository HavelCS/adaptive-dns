"use strict";

const $ = (s) => document.querySelector(s);
const OUT = ["HIT", "PREFETCHED", "NEG_HIT", "COALESCED", "STALE", "MISS", "SERVFAIL", "BLOCKED"];
const LABEL = {
  HIT: "Hit", PREFETCHED: "Prefetched", NEG_HIT: "Negative hit", COALESCED: "Coalesced",
  MISS: "Miss", STALE: "Stale", SERVFAIL: "Failed", BLOCKED: "Blocked",
};
const HELP = {
  HIT: "Answered from the cache",
  PREFETCHED: "Answered from the cache; the entry was refreshed in the background before it expired",
  NEG_HIT: "\"Does not exist\" answer remembered from earlier (negative caching)",
  COALESCED: "Another client was already asking upstream; shared that answer",
  MISS: "Not cached; asked the upstream resolver",
  STALE: "Upstream unreachable; served the last known answer (serve-stale)",
  SERVFAIL: "Upstream unreachable and nothing cached",
  BLOCKED: "Name is on the block list",
};
const FEATURES = [
  ["arc", "ARC replacement", "Keeps names that are asked for repeatedly when one-off lookups flood the cache. Off = plain LRU."],
  ["prefetch", "Popularity-aware prefetch", "Refreshes popular names in the background before they expire, so nobody waits for them."],
  ["stretch", "Stability-learned TTL", "Caches a name longer than its TTL only if its answer has proven stable, never beyond 10% of its observed lifetime."],
  ["negative", "Negative caching", "Remembers \"does not exist\" answers (NXDOMAIN / no data) for the zone's SOA minimum, per RFC 2308."],
  ["serve_stale", "Serve-stale", "If every upstream is down, answers with the last known data instead of failing (RFC 8767)."],
  ["coalesce", "Query coalescing", "When many clients ask the same uncached name at once, only one query goes upstream."],
];

let state = null, rows = [], lastSeen = 0, entries = [], sortKey = "popularity", sortDir = -1;
let chipsBuilt = false, settingsDirty = false;

/* ---------- utils ---------- */
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
function ms(v) {
  if (v == null) return "–";
  if (v < 1) return v.toFixed(2) + " ms";
  if (v < 100) return v.toFixed(1) + " ms";
  if (v < 1000) return Math.round(v) + " ms";
  return (v / 1000).toFixed(2) + " s";
}
function dur(s) {
  if (s < 0) return "expired";
  if (s < 90) return Math.round(s) + " s";
  if (s < 5400) return Math.round(s / 60) + " min";
  return (s / 3600).toFixed(1) + " h";
}
const int = (n) => (n || 0).toLocaleString();
const pct = (x, d = 0) => (x * 100).toFixed(d) + "%";
const clock = (ts) => new Date(ts * 1000).toTimeString().slice(0, 8);
const css = (n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
async function post(path, body) {
  const r = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.error || r.statusText);
  return j;
}
let tt;
function toast(m) { const t = $("#toast"); t.textContent = m; t.classList.add("show"); clearTimeout(tt); tt = setTimeout(() => t.classList.remove("show"), 2600); }

/* ---------- act 1: lookup ---------- */
$("#lookup-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const name = $("#qname").value.trim() || $("#qname").placeholder;
  $("#qname").value = name;
  try {
    const tr = await post("/api/lookup", { name, type: $("#qtype").value });
    renderLookup(tr);
  } catch (e) { toast(e.message); }
});
$("#chips").addEventListener("click", (ev) => {
  const b = ev.target.closest("button"); if (!b) return;
  $("#qname").value = b.dataset.name; $("#qtype").value = "A";
  $("#lookup-form").requestSubmit();
});

function renderLookup(tr) {
  const info = tr.info || {}, out = info.outcome || "MISS";
  const resp = tr.response;
  $("#lookup-empty").hidden = true;
  $("#lookup-result").hidden = false;
  const chip = $("#lr-chip");
  chip.className = "chip " + out; chip.textContent = LABEL[out] || out;
  $("#lr-ms").textContent = ms(tr.ms);
  let explain = HELP[out] || "";
  if (out === "MISS" && info.upstream_ms) explain = `Asked upstream, which took ${ms(info.upstream_ms)}. Resolve it again to see the cache answer.`;
  if (["HIT", "PREFETCHED", "NEG_HIT"].includes(out) && info.saved_ms) explain += ` — saved ${ms(info.saved_ms)} of upstream time.`;
  $("#lr-explain").textContent = explain;

  // timeline: client -> resolver cache -> upstream
  const local = ["HIT", "PREFETCHED", "NEG_HIT"].includes(out) || out === "BLOCKED";
  const down = out === "STALE" || out === "SERVFAIL";
  const c = `var(--${out})`;
  const node = (label, letter, on) => `<div class="node ${on ? "on" : "off"}" style="--c:${c}"><div class="dot">${letter}</div>${label}</div>`;
  const link = (cls, txt) => `<div class="link ${cls}" style="--c:${c}"><span>${txt}</span></div>`;
  let l2;
  if (local) l2 = link("off", "not needed");
  else if (down) l2 = link("down", "no answer");
  else if (out === "COALESCED") l2 = link("on", "shared in-flight query");
  else l2 = link("on", ms(info.upstream_ms));
  $("#timeline").innerHTML = node("Client", "C", true) + link("on", local || down ? ms(tr.ms) : "query") +
    node(out === "BLOCKED" ? "Block list" : "Resolver cache", "R", true) + l2 + node("Upstream", "U", !local && !down);

  const recs = resp.answers.length ? resp.answers : resp.authority;
  $("#lr-answers").innerHTML = recs.length ? recs.map((r) =>
    `<tr><td>${esc(r.name)}</td><td>${r.ttl}</td><td>${esc(r.type)}</td><td>${esc(r.data)}</td></tr>`).join("") +
    (!resp.answers.length ? `<tr><td colspan="4" class="none">${resp.rcode === "NXDOMAIN" ? "The name does not exist" : "The name exists but has no record of this type"} — the SOA above sets how long that answer may be cached.</td></tr>` : "")
    : `<tr><td colspan="4" class="none">${esc(resp.rcode)}</td></tr>`;
  $("#lr-steps").innerHTML = (info.steps || []).slice(1).map(([, t, m]) => `<li><span>${esc(t)}</span><span class="t">${m.toFixed(2)} ms</span></li>`).join("");
  renderPacket(tr.response_hex, resp);
}

function renderPacket(hex, resp) {
  const b = hex.match(/../g).map((h) => parseInt(h, 16));
  const u16 = (i) => (b[i] << 8) | b[i + 1];
  const flags = u16(2);
  const f = (bit, n) => (flags & bit ? n : null);
  const fl = [f(0x8000, "QR"), f(0x400, "AA"), f(0x200, "TC"), f(0x100, "RD"), f(0x80, "RA")].filter(Boolean).join(" ");
  $("#pkt-size").textContent = `· ${b.length} bytes`;
  $("#pkt-hdr").innerHTML = `
    <div><span>ID</span><b>${u16(0)}</b></div>
    <div class="wide"><span>Flags (0x${flags.toString(16).padStart(4, "0")})</span><b>${fl}</b></div>
    <div><span>RCODE</span><b>${esc(resp.rcode)}</b></div>
    <div><span>Questions</span><b>${u16(4)}</b></div><div><span>Answers</span><b>${u16(6)}</b></div>
    <div><span>Authority</span><b>${u16(8)}</b></div><div><span>Additional</span><b>${u16(10)}</b></div>`;
  // find section boundaries
  const skipName = (i) => { while (i < b.length) { const l = b[i]; if (l === 0) return i + 1; if ((l & 0xc0) === 0xc0) return i + 2; i += l + 1; } return i; };
  let i = 12;
  for (let q = 0; q < u16(4); q++) i = skipName(i) + 4;
  const qEnd = i;
  for (let a = 0; a < u16(6); a++) { i = skipName(i); i += 10 + u16(i + 8); }
  const anEnd = i;
  $("#pkt-hex").innerHTML = b.map((x, k) => {
    const cls = k < 12 ? "h-hdr" : k < qEnd ? "h-q" : k < anEnd ? "h-an" : "h-rest";
    return `<span class="${cls}">${x.toString(16).padStart(2, "0")}</span>`;
  }).join("");
}

/* ---------- act 2: live ---------- */
const LMAX = Math.log10(3000);
const frac = (v) => Math.min(1, Math.max(0.012, Math.log10(Math.max(0.1, v) * 10) / (LMAX + 1)));
function buildStatic() {
  $("#axis").innerHTML = [[0.1, "0.1 ms"], [1, "1 ms"], [10, "10 ms"], [100, "100 ms"], [3000, "3 s"]]
    .map(([v, l]) => `<span style="left:${(frac(v) * 100).toFixed(1)}%">${l}</span>`).join("");
  $("#legend").innerHTML = OUT.filter((o) => o !== "BLOCKED").map((o) => `<span title="${esc(HELP[o])}"><i class="sw ${o}"></i>${LABEL[o]}</span>`).join("");
  $("#features").innerHTML = FEATURES.map(([k, t, d]) => `
    <div class="feat">
      <label class="switch"><input type="checkbox" data-f="${k}"><span class="track"><span class="knob"></span></span><span>${t}</span></label>
      <p>${d}</p><div class="stat" id="fs-${k}"></div>
    </div>`).join("");
}
function renderList(fresh) {
  if (fresh.length) rows = fresh.slice().reverse().concat(rows).slice(0, 22);
  $("#q-empty").hidden = rows.length > 0;
  const ids = new Set(fresh.map((r) => r.id));
  $("#qlist").innerHTML = rows.map((r) => `
    <li class="q ${r.outcome}${ids.has(r.id) ? " new" : ""}" title="${esc(`${r.name} ${r.type} → ${r.rcode}\n${r.answer}\n${HELP[r.outcome] || ""}${r.note ? "\n" + r.note : ""}`)}">
      <span class="tm">${clock(r.ts)}</span>
      <span class="chip ${r.outcome}">${LABEL[r.outcome] || r.outcome}</span>
      <span class="nm">${esc(r.name)}<small>${esc(r.type)}</small>${r.outdated ? ' <span class="warn" title="Answer differed from the authoritative data">outdated</span>' : ""}</span>
      <span class="tr"><span class="bar" style="width:calc((100% - 76px) * ${frac(r.ms).toFixed(4)})"></span><span class="ms">${ms(r.ms)}</span></span>
    </li>`).join("");
}
function renderKpis(s) {
  $("#k-hit").textContent = pct(s.hit_ratio);
  const cached = s.outcomes.HIT + s.outcomes.PREFETCHED + s.outcomes.NEG_HIT + s.outcomes.COALESCED + s.outcomes.STALE;
  $("#k-hit-f").textContent = `${int(cached)} of ${int(cached + s.outcomes.MISS + s.outcomes.SERVFAIL)} queries answered without waiting for upstream`;
  $("#k-lc").textContent = ms(s.latency.cache_avg);
  $("#k-lu").textContent = ms(s.latency.upstream_avg);
  $("#k-lat-f").textContent = s.latency.cache_avg && s.latency.upstream_avg
    ? `cache answers in ${(s.latency.cache_avg / s.latency.upstream_avg * 100).toFixed(1)}% of the time` : "average of recent lookups";
  $("#k-saved").textContent = int(s.upstream_saved);
  $("#k-saved-f").textContent = `${int(s.upstream_queries)} sent for ${int(s.queries)} queries, incl. ${int(s.prefetches)} prefetches`;
  const t = s.time_saved_ms / 1000;
  $("#k-time").textContent = t < 90 ? t.toFixed(1) + " s" : dur(t);
  if (s.verified) {
    $("#k-acc").textContent = pct(1 - s.outdated / s.verified, 1);
    $("#k-acc-f").textContent = `${int(s.verified - s.outdated)} of ${int(s.verified)} answers matched the authoritative data`;
  } else {
    $("#k-acc").textContent = "–";
    $("#k-acc-f").textContent = state.demo ? "checked against the authoritative data" : "needs --demo (the true data is only known for the simulated zone)";
  }
}
function renderMix(s) {
  const tot = OUT.reduce((a, o) => a + s.outcomes[o], 0);
  $("#mix").innerHTML = OUT.filter((o) => s.outcomes[o]).map((o) => `<i class="${o}" style="flex-grow:${s.outcomes[o]}" title="${LABEL[o]}: ${s.outcomes[o]}"></i>`).join("");
  $("#mixlist").innerHTML = OUT.filter((o) => s.outcomes[o] || ["HIT", "MISS", "NEG_HIT", "PREFETCHED"].includes(o)).map((o) =>
    `<div title="${esc(HELP[o])}"><dt><i class="sw ${o}"></i>${LABEL[o]}</dt><dd>${int(s.outcomes[o])}${tot ? ` <span class="muted">${Math.round(s.outcomes[o] / tot * 100)}%</span>` : ""}</dd></div>`).join("");
}
function renderChart(series) {
  const cv = $("#tp"), dpr = devicePixelRatio || 1, w = cv.clientWidth, h = 150;
  if (cv.width !== Math.round(w * dpr)) { cv.width = Math.round(w * dpr); cv.height = h * dpr; }
  const x = cv.getContext("2d");
  x.setTransform(dpr, 0, 0, dpr, 0, 0); x.clearRect(0, 0, w, h);
  const pad = { l: 28, r: 4, t: 8, b: 18 }, n = 90, data = series.slice(-n);
  const max = Math.max(5, ...data.map((d) => Math.max(d.cache + d.upstream + d.fail, d.up_q)));
  const step = [1, 2, 5, 10, 20, 25, 50, 100, 200, 500].find((s) => s * 3 >= max) || 1000;
  const top = Math.ceil(max / step) * step, ih = h - pad.t - pad.b, iw = w - pad.l - pad.r;
  const y = (v) => pad.t + ih - v / top * ih;
  x.font = `11px ${css("--sans")}`; x.fillStyle = css("--faint"); x.strokeStyle = css("--rule-soft"); x.textAlign = "right"; x.textBaseline = "middle";
  for (let v = 0; v <= top; v += step) { x.beginPath(); x.moveTo(pad.l, Math.round(y(v)) + .5); x.lineTo(w - pad.r, Math.round(y(v)) + .5); x.stroke(); x.fillText(v, pad.l - 6, y(v)); }
  x.textBaseline = "alphabetic"; x.textAlign = "left"; x.fillText("−90 s", pad.l, h - 3); x.textAlign = "right"; x.fillText("now", w - pad.r, h - 3);
  const bw = iw / n, off = n - data.length, cols = [css("--HIT"), css("--MISS"), css("--SERVFAIL")];
  data.forEach((d, i) => {
    let base = 0; const px = pad.l + (off + i) * bw + .5;
    [d.cache, d.upstream, d.fail].forEach((v, k) => { if (!v) return; x.fillStyle = cols[k]; x.fillRect(px, y(base + v), Math.max(1, bw - 1.5), y(base) - y(base + v)); base += v; });
  });
  x.strokeStyle = css("--ink"); x.lineWidth = 1.5; x.beginPath();
  data.forEach((d, i) => { const px = pad.l + (off + i + .5) * bw; i ? x.lineTo(px, y(d.up_q)) : x.moveTo(px, y(d.up_q)); });
  x.stroke();
}
function renderPolicy(c) {
  if (c.name === "ARC") {
    $("#pol-title").textContent = "ARC replacement";
    $("#pol-hint").textContent = "T1 holds names seen once, T2 names seen again. B1/B2 remember recent evictions; a request for a remembered name moves the target p.";
    const tot = Math.max(1, c.b1 + c.t1 + c.t2 + c.b2);
    const seg = (cls, v, l) => `<div class="${cls}" style="flex-grow:${Math.max(v, .0001)}" title="${l}: ${v}">${v / tot > .1 ? `${l} ${v}` : ""}</div>`;
    const p = (c.b1 + c.p) / Math.max(1, c.b1 + c.capacity + c.b2) * 100;
    $("#pol-viz").innerHTML = `<div class="arc"><div class="arc-lab"><span>← recency</span><span>frequency →</span></div>
      <div class="arc-track">${seg("arc-b", c.b1, "B1")}${seg("arc-t1", c.t1, "T1")}${seg("arc-t2", c.t2, "T2")}${seg("arc-b", c.b2, "B2")}
      <span class="arc-p" style="left:${Math.min(99.5, p).toFixed(1)}%" title="p = ${c.p}"></span></div>
      <p class="arc-note">Target size of T1: <b>p = ${c.p}</b> of ${c.capacity}. Ghost hits so far: <b>${c.ghost_hits.B1}</b> in B1, <b>${c.ghost_hits.B2}</b> in B2.</p></div>`;
  } else {
    $("#pol-title").textContent = "LRU replacement";
    $("#pol-hint").textContent = "Evicts the name unused for the longest time. Switch ARC back on in Algorithm features to compare.";
    $("#pol-viz").innerHTML = "";
  }
  $("#f-n").textContent = `${int(c.entries)} / ${int(c.max_entries)}`;
  $("#f-bar").style.width = Math.min(100, c.entries / c.max_entries * 100) + "%";
  $("#f-str").textContent = int(c.stretched);
  $("#f-neg").textContent = int(c.negative);
  $("#f-ev").textContent = int(c.evictions);
}
function renderFeatures(st) {
  const f = st.cache.features, s = st.summary, c = st.cache;
  document.querySelectorAll("[data-f]").forEach((el) => { el.checked = !!f[el.dataset.f]; });
  const stat = {
    arc: c.name === "ARC" ? `${c.ghost_hits.B1 + c.ghost_hits.B2} ghost hits` : "",
    prefetch: `${int(s.prefetches)} prefetches · ${int(s.outcomes.PREFETCHED)} answers kept warm`,
    stretch: `${int(c.stretched)} entries stretched now`,
    negative: `${int(s.outcomes.NEG_HIT)} negative hits`,
    serve_stale: `${int(s.outcomes.STALE)} stale answers served`,
    coalesce: `${int(s.outcomes.COALESCED)} queries coalesced`,
  };
  for (const k in stat) $("#fs-" + k).textContent = f[k] ? stat[k] : "off";
}
document.addEventListener("change", async (ev) => {
  const k = ev.target.dataset?.f; if (!k) return;
  try { await post("/api/features", { [k]: ev.target.checked }); toast(`${FEATURES.find((x) => x[0] === k)[1]} ${ev.target.checked ? "on" : "off"}`); }
  catch (e) { toast(e.message); }
});
document.querySelectorAll("[data-preset]").forEach((b) => b.addEventListener("click", async () => {
  const on = b.dataset.preset === "all";
  await post("/api/features", Object.fromEntries(FEATURES.map(([k]) => [k, on])));
  toast(on ? "Full adaptive algorithm on" : "Basic TTL cache: every adaptive feature off");
}));

/* ---------- entries ---------- */
function renderEntries() {
  const q = $("#filter").value.trim().toLowerCase();
  const list = entries.filter((e) => !q || e.name.includes(q)).sort((a, b) => {
    const x = a[sortKey], y = b[sortKey];
    return x === y ? 0 : (x > y ? 1 : -1) * sortDir;
  });
  document.querySelectorAll("th[data-s]").forEach((th) => th.dataset.s === sortKey
    ? th.setAttribute("aria-sort", sortDir > 0 ? "ascending" : "descending") : th.removeAttribute("aria-sort"));
  $("#entries-empty").hidden = entries.length > 0;
  $("#entries").innerHTML = list.map((e) => {
    const fresh = e.remaining > 0, frac = fresh ? Math.min(1, e.remaining / e.ttl) : 1;
    const ans = e.negative ? `<td class="ans neg">${e.rcode === "NXDOMAIN" ? "does not exist" : "no such record"}</td>`
      : `<td class="ans" title="${esc((e.cname.length ? "via " + e.cname[0] + "\n" : "") + e.data.join("\n"))}">${e.cname.length ? "→ " : ""}${esc(e.data[0] || "")}</td>`;
    const stab = e.changes ? `<span class="stab changes" title="answer changed ${e.changes}×; typical lifetime ${dur(e.lifetime || 0)}">changes every ~${dur(e.lifetime || 0)}</span>`
      : `<span class="stab">unchanged ${dur(e.stable_for)}</span>`;
    return `<tr>
      <td class="nm" title="${esc(e.name)}">${esc(e.name)}</td><td>${esc(e.type)}</td>${ans}
      <td>${e.segment ? `<span class="seg ${esc(e.segment)}">${esc(e.segment)}</span>` : ""}</td>
      <td class="num">${e.popularity.toFixed(1)}</td>
      <td class="ttl">${dur(e.orig_ttl)}${e.stretch > 1 ? ` → <span class="str" title="stretched ×${e.stretch} because the answer has been stable">${dur(e.ttl)}</span>` : ""}</td>
      <td><div class="left${fresh ? "" : " stale"}"><div class="meter"><i style="width:${frac * 100}%"></i></div><span>${fresh ? dur(e.remaining) : "expired, kept for serve-stale"}</span></div></td>
      <td>${stab}</td>
      <td><button class="x" data-del="${esc(e.name)}|${esc(e.type)}" aria-label="Remove ${esc(e.name)}">✕</button></td></tr>`;
  }).join("");
}
document.querySelectorAll("th[data-s]").forEach((th) => th.addEventListener("click", () => {
  const k = th.dataset.s;
  if (sortKey === k) sortDir = -sortDir; else { sortKey = k; sortDir = ["name", "type", "segment"].includes(k) ? 1 : -1; }
  renderEntries();
}));
$("#filter").addEventListener("input", renderEntries);
$("#entries").addEventListener("click", async (ev) => {
  const v = ev.target.closest("[data-del]")?.dataset.del; if (!v) return;
  const [name, type] = v.split("|");
  await post("/api/cache/delete", { name, type }); toast(`Removed ${name}`);
});

/* ---------- act 3: evaluation ---------- */
const METRICS = [
  ["mean_ms", "Mean lookup time", "lower is better", (v) => ms(v), -1],
  ["hit_ratio", "Hit ratio", "higher is better", (v) => pct(v, 1), 1],
  ["upstream_queries", "Queries sent upstream", "lower is better", (v) => int(v), -1],
  ["outage_answered_pct", "Answered during outage", "higher is better", (v) => v.toFixed(0) + "%", 1],
  ["outdated_of_cached_pct", "Outdated cache answers", "lower is better", (v) => v.toFixed(1) + "%", -1],
];
let evalTimer;
$("#eval-btn").addEventListener("click", async () => {
  try {
    await post("/api/experiment", { minutes: 20, qps: 25, capacity: 300 });
    $("#eval-btn").disabled = true; $("#eval-btn").textContent = "Running…";
    $("#eval-prog").hidden = false;
    clearInterval(evalTimer); evalTimer = setInterval(pollEval, 400);
  } catch (e) { toast(e.message); }
});
async function pollEval() {
  const r = await (await fetch("/api/experiment")).json();
  $("#eval-prog i").style.width = (r.progress * 100) + "%";
  if (r.state === "done" || r.state === "error") {
    clearInterval(evalTimer);
    $("#eval-btn").disabled = false; $("#eval-btn").textContent = "Run again";
    $("#eval-prog").hidden = true;
    if (r.state === "error") return toast("Evaluation failed: " + r.error);
    renderEval(r.result);
  }
}
function renderEval(res) {
  const by = Object.fromEntries(res.rows.map((r) => [r.id, r]));
  const main = ["none", "basic", "proposed"].map((k) => by[k]);
  $("#eval-empty").hidden = true; $("#eval-out").hidden = false;
  $("#cmp").innerHTML = METRICS.map(([k, title, better, fmt, dir]) => {
    const vals = main.map((r) => r[k] ?? 0), max = Math.max(...vals, 1e-9);
    const b = by.basic[k], p = by.proposed[k];
    let delta = "";
    if (b) {
      const ch = (p - b) / b * 100, good = ch * dir > 0;
      delta = `<div class="delta ${good ? "" : "neg"}">${ch > 0 ? "+" : ""}${ch.toFixed(0)}% vs basic cache</div>`;
    }
    return `<div class="metric"><h4>${title}</h4><div class="better">${better}</div>
      ${main.map((r, i) => `<div class="mb ${r.id}"><div class="lbl"><span>${esc(r.label)}</span><b>${fmt(vals[i])}</b></div><div class="trk"><i style="width:0" data-w="${(vals[i] / max * 100).toFixed(1)}"></i></div></div>`).join("")}
      ${delta}</div>`;
  }).join("");
  requestAnimationFrame(() => document.querySelectorAll("#cmp .trk i").forEach((i) => { i.style.width = i.dataset.w + "%"; }));

  const ref = by.proposed;
  const d = (r, k, dir, fmt) => {
    if (r === ref) return fmt(r[k]);
    const diff = r[k] - ref[k], rel = ref[k] ? diff / ref[k] : 0;
    const cls = Math.abs(rel) < 0.005 ? "same" : diff * dir > 0 ? "better" : "worse";
    return `${fmt(r[k])}<small class="${cls}">${diff > 0 ? "+" : ""}${fmt(diff)}</small>`;
  };
  const f = { mean_ms: (v) => v.toFixed(1) + " ms", hit_ratio: (v) => (v * 100).toFixed(1) + "%", upstream_queries: (v) => int(Math.round(v)),
    outdated_of_cached_pct: (v) => v.toFixed(1) + "%", outage_answered_pct: (v) => v.toFixed(0) + "%" };
  $("#abl").innerHTML = [ref, ...res.rows.filter((r) => r.group === "ablation")].map((r) => `
    <tr class="${r === ref ? "ref" : ""}"><td>${esc(r.label)}</td>
    <td class="num">${d(r, "mean_ms", -1, f.mean_ms)}</td><td class="num">${d(r, "hit_ratio", 1, f.hit_ratio)}</td>
    <td class="num">${d(r, "upstream_queries", -1, f.upstream_queries)}</td><td class="num">${d(r, "outdated_of_cached_pct", -1, f.outdated_of_cached_pct)}</td>
    <td class="num">${d(r, "outage_answered_pct", 1, f.outage_answered_pct)}</td></tr>`).join("");
  const p = res.params;
  $("#eval-foot").textContent = `${int(p.queries)} queries over ${p.minutes} simulated minutes, ${int(p.names)} domain names, cache of ${p.capacity} entries, upstream outage from minute ${(p.outage_s[0] / 60).toFixed(0)} to ${(p.outage_s[1] / 60).toFixed(0)}. Latency and hit ratio exclude the outage. Simulated in ${res.elapsed_s} s.`;
}

/* ---------- top bar / misc ---------- */
function renderMeta(st) {
  const r = st.resolver;
  $("#res-pill").className = "pill ok";
  $("#res-text").innerHTML = `Resolver <b>${esc(r.host)}:${r.port}</b>`;
  const down = r.down || st.outage;
  $("#up-pill").className = "pill " + (down ? "bad" : "ok");
  $("#up-text").innerHTML = down ? "Upstream unreachable" : `Upstream <b>${esc(r.upstreams.join(", "))}</b>${st.demo ? " (simulated)" : ""}`;
  const ow = $("#outage-wrap");
  if (document.activeElement !== $("#outage")) $("#outage").checked = st.outage;
  ow.title = st.demo ? "Stop the simulated upstream from answering" : "Cut the resolver off from the real upstream servers";
  const ex = st.examples[0];
  $("#q-cmd").textContent = `nslookup -port=${r.port} ${ex} 127.0.0.1`;
  if (!chipsBuilt) {
    chipsBuilt = true;
    $("#qname").placeholder = ex;
    $("#chips").innerHTML = st.examples.map((n) => `<button type="button" data-name="${esc(n)}">${esc(n)}</button>`).join("");
  }
  const t = st.traffic, btn = $("#traffic-btn");
  if (t.running) { btn.classList.add("running"); btn.textContent = `Stop traffic · ${t.remaining}s`; }
  else { btn.classList.remove("running"); btn.textContent = "Start user traffic"; }
  if (!settingsDirty) {
    const f = $("#settings");
    f.max_entries.value = st.cache.max_entries;
    if (document.activeElement !== f.blocklist) f.blocklist.value = r.blocklist.join(", ");
  }
}
function apply(st) {
  state = st;
  renderMeta(st); renderKpis(st.summary); renderMix(st.summary); renderChart(st.series);
  renderPolicy(st.cache); renderFeatures(st);
  if (st.log.length && st.log[st.log.length - 1].id < lastSeen) rows = [];
  const fresh = st.log.filter((r) => r.id > lastSeen);
  if (st.log.length) lastSeen = st.log[st.log.length - 1].id;
  renderList(fresh);
  if (st.entries) { entries = st.entries; renderEntries(); }
}
$("#traffic-btn").addEventListener("click", async () => {
  if (state?.traffic.running) { await post("/api/traffic/stop"); toast("Traffic stopped"); }
  else { await post("/api/traffic/start", { seconds: 90, rate: 20 }); toast("Simulated users are browsing for 90 seconds"); }
});
$("#outage").addEventListener("change", async (ev) => {
  try { await post("/api/outage", { on: ev.target.checked }); toast(ev.target.checked ? "Upstream is down. Watch the stale answers." : "Upstream is back"); }
  catch (e) { toast(e.message); ev.target.checked = false; }
});
$("#settings").addEventListener("input", () => { settingsDirty = true; });
$("#settings").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const f = ev.target;
  await post("/api/config", { max_entries: +f.max_entries.value, blocklist: f.blocklist.value.split(",") });
  settingsDirty = false; toast("Settings saved");
});
$("#clear").addEventListener("click", async () => { await post("/api/cache/clear"); toast("Cache cleared"); });
$("#reset").addEventListener("click", async () => { await post("/api/stats/reset"); rows = []; lastSeen = 0; renderList([]); toast("Statistics reset"); });
window.addEventListener("resize", () => state && renderChart(state.series));

function connect() {
  const es = new EventSource("/api/stream");
  es.onmessage = (e) => apply(JSON.parse(e.data));
  es.onerror = () => { $("#res-pill").className = "pill bad"; $("#res-text").textContent = "Dashboard lost the resolver. Retrying…"; };
}
buildStatic();
renderList([]);
connect();
fetch("/api/experiment").then((r) => r.json()).then((r) => { if (r.state === "done") renderEval(r.result); });
