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


/* ------------------------------------------------------- states & confidence */

/**
 * An empty state that says what to do next.
 *
 * "No data" is a dead end; a sentence naming the action is not. Every list on this page routes
 * through here so an empty page never looks like a broken one.
 */
function empty(title, hint, action) {
  return `<div class="card empty">
    <div class="empty-mark">
      <svg width="34" height="34" viewBox="0 0 24 24" fill="none" stroke="currentColor"
           stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">
        <path d="M4 19V5M4 19h16"/><path d="M8 15V10M13 15V7M18 15v-3"/></svg></div>
    <div class="empty-title">${esc(title)}</div>
    <div class="empty-hint">${esc(hint)}</div>
    ${action ? `<button class="btn btn-sm" data-page="${action.page}">${esc(action.label)}</button>` : ""}
  </div>`;
}

/** Grey blocks in the shape of the content, so a slow page does not look like a stuck one. */
function skeleton(rows = 3) {
  return `<div class="card mcard">${Array.from({ length: rows })
    .map((_, i) => `<div class="skel" style="width:${92 - i * 14}%"></div>`)
    .join("")}</div>`;
}

function loading(target, rows = 3) {
  target.innerHTML = skeleton(rows);
}

/**
 * The confidence score, with the factors that produced it.
 *
 * The ring is the headline and the factors are the point: a percentage on its own is a claim,
 * and a percentage next to the four things it was computed from is something a reader can
 * disagree with. Factors that do not apply to this run are dropped rather than shown as
 * failures — a factual question has no hypotheses to test.
 */
function confidenceBlock(detail, compact) {
  if (!detail) return "";
  const score = Number(detail.score || 0);
  const tone = detail.band === "high" ? "var(--good)" : detail.band === "medium" ? "var(--warn)" : "var(--bad)";
  const circumference = 2 * Math.PI * 26;
  const filled = (score / 100) * circumference;

  const ring = `<svg width="72" height="72" viewBox="0 0 72 72" role="img"
      aria-label="Confidence ${score} percent">
    <circle cx="36" cy="36" r="26" fill="none" stroke="var(--border)" stroke-width="7"/>
    <circle cx="36" cy="36" r="26" fill="none" stroke="${tone}" stroke-width="7"
            stroke-linecap="round" stroke-dasharray="${filled.toFixed(1)} ${circumference.toFixed(1)}"
            transform="rotate(-90 36 36)"/>
    <text x="36" y="40" text-anchor="middle" font-size="16" font-weight="650"
          fill="var(--text)">${score}%</text></svg>`;

  const factors = (detail.factors || [])
    .filter((factor) => factor.weight > 0)
    .map(
      (factor) => `<div class="factor ${factor.passed ? "yes" : "no"}">
        <span class="mark">${factor.passed ? "✓" : "•"}</span>
        <span class="what">${esc(factor.label)}</span>
        <span class="weight">${Math.round(factor.earned)}/${Math.round(factor.weight)}</span>
      </div>`
    )
    .join("");

  const capped = detail.capped_by
    ? `<div class="capped">Held at ${esc(detail.capped_by)} — a stated band is a ceiling, never a
        floor.</div>`
    : "";

  return `<div class="confidence ${compact ? "compact" : ""}">
    <div class="conf-ring">${ring}<div class="conf-band" style="color:${tone}">${esc(detail.band || "")}</div></div>
    <div class="conf-factors">${factors || '<div class="empty-hint">No factors recorded.</div>'}${capped}</div>
  </div>`;
}

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
  const download = chart.chart_id
    ? `<a class="panel-download" href="/v1/charts/${chart.chart_id}/export.png"
         title="Download this chart as a PNG">PNG</a>`
    : "";
  if (read.kind === "pie") {
    return `<div class="chart-panel">${head}${download}
      ${donut(read.labels, read.values, read.colours)}</div>`;
  }
  const legend = read.name
    ? `<div class="legend"><span class="swatch" style="background:${read.colour}"></span>${esc(read.name)}</div>`
    : "";
  return `<div class="chart-panel">${head}${download}${legend}
    ${areaChart(read.x, read.y, read.colour)}</div>`;
}

/* ------------------------------------------------------------ the report */
/*
 * Eight sections, in the order a reader needs them: what happened, what the headlines are, what
 * was looked at, which explanations were tested, what the charts show, where the numbers came
 * from, how confident this is, and what to do next.
 *
 * The ordering is the whole design. A chat reply puts the reasoning first and the answer
 * somewhere in the middle; a report puts the answer in the first line and everything that
 * supports it underneath, so a reader can stop as soon as they have what they came for.
 */

