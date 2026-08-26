/* The analyst interface.
 *
 * Talks only to the API on the same origin. Two rules the whole file follows:
 *
 *  1. Nothing is invented to fill a panel. If a run produced no chart, the panel says so. A
 *     dashboard that always looks complete is one you cannot read when it isn't.
 *  2. Every number on screen is one click from the query behind it, and the queries the guard
 *     *refused* are shown too — what the agent tried is usually what a reviewer wants to know.
 *
 * Charts are drawn as inline SVG from the spec the API returns, rather than by loading a
 * plotting library. It keeps the page a single self-contained asset with no third-party request,
 * and the series colours arrive already chosen by the validated palette on the server — this
 * file never picks a colour for data.
 */

const API = "";
const LIVE = new Set(["received", "investigating"]);
const POLL_MS = 2500;

const QUESTIONS = [
  ["What was monthly revenue in 2018?", "green"],
  ["Why did revenue drop in March 2018?", "violet"],
  ["Which product categories drove the most revenue?", "amber"],
  ["How is on-time delivery trending by seller state?", "blue"],
  ["What was the average order value by customer state?", "green"],
  ["Which sellers concentrate the most revenue?", "violet"],
  // These two exist on purpose: one has no approved definition and one cannot be answered from
  // this data, so the starting questions include cases where the right move is to stop and say so.
  ["What is our customer churn rate?", "amber"],
  ["How did marketing spend affect sales?", "blue"],
];

const TILES = {
  green: ["rgba(31,122,77,.13)", "#1f7a4d"],
  violet: ["rgba(108,92,231,.13)", "#6c5ce7"],
  amber: ["rgba(192,112,0,.13)", "#c07000"],
  blue: ["rgba(42,120,214,.13)", "#2a78d6"],
};

const STATUS = {
  completed: ["Completed", "✓", "#1f7a4d"],
  investigating: ["Investigating", "◐", "#2a78d6"],
  received: ["Queued", "·", "#6b6a66"],
  clarifying: ["Waiting on you", "?", "#2a78d6"],
  awaiting_approval: ["Awaiting approval", "⏸", "#9a6b00"],
  truncated: ["Truncated", "!", "#9a6b00"],
  failed: ["Failed", "✕", "#b23c3c"],
  allowed: ["Allowed", "✓", "#1f7a4d"],
  approved: ["Approved by a human", "✓", "#2a78d6"],
  escalated: ["Escalated", "⏸", "#9a6b00"],
  rejected: ["Blocked", "✕", "#b23c3c"],
  supported: ["Supported", "✓", "#1f7a4d"],
  refuted: ["Refuted", "✕", "#b23c3c"],
  inconclusive: ["Inconclusive", "~", "#9a6b00"],
  proposed: ["Proposed", "·", "#6b6a66"],
};

const CONFIDENCE = {
  high: ["High confidence", "●●●", "#1f7a4d"],
  medium: ["Medium confidence", "●●○", "#9a6b00"],
  low: ["Low confidence", "●○○", "#b23c3c"],
};

const state = {
  page: "ask",
  runId: null,
  suggestPage: 0,
  who: localStorage.getItem("who") || "analyst@example.com",
  bookmarks: new Set(JSON.parse(localStorage.getItem("bookmarks") || "[]")),
  timer: null,
};

/* ---------------------------------------------------------------- helpers */

const $ = (id) => document.getElementById(id);
const esc = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

async function get(path) {
  const response = await fetch(API + path, { headers: { accept: "application/json" } });
  if (!response.ok) throw new Error((await response.text()) || response.statusText);
  return response.json();
}

