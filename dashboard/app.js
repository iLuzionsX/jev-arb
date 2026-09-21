const money = (value) => `$${Number(value || 0).toFixed(2)}`;
const pct = (value) => `${Number(value || 0).toFixed(3)}%`;
const time = (ms) => ms ? new Date(Number(ms)).toLocaleTimeString() : "—";
const esc = (value) => String(value ?? "—").replace(/[&<>"']/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[c]));

async function getJson(path) {
  const response = await fetch(path, {cache: "no-store"});
  if (!response.ok) throw new Error(`${response.status}`);
  return response.json();
}

function setMetrics(data) {
  const books = data.books || [];
  const ready = books.filter((book) => book.bid && book.ask).length;
  const candidates = data.candidate_opportunities || 0;
  const jev = data.strategies?.jev || {};
  const baseline = data.strategies?.baseline || {};
  document.getElementById("market-metrics").innerHTML = [
    ["Connected books", `${ready}/${books.length || 0}`, "normalized bid/ask state"],
    ["Candidates", candidates, `${Number(data.opportunities_per_hour || 0).toFixed(1)} / hour`],
    ["Baseline P&L", money(baseline.net_simulated_pnl_usd), `${baseline.simulated_trades || 0} accepted paper fills`],
    ["Jev value added", money(data.paired?.jev_incremental_pnl_usd), data.statistical_conclusion || "awaiting observations"],
  ].map(([label, value, sub]) => `<article class="metric-card"><div class="metric-label">${esc(label)}</div><div class="metric-value">${esc(value)}</div><div class="metric-sub">${esc(sub)}</div></article>`).join("");
}

function strategyCard(name, metrics, extraClass) {
  const pnlClass = Number(metrics.net_simulated_pnl_usd || 0) >= 0 ? "positive" : "negative";
  return `<article class="strategy-card ${extraClass}">
    <h3>${esc(name)}</h3>
    <div class="strategy-stat-grid">
      ${stat("Net P&L", money(metrics.net_simulated_pnl_usd), pnlClass)}
      ${stat("Trades", metrics.simulated_trades)}
      ${stat("Win rate", pct(Number(metrics.win_rate || 0) * 100))}
      ${stat("Avg trade", money(metrics.average_pnl_per_trade_usd))}
      ${stat("Max drawdown", money(metrics.max_drawdown_usd))}
      ${stat("Accepted", metrics.accepted_opportunities)}
      ${stat("Missed / blocked", Object.entries(metrics.status_counts || {}).filter(([key]) => key !== "executed").reduce((sum, [, value]) => sum + value, 0))}
      ${stat("Rejected made money", metrics.rejected_opportunities_that_would_have_made_money)}
      ${stat("Profit / $1k", money(metrics.profit_per_1000_deployed_usd))}
    </div></article>`;
}
function stat(label, value, className = "") { return `<div><span class="stat-label">${esc(label)}</span><span class="stat-value ${className}">${esc(value)}</span></div>`; }

function setStrategies(data) {
  document.getElementById("strategy-cards").innerHTML = strategyCard("Strategy A · deterministic baseline", data.strategies?.baseline || {}, "baseline") + strategyCard("Strategy B · Jev-assisted", data.strategies?.jev || {}, "jev");
  const latency = data.strategies?.jev?.latency_ms || {};
  document.getElementById("latency-block").innerHTML = [["p50", latency.p50], ["p95", latency.p95], ["p99", latency.p99]].map(([label, value]) => `<div><div class="latency-value">${value == null ? "—" : `${Number(value).toFixed(1)}ms`}</div><div class="latency-label">${label}</div></div>`).join("");
  const buckets = data.strategies?.jev?.confidence_buckets || {};
  document.getElementById("confidence-list").innerHTML = Object.keys(buckets).length ? Object.entries(buckets).map(([key, value]) => `<div class="confidence-row"><span>${esc(key)}</span><strong>${Number(value.avg_realized_pnl_usd || 0).toFixed(3)} avg · ${Number(value.count || 0)} obs</strong></div>`).join("") : `<div class="confidence-row"><span>No Jev confidence observations yet</span><strong>—</strong></div>`;
}

function setBooks(data) {
  document.getElementById("book-table").innerHTML = (data.books || []).map(book => `<tr><td>${esc(book.venue)}</td><td>${esc(book.symbol)}</td><td>${book.bid == null ? "—" : Number(book.bid).toFixed(2)}</td><td>${book.ask == null ? "—" : Number(book.ask).toFixed(2)}</td><td>${book.age_ms == null ? "—" : `${book.age_ms}ms`}</td><td>${esc(book.sequence)}</td></tr>`).join("") || `<tr><td colspan="6">Waiting for public order-book snapshots…</td></tr>`;
}

function setCandidates(data) {
  const rows = (data.candidates || []).slice(0, 80).map(candidate => {
    const baseline = (candidate.decisions || []).find(d => d.strategy === "baseline");
    const jev = (candidate.decisions || []).find(d => d.strategy === "jev");
    const trades = candidate.trades || [];
    const results = trades.map(t => `${t.strategy}: ${t.status} ${money(t.realized_pnl_usd)}`).join(" · ");
    return `<tr><td>${time(candidate.detected_at_ms)}</td><td>${esc(candidate.symbol)}</td><td>${esc(candidate.buy_venue)} → ${esc(candidate.sell_venue)}</td><td>${pct(candidate.gross_spread_pct)}</td><td>${pct(candidate.expected_net_spread_pct)}</td><td>${decisionPill(baseline)}</td><td>${decisionPill(jev)}</td><td>${esc(results || "pending")}</td></tr>`;
  });
  document.getElementById("candidate-table").innerHTML = rows.join("") || `<tr><td colspan="8">No positive after-cost candidate has been recorded yet.</td></tr>`;
}
function decisionPill(decision) {
  if (!decision) return `<span class="pill neutral">pending</span>`;
  return `<span class="pill ${decision.accepted ? "good" : "bad"}">${esc(decision.decision)}</span>`;
}

function drawPnl(data) {
  const canvas = document.getElementById("pnl-chart");
  const rect = canvas.getBoundingClientRect();
  const ratio = window.devicePixelRatio || 1;
  canvas.width = rect.width * ratio; canvas.height = 220 * ratio;
  const ctx = canvas.getContext("2d"); ctx.scale(ratio, ratio);
  const width = rect.width, height = 220;
  ctx.clearRect(0, 0, width, height);
  ctx.strokeStyle = "#e1e6eb"; ctx.lineWidth = 1;
  [40, 90, 140, 190].forEach(y => { ctx.beginPath(); ctx.moveTo(0,y); ctx.lineTo(width,y); ctx.stroke(); });
  const candidates = (data.candidates || []).slice().reverse();
  const points = {baseline: [], jev: []}; let totals = {baseline: 0, jev: 0};
  candidates.forEach((c, i) => { (c.trades || []).filter(t => t.accepted).forEach(t => { totals[t.strategy] = (totals[t.strategy] || 0) + Number(t.realized_pnl_usd || 0); }); points.baseline.push(totals.baseline); points.jev.push(totals.jev); });
  const all = [...points.baseline, ...points.jev, 0]; const min = Math.min(...all), max = Math.max(...all), span = Math.max(0.01, max - min);
  function line(values, color) { if (!values.length) return; ctx.strokeStyle = color; ctx.lineWidth = 2; ctx.beginPath(); values.forEach((v, i) => { const x = values.length === 1 ? width / 2 : 8 + (width - 16) * i / (values.length - 1); const y = height - 15 - ((v - min) / span) * (height - 30); i ? ctx.lineTo(x,y) : ctx.moveTo(x,y); }); ctx.stroke(); }
  line(points.baseline, "#78858e"); line(points.jev, "#1d5d75");
  ctx.font = "11px system-ui"; ctx.fillStyle = "#78858e"; ctx.fillText("Baseline", 8, 15); ctx.fillStyle = "#1d5d75"; ctx.fillText("Jev", 75, 15);
}

async function refresh() {
  try {
    const data = await getJson("/api/overview");
    setMetrics(data); setStrategies(data); setBooks(data); setCandidates(data); drawPnl(data);
    const connected = Object.values(data.health?.exchanges || {}).filter(item => item.connected).length;
    document.getElementById("run-status").textContent = `${connected} exchange connection${connected === 1 ? "" : "s"} · ${data.paper_only ? "paper-only safety boundary active" : "safety state unavailable"}`;
    document.getElementById("updated-at").textContent = `Updated ${new Date().toLocaleTimeString()}`;
    const enough = Number(data.candidate_opportunities || 0) >= 30;
    document.getElementById("evidence-note").innerHTML = `<strong>${enough ? "The sample is growing." : "This is not enough data to conclude an edge."}</strong> ${esc(data.statistical_conclusion || "The system records paired outcomes and will report whether the difference is statistically meaningful.")} The dashboard does not convert a few fills into a projected monthly or annual return.`;
  } catch (error) {
    document.getElementById("run-status").textContent = "Dashboard API unavailable";
    document.getElementById("evidence-note").textContent = String(error);
  }
}
refresh(); setInterval(refresh, 2000); window.addEventListener("resize", refresh);