const SEVERITY = {
  high: ["High", "var(--bad)"],
  medium: ["Medium", "var(--warn)"],
  low: ["Low", "var(--good)"],
};

const PRIORITY = {
  high: ["1st", "var(--bad)"],
  medium: ["2nd", "var(--warn)"],
  low: ["3rd", "var(--good)"],
};

/** 1 · Executive summary: the conclusion, in the first line of the page. */
function executiveSummary(run) {
  const answer = run.answer || {};
  const detail = answer.confidence_detail || {};
  const tone =
    detail.band === "high" ? "var(--good)" : detail.band === "medium" ? "var(--warn)" : "var(--bad)";
  return `<section class="report-section exec">
    <div class="exec-mark">
      <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor"
           stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <path d="M12 2.8l1.9 5.1 5.1 1.9-5.1 1.9L12 16.8l-1.9-5.1L5 9.8l5.1-1.9L12 2.8Z"/></svg>
    </div>
    <div class="exec-body">
      <div class="section-label">Executive summary</div>
      <p class="exec-text">${esc(answer.conclusion)}</p>
      <div class="exec-meta">
        <span class="chip" style="color:${tone};border-color:${tone}55;background:${tone}12">
          ${detail.score ?? 0}% confidence</span>
        ${chip(run.status)}
        <span class="when">${ago(run.created_at)}</span>
      </div>
    </div>
  </section>`;
}

/** 2 · Key findings, as cards: title, impact, severity. */
function keyFindings(run) {
  const findings = (run.answer && run.answer.key_findings) || [];
  if (!findings.length) {
    // Falls back to the investigation's own findings rather than inventing headlines. A card with
    // a title the analysis never produced would be the one piece of this page that is not evidence.
    const raised = run.findings || [];
    if (!raised.length) return "";
    return `<section class="report-section">
      <div class="section-head-row"><div class="section-label">Key findings</div>
        <span class="section-note">from the investigation; this run produced no headline
          summary</span></div>
      <div class="finding-grid">${raised
        .slice(0, 4)
        .map(
          (finding, index) => `<article class="finding-card ${
            finding.material ? "sev-high" : "sev-low"
          }">
            <div class="finding-index">Finding ${index + 1}</div>
            <h3 class="finding-title">${esc(finding.statement)}</h3>
            ${
              finding.material
                ? '<div class="finding-sev" style="color:var(--warn)">Needed explaining</div>'
                : ""
            }
          </article>`
        )
        .join("")}</div></section>`;
  }

  return `<section class="report-section">
    <div class="section-head-row"><div class="section-label">Key findings</div>
      <span class="section-note">${findings.length} of them, most severe first</span></div>
    <div class="finding-grid">${findings
      .map((finding, index) => {
        const [label, colour] = SEVERITY[finding.severity] || SEVERITY.medium;
        return `<article class="finding-card sev-${finding.severity}">
          <div class="finding-index">Finding ${index + 1}</div>
          <h3 class="finding-title">${esc(finding.title)}</h3>
          <div class="finding-impact">${esc(finding.impact)}</div>
          <div class="finding-foot">
            <span class="finding-sev" style="color:${colour}">${label} severity</span>
            ${
              (finding.evidence_query_ids || []).length
                ? `<button class="link-btn" data-jump="${finding.evidence_query_ids[0]}">
                     evidence →</button>`
                : ""
            }
          </div>
        </article>`;
      })
      .join("")}</div></section>`;
}

/** 3 · Investigation process: what was looked at, read off the trace. */
function investigationProcess(run) {
  const investigation = run.investigation;
  if (!investigation) return "";
  const list = (label, items, empty) => `<div class="proc-col">
    <div class="proc-label">${esc(label)}</div>
    ${
      items.length
        ? `<ul class="proc-list">${items
            .map((item) => `<li><span class="tick">✓</span>${esc(item)}</li>`)
            .join("")}</ul>`
        : `<div class="proc-empty">${esc(empty)}</div>`
    }</div>`;

  return `<section class="report-section">
    <div class="section-head-row"><div class="section-label">Investigation process</div>
      <span class="section-note">read off the audit trail, not described by the model</span></div>
    <div class="proc-grid">
      ${list("Metrics checked", investigation.metrics_checked, "No approved metric was needed")}
      ${list("Tables analysed", investigation.tables_analyzed, "No table was read")}
      ${list("Questions tested", investigation.questions_tested, "Nothing needed explaining")}
      ${list("Steps taken", investigation.steps, "The run stopped before it started")}
    </div>
    <div class="proc-foot">
      <span>${investigation.queries_executed} quer${
    investigation.queries_executed === 1 ? "y" : "ies"
  } executed</span>
      ${
        investigation.queries_blocked
          ? `<span class="blocked">${investigation.queries_blocked} blocked by the guard</span>`
          : '<span class="ok">nothing blocked</span>'
      }
    </div>
  </section>`;
}