async function post(path, body) {
  const response = await fetch(API + path, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  if (!response.ok) {
    let detail = await response.text();
    try { detail = JSON.parse(detail).detail || detail; } catch (_) {}
    throw new Error(detail);
  }
  return response.json();
}

function toast(message) {
  const element = $("toast");
  element.textContent = message;
  element.classList.add("show");
  clearTimeout(element._t);
  element._t = setTimeout(() => element.classList.remove("show"), 4200);
}

function chip(key) {
  const [label, icon, colour] = STATUS[key] || [key || "unknown", "·", "#6b6a66"];
  return `<span class="chip" style="color:${colour};border-color:${colour}55;background:${colour}12">
    <span class="ico">${icon}</span>${esc(label)}</span>`;
}

function confidenceChip(level) {
  const [label, icon, colour] = CONFIDENCE[level] || ["Confidence unstated", "○○○", "#6b6a66"];
  return `<span class="chip" style="color:${colour};border-color:${colour}55;background:${colour}12">
    <span class="ico">${icon}</span>${esc(label)}</span>`;
}

/** How long ago, in the units a person would use. */
function ago(iso) {
  if (!iso) return "";
  const then = new Date(iso);
  if (isNaN(then)) return String(iso).slice(0, 16);
  const seconds = (Date.now() - then.getTime()) / 1000;
  if (seconds < 90) return "just now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)} minutes ago`;
  if (seconds < 86400) {
    const hours = Math.floor(seconds / 3600);
    return `${hours} hour${hours > 1 ? "s" : ""} ago`;
  }
  const days = Math.floor(seconds / 86400);
  return days === 1 ? "yesterday" : `${days} days ago`;
}

const money = (value) => {
  const n = Number(value);
  if (!isFinite(n)) return String(value);
  if (Math.abs(n) >= 1e6) return (n / 1e6).toFixed(1).replace(/\.0$/, "") + "M";
  if (Math.abs(n) >= 1e3) return (n / 1e3).toFixed(0) + "K";
  return n.toFixed(Math.abs(n) < 10 ? 2 : 0);
};

/* ------------------------------------------------------------ svg charts */

/**
 * An area chart, drawn from the series the server put in the spec.
 *
 * Thin marks, a recessive grid, a soft fill under a 2px line, and labels only on the ends and
 * the middle of the x axis — a label on every point is noise at this size.
 */
function areaChart(x, y, colour) {
  const width = 560;
  const height = 232;
  const left = 46;
  const right = 10;
  const top = 12;
  const bottom = 26;
  const plotWidth = width - left - right;
  const plotHeight = height - top - bottom;

  const values = y.map(Number).filter((v) => isFinite(v));
  if (!values.length) return `<div class="plot-blank">No numeric values to plot.</div>`;
  const max = Math.max(...values);
  const min = Math.min(0, Math.min(...values));
  const span = max - min || 1;

  const px = (i) => left + (y.length === 1 ? plotWidth / 2 : (i / (y.length - 1)) * plotWidth);
  const py = (v) => top + plotHeight - ((Number(v) - min) / span) * plotHeight;

  const line = y.map((v, i) => `${i ? "L" : "M"}${px(i).toFixed(1)} ${py(v).toFixed(1)}`).join(" ");
  const area = `${line} L${px(y.length - 1).toFixed(1)} ${top + plotHeight} L${px(0).toFixed(1)} ${top + plotHeight} Z`;

  const ticks = 4;
  let grid = "";
  for (let t = 0; t <= ticks; t++) {
    const value = min + (span * t) / ticks;
    const yy = py(value);
    grid += `<line x1="${left}" y1="${yy.toFixed(1)}" x2="${width - right}" y2="${yy.toFixed(1)}"
             stroke="var(--border)" stroke-width="1"/>
             <text x="${left - 8}" y="${(yy + 3.5).toFixed(1)}" text-anchor="end"
             font-size="10.5" fill="var(--text-muted)">${money(value)}</text>`;
  }

  const marks = [0, Math.floor((x.length - 1) / 2), x.length - 1].filter(
    (i, k, all) => i >= 0 && all.indexOf(i) === k
  );
  const xLabels = marks
    .map(
      (i) =>
        `<text x="${px(i).toFixed(1)}" y="${height - 7}" font-size="10.5" fill="var(--text-muted)"
         text-anchor="${i === 0 ? "start" : i === x.length - 1 ? "end" : "middle"}">${esc(x[i])}</text>`
    )
    .join("");

  return `<svg class="plot" viewBox="0 0 ${width} ${height}" preserveAspectRatio="none" role="img">
    <defs><linearGradient id="fill" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="${colour}" stop-opacity=".34"/>
      <stop offset="1" stop-color="${colour}" stop-opacity=".03"/>
    </linearGradient></defs>
    ${grid}
    <path d="${area}" fill="url(#fill)"/>
    <path d="${line}" fill="none" stroke="${colour}" stroke-width="2"
          stroke-linejoin="round" stroke-linecap="round"/>
    ${xLabels}
  </svg>`;
}

/**
 * A ring, with the share in the legend beside each label.
 *
 * A ring of percentages is unreadable at this size, and putting the share in the legend means
 * the chart survives a screenshot, a grey print, and a reader who cannot separate two hues —
 * the label carries identity, not the colour.
 */
function donut(labels, values, colours) {
  const size = 132;
  const radius = 52;
  const thickness = 21;
  const centre = size / 2;
  const total = values.reduce((a, b) => a + Number(b), 0) || 1;

  let angle = -Math.PI / 2;
  let arcs = "";
  values.forEach((value, index) => {
    const sweep = (Number(value) / total) * Math.PI * 2;
    // A 2px surface gap between slices, so adjacent marks read as separate.
    const gap = 0.035;
    const from = angle + gap / 2;
    const to = angle + sweep - gap / 2;
    if (to > from) {
      const x1 = centre + radius * Math.cos(from);
      const y1 = centre + radius * Math.sin(from);
      const x2 = centre + radius * Math.cos(to);
      const y2 = centre + radius * Math.sin(to);
      const large = to - from > Math.PI ? 1 : 0;
      arcs += `<path d="M${x1.toFixed(2)} ${y1.toFixed(2)} A${radius} ${radius} 0 ${large} 1 ${x2.toFixed(2)} ${y2.toFixed(2)}"
               fill="none" stroke="${colours[index] || "#8a8985"}" stroke-width="${thickness}"
               stroke-linecap="butt"/>`;
    }
    angle += sweep;
  });

  const legend = labels
    .map(
      (label, index) =>
        `<div><span class="swatch" style="background:${colours[index] || "#8a8985"}"></span>${esc(label)}</div>`
    )
    .join("");

  return `<div class="donut-wrap">
    <svg width="${size}" height="${size}" viewBox="0 0 ${size} ${size}" role="img">${arcs}</svg>
    <div class="donut-legend">${legend}</div>
  </div>`;
}

/** Pull the plottable parts out of a stored plotly spec. */
function readSpec(chart) {
  const traces = (chart.spec && chart.spec.data) || [];
  if (!traces.length) return null;
  if (traces[0].type === "pie") {
    return {
      kind: "pie",
      labels: traces[0].labels || [],
      values: traces[0].values || [],
      colours: (traces[0].marker && traces[0].marker.colors) || [],
    };
  }
  const trace = traces[0];
  const colour =
    (trace.line && trace.line.color) ||
    (trace.marker && trace.marker.color) ||
    "#6c5ce7";
  return {
    kind: "xy",
    x: (trace.x || []).map(String),
    y: trace.y || [],
    colour: typeof colour === "string" ? colour : "#6c5ce7",
    name: trace.name || "",
  };
}

function chartPanel(chart, fallbackTitle) {
  if (!chart) {
    return `<div class="chart-panel">
      <div class="panel-title">${esc(fallbackTitle)}</div>
      <div class="plot-blank">No chart here — the agent judged a figure would not add to the
        numbers. The values are under View SQL &amp; Data.</div></div>`;
  }
  const read = readSpec(chart);
  const title = chart.title || fallbackTitle;
  const head = `<div class="panel-head"><div class="panel-title">${esc(title)}
      <span class="info" title="Built by the agent from query ${esc(chart.query_id)}">
        <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor"
             stroke-width="2"><circle cx="12" cy="12" r="9"/><path d="M12 11v5M12 7.6v.1"/></svg>
      </span></div></div>`;

  if (!read) {
    return `<div class="chart-panel">${head}<div class="plot-blank">This chart has no data.</div></div>`;
  }
  if (read.kind === "pie") {
    return `<div class="chart-panel">${head}${donut(read.labels, read.values, read.colours)}</div>`;
  }
  const legend = read.name
    ? `<div class="legend"><span class="swatch" style="background:${read.colour}"></span>${esc(read.name)}</div>`
    : "";
  return `<div class="chart-panel">${head}${legend}${areaChart(read.x, read.y, read.colour)}</div>`;
}

/* ------------------------------------------------------------- rendering */

function renderSuggestions() {
  const perPage = 4;
  const pages = Math.ceil(QUESTIONS.length / perPage);
  state.suggestPage = ((state.suggestPage % pages) + pages) % pages;
  const slice = QUESTIONS.slice(state.suggestPage * perPage, state.suggestPage * perPage + perPage);

  $("suggest-grid").innerHTML = slice
    .map(([question, tint]) => {
      const [wash, ink] = TILES[tint];
      return `<button class="suggest" data-q="${esc(question)}">
        <span class="tile" style="background:${wash};color:${ink}">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor"
               stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <rect x="3.5" y="3.5" width="17" height="17" rx="3"/><path d="M8 15.5v-3M12 15.5v-6M16 15.5v-4.5"/>
          </svg></span>
        <span class="q">${esc(question)}</span></button>`;
    })
    .join("");

  $("suggest-grid")
    .querySelectorAll(".suggest")
    .forEach((button) =>
      button.addEventListener("click", () => {
        $("question").value = button.dataset.q;
        updateCounter();
        $("question").focus();
      })
    );
}

async function renderRecent() {
  let runs = [];
  try {
    runs = await get("/v1/runs?limit=4");
  } catch (_) {
    return;
  }
  $("recent-grid").innerHTML = runs
    .map(
      (run) => `<button class="recent ${run.run_id === state.runId ? "current" : ""}"
        data-run="${run.run_id}">
        <span class="q">${esc(run.question)}</span>
        <span class="meta"><span class="when">${ago(run.created_at)}</span>${chip(run.status)}</span>
      </button>`
    )
    .join("");
  $("recent-grid")
    .querySelectorAll(".recent")
    .forEach((card) => card.addEventListener("click", () => openRun(card.dataset.run)));
}

function takeaways(run) {
  const findings = run.findings || [];
  if (!findings.length) {
    return `<div class="takeaway"><span class="tile" style="background:var(--accent-soft);color:var(--accent)">·</span>
      <span class="t">No separate findings were recorded for this question.</span></div>`;
  }
  const tints = ["green", "violet", "amber", "blue"];
  return findings
    .slice(0, 5)
    .map((finding, index) => {
      const material = finding.material;
      const [wash, ink] = TILES[tints[index % tints.length]];
      const tested = (finding.hypotheses || []).filter((h) =>
        ["supported", "refuted"].includes(h.status)
      ).length;
      // A glyph and a border tone, never colour alone.
      const glyph = material
        ? `<path d="M12 8v5M12 16.4v.1"/><circle cx="12" cy="12" r="9"/>`
        : `<path d="M20 6 9 17l-5-5"/>`;
      const note = material
        ? ` <span class="sub">· ${tested} explanation${tested === 1 ? "" : "s"} tested</span>`
        : "";
      return `<div class="takeaway ${material ? "material" : "good"}">
        <span class="tile" style="background:${wash};color:${ink}">
          <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor"
               stroke-width="2" stroke-linecap="round" stroke-linejoin="round">${glyph}</svg></span>
        <span class="t">${esc(finding.statement)}${note}</span></div>`;
    })
    .join("");
}

function approvalBanner(run) {
  return (run.pending_approvals || [])
    .map((approval) => {
      const payload = approval.payload || {};
      return `<div class="banner" data-approval="${approval.approval_id}">
        <div class="kind">${esc(approval.kind.replace(/_/g, " "))}</div>
        <div class="why">${esc(approval.reason)}</div>
        ${payload.sql ? `<pre class="sql">${esc(payload.sql)}</pre>` : ""}
        ${
          payload.estimated_cost
            ? `<div class="kv">Estimated plan cost: ${Math.round(payload.estimated_cost).toLocaleString()}</div>`
            : ""
        }
        <input class="field" style="margin-top:11px" placeholder="Reason (recorded with your decision)"
               data-reason="${approval.approval_id}">
        <div class="banner-actions">
          <button class="btn btn-primary btn-sm" data-decide="approve">Approve</button>
          <button class="btn btn-sm" data-decide="reject">Reject</button>
        </div></div>`;
    })
    .join("");
}

async function renderResult() {
  const slot = $("result-slot");
  if (!state.runId) {
    slot.innerHTML = "";
    $("details").classList.remove("open");
    return;
  }

  let run;
  let trace;
  try {
    [run, trace] = await Promise.all([
      get(`/v1/runs/${state.runId}`),
      get(`/v1/runs/${state.runId}/trace`),
    ]);
  } catch (error) {
    slot.innerHTML = `<div class="card result" style="padding:20px 22px">
      <div class="kv">Could not load that run: ${esc(error.message)}</div></div>`;
    return;
  }

  const charts = run.charts || [];
  const executed = (trace.summary || {}).queries_executed || 0;
  const blocked = (trace.summary || {}).queries_rejected || 0;
  const live = LIVE.has(run.status);
  const answered = Boolean(run.answer);

  let foot = `${executed} quer${executed === 1 ? "y" : "ies"} executed`;
  if (blocked) foot += ` · ${blocked} blocked by the guard`;

  slot.innerHTML = `<div class="card result">
    <div class="result-head">
      <div>
        <div class="result-title">${esc(run.question)}</div>
        <div class="result-meta">${chip(run.status)}<span class="when">${ago(run.created_at)}</span>
          ${live ? '<span class="spinner"></span>' : ""}</div>
      </div>
      ${
        answered
          ? `<div class="result-actions">
              <button class="btn btn-sm" id="share-btn">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                     stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                  <circle cx="18" cy="5.5" r="2.6"/><circle cx="6" cy="12" r="2.6"/><circle cx="18" cy="18.5" r="2.6"/>
                  <path d="M8.3 10.8 15.7 6.8M8.3 13.2l7.4 4"/></svg>Share</button>
              <button class="btn btn-sm" id="save-btn">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="${
                  state.bookmarks.has(run.run_id) ? "currentColor" : "none"
                }" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                  <path d="M6 3.8h12a1 1 0 0 1 1 1V21l-7-4.2L5 21V4.8a1 1 0 0 1 1-1Z"/></svg>${
                    state.bookmarks.has(run.run_id) ? "Saved" : "Save"
                  }</button>
              <button class="btn btn-primary btn-sm" id="details-btn">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                     stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                  <path d="M4 19V5M4 19h16"/><path d="M8 15V9M13 15V6M18 15v-4"/></svg>View Details</button>
            </div>`
          : ""
      }
    </div>
    ${approvalBanner(run)}
    ${
      run.status === "clarifying"
        ? `<div class="banner"><div class="kind">The agent has a question</div>
            <div class="why">${esc(
              (run.clarifications || []).filter((c) => !c.answer).map((c) => c.question).join(" ") ||
                "It stopped rather than guessing what you meant."
            )}</div>
            <input class="field" style="margin-top:11px" id="clarify-input" placeholder="Your answer">
            <div class="banner-actions"><button class="btn btn-primary btn-sm" id="clarify-send">Send</button></div>
          </div>`
        : ""
    }
    ${
      answered
        ? `<div class="result-body">
            <div><div class="panel-title">Key Takeaways</div>${takeaways(run)}</div>
            ${chartPanel(charts[0], "Trend")}
            ${chartPanel(charts[1], "Breakdown")}
          </div>
          <div class="result-foot">
            <div class="foot-left"><span class="foot-label">Evidence &amp; Queries</span>
              <span class="foot-count">${foot}</span></div>
            <button class="btn btn-sm" id="sql-btn">
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                   stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <ellipse cx="12" cy="6" rx="8" ry="3"/><path d="M4 6v12c0 1.7 3.6 3 8 3s8-1.3 8-3V6"/>
                <path d="M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3"/></svg>View SQL &amp; Data</button>
          </div>`
        : `<div style="padding:4px 22px 20px">
            ${
              live
                ? '<div class="kv">Working — this takes a few minutes. The steps appear as they finish.</div>'
                : run.error
                ? `<div class="kv" style="color:var(--bad)">${esc(run.error.type)}: ${esc(run.error.message)}</div>`
                : ""
            }
            ${timeline(trace.steps || [])}</div>`
    }
  </div>`;

  wireResult(run, trace);
  renderDetails(run, trace);

  clearTimeout(state.timer);
  if (live) state.timer = setTimeout(renderResult, POLL_MS);
}