/** 4 · Hypothesis testing: the competing explanations and how each was settled. */
function hypothesisTesting(run) {
  const findings = (run.findings || []).filter((f) => (f.hypotheses || []).length);
  if (!findings.length) return "";

  return `<section class="report-section">
    <div class="section-head-row"><div class="section-label">Hypothesis testing</div>
      <span class="section-note">the agent cannot reach an answer on the first explanation
        alone</span></div>
    ${findings
      .map(
        (finding) => `<div class="hyp-block">
          <div class="hyp-subject">${esc(finding.statement)}</div>
          ${(finding.hypotheses || [])
            .map((hypothesis, index) => {
              const status = hypothesis.status || "proposed";
              const [label, , colour] = [
                (STATUS[status] || ["Proposed", "·", "#6b6a66"])[0],
                null,
                (STATUS[status] || ["", "", "#6b6a66"])[2],
              ];
              return `<article class="hyp-card st-${status}">
                <div class="hyp-head">
                  <span class="hyp-index">Hypothesis ${index + 1}</span>
                  <span class="hyp-status" style="color:${colour}">${esc(label)}</span>
                </div>
                <div class="hyp-claim">${esc(hypothesis.statement)}</div>
                ${
                  hypothesis.reasoning
                    ? `<div class="hyp-why"><span class="hyp-why-label">Evidence</span>
                         ${esc(hypothesis.reasoning)}</div>`
                    : ""
                }
                ${(hypothesis.test_query_ids || [])
                  .map(
                    (queryId) =>
                      `<button class="link-btn" data-rows="${queryId}">
                        view the query and its rows →</button>`
                  )
                  .join("")}
              </article>`;
            })
            .join("")}
        </div>`
      )
      .join("")}
  </section>`;
}

/** 5 · Visual analytics: every chart with its title and what it means. */
function visualAnalytics(run, trace) {
  const charts = run.charts || [];
  if (!charts.length) {
    return `<section class="report-section">
      <div class="section-head-row"><div class="section-label">Visual analytics</div></div>
      <div class="plot-blank">No chart for this answer — the agent judged a figure would not add
        to the numbers. The values are in the evidence below.</div>
    </section>`;
  }
  return `<section class="report-section">
    <div class="section-head-row"><div class="section-label">Visual analytics</div>
      <span class="section-note">each chart names the query it was built from</span></div>
    <div class="chart-grid">${charts
      .map((chart) => {
        const meaning = chartMeaning(chart, trace);
        return `<figure class="chart-figure">
          ${chartPanel(chart, chart.title || "Chart")}
          <figcaption>${esc(meaning)}</figcaption>
        </figure>`;
      })
      .join("")}</div>
  </section>`;
}

/**
 * What a chart is *for*, taken from the purpose of the query behind it.
 *
 * Not generated prose. The purpose was written by the agent when it ran the query and recorded in
 * the audit trail, so it already says what the figure was meant to establish — inventing a
 * caption here would be the page speaking for the analysis.
 */
function chartMeaning(chart, trace) {
  const query = ((trace && trace.queries) || []).find(
    (q) => String(q.query_id) === String(chart.query_id)
  );
  if (query && query.purpose) {
    return `${query.purpose}${query.row_count != null ? ` · ${query.row_count} rows` : ""}`;
  }
  return `From query ${chart.query_id}`;
}

/** 6 · Evidence and traceability, with the SQL and the rows one click away. */
function evidenceSection(run, trace) {
  const answer = run.answer || {};
  const investigation = run.investigation || {};
  const considered = (trace && trace.queries) || [];
  const refused = considered.filter((q) => !q.executed);

  const facts = [
    ["Analysis ID", `RUN-${String(run.run_id).slice(0, 8).toUpperCase()}`],
    ["Queries executed", investigation.queries_executed ?? 0],
    [
      "Metrics used",
      (investigation.metrics_checked || []).join(", ") || "none — ad hoc SQL",
    ],
    [
      "Data sources",
      (investigation.tables_analyzed || [])
        .map((table) => table.split(".").pop())
        .join(", ") || "none",
    ],
  ];

  return `<section class="report-section">
    <div class="section-head-row"><div class="section-label">Evidence &amp; traceability</div>
      <span class="section-note">every number above leads back to one of these</span></div>

    <div class="fact-row">${facts
      .map(
        ([label, value]) => `<div class="fact">
          <div class="fact-label">${esc(label)}</div>
          <div class="fact-value">${esc(value)}</div></div>`
      )
      .join("")}</div>

    ${(answer.evidence || [])
      .map(
        (item) => `<details class="evidence-block">
          <summary>
            <span class="ev-purpose">${esc(item.purpose)}</span>
            <span class="ev-meta">${item.row_count} rows</span>
          </summary>
          <div class="evidence-body">
            <div class="idline">${esc(item.query_id)}</div>
            <pre class="sql">${esc(item.sql)}</pre>
            <button class="btn btn-sm" data-rows="${item.query_id}">Show the rows</button>
            <div class="rows-slot" id="rows-${item.query_id}"></div>
          </div>
        </details>`
      )
      .join("")}

    ${
      refused.length
        ? `<details class="evidence-block refused">
            <summary><span class="ev-purpose">${refused.length} quer${
            refused.length === 1 ? "y" : "ies"
          } that did not run</span>
              <span class="ev-meta">blocked or awaiting a decision</span></summary>
            <div class="evidence-body">${refused
              .map(
                (query) => `<div class="q-row">
                  <div class="purpose">${esc(query.purpose)}</div>
                  <div style="margin:5px 0">${chip(query.verdict)}</div>
                  ${
                    (query.reasons || []).length
                      ? `<ul class="plain">${query.reasons
                          .map((reason) => `<li>${esc(reason)}</li>`)
                          .join("")}</ul>`
                      : ""
                  }
                  <pre class="sql">${esc(query.rewritten_sql || query.sql)}</pre></div>`
              )
              .join("")}</div>
          </details>`
        : ""
    }

    <details class="evidence-block">
      <summary><span class="ev-purpose">Analysis steps</span>
        <span class="ev-meta">${((trace && trace.steps) || []).length} nodes</span></summary>
      <div class="evidence-body">${timeline((trace && trace.steps) || [])}</div>
    </details>
  </section>`;
}

/** 7 · Confidence, with the factors that produced it. */
function confidenceSection(run) {
  const detail = (run.answer || {}).confidence_detail;
  if (!detail) return "";
  return `<section class="report-section">
    <div class="section-head-row"><div class="section-label">Confidence</div>
      <span class="section-note">computed from the trace, not asserted by the model</span></div>
    ${confidenceBlock(detail, false)}
    ${
      (run.answer.caveats || []).length
        ? `<div class="caveats"><div class="proc-label">Caveats</div>
            <ul class="plain">${run.answer.caveats
              .map((caveat) => `<li>${esc(caveat)}</li>`)
              .join("")}</ul></div>`
        : ""
    }
  </section>`;
}

/** 8 · Recommended actions. */
function recommendations(run) {
  const items = (run.answer && run.answer.recommendations) || [];
  if (!items.length) return "";
  return `<section class="report-section">
    <div class="section-head-row"><div class="section-label">Recommended actions</div>
      <span class="section-note">each follows from a finding above</span></div>
    <ol class="rec-list">${items
      .map((item) => {
        const [label, colour] = PRIORITY[item.priority] || PRIORITY.medium;
        return `<li class="rec">
          <span class="rec-rank" style="background:${colour}18;color:${colour}">${label}</span>
          <div><div class="rec-action">${esc(item.action)}</div>
            <div class="rec-why">${esc(item.rationale)}</div></div>
        </li>`;
      })
      .join("")}</ol>
  </section>`;
}

/** The whole report, assembled. */
function reportView(run, trace) {
  return `<div class="report">
    ${executiveSummary(run)}
    ${keyFindings(run)}
    ${investigationProcess(run)}
    ${hypothesisTesting(run)}
    ${visualAnalytics(run, trace)}
    ${evidenceSection(run, trace)}
    ${confidenceSection(run)}
    ${recommendations(run)}
    ${
      (run.answer.refuted || []).length
        ? `<section class="report-section">
            <div class="section-label">Ruled out</div>
            <ul class="plain">${run.answer.refuted
              .map((item) => `<li>${esc(item)}</li>`)
              .join("")}</ul></section>`
        : ""
    }
  </div>`;
}