function timeline(steps) {
  if (!steps.length) return '<div class="kv">Nothing has run yet.</div>';
  return `<div class="timeline">${steps
    .map((step) => {
      const cls =
        step.status === "error" ? "error" : ["started", "paused"].includes(step.status) ? "active" : "";
      const bits = [step.summary || "", step.effort || "", step.duration_ms ? `${step.duration_ms} ms` : ""]
        .filter(Boolean)
        .join(" · ");
      return `<div class="tl-row ${cls}"><div class="tl-node">${esc(step.node)}</div>
        <div class="tl-meta">${esc(bits)}</div></div>`;
    })
    .join("")}</div>`;
}

function renderDetails(run, trace) {
  const answer = run.answer;
  const box = $("details");
  if (!answer) {
    box.innerHTML = "";
    return;
  }
  const evidence = (answer.evidence || [])
    .map(
      (item) => `<div class="q-row">
        <div class="purpose">${esc(item.purpose)}</div>
        <div class="idline">${esc(item.query_id)}${
        item.row_count != null ? ` · ${item.row_count} rows` : ""
      }</div>
        <pre class="sql">${esc(item.sql)}</pre></div>`
    )
    .join("");

  const considered = (trace.queries || [])
    .map(
      (query) => `<div class="q-row">
        <div class="purpose">${esc(query.purpose)}</div>
        <div style="margin:5px 0">${chip(query.verdict)}${
        query.executed ? `<span class="chip" style="margin-left:5px">${query.row_count} rows</span>` : ""
      }</div>
        ${
          (query.reasons || []).length
            ? `<ul class="plain">${query.reasons.map((r) => `<li>${esc(r)}</li>`).join("")}</ul>`
            : ""
        }
        <pre class="sql">${esc(query.rewritten_sql || query.sql)}</pre></div>`
    )
    .join("");

  const hypotheses = (run.findings || [])
    .map(
      (finding) => `<div class="q-row"><div class="purpose">${esc(finding.statement)}</div>
        ${(finding.hypotheses || [])
          .map(
            (h) => `<div class="hyp"><div class="claim">${esc(h.statement)}</div>
              <div style="margin:5px 0">${chip(h.status)}</div>
              ${h.reasoning ? `<div class="why">${esc(h.reasoning)}</div>` : ""}</div>`
          )
          .join("")}</div>`
    )
    .join("");

  box.innerHTML = `
    <h3>The answer</h3>
    <div class="conclusion">${esc(answer.conclusion)}</div>
    <div style="margin-top:11px">${confidenceChip(answer.confidence)}
      <span class="chip" style="margin-left:5px">${(answer.evidence || []).length} queries cited</span></div>
    ${
      (answer.refuted || []).length
        ? `<div class="sub-head">Ruled out</div><ul class="plain">${answer.refuted
            .map((r) => `<li>${esc(r)}</li>`)
            .join("")}</ul>`
        : ""
    }
    ${
      (answer.caveats || []).length
        ? `<div class="sub-head">Caveats</div><ul class="plain">${answer.caveats
            .map((c) => `<li>${esc(c)}</li>`)
            .join("")}</ul>`
        : ""
    }
    ${hypotheses ? `<div class="sub-head">Findings and the explanations tested</div>${hypotheses}` : ""}
    <div class="sub-head">The SQL behind each cited number</div>${evidence}
    <div class="sub-head">Every query considered, including the ones that never ran</div>${considered}
    <div class="sub-head">What it did, in order</div>${timeline(trace.steps || [])}`;
}