/** Fetch and render the rows behind one query, on demand. */
async function showRows(runId, queryId) {
  const slot = document.getElementById(`rows-${queryId}`);
  if (!slot) return;
  if (slot.dataset.loaded === "1") {
    slot.classList.toggle("hidden");
    return;
  }
  slot.innerHTML = '<div class="proc-empty">Loading the rows…</div>';
  try {
    const data = await get(`/v1/runs/${runId}/queries/${queryId}/rows?limit=50`);
    slot.dataset.loaded = "1";
    slot.innerHTML = rowsTable(data);
  } catch (error) {
    // The rows are rebuilt by re-running the statement, so this can genuinely fail - say why
    // rather than showing an empty table, which reads as "the query returned nothing".
    slot.innerHTML = `<div class="proc-empty">${esc(error.message)}</div>`;
  }
}

function rowsTable(data) {
  if (!data.rows.length) {
    return '<div class="proc-empty">That query returned no rows.</div>';
  }
  const head = data.columns.map((column) => `<th>${esc(column)}</th>`).join("");
  const body = data.rows
    .map(
      (row) =>
        `<tr>${data.columns
          .map((column) => `<td>${esc(row[column] ?? "")}</td>`)
          .join("")}</tr>`
    )
    .join("");
  const note =
    data.returned < data.row_count
      ? `<div class="rows-note">showing ${data.returned} of ${data.row_count} rows</div>`
      : `<div class="rows-note">${data.row_count} rows</div>`;
  return `<div class="rows-wrap"><table class="rows"><thead><tr>${head}</tr></thead>
    <tbody>${body}</tbody></table></div>${note}`;
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
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none"
                     stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                  <path d="M6 3.8h12a1 1 0 0 1 1 1V21l-7-4.2L5 21V4.8a1 1 0 0 1 1-1Z"/></svg>Save
                  report</button>
              <button class="btn btn-primary btn-sm" id="details-btn">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                     stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                  <path d="M4 19V5M4 19h16"/><path d="M8 15V9M13 15V6M18 15v-4"/></svg>Full report</button>
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
            <div><div class="panel-title">Key Takeaways</div>${takeaways(run)}
              ${confidenceBlock(run.answer.confidence_detail, true)}</div>
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

  // The eight-section report. The card above it is the glance; this is the read, and it is the
  // reason the page is not a chat transcript with a paragraph in it.
  box.innerHTML = reportView(run, trace);
  box.querySelectorAll("[data-rows]").forEach((button) =>
    button.addEventListener("click", (event) => {
      event.preventDefault();
      showRows(run.run_id, button.dataset.rows);
    })
  );
  box.querySelectorAll("[data-jump]").forEach((button) =>
    button.addEventListener("click", (event) => {
      event.preventDefault();
      const target = document.getElementById(`rows-${button.dataset.jump}`);
      const block = target && target.closest("details");
      if (block) {
        block.open = true;
        block.scrollIntoView({ behavior: "smooth", block: "center" });
        showRows(run.run_id, button.dataset.jump);
      }
    })
  );
  return;
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
    save.addEventListener("click", async () => {
      // A report is a snapshot taken now, not a bookmark: what it says has to stay what it said.
      const name = (prompt("Name this report", run.question) || "").trim();
      if (name === "") return;
      save.disabled = true;
      try {
        const created = await post("/v1/reports", {
          run_id: run.run_id,
          name,
          saved_by: state.who,
        });
        toast(`Saved as "${created.name}". Find it under Reports.`);
      } catch (error) {
        toast(error.message);
      } finally {
        save.disabled = false;
      }
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
  /**
   * The dashboard, from one request.
   *
   * `/v1/dashboard/summary` returns the counts and the three lists together, so the totals cannot
   * disagree with the rows beneath them — six requests would show six different moments.
   */
  async dashboard() {
    const box = $("page-dashboard");
    loading(box, 4);
    let data;
    try {
      data = await get("/v1/dashboard/summary?recent=6");
    } catch (error) {
      box.innerHTML = empty("The dashboard could not load", error.message);
      return;
    }
    const { totals, outcomes } = data;
    if (!totals.analyses) {
      box.innerHTML = empty(
        "Nothing analysed yet",
        "Ask a business question and this page fills in with what the agent did, what it cost, " +
          "and which definitions it used.",
        { page: "ask", label: "Ask a question" }
      );
      wirePageLinks(box);
      return;
    }

    const stat = (label, value, note) => `<div class="stat">
      <div class="value">${value}</div><div class="label">${esc(label)}</div>
      ${note ? `<div class="stat-note">${esc(note)}</div>` : ""}</div>`;

    const median = totals.median_duration_ms
      ? `${Math.round(totals.median_duration_ms / 1000)}s`
      : "—";

    // Success and failure over *finished* runs, with what is still open stated separately rather
    // than folded in: an in-flight run is not a failure, and a run waiting on a person is not one
    // either.
    const rate = outcomes.success_rate == null ? "—" : `${outcomes.success_rate}%`;
    const open = totals.in_flight + totals.clarifying + totals.awaiting_approval;

    const bar = (label, count, colour) => {
      const width = outcomes.finished ? (count / outcomes.finished) * 100 : 0;
      return `<div class="meter-row">
        <div class="meter-head"><span>${esc(label)}</span><span class="meter-n">${count}</span></div>
        <div class="meter"><div style="width:${width}%;background:${colour}"></div></div></div>`;
    };

    box.innerHTML = `
      <div class="card stat-strip">
        ${stat("Analyses", totals.analyses)}
        ${stat("Saved reports", totals.saved_reports)}
        ${stat("Success rate", rate, `${outcomes.finished} finished`)}
        ${stat("Open", open, "in flight or waiting on you")}
        ${stat("Queries run", totals.queries.toLocaleString())}
        ${stat("Tokens", totals.tokens.toLocaleString())}
        ${stat("Median run", median)}
      </div>

      <div class="grid-2">
        <div class="card mcard">
          <div class="panel-title">Outcomes</div>
          ${bar("Completed", totals.completed, "var(--good)")}
          ${bar("Truncated by budget", totals.truncated, "var(--warn)")}
          ${bar("Failed", totals.failed, "var(--bad)")}
          <div class="kv" style="margin-top:10px">Over finished runs only. A run still in flight
            or waiting on a decision is neither a success nor a failure yet.</div>
        </div>

        <div class="card mcard">
          <div class="panel-title">Most used definitions</div>
          ${
            data.top_metrics.length
              ? data.top_metrics
                  .map(
                    (metric) => `<div class="row-line">
                      <span class="mono" style="color:var(--text)">${esc(metric.metric)}</span>
                      <span class="when">${metric.uses} use${metric.uses === 1 ? "" : "s"} ·
                        ${ago(metric.last_used)}</span></div>`
                  )
                  .join("")
              : `<div class="empty-hint">No approved metric has been computed yet. The agent
                  reaches for one whenever a question names a business term it recognises.</div>`
          }
        </div>
      </div>

      <div class="grid-2">
        <div class="card mcard">
          <div class="panel-title">Recent questions</div>
          ${
            data.recent_questions
              .map(
                (run) => `<button class="row-line as-button" data-run="${run.run_id}">
                  <span class="q">${esc(run.question)}</span>
                  <span style="display:flex;gap:10px;align-items:center">
                    <span class="when">${ago(run.created_at)}</span>${chip(run.status)}</span>
                </button>`
              )
              .join("") || `<div class="empty-hint">Nothing asked yet.</div>`
          }
        </div>

        <div class="card mcard">
          <div class="panel-title">Recent insights
            <span class="hint">what the agent noticed</span></div>
          ${
            data.recent_insights.length
              ? data.recent_insights
                  .map(
                    (insight) => `<button class="takeaway as-button ${
                      insight.material ? "material" : "good"
                    }" data-run="${insight.run_id}">
                      <span class="t">${esc(insight.statement)}
                        <span class="sub">${
                          insight.material ? "· needed explaining" : ""
                        } · ${ago(insight.created_at)}</span></span></button>`
                  )
                  .join("")
              : `<div class="empty-hint">No findings recorded yet. A finding is something the
                  agent noticed in a result and had to account for.</div>`
          }
        </div>
      </div>`;

    box.querySelectorAll("[data-run]").forEach((element) =>
      element.addEventListener("click", () => openRun(element.dataset.run))
    );
    wirePageLinks(box);
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

  /**
   * Saved reports: open, rename, delete, export.
   *
   * A report is a snapshot rather than a pointer, so what is listed here is what each report
   * *said when it was saved*. The run it came from is one click away for anyone who wants to see
   * whether anything has changed since.
   */
  async reports() {
    const box = $("page-reports");
    loading(box, 4);
    let reports;
    try {
      reports = await get("/v1/reports?limit=60");
    } catch (error) {
      box.innerHTML = empty("Reports could not load", error.message);
      return;
    }
    if (!reports.length) {
      box.innerHTML = empty(
        "No saved reports",
        "Finish an analysis and press Save report. A report keeps the answer, its confidence, " +
          "the charts and the SQL behind every number — frozen as it read when you saved it.",
        { page: "ask", label: "Ask a question" }
      );
      wirePageLinks(box);
      return;
    }

    box.innerHTML = `<div class="kv" style="margin:20px 0 14px">${reports.length} saved.
        Exports carry the findings, the charts and the evidence section — publishing is not
        offered, because that is approval point 4 and the gate behind it is not built.</div>
      <div id="report-list"></div>
      <div id="report-body" style="margin-top:20px"></div>`;

    const list = $("report-list");
    list.innerHTML = reports
      .map(
        (report) => `<div class="card report-row" data-report="${report.report_id}">
          <div class="report-main">
            <button class="report-open" data-open="${report.report_id}">
              <span class="q">${esc(report.name)}</span>
              <span class="when">${esc(report.question || "")}</span>
            </button>
            <div class="report-meta">
              <span class="when">${ago(report.created_at)}</span>
              ${
                report.confidence_score != null
                  ? `<span class="chip">${report.confidence_score}% confidence</span>`
                  : ""
              }
              <span class="chip">${report.queries} quer${report.queries === 1 ? "y" : "ies"}</span>
              ${report.charts ? `<span class="chip">${report.charts} chart${report.charts === 1 ? "" : "s"}</span>` : ""}
            </div>
          </div>
          <div class="report-actions">
            <a class="btn btn-sm" href="/v1/reports/${report.report_id}/export.pdf">PDF</a>
            <a class="btn btn-sm" href="/v1/reports/${report.report_id}/export.xlsx">Excel</a>
            <button class="btn btn-sm" data-rename="${report.report_id}">Rename</button>
            <button class="btn btn-sm danger" data-delete="${report.report_id}">Delete</button>
          </div>
        </div>`
      )
      .join("");

    list.querySelectorAll("[data-open]").forEach((button) =>
      button.addEventListener("click", () => showReport(button.dataset.open))
    );

    list.querySelectorAll("[data-rename]").forEach((button) =>
      button.addEventListener("click", async () => {
        const row = reports.find((r) => r.report_id === button.dataset.rename);
        const name = (prompt("Rename this report", row ? row.name : "") || "").trim();
        if (!name) return;
        try {
          await request("PATCH", `/v1/reports/${button.dataset.rename}`, { name });
          toast("Renamed.");
          PAGE_LOADERS.reports();
        } catch (error) {
          toast(error.message);
        }
      })
    );

    list.querySelectorAll("[data-delete]").forEach((button) =>
      button.addEventListener("click", async () => {
        const row = reports.find((r) => r.report_id === button.dataset.delete);
        // Confirmed, because deleting a report cannot be undone: the snapshot is the only copy of
        // what the answer said at that moment.
        if (!confirm(`Delete "${row ? row.name : "this report"}"? This cannot be undone.`)) return;
        try {
          await request("DELETE", `/v1/reports/${button.dataset.delete}`);
          toast("Deleted.");
          PAGE_LOADERS.reports();
        } catch (error) {
          toast(error.message);
        }
      })
    );

    showReport(reports[0].report_id);
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


/** PATCH and DELETE, which the two-verb helpers above do not cover. */
async function request(method, path, body) {
  const response = await fetch(API + path, {
    method,
    headers: body ? { "content-type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!response.ok) {
    let detail = await response.text();
    try {
      const parsed = JSON.parse(detail);
      detail = parsed.detail || detail;
      if (Array.isArray(detail)) detail = detail.map((d) => d.msg || "").join("; ");
    } catch (_) {}
    throw new Error(detail || response.statusText);
  }
  return response.status === 204 ? null : response.json();
}

/** One saved report, read from its snapshot rather than from the live run. */
async function showReport(reportId) {
  const body = $("report-body");
  if (!body) return;
  loading(body, 5);
  let report;
  try {
    report = await get(`/v1/reports/${reportId}`);
  } catch (error) {
    body.innerHTML = empty("That report could not be loaded", error.message);
    return;
  }
  const snapshot = report.snapshot || {};
  const answer = snapshot.answer || {};
  const charts = snapshot.charts || [];

  body.innerHTML = `<div class="card" style="padding:20px 22px">
    <div class="result-head" style="padding:0 0 14px">
      <div>
        <div class="result-title">${esc(report.name)}</div>
        <div class="result-meta">${chip(snapshot.status)}
          <span class="when">saved ${ago(snapshot.saved_at)}</span>
          <span class="when">· asked ${ago(snapshot.asked_at)}</span></div>
      </div>
      <div class="result-actions">
        <a class="btn btn-sm" href="/v1/reports/${reportId}/export.pdf">Export PDF</a>
        <a class="btn btn-sm" href="/v1/reports/${reportId}/export.xlsx">Export Excel</a>
        <button class="btn btn-sm" data-run="${snapshot.run_id}">Open the run</button>
      </div>
    </div>

    <div class="result-body" style="padding:0">
      <div><div class="panel-title">Key Takeaways</div>
        ${
          (snapshot.findings || []).length
            ? snapshot.findings
                .map(
                  (finding, index) => `<div class="takeaway ${
                    finding.material ? "material" : "good"
                  }"><span class="tile" style="background:${
                    Object.values(TILES)[index % 4][0]
                  };color:${Object.values(TILES)[index % 4][1]}">${
                    finding.material ? "!" : "✓"
                  }</span><span class="t">${esc(finding.statement)}</span></div>`
                )
                .join("")
            : `<div class="empty-hint">No separate findings were recorded.</div>`
        }
        ${confidenceBlock(snapshot.confidence, true)}</div>
      ${chartPanel(charts[0], "Trend")}
      ${chartPanel(charts[1], "Breakdown")}
    </div>

    <div class="sub-head">Conclusion</div>
    <div class="conclusion">${esc(answer.conclusion)}</div>
    ${
      (answer.refuted || []).length
        ? `<div class="sub-head">Ruled out</div><ul class="plain">${answer.refuted
            .map((item) => `<li>${esc(item)}</li>`)
            .join("")}</ul>`
        : ""
    }
    ${
      (answer.caveats || []).length
        ? `<div class="sub-head">Caveats</div><ul class="plain">${answer.caveats
            .map((item) => `<li>${esc(item)}</li>`)
            .join("")}</ul>`
        : ""
    }
    ${
      (snapshot.metrics_used || []).length
        ? `<div class="sub-head">Approved definitions used</div><ul class="plain">${snapshot.metrics_used
            .map(
              (metric) =>
                `<li>${esc(metric.metric)}${metric.version ? ` · ${esc(metric.version)}` : ""}</li>`
            )
            .join("")}</ul>`
        : ""
    }
    <div class="sub-head">The SQL behind each cited number</div>
    ${
      (snapshot.evidence || [])
        .map(
          (item) => `<div class="q-row">
            <div class="purpose">${esc(item.purpose)}</div>
            <div class="idline">${esc(item.query_id)} · ${item.row_count} rows${
            item.definition_version ? ` · ${esc(item.definition_version)}` : ""
          }</div>
            <pre class="sql">${esc(item.sql)}</pre></div>`
        )
        .join("") || `<div class="empty-hint">This answer cited no queries.</div>`
    }
    ${
      (snapshot.queries_considered || []).some((q) => q.verdict !== "allowed")
        ? `<div class="sub-head">Queries that did not run</div>${snapshot.queries_considered
            .filter((q) => q.verdict !== "allowed")
            .map(
              (item) => `<div class="q-row">
                <div class="purpose">${esc(item.purpose)}</div>
                <div style="margin:5px 0">${chip(item.verdict)}</div>
                ${
                  (item.reasons || []).length
                    ? `<ul class="plain">${item.reasons
                        .map((r) => `<li>${esc(r)}</li>`)
                        .join("")}</ul>`
                    : ""
                }
                <pre class="sql">${esc(item.sql)}</pre></div>`
            )
            .join("")}`
        : ""
    }
  </div>`;

  body.querySelectorAll("[data-run]").forEach((button) =>
    button.addEventListener("click", () => openRun(button.dataset.run))
  );
}

/** Wire any element carrying data-page inside a freshly rendered container. */
function wirePageLinks(container) {
  container.querySelectorAll("[data-page]").forEach((element) =>
    element.addEventListener("click", (event) => {
      event.preventDefault();
      go(element.dataset.page);
    })
  );
}

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