function wireResult(run, trace) {
  const details = $("details");
  const share = $("share-btn");
  if (share)
    share.addEventListener("click", async () => {
      const link = `${location.origin}/v1/runs/${run.run_id}`;
      try {
        await navigator.clipboard.writeText(link);
        toast("Link to this run copied. Nothing was published.");
      } catch (_) {
        toast(link);
      }
    });

  const save = $("save-btn");
  if (save)
    save.addEventListener("click", () => {
      if (state.bookmarks.has(run.run_id)) state.bookmarks.delete(run.run_id);
      else state.bookmarks.add(run.run_id);
      localStorage.setItem("bookmarks", JSON.stringify([...state.bookmarks]));
      renderResult();
    });

  const open = () => {
    details.classList.toggle("open");
    if (details.classList.contains("open")) details.scrollIntoView({ behavior: "smooth", block: "start" });
  };
  if ($("details-btn")) $("details-btn").addEventListener("click", open);
  if ($("sql-btn")) $("sql-btn").addEventListener("click", open);

  document.querySelectorAll("[data-approval]").forEach((banner) => {
    const id = banner.dataset.approval;
    banner.querySelectorAll("[data-decide]").forEach((button) =>
      button.addEventListener("click", async () => {
        const reason = banner.querySelector(`[data-reason="${id}"]`).value.trim();
        try {
          await post(`/v1/runs/${run.run_id}/approvals/${id}/${button.dataset.decide}`, {
            decided_by: state.who,
            reason: reason || null,
          });
          toast(`Recorded as ${button.dataset.decide === "approve" ? "approved" : "rejected"}.`);
          setTimeout(renderResult, 700);
        } catch (error) {
          toast(error.message);
        }
      })
    );
  });

  const clarify = $("clarify-send");
  if (clarify)
    clarify.addEventListener("click", async () => {
      const value = $("clarify-input").value.trim();
      if (!value) return;
      try {
        await post(`/v1/runs/${run.run_id}/answer`, { answer: value });
        setTimeout(renderResult, 700);
      } catch (error) {
        toast(error.message);
      }
    });
}

/* --------------------------------------------------------------- actions */

async function ask() {
  const question = $("question").value.trim();
  if (!question) {
    toast("Type a question first.");
    return;
  }
  $("send").disabled = true;
  try {
    const started = await post("/v1/questions", { question, requested_by: state.who });
    state.runId = started.run_id;
    await renderRecent();
    await renderResult();
    $("result-slot").scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (error) {
    toast(`The API refused this: ${error.message}`);
  } finally {
    $("send").disabled = false;
  }
}

async function openRun(runId) {
  state.runId = runId;
  go("ask");
  await renderRecent();
  await renderResult();
  $("result-slot").scrollIntoView({ behavior: "smooth", block: "start" });
}

function updateCounter() {
  $("counter").textContent = `${$("question").value.length} / 500`;
}

/* ----------------------------------------------------------------- pages */

const TITLES = {
  ask: ["Hello, Analyst 👋", "Ask a business question. Get data-backed insights."],
  dashboard: ["Dashboard", "Every run this instance has done, and what it cost."],
  saved: ["Saved Analyses", "Every question asked, and the answer it reached."],
  metrics: ["Metrics Catalog", "The approved definitions. The agent may use these, not invent one."],
  schema: ["Data Explorer", "What is queryable, and what the column policy protects."],
  reports: ["Reports", "Finished answers, with the evidence each one rests on."],
  settings: ["Settings", "What this interface is talking to, and what it is allowed to see."],
};

function go(page) {
  state.page = page;
  document.querySelectorAll(".nav-item").forEach((item) =>
    item.classList.toggle("active", item.dataset.page === page)
  );
  document.querySelectorAll(".page").forEach((section) =>
    section.classList.toggle("active", section.id === `page-${page}`)
  );
  const [title, sub] = TITLES[page] || TITLES.ask;
  $("page-title").textContent = title;
  $("page-sub").textContent = sub;
  if (page !== "ask") clearTimeout(state.timer);
  const loader = PAGE_LOADERS[page];
  if (loader) loader();
}

const PAGE_LOADERS = {
  async dashboard() {
    const box = $("page-dashboard");
    const runs = await get("/v1/runs?limit=50").catch(() => []);
    if (!runs.length) {
      box.innerHTML = `<div class="card mcard" style="margin-top:22px">No runs yet. Ask a question and this fills in.</div>`;
      return;
    }
    const counts = {};
    runs.forEach((r) => (counts[r.status] = (counts[r.status] || 0) + 1));
    const tokens = runs.reduce((a, r) => a + (r.tokens_in || 0) + (r.tokens_out || 0), 0);
    const durations = runs.filter((r) => r.duration_ms).map((r) => r.duration_ms).sort((a, b) => a - b);
    const median = durations.length ? Math.round(durations[Math.floor(durations.length / 2)] / 1000) : null;

    const stat = (label, value) => `<div class="stat"><div class="value">${value}</div><div class="label">${label}</div></div>`;
    const bars = Object.entries(counts)
      .sort((a, b) => b[1] - a[1])
      .map(([key, count]) => {
        const [label, icon, colour] = STATUS[key] || [key, "·", "#6b6a66"];
        const width = (count / Math.max(...Object.values(counts))) * 100;
        return `<div style="margin-bottom:11px">
          <div style="display:flex;justify-content:space-between;font-size:12.5px;margin-bottom:5px">
            <span>${icon} ${esc(label)}</span><span style="color:var(--text-muted)">${count}</span></div>
          <div style="height:9px;background:var(--sunken);border-radius:5px;overflow:hidden">
            <div style="width:${width}%;height:100%;background:${colour};border-radius:5px"></div></div></div>`;
      })
      .join("");

    box.innerHTML = `<div class="card stat-strip">
        ${stat("Runs", runs.length)}${stat("Completed", counts.completed || 0)}
        ${stat("Awaiting a decision", counts.awaiting_approval || 0)}
        ${stat("Asked you something", counts.clarifying || 0)}
        ${stat("Tokens", tokens.toLocaleString())}${stat("Median run", median ? median + "s" : "—")}
      </div>
      <div class="grid-2">
        <div class="card mcard"><div class="panel-title">Runs by outcome</div>${bars}</div>
        <div class="card mcard"><div class="panel-title">Latest</div>
          ${runs
            .slice(0, 6)
            .map(
              (r) => `<div class="takeaway"><span class="t">${esc(r.question)}<br>
                <span class="sub" style="font-size:11.5px">${ago(r.created_at)}</span></span></div>`
            )
            .join("")}</div>
      </div>`;
  },

  async saved() {
    const box = $("page-saved");
    const runs = await get("/v1/runs?limit=40").catch(() => []);
    box.innerHTML = `<input class="field" id="saved-filter" placeholder="Search the questions…"
        style="margin:22px 0 14px;max-width:420px">
      <div id="saved-list"></div>`;
    const draw = (filter) => {
      const shown = runs.filter((r) => r.question.toLowerCase().includes(filter.toLowerCase()));
      $("saved-list").innerHTML = shown.length
        ? shown
            .map(
              (r) => `<button class="card list-row" data-run="${r.run_id}">
                <span class="q">${esc(r.question)}</span>
                <span style="display:flex;gap:12px;align-items:center">
                  <span class="when">${ago(r.created_at)}</span>${chip(r.status)}</span></button>`
            )
            .join("")
        : `<div class="card mcard">Nothing matches.</div>`;
      $("saved-list")
        .querySelectorAll("[data-run]")
        .forEach((row) => row.addEventListener("click", () => openRun(row.dataset.run)));
    };
    draw("");
    $("saved-filter").addEventListener("input", (event) => draw(event.target.value));
  },

  async metrics() {
    const box = $("page-metrics");
    const metrics = await get("/v1/metrics").catch(() => []);
    box.innerHTML = `<div class="kv" style="margin-top:20px">${metrics.length} approved metrics.
        Ask for one by name and the registry renders the statement — for these, no SQL written by
        the model reaches the warehouse.</div>
      <div class="grid-3">${metrics
        .map(
          (metric) => `<div class="card mcard">
            <h4>${esc(metric.title)}</h4>
            <div class="mono">${esc(metric.name)} · ${esc(metric.definition_version)}</div>
            <div style="margin:9px 0">
              <span class="chip">${esc(metric.unit)}</span>
              <span class="chip" style="margin-left:4px">per ${esc(metric.grain)}</span>
              <span class="chip" style="margin-left:4px">${esc(metric.shape)}</span></div>
            <div class="kv">By: ${esc((metric.dimensions || []).join(", ") || "—")}</div>
            ${(metric.caveats || [])
              .slice(0, 2)
              .map((c) => `<div class="mono" style="margin-top:5px">— ${esc(c)}</div>`)
              .join("")}</div>`
        )
        .join("")}</div>`;
  },

  async schema() {
    const box = $("page-schema");
    const catalogue = await get("/v1/schema").catch(() => ({ objects: [], schemas: [] }));
    const objects = catalogue.objects || [];
    const restricted = objects.reduce(
      (a, o) => a + o.columns.filter((c) => c.restricted).length,
      0
    );
    const columns = objects.reduce((a, o) => a + o.columns.length, 0);
    const stat = (label, value) => `<div class="stat"><div class="value">${value}</div><div class="label">${label}</div></div>`;
    box.innerHTML = `<div class="card stat-strip">
        ${stat("Schemas", (catalogue.schemas || []).length)}${stat("Tables", objects.length)}
        ${stat("Columns", columns)}${stat("Restricted columns", restricted)}</div>
      <div class="kv" style="margin:14px 0 18px">A restricted column is not hidden from the agent —
        it is listed, never sampled, and projecting one requires a human decision.</div>
      ${objects
        .map((object) => {
          const locked = object.columns.filter((c) => c.restricted).length;
          return `<details class="table-block"><summary>${esc(object.name)} · ${
            object.columns.length
          } columns${locked ? ` · 🔒 ${locked} restricted` : ""}</summary>
            <div class="col-list">${object.columns
              .map(
                (c) =>
                  `<div class="${c.restricted ? "locked" : ""}">${c.restricted ? "🔒 " : ""}${esc(c.name)}</div>`
              )
              .join("")}</div></details>`;
        })
        .join("")}`;
  },

  async reports() {
    const box = $("page-reports");
    const runs = (await get("/v1/runs?limit=40").catch(() => [])).filter(
      (r) => r.status === "completed"
    );
    if (!runs.length) {
      box.innerHTML = `<div class="card mcard" style="margin-top:22px">No completed analyses yet.</div>`;
      return;
    }
    box.innerHTML = `<div class="kv" style="margin:20px 0 12px">${runs.length} completed.
        Read-only: publishing a report is approval point 4, and that gate has no implementation
        behind it — a button that quietly skipped it would be worse than none.</div>
      <select class="field" id="report-pick" style="max-width:560px">${runs
        .map((r) => `<option value="${r.run_id}">${esc(r.question)} · ${ago(r.created_at)}</option>`)
        .join("")}</select>
      <div id="report-body" style="margin-top:18px"></div>`;

    const draw = async (runId) => {
      const [run, trace] = await Promise.all([
        get(`/v1/runs/${runId}`),
        get(`/v1/runs/${runId}/trace`),
      ]);
      const charts = run.charts || [];
      $("report-body").innerHTML = `<div class="card" style="padding:20px 22px">
        <div class="result-title">${esc(run.question)}</div>
        <div class="result-meta">${chip(run.status)}<span class="when">${ago(run.created_at)}</span></div>
        <div class="result-body" style="padding:16px 0 0">
          <div><div class="panel-title">Key Takeaways</div>${takeaways(run)}</div>
          ${chartPanel(charts[0], "Trend")}${chartPanel(charts[1], "Breakdown")}</div>
        ${
          run.answer
            ? `<div class="sub-head">Conclusion</div>
               <div class="conclusion">${esc(run.answer.conclusion)}</div>
               <div style="margin-top:10px">${confidenceChip(run.answer.confidence)}</div>
               <div class="sub-head">Evidence</div>
               ${(run.answer.evidence || [])
                 .map(
                   (e) => `<div class="q-row"><div class="purpose">${esc(e.purpose)}</div>
                     <div class="idline">${esc(e.query_id)} · ${e.row_count} rows</div>
                     <pre class="sql">${esc(e.sql)}</pre></div>`
                 )
                 .join("")}`
            : ""
        }</div>`;
    };
    draw(runs[0].run_id);
    $("report-pick").addEventListener("change", (event) => draw(event.target.value));
  },

  async settings() {
    const box = $("page-settings");
    const [healthy, metrics, schema] = await Promise.all([
      get("/readyz").then(() => true).catch(() => false),
      get("/v1/metrics").catch(() => []),
      get("/v1/schema").catch(() => ({ objects: [] })),
    ]);
    box.innerHTML = `<div class="grid-2">
      <div class="card mcard"><div class="panel-title">Connection</div>
        <div style="margin-bottom:9px">${chip(healthy ? "completed" : "failed")}</div>
        <div class="kv">API: <span class="mono">${esc(location.origin)}</span></div>
        <div class="kv">Approved metrics loaded: ${metrics.length}</div>
        <div class="kv">Queryable tables: ${(schema.objects || []).length}</div></div>
      <div class="card mcard"><div class="panel-title">Signed in as</div>
        <input class="field" id="who-input" value="${esc(state.who)}">
        <div class="kv" style="margin-top:8px">Recorded with any approval decision you make.</div></div>
      <div class="card mcard"><div class="panel-title">Appearance</div>
        <div class="kv">Dark mode is a selected palette, not an inverted one: the chart series have
          their own steps, checked against the dark surface.</div>
        <button class="btn btn-sm" id="theme-btn-2" style="margin-top:10px">Switch theme</button></div>
      <div class="card mcard"><div class="panel-title">This interface never touches the database</div>
        <div class="kv">Everything above arrived through the API, so the interface cannot display
          something the API would not.</div></div>
    </div>`;
    $("who-input").addEventListener("change", (event) => {
      state.who = event.target.value.trim() || "analyst@example.com";
      localStorage.setItem("who", state.who);
      $("user-who").textContent = state.who;
      $("avatar").textContent = state.who.slice(0, 1).toUpperCase();
      toast("Saved. Decisions will be recorded against this name.");
    });
    $("theme-btn-2").addEventListener("click", toggleTheme);
  },
};

/* ---------------------------------------------------------------- health */

async function refreshHealth() {
  try {
    await get("/readyz");
    $("status-dot").style.background = "var(--good)";
    $("status-label").style.color = "var(--good)";
    $("status-label").textContent = "API Reachable";
    $("status-sub").textContent = "All systems operational";
  } catch (_) {
    $("status-dot").style.background = "var(--bad)";
    $("status-label").style.color = "var(--bad)";
    $("status-label").textContent = "API Unreachable";
    $("status-sub").textContent = "Start it with make api";
  }
}

function toggleTheme() {
  const next = document.body.dataset.theme === "dark" ? "light" : "dark";
  document.body.dataset.theme = next;
  localStorage.setItem("theme", next);
  if (state.runId) renderResult();
}

/* ------------------------------------------------------------------ boot */

function boot() {
  document.body.dataset.theme = localStorage.getItem("theme") || "light";
  $("user-who").textContent = state.who;
  $("avatar").textContent = state.who.slice(0, 1).toUpperCase();

  $("question").addEventListener("input", updateCounter);
  $("question").addEventListener("keydown", (event) => {
    if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) ask();
  });
  $("send").addEventListener("click", ask);
  $("suggest-next").addEventListener("click", () => {
    state.suggestPage += 1;
    renderSuggestions();
  });
  $("theme-btn").addEventListener("click", toggleTheme);
  $("new-btn").addEventListener("click", () => {
    state.runId = null;
    $("question").value = "";
    updateCounter();
    go("ask");
    renderResult();
    renderRecent();
    $("question").focus();
  });
  $("collapse").addEventListener("click", () => $("sidebar").classList.toggle("collapsed"));
  $("user-card").addEventListener("click", () => go("settings"));

  document.querySelectorAll("[data-page]").forEach((element) =>
    element.addEventListener("click", (event) => {
      event.preventDefault();
      go(element.dataset.page);
    })
  );

  renderSuggestions();
  refreshHealth();
  setInterval(refreshHealth, 20000);

  // With nothing selected the latest run is shown: an analyst arriving here is usually coming
  // back to the answer they just asked for.
  get("/v1/runs?limit=1")
    .then((runs) => {
      if (runs.length) state.runId = runs[0].run_id;
      return renderRecent();
    })
    .then(renderResult)
    .catch(() => {});
}

boot();
