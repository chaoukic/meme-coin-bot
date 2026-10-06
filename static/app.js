// Memebot dashboard (read-only). Renders /api/state, or window.__SNAPSHOT__ in static exports.
// App-style layout: hash router (#home, #trades, #scanner, #health, #docs, #settings), same structure as the Cross-Chain admin site (light theme).
"use strict";
const TZ = "America/Toronto";
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const num = (v) => (v == null || v === "" || isNaN(+v)) ? null : +v;
const usd = (v, d = 2) => (v = num(v)) == null ? "—" : (v < 0 ? "-$" : "$") + Math.abs(v).toLocaleString("en-US", {minimumFractionDigits: d, maximumFractionDigits: d});
const big = (v) => (v = num(v)) == null ? "—" : "$" + (v >= 1e9 ? (v/1e9).toFixed(2) + "B" : v >= 1e6 ? (v/1e6).toFixed(2) + "M" : v >= 1e3 ? (v/1e3).toFixed(1) + "k" : v.toFixed(0));
const pct = (v, d = 1) => (v = num(v)) == null ? "—" : (v > 0 ? "+" : "") + v.toFixed(d) + "%";
const cls = (v) => (v = num(v)) == null ? "" : v > 0 ? "pos" : v < 0 ? "neg" : "";
const price = (v) => (v = num(v)) == null ? "—" : "$" + (v >= 1 ? v.toFixed(4) : Number(v.toPrecision(4)).toString());
const tfmt = (ts, withDate = false) => ts ? new Date(ts * 1000).toLocaleString("en-CA", {timeZone: TZ, hour12: false,
  ...(withDate ? {month: "short", day: "numeric"} : {}), hour: "2-digit", minute: "2-digit", ...(withDate ? {} : {second: "2-digit"})}) : "—";
const dur = (s) => { s = num(s); if (s == null) return "—"; s = Math.max(0, s); return s < 60 ? Math.round(s) + "s" : s < 3600 ? Math.round(s/60) + "m" : s < 86400 ? (s/3600).toFixed(1) + "h" : (s/86400).toFixed(1) + "d"; };
const ago = (ts, now) => ts ? dur(now - ts) : "—";
const pill = (c, t) => `<span class="pill ${c}">${esc(t)}</span>`;
const HUES = [262, 200, 160, 30, 330, 110, 45, 290];
function avatar(seed, label) {
  const s = String(seed || label || "?"); let h = 0; for (const c of s) h = (h * 31 + c.charCodeAt(0)) >>> 0;
  const txt = esc(String(label || "?").replace(/[^A-Za-z0-9]/g, "").slice(0, 2).toUpperCase() || "?");
  return `<span class="avatar" style="background:hsl(${HUES[h % HUES.length]} var(--avatar-s) var(--avatar-l))">${txt}</span>`;
}
const link = (sym, url, addr) => { const n = esc(sym || (addr || "").slice(0, 6) || "?");
  return url ? `<a href="${esc(url)}" target="_blank" rel="noopener">${n}</a>` : `<span class="nm">${n}</span>`; };
// Pre-buy Jupiter fallback label: only when entry_source === "jup_fallback"; tooltip from entry_check (JSON) if simple
function jupChip(p) {
  if (!p || p.entry_source !== "jup_fallback") return "";
  let c = p.entry_check, t = "Bought after DexScreener was busy: checked with a fresh Jupiter reading and two test quotes (never executed)";
  if (typeof c === "string") { try { c = JSON.parse(c); } catch (e) { c = null; } }
  if (c && typeof c === "object") {
    const bits = [];
    if (typeof c.reason === "string" && c.reason) bits.push(c.reason.slice(0, 220));
    else {
      if (num(c.trade_usd) != null && num(c.trade_cost_pct) != null) bits.push(`$${num(c.trade_usd)} quote cost ${num(c.trade_cost_pct).toFixed(2)}%`);
      if (num(c.depth_usd) != null && num(c.depth_impact_pct) != null) bits.push(`$${num(c.depth_usd)} impact ${num(c.depth_impact_pct).toFixed(3)}%`);
      if (num(c.liq_est) != null) bits.push(`~$${Math.round(num(c.liq_est)).toLocaleString("en-US")} liquidity`);
    }
    if (bits.length) t += ". " + bits.join(", ");
  }
  return ` <span class="pill jupchk" title="${esc(t)}"><span class="jc-l">checked via Jupiter quote</span><span class="jc-s">Jup check</span></span>`;
}
const tok = (sym, url, addr) => `<span class="coin">${avatar(addr, sym)}${link(sym, url, addr)}</span>`;
// cols: [[key, title, isNum], ...]; each row's cells get data-label from the column titles (phone card layout)
function table(el, cols, rows, emptyMsg) {
  $(el).innerHTML = rows.length
    ? `<thead><tr>${cols.map(([k, h, n]) => `<th class="col-${k}${n ? " num" : ""}">${esc(h)}</th>`).join("")}</tr></thead><tbody>${rows.join("")}</tbody>`
    : `<tbody><tr><td class="empty">${esc(emptyMsg)}</td></tr></tbody>`;
  if (rows.length) $(el).querySelectorAll("tbody tr").forEach(tr => [...tr.children].forEach((c, i) => cols[i] && c.setAttribute("data-label", cols[i][1])));
}
let chart, DATA, HEALTH = "ok", feedAll = false;

// ---------------------------------------------------------------- bot offline banner (Oct 6)
// data.health = {last_cycle_ts, heartbeat_ts, generated, offline_after_sec}. Uses the visitor's clock: if the newer of heartbeat_ts /
// last_cycle_ts is older than offline_after_sec (default 600), the bot counts as offline. `generated` is ignored on purpose (Oct 6):
// the publisher can keep running after the bot loop stops. Both null while a health block exists: offline, no "since" time.
// No health block at all: no banner.
const OFFLINE = {on: false, since: null};
const dataNow = (d) => OFFLINE.on ? Date.now() / 1000 : d.generated;  // when offline, "x ago" is measured against the real clock
function offlineState(d) {
  const H = d && d.health && typeof d.health === "object" ? d.health : null;
  if (!H) return {on: false, since: null};
  const ts = [H.heartbeat_ts, H.last_cycle_ts].map(num).filter(v => v != null && v > 0);
  if (!ts.length) return {on: !window.__SNAPSHOT__, since: null};
  const last = Math.max(...ts), lim = num(H.offline_after_sec) > 0 ? num(H.offline_after_sec) : 600;
  return {on: !window.__SNAPSHOT__ && Date.now() / 1000 - last > lim, since: last};  // a static snapshot is old by design: no banner
}
// heartbeat / last scan cycle for the health tile and Health screen: the health block when present (same source as the banner)
const hasHealth = (d) => !!(d && d.health && typeof d.health === "object");
const hbTs = (d) => hasHealth(d) ? num(d.health.heartbeat_ts) : num(d.trader_heartbeat);
const cycleTs = (d, s) => hasHealth(d) ? num(d.health.last_cycle_ts) : s.lastRun;
function offlineBanner() {
  const b = $("offlinebanner"); if (!b) return;
  if (!OFFLINE.on) { b.classList.add("hidden"); return; }
  let h;
  if (OFFLINE.since == null) h = `<span><b>Bot offline</b> · no recent heartbeat · data may be out of date</span>`;
  else {
    const t = new Date(OFFLINE.since * 1000).toLocaleString(undefined, {timeZone: TZ, month: "short", day: "numeric", hour: "numeric", minute: "2-digit"});
    h = `<span><b>Bot offline</b> since ${esc(t)} Toronto time (${dur(Date.now() / 1000 - OFFLINE.since)} ago) · data may be out of date.</span>`;
  }
  if (b.innerHTML !== h) b.innerHTML = h;
  b.classList.remove("hidden");
}
// re-check on every data refresh (render) and once a minute; re-render the status parts when the state flips
function checkOffline(d, rerender) {
  const was = OFFLINE.on, o = offlineState(d);
  OFFLINE.on = o.on; OFFLINE.since = o.since;
  offlineBanner();
  if (rerender && was !== OFFLINE.on && DATA) render(DATA);
}
setInterval(() => DATA && checkOffline(DATA, true), 60000);

// ---------------------------------------------------------------- status helpers
function scanInfo(d) {
  const st = d.status || {}, now = dataNow(d), c0 = d.cycles[0];
  const lastRun = st.last_run || (c0 && c0.finished) || null;
  const interval = num((d.config.scanner || {}).scan_interval_sec) || 90;
  const age = lastRun ? now - lastRun : null;
  let state, p;
  if (OFFLINE.on) { state = "bad"; p = pill("wait", "bot offline"); }
  else if (st.state === "error") { state = "bad"; p = pill("bad", "error"); }
  else if (st.state === "scanning") { const long = st.since && now - st.since > 600; state = long ? "bad" : "ok"; p = pill(long ? "bad" : "ok", long ? "scan stuck?" : "scanning…"); }
  else if (age != null) { state = age < 300 ? "ok" : "bad"; p = pill(state, age < 300 ? "running" : "stale"); }
  else { state = "wait"; p = pill("", "unknown"); }
  return {st, lastRun, age, interval, state, pill: p};
}

// ---------------------------------------------------------------- daily loss mode
// kpi.day_mode: "normal" | "soft" (soft cap: only high scores at reduced size) | "hard" (hard stop: no entries till midnight).
// Older data without day_mode: fall back to kpi.paused (true = cap hit, no new entries).
function dayMode(d) {
  const k = d.kpi || {}, P = (d.config || {}).paper || {};
  const known = ["normal", "soft", "hard"].includes(k.day_mode);
  const mode = known ? k.day_mode : k.paused ? "hard" : "normal";
  const score = num(k.cap_min_score) ?? num(P.cap_min_score), fac = num(k.cap_size_factor) ?? num(P.cap_size_factor);
  const built = {normal: known ? "all entries allowed" : "daily loss cap OK",
    soft: `soft cap (only score ≥ ${score ?? "the high-score minimum"}${fac != null ? ` at ${Math.round(fac * 100)}% size` : " at reduced size"})`,
    hard: known ? "hard stop (no new entries until midnight Toronto)" : "daily loss cap hit (no new entries until midnight Toronto)"}[mode];
  const label = known && k.day_mode_label ? String(k.day_mode_label).replaceAll(">=", "≥") : built;
  const eq = num(k.equity), dp = num(k.day_pnl), base = eq != null && dp != null ? eq - dp : null;
  return {mode, label, known, score, fac, soft: num(k.day_cap), hard: num(k.day_hard_cap), dp, dpp: base ? dp / base * 100 : null,
    softPct: num(P.daily_loss_cap_pct), hardPct: num(P.daily_hard_stop_pct)};
}
const DM_ICON = {normal: "✓", soft: "!", hard: "■"};
const dayBadge = (m) => `<span class="daymode ${m.mode}" title="Daily loss mode${m.mode === "normal" ? "" : ": open positions are still managed"}"><i>${DM_ICON[m.mode]}</i>${esc(m.label)}</span>`;
// small meter: today's loss against the soft (tick) and hard (end) limits
function dayMeter(m) {
  if (m.hard == null || !(m.hard > 0) || m.dp == null) return "";
  const loss = Math.max(0, -m.dp), fill = Math.min(100, loss / m.hard * 100), tick = m.soft != null ? Math.min(100, m.soft / m.hard * 100) : null;
  return `<div class="dmeter ${m.mode}" title="Today's loss vs soft cap and hard stop"><span class="dm-fill" style="width:${fill.toFixed(1)}%"></span>${tick != null ? `<span class="dm-tick" style="left:${tick.toFixed(1)}%"></span>` : ""}</div>`;
}

// ---------------------------------------------------------------- real-money test ([live] in config; fixed fallbacks)
const RT_WALLET = "5Tdn5tRELLVAVBj5i7DK3BherBWKx82K4siovd6tjbFm";  // public address of the dedicated test wallet
function realTest(d) {
  const L = ((d && d.config) || {}).live, has = L && typeof L === "object";
  const LV = d && d.live && typeof d.live === "object" && !d.live.error ? d.live : {};  // bot's live block wins (budget, size, max open)
  const v = (k, fb) => LV[k] != null && LV[k] !== "" && !isNaN(Number(LV[k])) ? Number(LV[k]) : has && L[k] != null && L[k] !== "" && !isNaN(Number(L[k])) ? Number(L[k]) : fb;
  const bpsList = (x) => Array.isArray(x) ? x.map(Number).filter(y => !isNaN(y) && y > 0) : [];
  const sell = has ? bpsList(L.sell_slippage_bps) : [];
  const buyL = has ? bpsList(L.buy_slippage_bps) : [];  // a list now: first try, then retries on a slippage reject
  const buyList = buyL.length > 1 ? buyL.map(x => x / 100) : null;
  // No lifetime real-spending limit when the bot says budget_unlimited; if that flag is missing, the old budget display is used
  const unlimited = LV.budget_unlimited != null ? LV.budget_unlimited === true : (has && L.budget_unlimited != null ? L.budget_unlimited === true : false);
  return {fromCfg: !!has || Object.keys(LV).length > 0, on: LV.active != null ? !!LV.active : has && L.enabled != null ? !!L.enabled : true, trade: v("trade_usd", 5), budget: v("budget_usd", 50), unlimited, noBudget: !unlimited && !(v("budget_usd", 50) > 0),
    spent: num(LV.spent_usd), left: num(LV.budget_left_usd) ?? (num(LV.spent_usd) != null && v("budget_usd", 0) > 0 ? Math.max(v("budget_usd", 0) - num(LV.spent_usd), 0) : null), maxOpen: v("max_open", null),
    buySlip: buyList ? buyList[0] : (num(LV.buy_slippage_bps) ?? (has && !Array.isArray(L.buy_slippage_bps) ? num(L.buy_slippage_bps) : null) ?? 300) / 100,
    buyRetries: buyList ? buyList.slice(1) : [6, 10], buySlipFromCfg: !!buyList, dstop: v("daily_stop_pct", 10), funded: v("funded_usd", 83.47), sellSlip: (sell.length ? sell : [300, 800, 1500]).map(x => x / 100), reserve: v("min_sol_reserve", 0.02)};
}
const rtMoney = (x) => "$" + (Number.isInteger(x) ? x : x.toFixed(2));
function realTestChips(d) {
  const r = realTest(d);
  const chip = $("realchip");
  const t = r.on ? `Real: ${rtMoney(r.trade)} per buy, ${r.unlimited ? "no limit" : r.noBudget ? "" : rtMoney(r.budget) + " max"}`.replace(/, $/, "") : "Real-money test: off";
  if (chip && chip.textContent !== t) chip.textContent = t;
  const pl = document.querySelector(".paperline");
  const ph = r.on ? `Paper trading plus a small real-money test (${rtMoney(r.trade)} per buy${r.unlimited ? ", no spending limit" : r.noBudget ? "" : ", " + rtMoney(r.budget) + " max"}). Figures are paper (fake money) unless marked REAL.`
    : "Paper trading: fake money. The real-money test is switched off. Figures are paper unless marked REAL.";
  if (pl && pl.innerHTML !== ph) pl.innerHTML = ph;
  const fr = $("footreal");
  const ft = r.on ? `Paper trading plus a small real-money test (${rtMoney(r.trade)} per buy${r.unlimited ? ", no spending limit" : r.noBudget ? "" : `, ${rtMoney(r.budget)} limit in total${r.left != null ? `, ${usd(r.left)} left` : ""}`})` : "Paper trading (real-money test off)";
  if (fr && fr.textContent !== ft) fr.textContent = ft;
}

// ---------------------------------------------------------------- REAL money (data.json "live" block, read-only)
// Built only from the bot's published data: live.{active, wallet, wallet_sol, wallet_usd, total_est_usd, funded_usd, budget_*,
// spent_usd, open[], trades[]}. No keys, no signing, no RPC from the browser.
const SOLSCAN = "https://solscan.io";
const b58 = (x) => typeof x === "string" && /^[1-9A-HJ-NP-Za-km-z]{32,90}$/.test(x);
const realTag = `<span class="realtag">REAL</span>`;
function liveInfo(d) {
  const L = d.live && typeof d.live === "object" ? d.live : null;
  if (!L) return {missing: true};
  if (L.error) return {missing: true, error: String(L.error)};
  const cfgL = (d.config || {}).live || {};
  const total = num(L.total_est_usd) ?? (num(L.wallet_usd) != null ? num(L.wallet_usd) + (num(L.coins_est_usd) || 0) : null);
  const funded = num(L.funded_usd), lpnl = total != null && funded ? total - funded : null;
  const pnl = num(L.total_pnl_usd) ?? lpnl, pnlPct = num(L.total_pnl_pct) ?? (pnl != null && funded ? pnl / funded * 100 : null);  // server figures first
  const D = L.day && typeof L.day === "object" ? L.day : null, dStart = D ? num(D.start_usd) : null;
  const today = new Date(((num(d.generated) || Date.now() / 1000)) * 1000).toLocaleDateString("en-CA", {timeZone: TZ});
  const day = D ? {date: D.date, stale: typeof D.date === "string" && /^\d{4}-\d{2}-\d{2}$/.test(D.date) && D.date !== today, start: dStart, stopped: !!D.stopped,
    pct: num(D.pct) ?? (dStart && total != null ? (total / dStart - 1) * 100 : null)} : null;
  const unlimited = L.budget_unlimited != null ? L.budget_unlimited === true : (cfgL.budget_unlimited != null ? cfgL.budget_unlimited === true : false);
  const budget = num(L.budget_usd) ?? num(cfgL.budget_usd), spent = num(L.spent_usd);
  const wallet = b58(L.wallet) ? L.wallet : RT_WALLET;
  return {L, active: L.active != null ? !!L.active : (cfgL.enabled != null ? !!cfgL.enabled : null), wallet,
    walletUrl: `${SOLSCAN}/account/${wallet}`, sol: num(L.wallet_sol), solUsd: num(L.wallet_usd), solPx: num(L.sol_price), wts: num(L.wallet_ts),
    coins: num(L.coins_est_usd), total, funded, pnl, pnlPct, budget, unlimited, spent, realized: num(L.realized_pnl_usd), unrealized: num(L.unrealized_pnl_usd),
    wins: num(L.wins), losses: num(L.losses), day, dstop: num(L.daily_stop_pct) ?? num(cfgL.daily_stop_pct),
    closed: Array.isArray(L.closed) ? L.closed.filter(c => c && typeof c === "object") : [],
    left: unlimited ? null : num(L.budget_left_usd) ?? (budget != null && spent != null ? Math.max(budget - spent, 0) : null), returned: num(L.returned_usd),
    trade: num(L.trade_usd) ?? num(cfgL.trade_usd), maxOpen: num(L.max_open) ?? num(cfgL.max_open),
    open: Array.isArray(L.open) ? L.open.filter(o => o && typeof o === "object") : [], trades: Array.isArray(L.trades) ? L.trades.filter(t => t && typeof t === "object") : []};
}
const onOff = (r) => r.active == null ? pill("", "unknown") : r.active ? `<span class="pill ok">ON</span>` : `<span class="pill">OFF</span>`;
const txLink = (sig, txt = "tx") => b58(sig) ? `<a href="${SOLSCAN}/tx/${esc(sig)}" target="_blank" rel="noopener" class="txl">${txt} ↗</a>` : `<span class="muted">—</span>`;
const tokLink = (sym, mint) => b58(mint) ? `<a href="${SOLSCAN}/token/${esc(mint)}" target="_blank" rel="noopener">${esc(sym || mint.slice(0, 6))}</a>` : esc(sym || "?");
const RT_ST = {ok: ["ok", "done"], pending: ["wait", "pending"], failed: ["bad", "failed"], skipped: ["wait", "skipped"]};
function rtStatus(t) {
  const [c, l] = RT_ST[t.status] || ["", t.status ? String(t.status) : "—"];
  const why = t.status === "failed" ? t.error : t.status === "skipped" ? (t.reason || t.error) : null;
  return pill(c, l) + (why ? ` <span class="small ${t.status === "failed" ? "neg" : "warn"} rt-why">${esc(String(why).slice(0, 160))}</span>` : "");
}
const rtSide = (t) => t.side === "buy" ? pill("ok", "buy") : t.side === "sell" ? pill("info", num(t.frac) != null && t.frac < 0.999 ? `sell ${Math.round(t.frac * 100)}%` : "sell") : pill("", t.side ? String(t.side) : "—");
const rtWallet = (w, cls = "") => `<a class="mono rtw ${cls}" href="${SOLSCAN}/account/${w}" target="_blank" rel="noopener"><span class="rtw-full">${w}</span><span class="rtw-short">${w.slice(0, 6)}…${w.slice(-6)}</span> ↗</a>`;
function budgetBar(r) {
  if (r.unlimited || r.budget == null || !(r.budget > 0) || r.spent == null) return "";
  return `<div class="rbar"><span style="width:${Math.min(100, r.spent / r.budget * 100).toFixed(1)}%"></span></div>`;
}
// Spending tile: "No limit" when the bot reports budget_unlimited, else the old "$X / $Y" budget display
const maxPart = (r) => r.maxOpen != null ? `${r.open.length}/${r.maxOpen} open` : `${r.open.length} open`;
const buysLeft = (left, trade) => left != null && trade > 0 ? Math.floor(left / trade + 1e-9) : null;
function spendTile(r) {
  const per = r.trade != null ? usd(r.trade, 0) + " per buy" : "";
  if (r.unlimited) return {label: "Real spent", value: `${usd(r.spent)} <span class="muted small">· no limit</span>`,
    sub: [per, r.returned != null ? `${usd(r.returned)} back from sells` : ""].filter(Boolean).join(" · ")};
  if (!(r.budget > 0)) return {label: "Real spent", value: usd(r.spent), sub: [per, r.returned != null ? `${usd(r.returned)} back from sells` : ""].filter(Boolean).join(" · ") || "—"};
  return {label: "Budget used", value: `${usd(r.spent)} <span class="muted small">/ ${usd(r.budget, 0)}</span>`, bar: budgetBar(r),
    sub: [`${usd(r.left)} left${buysLeft(r.left, r.trade) != null ? ` (${buysLeft(r.left, r.trade)} buy${buysLeft(r.left, r.trade) === 1 ? "" : "s"})` : ""}`, per].filter(Boolean).join(" · ")};
}
// P&L cell: soft green / soft red, "—" when unknown, "open · est." while the position is still open
function pnlCell(v, pc, open) {
  v = num(v); pc = num(pc);
  if (v == null) return `<span class="muted">—</span>`;
  return `<span class="rpnl ${v > 0 ? "up" : v < 0 ? "down" : ""}">${usd(v)}${pc != null ? ` <small>${pct(pc)}</small>` : ""}</span>${open ? ` <span class="pill est" title="position still open: estimate at the latest price">open · est.</span>` : ""}`;
}
const wl = (r) => r.wins != null || r.losses != null ? `${r.wins ?? 0} win${r.wins === 1 ? "" : "s"} · ${r.losses ?? 0} loss${r.losses === 1 ? "" : "es"}` : "—";
function realDayBadge(r) {
  if (!r.day) return "";
  if (r.day.stopped) return `<span class="daymode hard" title="Real wallet's own daily stop${r.dstop != null ? ` (−${r.dstop}%)` : ""}"><i>■</i>real daily stop hit, no real buys until midnight</span>`;
  if (r.day.pct == null) return "";
  const dl = r.day.stale ? `real since ${new Date(r.day.date + "T12:00:00Z").toLocaleDateString("en-US", {month: "short", day: "numeric", timeZone: "UTC"})} start` : "real today";
  return `<span class="daymode ${r.day.pct < 0 ? "rneg" : "normal"}" title="Real wallet vs its value at the start of the bot's last recorded real day (${esc(r.day.date || "?")}, Toronto)${r.dstop != null ? `; real buys stop at −${r.dstop}%` : ""}"><i>${r.day.pct < 0 ? "↓" : "↑"}</i>${dl} ${pct(r.day.pct, 2)}${r.day.stale ? "" : " vs start"}${r.dstop != null ? ` · stop −${r.dstop}%` : ""}</span>`;
}
function realSummary(r) {
  return `<div class="rsum"><span>Realized <b class="rpnl ${r.realized > 0 ? "up" : r.realized < 0 ? "down" : ""}">${usd(r.realized)}</b></span><span>Unrealized <b class="rpnl ${r.unrealized > 0 ? "up" : r.unrealized < 0 ? "down" : ""}">${usd(r.unrealized)}</b></span><span>${wl(r)}</span></div>`;
}
function realTradeRows(trades, now, full) {
  return trades.map(t => `<tr><td class="col-time">${tfmt(num(t.ts), true)}</td><td class="col-side">${rtSide(t)}</td><td class="col-token">${tokLink(t.symbol, t.mint)}</td>
    <td class="num col-usd">${num(t.usd) != null ? usd(t.usd) : "—"}</td><td class="num col-pnl">${pnlCell(t.position_pnl_usd, t.position_pnl_pct, t.position_open === true)}</td><td class="col-status">${rtStatus(t)}${fillLine(t)}</td>${full ? `<td class="col-reason small muted">${esc(t.side === "sell" && t.reason && t.status !== "skipped" ? t.reason : "")}</td>` : ""}<td class="col-tx">${txLink(t.sig)}</td></tr>`);
}
// new-coin trailing stop (config.paper.trailing_activate_pct / trailing_stop_pct); "was 30%" note only while activation is 20
function trailInfo(P, bold) {
  const a = num(P && P.trailing_activate_pct), t = num(P && P.trailing_stop_pct), b = (v) => bold ? `<b class="cv">${v}</b>` : v;
  const was = a === 20 ? " (was 30% until Oct 3)" : "";
  const txt = a != null && t != null ? `once a trade has been up ${b(a + "%")}${was}, it sells if the price falls ${b(t + "%")} from its peak` : "once a trade is up enough, it sells if the price falls a set amount from its peak";
  const worst = a != null && t != null ? ((1 + a / 100) * (1 - t / 100) - 1) * 100 : null;  // exit level if the peak is exactly the activation gain
  return {a, t, was, txt, worst};
}
// real fill vs paper price (live.trades[]: fill_price_usd, quoted_price_usd, slippage_pct, paper_price, vs_paper_pct; positive vs_paper = worse than paper)
function fillLine(t) {
  const sl = num(t.slippage_pct), vp = num(t.vs_paper_pct);
  if (sl == null && vp == null) return "";
  const tip = [num(t.fill_price_usd) != null ? `fill ${price(t.fill_price_usd)}` : "", num(t.quoted_price_usd) != null ? `quoted ${price(t.quoted_price_usd)}` : "", num(t.paper_price) != null ? `paper ${price(t.paper_price)}` : ""].filter(Boolean).join(" · ");
  const vc = vp == null ? "" : vp > 0.05 ? "worse" : vp < -0.05 ? "better" : "same";
  const fmt = (v) => (v > 0 ? "+" : v < 0 ? "−" : "") + Math.abs(v).toFixed(Math.abs(v) >= 10 ? 0 : 1) + "%";
  return `<div class="fq"${tip ? ` title="${esc(tip + (vp != null ? `. vs paper: ${t.side === "sell" ? "sold" : "bought"} ${vp > 0 ? "worse" : "better"} than the paper price` : ""))}"` : ""}>${sl != null ? `<span class="fq-s">slippage ${fmt(sl)}</span>` : ""}${vp != null ? `<span class="fq-v ${vc}">vs paper ${fmt(vp)}</span>` : ""}</div>`;
}
function fillSummary(trades) {
  const v = trades.filter(t => t.status === "ok").map(t => num(t.vs_paper_pct)).filter(x => x != null);
  if (!v.length) return null;
  const s = [...v].sort((a, b) => a - b), med = s.length % 2 ? s[(s.length - 1) / 2] : (s[s.length / 2 - 1] + s[s.length / 2]) / 2;
  const norm = v.filter(x => Math.abs(x) < 50), avg = norm.length ? norm.reduce((a, b) => a + b, 0) / norm.length : null;
  const sl = trades.filter(t => t.status === "ok").map(t => num(t.slippage_pct)).filter(x => x != null);
  return {n: v.length, med, avg, nNorm: norm.length, out: v.length - norm.length, slAvg: sl.length ? sl.reduce((a, b) => a + b, 0) / sl.length : null, slN: sl.length};
}
const RT_COLS = [["time", "Time"], ["side", "Side"], ["token", "Token"], ["usd", "USD", 1], ["pnl", "Position P&L", 1], ["status", "Status"]];
function realCard(d) {
  const el = $("realcard"); if (!el) return;
  const r = liveInfo(d);
  const head = `<h2><span>💵 Real money</span> ${realTag}${r.missing ? "" : " " + onOff(r)}<a href="#wallet" class="small more">Wallet details</a></h2>`;
  if (r.missing) { el.innerHTML = head + `<div class="empty">Real wallet data unavailable${r.error ? "" : " in this data"}. Paper figures above are not affected.</div>`; return; }
  const pc = r.pnl != null ? cls(r.pnl) : "";
  const db = realDayBadge(r);
  el.innerHTML = head + (db ? `<div class="rdayrow">${db}</div>` : "") + `<div class="rgrid">
      <div class="rtile rmain"><div class="label">Total real P&amp;L</div><div class="value ${pc}">${r.pnl != null ? usd(r.pnl) : "—"}${r.pnlPct != null ? ` <span class="rpct ${pc}">${pct(r.pnlPct, 1)}</span>` : ""}</div>
        <div class="sub">vs ${r.funded ? usd(r.funded) : "the"} deposit, incl. network fees</div>${realSummary(r)}</div>
      <div class="rtile"><div class="label">Real wallet total</div><div class="value">${usd(r.total)}</div>
        <div class="sub">${r.sol != null ? `${r.sol.toLocaleString("en-US", {maximumFractionDigits: 4})} SOL` : "SOL —"}${r.coins ? ` + coins ~${usd(r.coins)}` : ""}${r.wts ? ` · ${ago(r.wts, d.generated)} ago` : ""}</div></div>
      ${(() => { const s = spendTile(r); return `<div class="rtile"><div class="label">${s.label}</div><div class="value">${s.value}</div>
        ${s.bar || ""}<div class="sub">${s.sub} · ${maxPart(r)}</div></div>`; })()}
    </div>
    <div class="rrecent">${r.trades.length ? `<div class="small muted rlh">Latest real trades · position P&amp;L</div><div class="rlist">${r.trades.slice(0, 5).map(t => `<div class="rrow"><span class="rt-t">${tfmt(num(t.ts), true)}</span>${rtSide(t)}<b class="rt-s">${tokLink(t.symbol, t.mint)}</b><span class="rt-u">${num(t.usd) != null ? usd(t.usd) : "—"}</span><span class="rt-p">${pnlCell(t.position_pnl_usd, t.position_pnl_pct, t.position_open === true)}</span><span class="rt-st">${rtStatus(t)}</span><span class="rt-x">${b58(t.sig) ? txLink(t.sig) : ""}</span></div>`).join("")}</div>` : `<div class="muted small">No real trades yet.</div>`}</div>
    <div class="rfoot small muted">Test wallet ${rtWallet(r.wallet)} · real money, separate from the paper figures</div>`;
}
function walletView(d) {
  const el = $("walletview"); if (!el) return;
  const r = liveInfo(d);
  if (r.missing) { el.innerHTML = `<div class="card realcard"><h2>💵 Real wallet ${realTag}</h2><div class="empty">Real wallet data unavailable${r.error ? "" : " in this data"}. Check the wallet on <a href="${SOLSCAN}/account/${RT_WALLET}" target="_blank" rel="noopener">Solscan ↗</a>.</div></div>`; return; }
  const pc = r.pnl != null ? cls(r.pnl) : "";
  const tile = (l, v, sub) => `<div class="stat rstat"><div class="label">${l}</div><div class="value">${v}</div><div class="sub">${sub}</div></div>`;
  const openVal = r.open.reduce((a, o) => a + (num(o.est_value_usd) || 0), 0);
  const sgn = (v) => v == null ? "" : v > 0 ? "up" : v < 0 ? "down" : "";
  const db = realDayBadge(r);
  el.innerHTML = `<div class="card realcard rhead"><h2><span>💵 Real wallet</span> ${realTag} ${onOff(r)}</h2>
      ${db ? `<div class="rdayrow">${db}</div>` : ""}
      <div class="rtotal"><span class="label">Total real P&amp;L</span> <b class="${pc}">${r.pnl != null ? usd(r.pnl) : "—"}</b>${r.pnlPct != null ? ` <span class="${pc}">${pct(r.pnlPct, 1)}</span>` : ""} <span class="muted small">vs ${r.funded ? usd(r.funded) : "the"} deposit, incl. network fees</span></div>
      ${realSummary(r)}
      <p class="small muted">Read-only view of the real-money test wallet: real money, kept separate from the paper figures on the other screens. Figures come from the bot's own records; the SOL balance is refreshed at most once a minute${r.wts ? ` (last ${tfmt(r.wts, true)})` : ""}. Open coins are valued at the paper position's latest price (estimate).</p>
      <div class="small">Wallet ${rtWallet(r.wallet)}</div></div>
    <div class="stats stats4">
      ${tile("Realized P&amp;L", `<span class="rpnl ${sgn(r.realized)}">${usd(r.realized)}</span>`, "finished real trades")}
      ${tile("Unrealized P&amp;L", `<span class="rpnl ${sgn(r.unrealized)}">${usd(r.unrealized)}</span>`, r.open.length ? "open real positions (est.)" : "no open real positions")}
      ${tile("Wins / losses", wl(r), `${r.closed.length} finished real trade${r.closed.length === 1 ? "" : "s"}`)}
      ${tile("Real wallet total", usd(r.total), `${r.sol != null ? r.sol.toLocaleString("en-US", {maximumFractionDigits: 6}) + " SOL" : "SOL —"}${r.solPx != null ? ` · SOL ${usd(r.solPx)}` : ""}${r.coins ? ` · coins ~${usd(r.coins)}` : ""}`)}
      ${(() => { const s = spendTile(r); return tile(s.label, s.value, `${s.bar || ""}${s.sub}`); })()}
      ${tile("Returned from sells", usd(r.returned ?? 0), "real USD back from sells")}
      ${tile("Open real positions", r.maxOpen != null ? `${r.open.length} / ${r.maxOpen}` : String(r.open.length), r.open.length ? `est. value ${usd(openVal)}` : "none open")}
      ${tile("Status", onOff(r), r.active ? `real buys on${r.dstop != null ? ` · own daily stop −${r.dstop}%` : ""}` : r.active === false ? "no new real buys; sells still run" : "")}
    </div>
    <div class="card realcard"><h2>Open real positions ${realTag}</h2><div class="tablewrap"><table id="realopen" class="cards"></table></div></div>
    <div class="card realcard"><h2>Finished real trades ${realTag} <span class="muted small">(newest first)</span></h2><div class="tablewrap"><table id="realclosed" class="cards"></table></div></div>
    <div class="card realcard"><h2>Real trades ${realTag} <span class="muted small">(${r.trades.length}, newest first; P&amp;L is the whole position's)</span></h2>${(() => { const f = fillSummary(r.trades); if (!f) return "";
      const c = (x) => x > 0.05 ? "worse" : x < -0.05 ? "better" : "same", sg = (x) => (x > 0 ? "+" : x < 0 ? "−" : "") + Math.abs(x).toFixed(1) + "%";
      return `<div class="fqsum small">Real fill vs paper price: median <span class="fq-v ${c(f.med)}">${sg(f.med)}</span>${f.avg != null ? ` · average <span class="fq-v ${c(f.avg)}">${sg(f.avg)}</span>${f.out ? ` <span class="muted">(${f.out} extreme fill${f.out === 1 ? "" : "s"} of 50%+ left out, e.g. drained pools)</span>` : ""}` : ""} <span class="muted">· ${f.n} ok trade${f.n === 1 ? "" : "s"}${f.slAvg != null ? ` · avg slippage ${sg(f.slAvg)}` : ""}. Plus = worse than paper.</span></div>`; })()}<div class="tablewrap"><table id="realall" class="cards"></table></div></div>`;
  table("realopen", [["token", "Token"], ["cost", "Cost", 1], ["left", "Left", 1], ["value", "Est. value", 1], ["pnl", "P&L", 1], ["opened", "Opened"], ["tx", "Buy tx"]],
    r.open.map(o => { const cost = num(o.cost_usd), lf = num(o.left_frac), v = num(o.est_value_usd);
      const pl = num(o.pnl_usd) ?? (v != null && cost != null && lf != null ? v - cost * lf : null), plp = num(o.pnl_pct) ?? (pl != null && cost ? pl / cost * 100 : null);
      return `<tr><td class="col-token">${tokLink(o.symbol, o.mint)}${jupChip(o)}</td><td class="num col-cost">${usd(cost)}</td><td class="num col-left">${lf != null ? Math.round(lf * 100) + "%" : "—"}</td>
        <td class="num col-value">${usd(v)}</td><td class="num col-pnl">${pnlCell(pl, plp, true)}</td><td class="col-opened">${tfmt(num(o.opened_at), true)}</td><td class="col-tx">${txLink(o.sig)}</td></tr>`; }),
    "No open real positions.");
  table("realclosed", [["token", "Token"], ["opened", "Opened"], ["closedt", "Closed"], ["cost", "Cost", 1], ["ret", "Returned", 1], ["pnl", "P&L", 1], ["reason", "Exit reason"], ["tx", "Buy tx"]],
    r.closed.map(c => `<tr><td class="col-token">${tokLink(c.symbol, c.mint)}${jupChip(c)}</td><td class="col-opened">${tfmt(num(c.opened_at), true)}</td><td class="col-closedt">${tfmt(num(c.closed_at), true)}</td>
      <td class="num col-cost">${usd(c.cost_usd)}</td><td class="num col-ret">${usd(c.returned_usd)}</td><td class="num col-pnl">${pnlCell(c.pnl_usd, c.pnl_pct, false)}</td>
      <td class="col-reason small">${esc(c.exit_reason || "—")}</td><td class="col-tx">${txLink(c.buy_sig)}</td></tr>`),
    "No finished real trades yet.");
  table("realall", [...RT_COLS, ["reason", "Reason"], ["tx", "Tx"]], realTradeRows(r.trades, d.generated, true), "No real trades yet.");
}

// ---------------------------------------------------------------- home
function kpis(d) {
  const k = d.kpi, now = dataNow(d), s = scanInfo(d);
  const paused = k.manual_pause || k.paused;
  HEALTH = s.state === "bad" ? "bad" : paused || s.state === "wait" ? "wait" : "ok";
  $("healthdot").className = "dot " + (HEALTH === "ok" ? "" : HEALTH);
  if ($("moredot")) $("moredot").className = "dot " + (HEALTH === "ok" ? "" : HEALTH);
  const dm = dayMode(d);
  $("tbstatus").innerHTML = dm.mode === "hard" ? pill("bad", dm.known ? "hard stop" : "paused: loss cap") : k.manual_pause ? pill("wait", "entries paused")
    : dm.mode === "soft" ? pill("wait", "soft cap") : s.pill;
  const pb = $("pausebanner");
  pb.classList.remove("soft");
  if (k.manual_pause) { pb.textContent = "New paper entries are PAUSED (Telegram /pause). Open positions are still managed. Send /resume to the bot to allow new entries."; pb.classList.remove("hidden"); }
  else if (dm.mode === "hard") { pb.textContent = (dm.known ? `Daily hard stop hit${dm.hardPct != null ? ` (−${dm.hardPct}%)` : ""}` : "Daily loss cap reached") + `: no new paper entries until midnight (Toronto)${dm.known ? ", even if the day recovers" : ""}. Open positions are still managed.${liveInfo(d).active ? " Exception: while a real-money buy can still go through, entries continue at normal size (paper and real)." : ""}`; pb.classList.remove("hidden"); }
  else if (dm.mode === "soft") { pb.textContent = `Daily soft cap active${dm.softPct != null ? ` (−${dm.softPct}%)` : ""}: new buys only for score ≥ ${dm.score ?? "the high-score minimum"}, at ${dm.fac != null ? Math.round(dm.fac * 100) + "%" : "reduced"} size. It lifts on its own if the day recovers. Open positions are still managed.${liveInfo(d).active ? " While a real-money buy can go through, entries use the normal rules and size." : ""}`; pb.classList.add("soft"); pb.classList.remove("hidden"); }
  else pb.classList.add("hidden");
  const hl = $("kpi-equity").querySelector(".label");
  if (hl) { const h = `Fake balance ${dayBadge(dm)}`; if (hl.innerHTML !== h) hl.innerHTML = h; }

  $("kpi-equity").querySelector(".value").innerHTML = usd(k.equity);
  $("hero-sub").innerHTML = heroSub(k.pnl, k.pnl_pct, `all time · new coins · started with ${usd(k.start, 0)} · cash ${usd(k.cash)}`);
  const set = (id, v, sub) => { const e = $(id); e.querySelector(".value").innerHTML = v; e.querySelector(".sub").innerHTML = sub; };
  set("kpi-pnl", `<span class="${cls(k.pnl)}">${usd(k.pnl)}</span>`, `<span class="${cls(k.pnl)}">${pct(k.pnl_pct, 2)}</span>`);
  set("kpi-win", num(k.win_rate) == null ? "—" : num(k.win_rate).toFixed(0) + "%", `${k.wins ?? 0} wins / ${k.closed ?? 0} closed`);
  set("kpi-open", `${k.open ?? d.open.length} / ${k.max_open ?? "?"}`, `${k.trades_today ?? 0} trades today`);
  set("kpi-day", `<span class="${cls(k.day_pnl)}">${usd(k.day_pnl)}</span>`,
    k.manual_pause ? `<span class="warn">paused via /pause since ${tfmt(k.manual_pause_since, true)}</span>`
    : dm.mode === "hard" ? `<span class="neg">${dm.known ? (liveInfo(d).active ? "hard stop: paper-only buys off" : "hard stop: no entries today") : "loss cap hit, paused"}</span>${dayMeter(dm)}`
    : dm.mode === "soft" ? `<span class="warn">soft cap: score ≥ ${dm.score ?? "min"} only</span>${dm.hard != null ? ` · <span class="nowrap">hard −${usd(dm.hard)}</span>` : ""}${dayMeter(dm)}`
    : dm.hard != null ? `<span class="nowrap">soft −${usd(dm.soft)}</span> · <span class="nowrap">hard −${usd(dm.hard)}</span>${dayMeter(dm)}` : dm.soft != null ? `loss cap -${usd(dm.soft)} · OK` : "");
  set("kpi-scan", s.pill, s.lastRun ? `last run ${tfmt(s.lastRun)} (${ago(s.lastRun, now)} ago)` : "last run —");
}
// Chart.js colours come from the CSS variables (theme lives in style.css :root)
function chartTheme() {
  const css = getComputedStyle(document.documentElement), v = (k) => css.getPropertyValue(k).trim();
  return {grid: v("--line"), txt: v("--text-muted"), line: v("--accent"), fill: v("--accent-soft"), base: v("--chart-base"),
    tip: {backgroundColor: v("--tip-bg"), borderColor: v("--tip-border"), borderWidth: 1, titleColor: v("--text"), bodyColor: v("--text"), padding: 8, displayColors: false}};
}
const heroSub = (pnl, pc, note) => `<span class="chg ${cls(pnl)}">${pct(pc, 2)}</span><span class="${cls(pnl)}">${usd(pnl)}</span><span class="muted">${note}</span>`;

function equityChart(d) {
  if (typeof Chart === "undefined") return;
  const pts = d.equity.length ? d.equity : [{ts: d.generated, equity: d.kpi.equity}];
  const labels = pts.map(p => new Date(p.ts * 1000).toLocaleString("en-CA", {timeZone: TZ, month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false}));
  const T = chartTheme(), grid = T.grid, txt = T.txt, line = T.line;
  const vals = pts.map(p => p.equity), start = num(d.kpi.start) ?? vals[0];
  const lo = Math.min(...vals, start), hi = Math.max(...vals, start), pad = Math.max(2, (hi - lo) * 0.25);
  const ds = {labels, datasets: [
    {data: vals, borderColor: line, backgroundColor: T.fill, fill: true, pointRadius: 0, borderWidth: 2, tension: 0.2},
    {data: pts.map(() => start), borderColor: T.base, borderDash: [5, 5], pointRadius: 0, borderWidth: 1}]};
  const scales = {x: {ticks: {color: txt, maxTicksLimit: 6, maxRotation: 0}, grid: {color: grid}},
                  y: {min: Math.floor(lo - pad), max: Math.ceil(hi + pad), ticks: {color: txt, callback: v => "$" + v}, grid: {color: grid}}};
  if (chart) { chart.data = ds; chart.options.scales = scales; chart.update("none"); return; }
  chart = new Chart($("eqchart"), {type: "line", data: ds, options: {responsive: true, maintainAspectRatio: false, animation: RM() ? false : {duration: 1100, easing: "easeOutCubic"},
    plugins: {legend: {display: false}, tooltip: {...T.tip, callbacks: {label: c => usd(c.parsed.y)}}}, scales}});
  setTimeout(() => { if (chart) chart.options.animation = false; }, 1400);  // draw in once, then stay still (refreshes, resizes)
}

// trades the bot re-priced after the fact carry "corrected: bad price data" in exit_reason (or notes)
const isCorrected = (p) => /^corrected:/i.test(String(p.exit_reason || "")) || /corrected/i.test(String(p.notes || ""));
const corrBadge = (p) => isCorrected(p) ? `<span class="pill bad cbadge" title="Re-priced by the bot: the original sale used prices with no real trades behind them">corrected</span> ` : "";
function recent(d) {
  const rows = d.closed.slice(0, 6);
  $("recent").innerHTML = rows.length ? rows.map(p => `<div class="rtr" data-cid="${esc(p.id)}">${avatar(p.token, p.symbol)}
      <span class="rt-n"><span class="nm">${link(p.symbol, p.url, p.token)}${jupChip(p)}</span><span class="why" title="${esc(p.exit_reason)}">${corrBadge(p)}${esc(p.exit_reason)}</span></span>
      ${tradeSpark(p) || `<span class="spk"></span>`}<span class="rt-a"><b class="${cls(p.pnl_usd)}">${usd(p.pnl_usd)}</b><span class="small"><span class="${cls(p.pnl_usd)}">${pct(p.pnl_pct)}</span> <span class="muted">· ${tfmt(p.closed_at, true)}</span></span></span></div>`).join("")
    : `<div class="empty">No closed paper trades yet.</div>`;
}

// ---------------------------------------------------------------- trades
const OPEN_COLS = [["token", "Token"], ["price", "Current", 1], ["pnl", "P&L", 1], ["entry", "Entry", 1], ["size", "Size", 1], ["age", "Age"], ["opened", "Opened"], ["targets", "Exit targets"]];
function positions(d) {
  const now = d.generated;
  // Each cell carries a col-* class and data-label; phones show rows as cards (style.css), so no columns are hidden.
  for (const id of ["open", "open2"]) table(id, OPEN_COLS, d.open.map(p => { const t = p.targets || {};
    return `<tr data-pos="${esc(p.id)}"><td class="col-token">${tok(p.symbol, p.url, p.token)}${jupChip(p)}${tradeSpark(p)}</td>
     <td class="num col-price" data-f="price">${price(p.last_price)}</td>
     <td class="num col-pnl ${cls(p.pnl_usd)}" data-f="pnl">${usd(p.pnl_usd)}<br><span class="small">${pct(p.pnl_pct)}</span></td>
     <td class="num col-entry">${price(p.entry_price)}</td>
     <td class="num col-size">${usd(p.cost_usd)}${p.tp1_done && p.qty ? `<br><span class="small muted">${(p.remaining_qty / p.qty * 100).toFixed(0)}% left</span>` : ""}</td>
     <td class="col-age">${ago(p.opened_at, now)}</td><td class="col-opened">${tfmt(p.opened_at, true)}</td>
     <td class="col-targets">TP1 <b>${price(t.tp1)}</b>${p.tp1_done ? " ✓" : ""} · TP2 <b>${price(t.tp2)}</b> · SL <b>${price(t.sl)}</b>${t.trail ? " · trail <b>" + price(t.trail) + "</b>" : ""}<br>max hold until ${tfmt(t.max_hold_until, true)}</td></tr>`; }),
    "No open paper positions right now. The bot buys only when a token passes every filter.");
  const k = d.kpi, cl = d.closed;
  const realized = cl.reduce((a, p) => a + (num(p.pnl_usd) || 0), 0);
  const best = cl.reduce((b, p) => (b == null || num(p.pnl_usd) > num(b.pnl_usd)) ? p : b, null);
  const worst = cl.reduce((b, p) => (b == null || num(p.pnl_usd) < num(b.pnl_usd)) ? p : b, null);
  const tile = (l, v, s) => `<div class="stat"><div class="label">${l}</div><div class="value">${v}</div><div class="sub">${s}</div></div>`;
  $("tradestats").innerHTML = tile("Closed trades", k.closed ?? cl.length, `${k.wins ?? 0} wins · ${(k.closed ?? 0) - (k.wins ?? 0)} losses`)
    + tile("Realized P&L", `<span class="${cls(realized)}">${usd(realized)}</span>`, cl.length < (k.closed ?? 0) ? `last ${cl.length} trades` : "all closed trades")
    + tile("Best trade", best ? `<span class="${cls(best.pnl_usd)}">${usd(best.pnl_usd)}</span>` : "—", best ? `${esc(best.symbol || "")} · ${pct(best.pnl_pct)}` : "")
    + tile("Worst trade", worst ? `<span class="${cls(worst.pnl_usd)}">${usd(worst.pnl_usd)}</span>` : "—", worst ? `${esc(worst.symbol || "")} · ${pct(worst.pnl_pct)}` : "");
  $("closednote").textContent = cl.length ? `(${cl.length})` : "";
  table("closed", [["token", "Token"], ["opened", "Opened"], ["closedat", "Closed"], ["size", "Size", 1], ["entry", "Entry", 1], ["exit", "Exit", 1], ["pnl", "P&L", 1], ["reason", "Exit reason"]],
    cl.map(p => `<tr data-cid="${esc(p.id)}"><td class="col-token">${tok(p.symbol, p.url, p.token)}${jupChip(p)}${tradeSpark(p)}</td><td class="col-opened">${tfmt(p.opened_at, true)}</td><td class="col-closedat">${tfmt(p.closed_at, true)}</td>
     <td class="num col-size">${usd(p.cost_usd)}</td><td class="num col-entry">${price(p.entry_price)}</td><td class="num col-exit">${price(p.last_price)}</td>
     <td class="num col-pnl ${cls(p.pnl_usd)}">${usd(p.pnl_usd)}<br><span class="small">${pct(p.pnl_pct)}</span></td><td class="reasons">${corrBadge(p)}${esc(p.exit_reason)}</td></tr>`),
    "No closed paper trades yet.");
  const isRug = (e) => /rug check failed/i.test(String(e.message || ""));
const KC = {BUY: "ok", SELL: "wait", PAUSE: "bad", RESUME: "info", GUARD: "info", CORRECTION: "bad", SCORE: "score"};
  table("decisions", [["time", "Time"], ["kind", "Action"], ["symbol", "Token"], ["message", "Details"]],
    d.decisions.map(e => `<tr${isRug(e) ? ' class="rugrow"' : ""}><td class="col-time">${tfmt(e.ts, true)}</td><td class="col-kind">${isRug(e) ? `<span class="pill rug">${esc(e.kind || "SKIP")} · rug</span>` : pill(KC[e.kind] ?? "", e.kind || "")}</td><td class="col-symbol">${esc(e.symbol || "")}</td><td class="reasons">${esc(e.message)}</td></tr>`),
    "No trading decisions yet (buys, sells, skips appear here).");
}

// ---------------------------------------------------------------- scanner
const STAGE_LABEL = {data: "Listed on DexScreener", age: "Pool age window", liquidity: "Min liquidity", volume: "Volume 1h/24h",
  market_cap: "Market cap / not already pumped", txns: "Buys & sells present", contract: "Mint/freeze revoked", holders: "Holder concentration",
  holder_trend: "Holders vs price", score: "Score threshold", error: "Errors"};
const STAGES = ["data", "age", "liquidity", "volume", "market_cap", "txns", "contract", "holders", "holder_trend", "score"];
const resPill = (r) => { r = String(r || ""); return r === "pass" ? pill("ok", "PASS") : r.startsWith("reject") ? pill("bad", r.includes("final") ? "REJECT·final" : "REJECT") : pill("wait", "pending"); };

function scanStats(d) {
  const k = d.kpi, c = d.cycles[0];
  const tile = (l, v, s) => `<div class="stat"><div class="label">${l}</div><div class="value">${v}</div><div class="sub">${s}</div></div>`;
  $("scanstats").innerHTML = tile("Tokens scanned today", (k.scanned_today ?? 0).toLocaleString("en-US"), "unique tokens since midnight")
    + tile("Checks today", (k.evals_today ?? 0).toLocaleString("en-US"), "filter runs since midnight")
    + tile("Watchlist", (k.watchlist ?? 0).toLocaleString("en-US"), `newest ${Number(((d.config || {}).scanner || {}).max_watchlist_eval ?? 600).toLocaleString("en-US")} re-checked each cycle until 24h old`)
    + tile("Latest cycle", c ? `#${esc(c.id)}` : "—", c ? `${c.discovered ?? 0} pools found · ${c.new_tokens ?? 0} new` : "waiting for first scan");
}

// per-cycle API calls: GT / DS / JUP / RPC counts, then 429s per source (only the ones present and > 0)
function apiCell(a) {
  if (!a || typeof a !== "object") return "";
  const parts = [["GT", "gt"], ["DS", "ds"], ["JUP", "jup"], ["RPC", "rpc"]].filter(([, k]) => num(a[k]) != null).map(([l, k]) => `${l} ${num(a[k])}`);
  const r429 = [["GT", "gt_429"], ["DS", "ds_429"], ["JUP", "jup_429"]].filter(([, k]) => num(a[k]) > 0).map(([l, k]) => `${l} ${num(a[k])}`);
  return parts.join(" · ") + (num(a.errors) > 0 ? ` · <span class="warn">errors ${num(a.errors)}</span>` : "") + (r429.length ? ` · <span class="warn">429s: ${r429.join(", ")}</span>` : "");
}
// funnel data_source breakdown (jupiter / dexscreener / busy / not_listed / jup_batches_failed), compact, non-zero only
const DS_LABEL = {jupiter: "Jupiter", dexscreener: "DexScreener", busy: "busy (retry next cycle)", not_listed: "not listed", jup_batches_failed: "Jupiter batches failed"};
function dataSourceLine(f) {
  const s = f && f.data_source; if (!s || typeof s !== "object") return "";
  const parts = Object.keys(s).filter(k => num(s[k]) > 0).map(k => `${esc(DS_LABEL[k] || k.replace(/_/g, " "))} ${num(s[k])}`);
  return parts.length ? `<div class="muted small fsrc">Data from: ${parts.join(" · ")}</div>` : "";
}
function funnel(d) {
  const c = d.cycles[0];
  if (!c || !c.funnel) { $("funnelnote").textContent = ""; $("funnel").innerHTML = `<div class="empty">Waiting for the first scan cycle…</div>`; return; }
  const f = c.funnel, rej = f.rejected || {}, pen = f.pending || {}, total = f.evaluated || 1;
  $("funnelnote").textContent = `(cycle #${c.id}, ${tfmt(c.finished)})`;
  let remaining = f.evaluated || 0;
  let html = `<div class="frow total"><span>Tokens checked</span><b>${f.evaluated ?? 0}</b><div class="bar"><i style="width:100%"></i></div></div>${dataSourceLine(f)}`;
  for (const s of STAGES) {
    const drop = rej[s] || 0, pend = pen[s] || 0;
    if (!drop && !pend && remaining === 0) continue;
    remaining -= drop + pend;
    html += `<div class="frow${!drop && !pend ? " zero" : ""}"><span>${esc(STAGE_LABEL[s])}</span>
      <b>${drop ? `<span class="neg">−${drop}</span>` : ""}${pend ? ` <span class="warn">${drop ? "· " : ""}${pend} waiting</span>` : ""}${!drop && !pend ? "0" : ""}</b>
      <div class="bar"><i class="${drop ? "drop" : "pend"}" style="width:${drop || pend ? Math.max(1, (drop + pend) / total * 100) : 0}%"></i></div></div>`;
  }
  html += `<div class="fpass"><span>Passed all filters</span><span>${f.passed ?? 0}</span></div>`;
  html += `<div class="muted small fnote">Red −N = rejected at that step · amber "N waiting" = too young, awaiting data or API budget; re-checked next cycle. Discovered ${c.discovered ?? 0} pools this cycle (${c.new_tokens ?? 0} new tokens).</div>`;
  $("funnel").innerHTML = html;
}

const FEED_N = 40;
function feeds(d) {
  const hideP = $("hidePending").checked, hideL = $("hideLiq").checked;
  const rows = d.feed.filter(e => !(hideP && e.result === "pending") && !(hideL && (e.stage === "liquidity" || e.stage === "data")));
  const shown = feedAll ? rows : rows.slice(0, FEED_N);
  $("feednote").textContent = `(latest cycle: ${d.feed.length} tokens, showing ${rows.length})`;
  table("feed", [["token", "Token"], ["result", "Result"], ["stage", "Stage"], ["liq", "Liq", 1], ["vol", "Vol 1h", 1], ["mcap", "MCap", 1], ["agem", "Age", 1], ["score", "Score", 1], ["reasons", "Reasons"]],
    shown.map(e => { const m = e.metrics || {}, a = num(m.age_min);
      return `<tr><td class="col-token">${tok(e.symbol, e.url, e.token)}${scanSpark(m)}</td><td class="col-result">${resPill(e.result)}</td><td class="col-stage small">${esc(STAGE_LABEL[e.stage] || e.stage)}</td>
      <td class="num col-liq">${big(m.liq)}</td><td class="num col-vol">${big(m.vol_h1)}</td><td class="num col-mcap">${big(m.mcap)}</td>
      <td class="num col-agem">${a == null ? "—" : a < 60 ? Math.round(a) + "m" : (a / 60).toFixed(1) + "h"}</td>
      <td class="num col-score">${e.score ?? "—"}${scoreNewCell(e)}</td><td class="reasons">${esc(e.reasons)}</td></tr>`; }),
    "Nothing to show for the latest cycle with these filters.");
  const mb = $("feedmore");
  if (rows.length > FEED_N) { mb.textContent = feedAll ? `Show first ${FEED_N} only` : `Show all ${rows.length}`; mb.classList.remove("hidden"); }
  else mb.classList.add("hidden");
  table("deep", [["time", "Time"], ["token", "Token"], ["result", "Result"], ["stage", "Stage"], ["score", "Score", 1], ["reasons", "Reasons"]],
    d.deep.map(e => `<tr><td class="col-time">${tfmt(e.ts)}</td><td class="col-token">${tok(e.symbol, e.url, e.token)}</td><td class="col-result">${resPill(e.result)}</td>
      <td class="col-stage small">${esc(STAGE_LABEL[e.stage] || e.stage)}</td><td class="num col-score">${e.score ?? "—"}${scoreNewCell(e)}</td><td class="reasons">${esc(e.reasons)}</td></tr>`),
    "No token has made it past the cheap filters in the last 6 hours.");
}

// ---------------------------------------------------------------- health
function health(d) {
  const k = d.kpi, now = dataNow(d), s = scanInfo(d), c0 = d.cycles[0];
  const dm = dayMode(d);
  const hb = hbTs(d);
  const mode = window.__SNAPSHOT__ ? "single-file snapshot (no refresh, no live prices)" : window.__DATA_URL__ ? "published site · re-reads data every 15s" : "local server · refreshes every 12s";
  const items = [
    ["Scanner", s.pill, s.st.state === "scanning" && s.st.since ? `scanning since ${tfmt(s.st.since)}` + (s.lastRun ? ` · previous run finished ${tfmt(s.lastRun)} (${ago(s.lastRun, now)} ago)` : "")
      : s.lastRun ? `last run ${tfmt(s.lastRun)} (${ago(s.lastRun, now)} ago)${num(s.st.duration) ? ` · took ${dur(s.st.duration)}` : ""} · runs every ${s.interval}s` : ""],
    ["Latest scan cycle", c0 ? pill(c0.passed ? "ok" : "", `#${c0.id}`) : pill("", "none yet"),
      c0 ? `finished ${tfmt(c0.finished)} · took ${dur(c0.finished - c0.started)} · found ${c0.discovered ?? 0} · new ${c0.new_tokens ?? 0} · checked ${c0.evaluated ?? 0} · passed ${c0.passed ?? 0}`
         + ((c0.errors || []).length ? ` · <span class="warn">${c0.errors.length} error(s)</span>` : "") : ""],
    ["Trader (price checks & exits)", hb == null ? pill("", "unknown") : now - hb > 300 ? pill("bad", "stale") : pill("ok", "alive"), hb ? `heartbeat ${tfmt(hb)} (${ago(hb, now)} ago)` : ""],
    OFFLINE.on ? ["Bot status", pill("wait", "offline"), OFFLINE.since != null ? `no heartbeat or scan cycle since ${tfmt(OFFLINE.since, true)} (${ago(OFFLINE.since, now)} ago)` : "no recent heartbeat or scan cycle reported"]
      : ["Bot running since", pill("info", num(d.bot_started) ? ago(d.bot_started, now) : "—"), num(d.bot_started) ? tfmt(d.bot_started, true) : ""],
    ["Last error", s.st.last_error ? pill("bad", "error") : pill("ok", "none"), s.st.last_error ? `<pre>${esc(s.st.last_error)}</pre>` : ""],
    ["Daily loss mode", dm.mode === "hard" ? pill("bad", dm.known ? "hard stop" : "hit, paused") : dm.mode === "soft" ? pill("wait", "soft cap") : pill("ok", dm.known ? "normal" : "OK"),
      `${esc(dm.label)} · today <span class="${cls(dm.dp)}">${usd(dm.dp)}</span>${dm.dpp != null ? ` (${pct(dm.dpp, 2)})` : ""}`
      + (dm.soft != null ? ` · soft cap −${usd(dm.soft)}${dm.softPct != null ? ` (−${dm.softPct}%)` : ""}` : "")
      + (dm.hard != null ? ` · hard stop −${usd(dm.hard)}${dm.hardPct != null ? ` (−${dm.hardPct}%)` : ""}` : "") + " · resets at midnight Toronto"],
    ["New entries", k.manual_pause ? pill("wait", "paused") : dm.mode === "hard" ? pill("bad", "none today") : dm.mode === "soft" ? pill("wait", "high score only") : pill("ok", "allowed"),
      k.manual_pause ? `paused via Telegram /pause since ${tfmt(k.manual_pause_since, true)} · send /resume to allow`
      : (dm.mode === "soft" ? `score ≥ ${dm.score ?? "min"} at ${dm.fac != null ? Math.round(dm.fac * 100) + "%" : "reduced"} size · ` : dm.mode === "hard" ? `${dm.known ? "daily hard stop" : "daily loss cap"} until midnight Toronto · ` : "") + "pause or resume from Telegram with /pause and /resume"],
    ["Live prices", `<span id="h-live">${liveHealth()[0]}</span>`, `<span id="h-live-d">${liveHealth()[1]}</span>`],
    ["Dashboard data", pill("info", window.__SNAPSHOT__ ? "snapshot" : "auto-refresh"), `${mode} · data from ${tfmt(now, true)}`],
  ];
  $("health-list").innerHTML = items.map(([key, p, det]) => `<div class="check"><span class="k">${esc(key)}</span>${p}${det ? `<span class="d">${det}</span>` : ""}</div>`).join("");
  table("cycles", [["id", "#"], ["time", "Finished"], ["found", "Found", 1], ["new", "New", 1], ["checked", "Checked", 1], ["passed", "Passed", 1], ["secs", "Secs", 1], ["api", "API calls"]],
    d.cycles.map(c => { const a = c.api_calls;
      return `<tr><td class="col-id">${esc(c.id)}</td><td class="col-time">${tfmt(c.finished)}</td><td class="num col-found">${c.discovered ?? "—"}</td><td class="num col-new">${c.new_tokens ?? "—"}</td>
      <td class="num col-checked">${c.evaluated ?? "—"}</td><td class="num col-passed ${c.passed ? "pos" : ""}">${c.passed ?? 0}</td><td class="num col-secs">${c.finished && c.started ? (c.finished - c.started).toFixed(0) : "—"}</td>
      <td class="col-api small muted">${apiCell(a)}</td></tr>`; }),
    "No cycles yet.");
}

// ---------------------------------------------------------------- settings
const TITLES = {scoring: "Scoring test (side by side)", established: "Older coins strategy (paper only)", entry: "Pre-buy check (Jupiter fallback)", scanner: "Scanner", filters: "Filters & safety checks", paper: "Paper money, sizing, exits & daily loss limits", guard: "Safety guards (price guard)", live: "Real-money test", rugcheck: "Rug check (RugCheck)", apis: "Data sources & API budget", telegram: "Telegram"};
const PRETTY = (k) => String(k).replaceAll("_", " ");
function fmtVal(v) {
  if (v === true) return `<span class="pos">on</span>`; if (v === false) return `<span class="muted">off</span>`;
  if (v == null) return `<span class="muted">—</span>`;
  if (Array.isArray(v)) return esc(v.join(", ")) || `<span class="muted">none</span>`;
  if (typeof v === "object") return esc(JSON.stringify(v));
  return esc(typeof v === "number" ? v.toLocaleString("en-US", {maximumFractionDigits: 6}) : v);
}
let settingsOpen = null;
function config(d) {
  const skip = new Set(["rpc_endpoints"]);
  const C = d.config || {}, P = C.paper || {}, F = C.filters || {}, S = C.scanner || {};
  const g = (o, k, suf = "") => o[k] == null ? "?" : o[k] + suf;
  $("strategy").innerHTML = [
    ["Finds tokens", `New Solana pools every ${g(S, "scan_interval_sec", "s")}, kept on a watchlist and re-checked until ${g(F, "max_pool_age_hours", "h")} old (newest ${S.max_watchlist_eval != null ? Number(S.max_watchlist_eval).toLocaleString("en-US") : "600"} per scan${String(((C.apis || {}).scan_source) ?? "jupiter").toLowerCase() === "jupiter" ? ", data from Jupiter with DexScreener check" : ""}, DexScreener ${(C.apis || {}).dexscreener_per_min ?? 60}/min).`],
    ["Cheap filters", `Pool age ≥ ${g(F, "min_pool_age_min", " min")}, liquidity ≥ ${big(F.min_liquidity_usd)}, 1h volume ≥ ${big(F.min_volume_h1_usd)}, market cap ${big(F.min_market_cap_usd)}–${big(F.max_market_cap_usd)}${F.max_price_change_h1_pct != null ? `, not up more than ${g(F, "max_price_change_h1_pct", "%")} in the past hour` : ""}, real buys and sells.`],
    ["Safety checks", `${[F.require_mint_revoked && "mint", F.require_freeze_revoked && "freeze"].filter(Boolean).join(" and ").replace(/^./, c => c.toUpperCase()) || "No contract"} authority ${F.require_mint_revoked || F.require_freeze_revoked ? "revoked" : "check"}${F.reject_honeypot ? ", no honeypot flag" : ""}, top wallet ≤ ${g(F, "max_top_holder_pct", "%")}, top 10 ≤ ${g(F, "max_top10_pct", "%")}, holders ≥ ${g(F, "min_holders")}, score ≥ ${g(F, "min_score_to_buy")}.`],
    ["Buys", `Fake ${usd(P.starting_balance_usd, 0)} start, max ${g(P, "max_open_positions")} positions, ${g(P, "position_pct_of_balance", "%")} of balance each, ${g(P, "slippage_pct", "%")} slippage + ${g(P, "fee_pct", "%")} fee assumed.`],
    ["Exits", `+${g(P, "take_profit_pct", "%")} sells ${P.take_profit_sell_fraction != null ? Math.round(P.take_profit_sell_fraction * 100) + "%" : "part"}, +${g(P, "take_profit2_pct", "%")} sells the rest, stop −${g(P, "stop_loss_pct", "%")}, trailing ${g(P, "trailing_stop_pct", "%")} after +${g(P, "trailing_activate_pct", "%")}${trailInfo(P).was ? " (was +30% until Oct 3)" : ""}, max hold ${g(P, "max_hold_hours", "h")}.`],
    ["Real-money test", (() => { const r = realTest(d); return r.on ? `A real ${rtMoney(r.trade)} buy alongside each paper buy, ${r.unlimited ? "no spending limit" : r.noBudget ? "" : rtMoney(r.budget) + " spending limit in total" + (r.left != null ? ` (${rtMoney(Math.round(r.left * 100) / 100)} left)` : "")}${r.maxOpen != null ? `, max ${r.maxOpen} real positions` : ""}, ${r.buySlip}% buy slippage${r.unlimited || r.buySlipFromCfg ? ` (retries at ${r.buyRetries.map(x => x + "%").join(", then ")} if the price moves)` : ""}, own daily stop −${r.dstop}% (paper limits don't block it). Dashboard P&L stays paper only.` : "Switched off. Dashboard P&L is paper only."; })()],
    ["Rug check", (() => { const R = C.rugcheck || {}; return R.enabled === false ? "Off." : `Before every buy (paper and real): RugCheck must answer, pool money (LP) at least ${g(R, "min_lp_locked_pct", "%")} locked or burned${R.reject_danger !== false ? ", no 'danger' warning" : ""}.`; })()],
    ...(d.scoring ? [["Scoring test (paper only)", (() => { const I = scInfo(d), th = scThr(d, I); return `Second $1,000 paper account on the new score (${esc(scModel(I))}): buys at ${th.f(th.nb)}+ (${th.f(th.nCap)}+ at ${th.half} on a bad day) vs the current ${th.f(th.cur)}+ (${th.f(th.curCap)}+). Same coins, checks and exits; never real money. Since ${scWhen(I.started)}.`; })()]] : []),
    ...((d.established || d.strategies) ? [["Older coins (paper only)", (() => { const n = estNums(d); return `Second fake-money strategy on established coins: ${n.ageTxt}, liquidity ≥ ${bigr(n.liq)}, ${n.holders.toLocaleString("en-US")}+ holders, organic score ≥ ${n.f(n.organic)}. Buys a dip that is starting to recover, TP +${n.f(n.tp1)}% / +${n.f(n.tp2)}%, stop −${n.f(n.sl)}%, trailing stop, max hold ${n.holdTxt}. Own ${usd(n.start, 0)} fake balance; never real money.`; })()]] : []),
    ["Daily loss limits", `Soft cap at −${g(P, "daily_loss_cap_pct", "%")}: new buys only for score ≥ ${g(P, "cap_min_score")} at ${P.cap_size_factor != null ? Math.round(P.cap_size_factor * 100) + "%" : "reduced"} size, lifts if the day recovers. Hard stop at −${g(P, "daily_hard_stop_pct", "%")}: no new buys until midnight Toronto.`],
  ].map(([t, x]) => `<div><b>${t}</b>${x}</div>`).join("");
  const box = $("config");
  if (settingsOpen === null) settingsOpen = new Set(Object.keys(C));
  else { settingsOpen = new Set(); box.querySelectorAll("details[open]").forEach(el => settingsOpen.add(el.dataset.k)); }
  box.innerHTML = Object.entries(C).map(([sect, vals]) => `<details data-k="${esc(sect)}"${settingsOpen.has(sect) ? " open" : ""}><summary>${esc(TITLES[sect] || PRETTY(sect))}</summary>
    <div class="kv">${vals && typeof vals === "object" ? Object.entries(vals).filter(([k]) => !skip.has(k) && !/token|secret|password|api_key|private|keypair|seed|mnemonic|wallet_path|key_file/i.test(k)).map(([k, v]) => `<div class="k">${esc(PRETTY(k))}</div><div class="v">${fmtVal(v)}</div>`).join("")
      : `<div class="k">${esc(sect)}</div><div class="v">${fmtVal(vals)}</div>`}</div></details>`).join("")
    || `<div class="empty">No settings in the data.</div>`;
}

// ---------------------------------------------------------------- docs (how decisions are made)
// Written from scanner.py, trader.py, bot.py, common.py, telegram_bot.py and notify.py.
// Numbers come from the live config in the data (d.config) so they stay right when settings change.
const FC_ICON = {step: "", decision: "?", start: "▶", buy: "$", end: "■", hold: "↻"};
// steps: [{t: "step"|"decision"|"start"|"buy"|"end"|"hold", title, text, br: [[kind, label, text], ...], link: "label on the arrow below"}]
function flow(steps, cap) {
  const out = steps.map((s, i) => {
    const node = `<div class="fc-node ${s.t || "step"}">${FC_ICON[s.t] ? `<span class="fc-ic">${FC_ICON[s.t]}</span>` : ""}<div class="fc-tx"><b>${s.title}</b>${s.text ? `<small>${s.text}</small>` : ""}</div></div>`;
    const br = (s.br || []).map(([k, l, x]) => `<div class="fc-br ${k}"><span class="fc-bl">${l}</span><span>${x}</span></div>`).join("");
    const link = i < steps.length - 1 ? `<div class="fc-link"><i></i>${s.link ? `<span>${s.link}</span>` : ""}</div>` : "";
    return `<div class="fc-row${br ? " has-br" : ""}">${node}${br ? `<div class="fc-brs">${br}</div>` : ""}</div>${link}`;
  }).join("");
  return `<figure class="fc">${out}${cap ? `<figcaption>${cap}</figcaption>` : ""}</figure>`;
}
const DOC_SECTIONS = [["doc-overview", "Overview"], ["doc-realtest", "Real-money test"], ["doc-sources", "Where coins come from"], ["doc-filters", "Filters & safety"], ["doc-score", "The score"],
  ["doc-buy", "The buy decision"], ["doc-manage", "Managing a position"], ["doc-guards", "Safety guards"], ["doc-risk", "Risk & health"],
  ["doc-older", "Older coins"], ["doc-scoring", "Scoring test"], ["doc-corrections", "Data corrections"], ["doc-glossary", "Glossary"]];
let docsKey = null;
function docs(d) {
  const SCk = d.scoring && typeof d.scoring === "object" ? d.scoring : {};
  const key = JSON.stringify(d.config) + "|" + Math.round((num(d.kpi.equity) || 0)) + "|" + JSON.stringify([SCk.thresholds, SCk.model_version, SCk.selftest, SCk.started, SCk.enabled, !!SCk.current]);
  if (key === docsKey) return; docsKey = key;
  const C = d.config || {}, S = C.scanner || {}, F = C.filters || {}, P = C.paper || {}, A = C.apis || {}, GD = C.guard || {};
  const TR = trailInfo(P, true);
  // value helpers: fall back to plain words when a setting is missing
  const has = (o, k) => o && o[k] != null && o[k] !== "";
  const m = (o, k, fb) => has(o, k) ? `<b class="cv">${usd(o[k], 0)}</b>` : fb;             // money
  const p = (o, k, fb) => has(o, k) ? `<b class="cv">${o[k]}%</b>` : fb;                   // percent
  const n = (o, k, fb, unit = "") => has(o, k) ? `<b class="cv">${Number(o[k]).toLocaleString("en-US")}${unit}</b>` : fb;  // number
  const sec = (o, k, fb) => has(o, k) ? `<b class="cv">${o[k]} seconds</b>` : fb;
  const on = (o, k, dflt = true) => has(o, k) ? !!o[k] : dflt;
  const frac = has(P, "take_profit_sell_fraction") ? `<b class="cv">${Math.round(P.take_profit_sell_fraction * 100)}%</b>` : "part";
  const costSide = has(P, "slippage_pct") && has(P, "fee_pct") ? `<b class="cv">${(+P.slippage_pct + +P.fee_pct).toFixed(1)}%</b>` : "a small cost";
  const eq = num(d.kpi.equity);
  // [guard] settings (config.toml, read live). Missing values fall back to plain text with the defaults.
  const gx = has(GD, "max_jump_up_factor") ? `<b class="cv">${GD.max_jump_up_factor}×</b>` : "5×";
  const gp = (k, fb) => p(GD, k, fb);
  const gmin = (k, fb) => has(GD, k) ? `<b class="cv">${Math.round(GD[k] / 60 * 10) / 10} minutes</b>` : fb;
  const gOn = GD.enabled !== false, gt2 = on(GD, "use_second_source"), vConfirm = on(GD, "volume_decay_confirm"), fillCap = on(GD, "fill_cap");
  const gWin = gmin("confirm_window_sec", "5 minutes"), gTol = gp("confirm_tolerance_pct", "25%");
  // two-tier daily loss limits (paper.*)
  const softC = p(P, "daily_loss_cap_pct", "5%"), hardC = p(P, "daily_hard_stop_pct", "10%");
  const RC = C.rugcheck || {}, rcOn = RC.enabled !== false, rcLp = p(RC, "min_lp_locked_pct", "90%"), rcDanger = RC.reject_danger !== false, rcClosed = RC.fail_closed !== false;
  const capScore = n(P, "cap_min_score", "75"), capSize = has(P, "cap_size_factor") ? `<b class="cv">${Math.round(P.cap_size_factor * 100)}%</b>` : "50%";
  const EN = C.entry || {}, fbOn = !has(EN, "prebuy_fallback") || String(EN.prebuy_fallback).toLowerCase() !== "off";   // config.entry.*
  const fbTr = realTest(d).trade;   // live.trade_usd / config.live.trade_usd (RT is declared further down)
  const fbT = fbTr != null ? `<b class="cv">${rtMoney(fbTr)}</b>` : "$5", fbD = has(EN, "fallback_depth_usd") ? `<b class="cv">$${Number(EN.fallback_depth_usd).toLocaleString("en-US")}</b>` : "$30";
  const fbTp = fbTr != null ? rtMoney(fbTr) : "$5", fbDp = has(EN, "fallback_depth_usd") ? "$" + Number(EN.fallback_depth_usd).toLocaleString("en-US") : "$30";
  const fbText = `Before every buy the bot re-checks the coin on DexScreener. If DexScreener is busy, it checks with Jupiter instead: a fresh price reading run through the same filters, plus two test quotes (${fbT} and ${fbD}) to confirm the pool is deep enough. These quotes are never executed. RugCheck and all other safety rules still apply, and these buys are labelled 'checked via Jupiter quote'.`;
  const fbLimits = [has(EN, "fallback_max_trade_impact_pct") ? `the ${fbT} quote may cost at most ${p(EN, "fallback_max_trade_impact_pct", "")} vs Jupiter's price (and Jupiter's price impact must stay under that at both sizes)` : "",
    `the extra cost of the ${fbD} quote must point to at least ${m(F, "min_liquidity_usd", "the minimum")} of pool liquidity`,
    has(EN, "fallback_min_jup_liq_factor") ? `Jupiter's own liquidity figure must be at least ${Math.round(EN.fallback_min_jup_liq_factor * 100)}% of the minimum (Jupiter reports about half of DexScreener's figure)` : ""].filter(Boolean);
  // DexScreener pause after a 429 (apis.dexscreener_pause_after_429_sec / _max_sec), doubling
  const dpB = has(A, "dexscreener_pause_after_429_sec") ? Number(A.dexscreener_pause_after_429_sec) : 180, dpM = has(A, "dexscreener_pause_max_sec") ? Number(A.dexscreener_pause_max_sec) : 900;
  const mins = (s) => (s % 60 ? (s / 60).toFixed(1) : s / 60) + " min";
  const dpSeq = (() => { const o = []; for (let s = dpB; s > 0 && o.length < 6; s *= 2) { o.push(Math.min(s, dpM)); if (s >= dpM) break; } return o; })();
  const W = n(S, "max_watchlist_eval", "600");            // newest-N watchlist window (scanner.max_watchlist_eval; 600 fallback)
  // coin-data source: apis.scan_source ("jupiter" = Jupiter first, DexScreener double-check / fallback); missing -> Jupiter text
  const jupOn = !has(A, "scan_source") || String(A.scan_source).toLowerCase() === "jupiter";
  const jbKey = ["jupiter_batch_size", "jupiter_batch", "jup_batch_size"].find(k => has(A, k) || has(S, k));
  const JB = jbKey ? n(has(A, jbKey) ? A : S, jbKey, "100") : "100";
  const cycleT = !has(S, "scan_interval_sec") || Number(S.scan_interval_sec) === 90 ? "about every <span style=\"white-space:nowrap\">45–90 seconds</span>" : `about every ${sec(S, "scan_interval_sec", "")}`;
  const coinData = `<b>Coin data:</b> every round (${cycleT}) the bot re-checks the ${W} newest coins on its watchlist using Jupiter, ${JB} coins per request. Coins that could qualify are double-checked against DexScreener's pool data before the normal filters and score are applied, and if Jupiter is unavailable, DexScreener is used instead.`;
  const vr = has(P, "volume_decay_ratio") ? `<b class="cv">${Math.round(P.volume_decay_ratio * 100)}%</b>` : "a set fraction";
  const vhold = n(P, "volume_decay_min_hold_min", "a minimum time", " min");
  // net P&L of a price move after buy + sell costs: (1+g)·(1−fee)²·(1−slip)/(1+slip) − 1
  const netPct = (g) => has(P, "slippage_pct") && has(P, "fee_pct") ? ((1 + g / 100) * (1 - P.fee_pct / 100) ** 2 * (1 - P.slippage_pct / 100) / (1 + P.slippage_pct / 100) - 1) * 100 : null;
  const tpNet = has(P, "take_profit_pct") ? netPct(+P.take_profit_pct) : null, slNet = has(P, "stop_loss_pct") ? netPct(-P.stop_loss_pct) : null;
  const exSize = eq != null && has(P, "position_pct_of_balance") ? eq * P.position_pct_of_balance / 100 : null;
  const srcs = [`${n(S, "new_pool_pages", "a few", "")} page${S.new_pool_pages == 1 ? "" : "s"} of GeckoTerminal's <i>new Solana pools</i> (20 pools per page)`];
  if (on(S, "include_trending")) srcs.push("GeckoTerminal's <i>trending pools</i> (6-hour view)");
  if (on(S, "include_top_volume")) srcs.push("GeckoTerminal's <i>top pools by 24h volume</i>");
  const RT = realTest(d);
  const rtP = (x, pre = "") => RT.fromCfg ? `<b class="cv">${pre}${x}%</b>` : `${pre}${x}%`;
  const rtC = (x, pre = "$") => RT.fromCfg ? `<b class="cv">${pre === "$" ? rtMoney(x) : x}</b>` : (pre === "$" ? rtMoney(x) : String(x));
  const card = (id, title, body) => `<section class="card doc" id="${id}"><h2>${title}</h2>${body}</section>`;
  const facts = (rows) => `<div class="facts">${rows.map(([k, v]) => `<div><span>${k}</span><b>${v}</b></div>`).join("")}</div>`;
  const gate = (no, title, rule, why) => `<div class="gate"><span class="gn">${no}</span><div><b>${title}</b><div class="gr">${rule}</div><div class="gw">${why}</div></div></div>`;

  const overview = card("doc-overview", "Overview", `
    <p>Memebot is mainly a <b>paper trader</b>: its trades, rules and every number on this dashboard use fake money.${estInfo(d).E || estInfo(d).sEst ? ` It runs two separate paper strategies: <b>new coins</b> (this page's main subject, and the Home and Trades figures) and <b>older coins</b>, a second fake-money test on established coins (see <b>Older coins</b> below).` : ""}${d.scoring ? ` Since ${scWhen(scInfo(d).started)} a <b>side-by-side scoring test</b> also runs: a second $1,000 paper account buys the same coins on a new score (see <b>Scoring test</b> below).` : ""}${RT.on ? ` Alongside it runs a <b>small real-money test</b>: each paper buy is copied with a real ${rtC(RT.trade)} buy, ${RT.unlimited ? "with no overall spending limit" : RT.noBudget ? "within the limits below" : `up to ${rtC(RT.budget)} in total`} (see <b>Real-money test</b> below).` : " The small real-money test is currently switched off."} Two loops run side by side. The <b>scanner</b> looks for new Solana meme coins every ${sec(S, "scan_interval_sec", "couple of minutes")} and runs each one through a funnel of checks, cheapest first. The <b>position manager</b> re-prices open trades every ${sec(P, "update_interval_sec", "minute or so")} and decides when to sell.</p>
    <p>A coin only gets bought if it clears every check in one scan and there is room for a new position. Most coins stop early, usually at liquidity or pool age.</p>
    ${flow([
      {t: "start", title: "Discover new pools", text: `GeckoTerminal lists, every ${sec(S, "scan_interval_sec", "scan")}`},
      {t: "step", title: "Add to the watchlist", text: `Each new coin is kept on the watchlist. Only the ${W} most recently discovered coins are re-checked each scan; older coins stay listed but are never checked again.`},
      {t: "step", title: "Fetch market data", text: jupOn ? `Jupiter first: prices, liquidity, volume and trades for the newest ${W} coins (${JB} per request). Coins that could qualify are double-checked with DexScreener's pool data; if Jupiter is unavailable, DexScreener is used instead` : `DexScreener prices, liquidity, volume and trades for the newest ${W} coins`},
      {t: "decision", title: "Cheap filters pass?", text: "Listing, pool age, liquidity, volume, market cap, trading activity", br: [["no", "No", "rejected or waiting (most coins stop here)"]], link: "yes"},
      {t: "decision", title: "Safety checks pass?", text: "Mint & freeze authority, honeypot flag, holder concentration", br: [["no", "No", "rejected or waiting"]], link: "yes"},
      {t: "decision", title: "Holder trend OK?", text: "Price isn't rising while holders stay flat", br: [["no", "No", "rejected or waiting"]], link: "yes"},
      {t: "decision", title: `Score at least ${n(F, "min_score_to_buy", "the minimum")}?`, text: "Rule-based score out of 100", br: [["no", "No", "rejected this scan"]], link: "yes"},
      {t: "decision", title: "Room to buy?", text: `Coin not already held, open slots, no daily hard stop, not paused. In soft-cap mode only score ≥ ${capScore}, at ${capSize} size. While a real buy can go through, the paper limits don't block`, br: [["wait", "No", "skipped and logged"]], link: "yes"},
      {t: "decision", title: "Pre-buy re-check passes?", text: `Same pool re-read right before buying: price, liquidity and GeckoTerminal still agree${fbOn ? `. If DexScreener is busy: Jupiter check (price + ${fbTp}/${fbDp} test quotes)` : ""}`, br: [["no", "No", "buy skipped (GUARD)"]], link: "yes"},
      ...(rcOn ? [{t: "decision", title: "Rug check passes?", text: `RugCheck: pool money ≥ ${rcLp} locked or burned, no danger warning`, br: [["no", "No", "SKIP logged: rug check failed"]], link: "yes"}] : []),
      {t: "buy", title: "Paper buy", text: `At the current price plus ${costSide} assumed costs${RT.on ? `; also a real ${rtC(RT.trade)} buy if ${RT.unlimited || RT.noBudget ? "" : "budget, "}${RT.maxOpen != null ? "a real slot, " : ""}enough SOL and the real wallet's daily stop allow` : ""}`},
      {t: "hold", title: "Manage the position", text: `Re-priced every ${sec(P, "update_interval_sec", "update")}; every reading must pass the price guard`},
      {t: "end", title: "Sell", text: "Take profit, stop loss, trailing stop, volume decay, max hold time or rug close"},
    ], "The whole pipeline, from discovery to sale.")}`);

  const wl = `<a class="mono rtw" title="Test wallet on Solscan (public address)" href="https://solscan.io/account/${RT_WALLET}" target="_blank" rel="noopener"><span class="rtw-full">${RT_WALLET}</span><span class="rtw-short">${RT_WALLET.slice(0, 6)}…${RT_WALLET.slice(-6)}</span></a>`;
  const realtest = card("doc-realtest", "Real-money test", `
    <p class="rt-note"><b>Since Sep 26, 2026, 2:20 PM ET</b>, a small <b>real-money</b> test runs alongside paper trading${RT.on ? "" : `. <b>It is switched off in the current settings</b>; the rules below apply when it's on`}.</p>
    <ul class="dl">
      <li><b>Copies the paper bot:</b> every time the paper bot buys, it also makes a real ${rtC(RT.trade)} buy (within the limits below) on Solana (a swap through Jupiter) from a dedicated test wallet. When the paper bot sells, it sells the same share of the real coins.</li>
      <li><b>Paper limits don't block it:</b> the paper daily limits (−${softC} and −${hardC}) no longer block real trades. While paper is capped, a coin that passes every check is still bought, at the normal paper size plus the real buy, as long as the real buy can go through.</li>
      <li><b>Own daily limit:</b> the real wallet has its own daily limit. If its value drops ${rtP(RT.dstop)} below where it started the day (Toronto time), the bot stops making new real buys until midnight. Real sells keep running.</li>
      ${RT.unlimited ? `<li><b>Real-money spending limit: none.</b> Each real buy is ${rtC(RT.trade)}. Real buying only pauses if the wallet drops ${rtP(RT.dstop)} in a day (until midnight Toronto) or the wallet runs low on SOL. If the price moves while a real buy is being sent and the swap is rejected (no money spent), the bot retries with ${RT.buyRetries.map(x => RT.buySlipFromCfg ? `<b class="cv">${x}%</b>` : x + "%").join(" and then ")} slippage.</li>`
        : RT.noBudget ? `<li><b>Each real buy</b> is ${rtC(RT.trade)}; real buys accept at most ${rtP(RT.buySlip)} slippage.</li>`
        : `<li><b>Real-money spending limit: ${rtC(RT.budget)} in total</b>${RT.budget === 130 ? " (set on Oct 2, 2026)" : ""}.${RT.spent != null ? ` <b class="cv">${usd(RT.spent)}</b> spent so far${RT.left != null ? `, leaving <b class="cv">${usd(RT.left)}</b>${RT.trade > 0 ? `, or <b class="cv">${buysLeft(RT.left, RT.trade)}</b> more ${rtC(RT.trade)} buy${buysLeft(RT.left, RT.trade) === 1 ? "" : "s"}` : ""}` : ""}.` : ""} When it's used up, real buying stops. Real buying also pauses if the wallet drops ${rtP(RT.dstop)} in a day (until midnight Toronto) or runs low on SOL. If the price moves while a real buy is being sent and the swap is rejected (no money spent), the bot retries with ${RT.buyRetries.map(x => RT.buySlipFromCfg ? `<b class="cv">${x}%</b>` : x + "%").join(" and then ")} slippage.</li>`}
      ${RT.maxOpen != null ? `<li><b>Open positions:</b> at most ${rtC(RT.maxOpen, "")} real positions at once.</li>` : ""}
      <li><b>Exits:</b> real positions follow the paper exits (see <i>Managing a position</i>), including the trailing stop: ${TR.txt}.</li>
      <li><b>Real fill vs paper:</b> each real trade records the price it actually filled at, Jupiter's quoted price and the paper price at that moment (when known). The Wallet tab shows <i>slippage</i> (fill vs quote) and <i>vs paper</i> on each trade: amber means the real trade got a worse price than paper (paid more on a buy, received less on a sell), green means better. Hover or tap for the prices.</li>
      <li><b>Sells:</b> real sells retry at ${RT.sellSlip.map(x => RT.fromCfg ? `<b class="cv">${x}%</b>` : x + "%").join(", then ")} slippage.</li>
      <li><b>Paper is unchanged:</b> paper trading, its rules and all paper numbers stay the same. <b>All P&amp;L on this dashboard is paper (fake money), except the figures marked REAL</b> on the Wallet tab and the 💵 Real money card.</li>
      <li><b>Telegram:</b> 💵 REAL BUY and REAL SELL alerts with a Solscan link, and a 🚨 alert if a real sell fails. <b>/live</b> shows the status, amount spent and wallet balance. <b>/livestop</b> is the emergency stop for real buys; sells still run. Every Telegram buy alert says whether real money was used ("💵 REAL MONEY: yes" or "📝 Paper only").</li>
      <li><b>Real trades P&amp;L:</b> each finished real trade shows what it cost (${rtC(RT.trade)}), what the sell brought back, and the profit or loss in dollars and percent. The total P&amp;L compares the wallet's current value with the ${rtC(RT.funded)} that was deposited, so it includes Solana network fees.</li>
    </ul>
    <p>The <b>Wallet</b> tab (and the 💵 Real money card on Home) shows this test read-only: total real P&amp;L against the deposit, realized and unrealized P&amp;L, wins and losses, today's real change (or the real daily stop), amount spent${RT.unlimited ? " (no spending limit)" : " and budget"}, open real positions, finished real trades with cost, return and P&amp;L, and every real trade with its Solscan link. Those are the only real-money figures on the site; everything else is paper.</p>
    ${facts([["Per buy", rtC(RT.trade)], ...(RT.unlimited ? [["Spending limit", "None"]] : RT.noBudget ? [] : [["Spending limit", `${rtC(RT.budget)} total`], ...(RT.spent != null ? [["Spent so far", `<b class="cv">${usd(RT.spent)}</b>`]] : []), ...(RT.left != null ? [["Left", `<b class="cv">${usd(RT.left)}</b>${RT.trade > 0 ? ` · ${buysLeft(RT.left, RT.trade)} buys` : ""}`]] : [])]), ...(RT.maxOpen != null ? [["Max real positions", rtC(RT.maxOpen, "")]] : []), ["Real daily stop", rtP(RT.dstop, "−")], ["Buy slippage", [RT.buySlip, ...(RT.unlimited || RT.buySlipFromCfg ? RT.buyRetries : [])].map(x => RT.buySlipFromCfg ? `${x}%` : x + "%").join(" → ")], ["Deposit", rtC(RT.funded)], ["Test wallet", wl]])}`);

  const sources = card("doc-sources", "Where coins come from", `
    <p>Every scan, the bot reads ${srcs.join(", plus ")}${on(S, "include_top_volume") ? "" : ". The top-volume list is switched off right now"}. Wrapped SOL, USDC and USDT are ignored. Any coin it hasn't seen before goes on the <b>watchlist</b>.</p>
    ${jupOn ? `<p class="rt-note jupnote">${coinData}</p>
    <p>The re-checked coins are the <b>newest ${W} watchlist coins</b> (by when the bot first saw them).` : `<p>Each scan round re-checks the <b>newest ${W} watchlist coins</b> (by when the bot first saw them), fetched from DexScreener 30 coins per request.`} For each coin it uses the pool with the most liquidity. Within those ${W}, coins that reached the safety checks on an earlier scan are evaluated first, so they get the limited safety-lookup budget. Coins outside the newest ${W} are not re-checked, even if they had reached the safety checks.</p>
    <p>All data comes from free public sources: ${jupOn ? "Jupiter (main coin data), " : ""}GeckoTerminal, DexScreener and public Solana RPC nodes. The bot spaces its requests out (${jupOn ? `Jupiter ${n(A, "jupiter_per_min", "40/min", "/min")}, ` : ""}GeckoTerminal ${n(A, "geckoterminal_per_min", "a few", "/min")}, DexScreener ${n(A, "dexscreener_per_min", "60/min", "/min")}, RPC ${n(A, "rpc_per_sec", "about one", "/s")}) so it doesn't get blocked.</p>
    ${facts([["Scan interval", sec(S, "scan_interval_sec", "set in config")], ["Coins re-checked per scan", W], ["Coin data source", jupOn ? `Jupiter, ${JB} per request · DexScreener double-check / fallback` : "DexScreener"],
      ...(jupOn ? [["Jupiter requests", n(A, "jupiter_per_min", "40/min", "/min")]] : []), ["DexScreener requests", n(A, "dexscreener_per_min", "60/min", "/min")],
      ["Safety lookups per scan", n(S, "contract_checks_per_cycle", "limited")], ["Holder-lookup retries per scan", n(S, "holder_retries_per_cycle", "limited")]])}
    <p class="note">Settings are re-read at the start of every scan, so edits to config.toml take effect without a restart.</p>`);

  const filters = card("doc-filters", "Filters and safety checks", `
    <p>The checks run in this order and a coin stops at the first one it fails. There are three possible outcomes:</p>
    <ul class="dl"><li><span class="pill wait">waiting</span> the bot needs more time or data and re-checks the coin next scan.</li>
      <li><span class="pill bad">rejected</span> the coin failed this scan but <b>stays on the watchlist</b> and can pass later, but only while it is still among the ${W} most recently discovered coins.</li>
      <li><span class="pill bad">rejected · final</span> the coin is dropped from the watchlist for good.</li></ul>
    <div class="gates">
      ${gate(1, "Listed on DexScreener", `No DexScreener data yet: waits. Still not listed <b>30 minutes</b> after first seen: final reject.`, "No market data means no reliable price, liquidity or volume.")}
      ${gate(2, "Pool age window", `Younger than ${n(F, "min_pool_age_min", "the minimum", " min")}: waits. Older than ${n(F, "max_pool_age_hours", "the maximum", " h")}: final reject. Unknown creation time: waits. The over-${n(F, "max_pool_age_hours", "maximum", " h")} rejection only happens when the coin is re-checked. Coins that have dropped out of the newest ${W} are never aged out.`, "The first minutes are chaos, and the bot hunts for young coins only.")}
      ${gate(3, "Minimum liquidity", `At least ${m(F, "min_liquidity_usd", "the minimum")}. No liquidity at all (bonding curve or unlisted): rejected, final if the pool is over 2 hours old. Below 25% of the minimum and over 1 hour old: final ("dead").`, "Thin pools mean big price impact and are easy to rug.")}
      ${gate(4, "Volume", `1-hour volume at least ${m(F, "min_volume_h1_usd", "the minimum")} and 24-hour volume at least ${m(F, "min_volume_h24_usd", "the minimum")}.`, "No volume means no real interest, and it would be hard to sell.")}
      ${gate(5, "Market cap, not already pumped", `Market cap must be known and between ${m(F, "min_market_cap_usd", "the minimum")} and ${m(F, "max_market_cap_usd", "the maximum")}. The price must not be up more than ${p(F, "max_price_change_h1_pct", "a set amount")} in the last hour.`, `Too small is fragile. Too big means a late entry. The bot skips any coin that's already up more than ${p(F, "max_price_change_h1_pct", "the set amount")} in the past hour, since buying that late usually loses. This applies to paper buys, and real buys only copy paper buys, so it covers them too.`)}
      ${gate(6, "Real buying and selling", `In the last hour, at least ${n(F, "min_buys_h1", "some")} buys and ${n(F, "min_sells_h1", "some")} sells, with sells at least ${n(F, "min_sell_buy_ratio", "a set share")}× the buys. At least ${n(F, "min_txns_h24", "a minimum number of")} trades in 24 hours.`, "Near-zero sells is a classic honeypot sign: people can buy but can't sell.")}
      ${gate(7, "Safety lookup (limited)", `Needs a GeckoTerminal + on-chain lookup, cached for 5 minutes. Only ${n(S, "contract_checks_per_cycle", "a few")} new lookups per scan; if the budget is used up the coin waits.`, "These calls are slow and rate-limited, so they're saved for coins that got this far.")}
      ${gate(8, "Contract safety", `${on(F, "require_mint_revoked") ? "Mint authority must be revoked. " : ""}${on(F, "require_freeze_revoked") ? "Freeze authority must be revoked. " : ""}When only GeckoTerminal's "unknown" is available, it's treated as <i>not</i> revoked (rejected). ${on(F, "reject_honeypot") ? "A GeckoTerminal honeypot flag of \"yes\" is rejected outright (final); \"unknown\" only costs 3 score points." : ""} If both data sources fail, the coin waits and is re-checked.`, "An active mint can print new coins, and an active freeze can lock your tokens.")}
      ${gate(9, "Holder concentration", `Biggest wallet at most ${p(F, "max_top_holder_pct", "a set share")} and top 10 wallets at most ${p(F, "max_top10_pct", "a set share")} of supply. Pool, LP and burn accounts are excluded, read from the Solana chain. At least ${n(F, "min_holders", "a minimum number of")} holders, only checked when a holder count is known. If the RPC refuses the lookup the coin waits (up to ${n(S, "holder_retries_per_cycle", "a few")} retries per scan).`, "A few big wallets can dump on everyone else.")}
      ${gate(10, "Holder trend vs price", `Only when GeckoTerminal has a holder count. It needs two holder counts at least ${n(F, "trend_min_interval_min", "a few", " min")} apart within the last hour; until then the coin waits. Rejected if holders fell more than ${p(F, "trend_max_holder_drop_pct", "a set amount")}, or if the price rose more than ${p(F, "trend_price_up_pct", "a set amount")} while holders grew less than ${p(F, "trend_min_holder_growth_pct", "a set amount")}. With no holder count the check is skipped (and the score is lower).`, "Price going up without new holders usually means one buyer pushing it.")}
      ${gate(11, "Score threshold", `Score must be at least ${n(F, "min_score_to_buy", "the minimum")} out of 100 (see below).`, "Ranks the survivors, so only the best-shaped setups get bought.")}
      ${rcOn ? gate(12, "Rug check (RugCheck), right before every buy", `Runs in the buy step for every buy, paper and real. The bot asks RugCheck and only buys if the pool money (LP) is at least ${rcLp} locked or burned${rcDanger ? ", RugCheck shows no 'danger' warning (a big single holder, top-10 wallets too high, low liquidity, already rugged)" : ""}${rcClosed ? ", and RugCheck is reachable. If the coin can't be checked, it is skipped" : ""}. Skips are logged as SKIP with "rug check failed: …".`, "Unlocked pool money can be pulled by the creator at any time: the classic rug pull.") : ""}
    </div>
    <h3>Where coins get stopped</h3>
    ${flow([
      {t: "start", title: "Coin from the watchlist"},
      {t: "decision", title: "On DexScreener?", br: [["wait", "No", "waits; final reject after 30 min"]], link: "yes"},
      {t: "decision", title: "Pool age in window?", text: `${n(F, "min_pool_age_min", "min", " min")} to ${n(F, "max_pool_age_hours", "max", " h")}`, br: [["wait", "Too young", "waits"], ["no", "Too old", "final reject"]], link: "yes"},
      {t: "decision", title: "Enough liquidity?", text: `≥ ${m(F, "min_liquidity_usd", "minimum")}`, br: [["no", "No", `rejected: liquidity below ${m(F, "min_liquidity_usd", "the minimum")}`]], link: "yes"},
      {t: "decision", title: "Enough volume?", text: `1h ≥ ${m(F, "min_volume_h1_usd", "min")}, 24h ≥ ${m(F, "min_volume_h24_usd", "min")}`, br: [["no", "No", "rejected: volume"]], link: "yes"},
      {t: "decision", title: "Market cap OK, not pumped?", text: `${m(F, "min_market_cap_usd", "min")}–${m(F, "max_market_cap_usd", "max")}, 1h ≤ ${p(F, "max_price_change_h1_pct", "max")}`, br: [["no", "No", "rejected: market cap / late entry"]], link: "yes"},
      {t: "decision", title: "Real buys and sells?", br: [["no", "No", "rejected: possible honeypot / too quiet"]], link: "yes"},
      {t: "decision", title: "Safety lookup available?", br: [["wait", "No", "waits: budget used this scan"]], link: "yes"},
      {t: "decision", title: "Mint & freeze revoked, no honeypot?", br: [["no", "No", "rejected (honeypot = final)"]], link: "yes"},
      {t: "decision", title: "Holders spread out?", text: `top wallet ≤ ${p(F, "max_top_holder_pct", "max")}, top 10 ≤ ${p(F, "max_top10_pct", "max")}`, br: [["wait", "No data", "waits: RPC refused"], ["no", "No", "rejected: concentration"]], link: "yes"},
      {t: "decision", title: "Holder trend healthy?", br: [["wait", "Too early", "waits for a 2nd holder count"], ["no", "No", "rejected: one-buyer shape"]], link: "yes"},
      {t: "decision", title: `Score ≥ ${n(F, "min_score_to_buy", "minimum")}?`, br: [["no", "No", "rejected: score"]], link: "yes"},
      {t: "buy", title: "Passed: buy candidate"},
      ...(rcOn ? [{t: "decision", title: "Rug check at buy time", text: `LP ≥ ${rcLp} locked/burned, no danger flag${rcClosed ? ", RugCheck reachable" : ""}`, br: [["no", "No", "SKIP: rug check failed"]], link: "yes"}, {t: "buy", title: "Bought (paper, plus real if allowed)"}] : []),
    ], "Every gate in order. Amber side pills wait and retry next scan; red ones are rejections.")}`);

  const score = card("doc-score", "The score", `
    <p>Coins that pass every gate get a simple, transparent score out of 100. Each part is listed in the scan feed's reasons column. The weights are fixed in the code, and only the pass mark (${n(F, "min_score_to_buy", "the minimum")}) is a setting.</p>
    <div class="tablewrap"><table class="cards sc"><thead><tr><th>Part</th><th class="num">Max</th><th>How it's earned</th></tr></thead><tbody>
      <tr><td data-label="Part"><b>Liquidity</b></td><td class="num" data-label="Max">20</td><td class="reasons" data-label="How">Grows with liquidity; full marks at 8× the minimum (${has(F, "min_liquidity_usd") ? `<b class="cv">${usd(F.min_liquidity_usd * 8, 0)}</b>` : "8× the minimum"}).</td></tr>
      <tr><td data-label="Part"><b>Volume / liquidity</b></td><td class="num" data-label="Max">15</td><td class="reasons" data-label="How">1-hour volume divided by liquidity; full marks when an hour's volume equals the pool size.</td></tr>
      <tr><td data-label="Part"><b>Buy / sell balance</b></td><td class="num" data-label="Max">15</td><td class="reasons" data-label="How">15 if buys are 1.1–2.5× sells, 10 if 0.9–1.1× or 2.5–4×, otherwise 3.</td></tr>
      <tr><td data-label="Part"><b>Holder growth vs price</b></td><td class="num" data-label="Max">20</td><td class="reasons" data-label="How">Starts at 10 and rises when holder growth beats a quarter of the price rise (falls when it doesn't). 5 if no holder trend is available.</td></tr>
      <tr><td data-label="Part"><b>Concentration</b></td><td class="num" data-label="Max">15</td><td class="reasons" data-label="How">Smaller top wallet scores higher (15 at 0%, 0 at the ${p(F, "max_top_holder_pct", "maximum")} limit).</td></tr>
      <tr><td data-label="Part"><b>Age sweet spot</b></td><td class="num" data-label="Max">10</td><td class="reasons" data-label="How">10 if the pool is 30 min–6 h old; 6 if it is younger than 30 min or 6–12 h old; 3 if older than 12 h.</td></tr>
      <tr><td data-label="Part"><b>Momentum</b></td><td class="num" data-label="Max">5</td><td class="reasons" data-label="How">5 if the 1 h change is above 0% and up to 60%; 2 if above 60% and up to 150%; otherwise 0 (including 0% or falling).</td></tr>
      <tr><td data-label="Part"><b>Penalties</b></td><td class="num" data-label="Max">−</td><td class="reasons" data-label="How">−3 when the honeypot flag is unknown (common on Solana; a "yes" is rejected outright before scoring); −10 if already up more than 150% in 1 h${has(F, "max_price_change_h1_pct") && +F.max_price_change_h1_pct <= 150 ? ` (can't happen now: coins up more than ${p(F, "max_price_change_h1_pct", "")} in 1 h are already skipped)` : ""}.</td></tr>
    </tbody></table></div>`);

  const buy = card("doc-buy", "The buy decision", `
    <p>There is no extra "wait for a dip" step. Once a scan finishes, every coin that passed is a <b>candidate</b>, and candidates are tried from highest score to lowest. Each one goes through these checks:</p>
    ${flow([
      {t: "start", title: "Candidate (passed all filters)", text: "Highest score first"},
      {t: "decision", title: "Same coin not held or recently traded?", text: `Matched by mint, pool${on(GD, "block_same_symbol") ? " and ticker (case-insensitive)" : ""}. No open position, and none closed in the last ${n(P, "reentry_cooldown_hours", "cooldown", " h")}`, br: [["wait", "Held", "SKIP logged: already holding X (position #N)"], ["wait", "Cooldown", "silently skipped"]], link: "yes"},
      {t: "decision", title: "Open slot?", text: `Fewer than ${n(P, "max_open_positions", "the maximum")} open positions`, br: [["wait", "No", "SKIP logged: max open positions"]], link: "yes"},
      {t: "decision", title: "Daily hard stop not hit?", text: `Today's loss hasn't reached −${hardC}${RT.on ? ", or a real buy can still go through (then normal rules and size)" : ""}`, br: [["no", "Hit", "SKIP logged: daily hard floor hit, no new entries today"]], link: "yes"},
      {t: "decision", title: "Soft cap off, or score high enough?", text: `Past −${softC} today, only score ≥ ${capScore} may buy${RT.on ? " (not applied while a real buy can go through)" : ""}`, br: [["wait", "Too low", "SKIP logged: score below the minimum (daily soft cap active)"]], link: "yes"},
      {t: "decision", title: "Not paused from Telegram?", br: [["wait", "No", "SKIP logged: /pause is on"]], link: "yes"},
      ...(rcOn ? [{t: "decision", title: "Rug check (RugCheck) passes?", text: `Pool money ≥ ${rcLp} locked or burned${rcDanger ? ", no danger warning" : ""}${rcClosed ? ", RugCheck reachable" : ""}. Paper and real`, br: [["no", "No", "SKIP logged: rug check failed: …"]], link: "yes"}] : []),
      {t: "decision", title: "Size at least $1?", text: `Smallest of the three limits below; the equity limit is cut to ${capSize} in soft-cap mode`, br: [["no", "No", "SKIP logged: too small"]], link: "yes"},
      ...(gOn && fbOn ? [{t: "decision", title: "DexScreener busy?", text: "The pre-buy re-read of the same pool fails (429 or error, not \"pool not found\")", br: [["alt", "Yes", `→ Jupiter check (price + ${fbTp}/${fbDp} test quotes, never executed); fails → GUARD logged, buy skipped`]], link: "no"}] : []),
      ...(gOn ? [{t: "decision", title: "Pre-buy re-check passes?", text: `${fbOn ? "DexScreener re-read (or the Jupiter check):" : "Same pool re-read:"} price within ${gp("entry_price_tolerance_pct", "20%")} of the scan price, liquidity at least ${m(F, "min_liquidity_usd", "the minimum")}${gt2 ? `, GeckoTerminal agrees within ${gTol}` : ""}`, br: [["no", "No", "GUARD logged: buy skipped"]], link: "yes"}] : []),
      {t: "buy", title: "Paper buy", text: "Logged as BUY and sent to Telegram"},
      ...(RT.on ? [{t: "step", title: `Also a real ${rtC(RT.trade)} buy?`, text: `Only if ${RT.unlimited || RT.noBudget ? "" : `the ${rtC(RT.budget)} budget has room, `}${RT.maxOpen != null ? `fewer than ${rtC(RT.maxOpen, "")} real positions are open, ` : ""}the wallet has enough SOL and the real wallet's own daily stop (−${rtP(RT.dstop)}) isn't hit. If the swap is rejected for slippage (nothing spent), it retries at ${RT.buyRetries.map(x => x + "%").join(", then ")}. Paper daily limits don't block it. Runs in the background; the paper buy never waits for it`, br: [["wait", "No room", "paper buy only"]]}] : []),
    ], "Checks for each candidate, in order.")}
    <h3>Position size</h3>
    <p>The size is the <b>smallest</b> of: ${p(P, "position_pct_of_balance", "a set share")} of current equity (cash plus open positions), ${p(P, "max_pct_of_pool_liquidity", "a set share")} of the pool's liquidity, and the cash available. While the daily soft cap is active, the equity share is multiplied by ${has(P, "cap_size_factor") ? `<b class="cv">${P.cap_size_factor}</b>` : "a reduction factor"} (${capSize} size); the pool and cash limits stay the same, and the buy's Telegram alert says the size was reduced.${gOn ? ` After the pre-buy re-check, the size is capped again at ${p(P, "max_pct_of_pool_liquidity", "the same share")} of the <i>fresh</i> liquidity.` : ""}${exSize != null ? ` With today's equity of <b class="cv">${usd(eq)}</b>, the equity limit works out to about <b class="cv">${usd(exSize)}</b> per trade.` : ""}</p>
    <h3>The simulated fill</h3>
    <p>The bot "buys" at DexScreener's current price plus ${fillCap ? `whichever is larger: ${p(P, "slippage_pct", "the set")} slippage or the pool's own <b>price impact</b> for that size` : `${p(P, "slippage_pct", "some")} slippage`}, and a ${p(P, "fee_pct", "small")} fee reduces the coins received. ${fillCap ? `When selling, the proceeds are the price minus slippage, but <b>never more than the pool could actually pay</b> (a constant-product x·y=k estimate from its liquidity), then minus the fee. Open positions are valued the same way. ` : "The same costs apply again when selling. "}Each trade starts slightly in the red.</p>
    <h3>One position per coin</h3>
    <p>A coin counts as "the same" if its mint address, its pool${on(GD, "block_same_symbol") ? " <b>or</b> its ticker (case-insensitive)" : ""} matches an open position. That blocks copycat tokens with the same name. The ${n(P, "reentry_cooldown_hours", "re-entry", " h")} cooldown after a sale applies to each of these. A blocked candidate is logged as <i>"already holding X (position #N)"</i>.</p>
    ${gOn ? `<h3>Pre-buy re-check</h3>${fbOn ? `<p class="rt-note jupnote" id="doc-prebuy-fallback">${fbText}</p>` : ""}<p>Right before buying, the bot re-reads the <b>same pool</b> from DexScreener. The buy is skipped (logged as GUARD) if it's a different coin, the price moved more than ${gp("entry_price_tolerance_pct", "20%")} from the scan price, liquidity fell under ${m(F, "min_liquidity_usd", "the minimum")}${gt2 ? `, or GeckoTerminal's price differs by more than ${gTol} (if GeckoTerminal has no answer, this part passes)` : ""}. The buy then uses the fresh price and liquidity.</p>${fbOn ? `<p><b>Jupiter check, in detail:</b> it runs only when the DexScreener re-read fails (busy or error), not when the pool isn't found. The fresh Jupiter reading must pass the same filters, holder minimum and ${gp("entry_price_tolerance_pct", "20%")} price check against the scan; then ${fbLimits.join("; ")}. RugCheck is required to answer here (no answer means no buy). If any part fails, the buy is skipped. Positions bought this way carry a <span class="pill jupchk">checked via Jupiter quote</span> label on the Home and Trades screens.</p>` : ""}` : ""}
    <p> Skip messages for the same coin (max positions, daily loss limits, pause) are logged at most once every 30 minutes, so the decision log doesn't fill up with repeats. The "size too small" skip is logged every time; it is not limited to once per 30 minutes.</p>`);

  const manage = card("doc-manage", "Managing a position", `
    <p>Every ${sec(P, "update_interval_sec", "update")}, the position manager fetches the latest DexScreener price for each open trade, updates the highest price seen (the <b>peak</b>), and runs the exit checks below in this order. The first one that triggers wins. Gains are measured on the <b>market price versus the entry price</b>, before costs.${tpNet != null && slNet != null ? ` After the ${costSide} costs on both the buy and the sell, the +${p(P, "take_profit_pct", "TP1")} take-profit nets about <b class="cv">${pct(tpNet)}</b> and the −${p(P, "stop_loss_pct", "stop")} stop about <b class="cv">${pct(slNet)}</b>.` : " Costs on the buy and the sell make the real result a little worse than the trigger level."}</p>
    ${flow([
      {t: "start", title: "Every update: new price", text: "DexScreener reading for the position's pool"},
      ...(gOn ? [{t: "decision", title: "Reading passes the price guard?", text: "Right pool and coin, sane numbers, extreme moves confirmed (see Safety guards)", br: [["wait", "No", "ignored: nothing happens this check"], ["no", "Pool drained", "rug close on 2 checks"]], link: "yes"}] : []),
      {t: "step", title: "Update price and peak"},
      {t: "decision", title: `Down ${p(P, "stop_loss_pct", "the stop amount")} or more?`, br: [["no", "Yes", "sell all: stop loss"]], link: "no"},
      {t: "decision", title: `Up ${p(P, "take_profit2_pct", "TP2")} or more?`, br: [["ok", "Yes", "sell all: take profit 2"]], link: "no"},
      {t: "decision", title: `Up ${p(P, "take_profit_pct", "TP1")} or more, TP1 not taken yet?`, br: [["ok", "Yes", `sell ${frac}: take profit 1, then wait for the next update`]], link: "no"},
      {t: "decision", title: "Trailing stop hit?", text: `Peak was up ≥ ${p(P, "trailing_activate_pct", "the trigger")}${TR.was} and price is ${p(P, "trailing_stop_pct", "a set amount")} or more below the peak`, br: [["no", "Yes", "sell all: trailing stop"]], link: "no"},
      {t: "decision", title: "Volume dying?", text: `Only once the pool is 2 h old and held ${vhold}. Recent volume under ${vr} of its average pace${vConfirm ? ", on 2 checks in a row" : ""}`, br: [["no", "Yes", `sell all: volume decay${vConfirm ? " (confirmed on 2 checks)" : ""}`]], link: "no"},
      {t: "decision", title: `Held ${n(P, "max_hold_hours", "the maximum", " h")} or more?`, br: [["no", "Yes", "sell all: max hold time"]], link: "no"},
      {t: "hold", title: "Keep holding", text: "Checked again next update"},
    ], "Exit checks on every price update. The first match sells.")}
    ${facts([["Take profit 1", `+${p(P, "take_profit_pct", "?")} sells ${frac}`], ["Take profit 2", `+${p(P, "take_profit2_pct", "?")} sells the rest`], ["Stop loss", `−${p(P, "stop_loss_pct", "?")}`],
      ["Trailing stop", TR.a != null && TR.t != null ? `<b class="cv">${TR.t}%</b> below peak after +<b class="cv">${TR.a}%</b>${TR.was ? `<br><span class="muted small">was +30% until Oct 3</span>` : ""}` : "after a set gain"], ["Max hold", n(P, "max_hold_hours", "set in config", " h")]])}
    <h3>The exit rules in plain words</h3>
    <ul class="dl">
      <li><b>Stop loss:</b> sells everything if the price is ${p(P, "stop_loss_pct", "a set amount")} or more below entry. The stop stays at −${p(P, "stop_loss_pct", "the stop level")} from entry after TP1; that's how it works today.</li>
      <li><b>Take profit 1:</b> at +${p(P, "take_profit_pct", "a set gain")} it sells ${frac} of what's left, once per trade.</li>
      <li><b>Take profit 2:</b> at +${p(P, "take_profit2_pct", "a higher gain")} it sells everything left. It's checked before TP1, so if the price jumps straight past it, the whole position is sold at once.</li>
      <li><b>Trailing stop:</b> ${TR.txt}; it sells everything that's left.${TR.a != null && num(P.take_profit_pct) != null && TR.a < num(P.take_profit_pct) ? ` Because it switches on below TP1 (+${p(P, "take_profit_pct", "TP1")}), it can sell the whole position before TP1 is reached${TR.worst != null ? `: a trade that peaks at exactly +${TR.a}% and then drops ${TR.t}% exits at about <b class="cv">${pct(TR.worst, 0)}</b> before costs` : ""}.` : ""} The real wallet copies these exits.</li>
      <li><b>Volume decay:</b> only once the pool is 2 h old and the position has been held ${vhold}. For pools 2–12 h old: sells if the last hour's volume is under ${vr} of (6 h volume ÷ pool age in hours, max 6), the hourly pace over the last 6 h, or over the pool's life if younger. For pools 12 h+: sells if the last 6 h volume is under ${vr} of (24 h volume × 6 ÷ pool age in hours, max 24), which equals 24 h ÷ 4 once the pool is 24 h old. A missing creation time counts as 24 h.${vConfirm ? ` It only sells when <b>two checks in a row</b> (within ${gWin}) both say volume is dying; a single low reading is not enough.` : ""}</li>
      <li><b>Max hold:</b> sells after ${n(P, "max_hold_hours", "a maximum", " h")} no matter what.</li>
      <li><b>Costs:</b> every sale gets the price minus ${p(P, "slippage_pct", "slippage")} slippage and a ${p(P, "fee_pct", "small")} fee, so about ${costSide} per side${fillCap ? ", and never more than the pool could pay (x·y=k estimate). The net figures above assume a small trade in a deep pool, where that cap doesn't bite" : ""}. If no price arrives for a coin, that coin is simply skipped until the next update.</li>
      ${gOn ? `<li><b>Rug close:</b> if the pool is drained on two checks in a row, the position closes as <i>"rug: liquidity pulled"</i> (see Safety guards).</li>` : ""}
    </ul>`);

  const guards = card("doc-guards", "Safety guards", `
    ${gOn ? "" : `<p class="warn"><b>The guard is switched off in the current settings</b>; the rules below describe what it does when it's on.</p>`}
    <p>Meme-coin price feeds sometimes show prices that never really traded: a reading from the wrong pool, a spike with no trades behind it, or a pool that was just emptied. The <b>price guard</b> checks every price reading for an open position before the bot acts on it. Its settings live in the <i>[guard]</i> section of the config and are read live.</p>
    ${flow([
      {t: "start", title: "New price reading", text: `For an open position, every ${sec(P, "update_interval_sec", "update")}`},
      {t: "decision", title: "Same pool and same coin?", br: [["wait", "No", "ignored"]], link: "yes"},
      {t: "decision", title: "Supply looks right?", text: `Market cap ÷ price within ${gp("max_supply_deviation_pct", "50%")} of the known supply`, br: [["wait", "No", "ignored"]], link: "yes"},
      {t: "decision", title: "Pool still has liquidity?", text: `Not down ${gp("liq_drain_pct", "80%")}+ from entry and not under ${m(GD, "min_exit_liquidity_usd", "$1,000")}`, br: [["no", "Drained 2 checks in a row", "rug close at what the pool can pay (≈ $0)"], ["wait", "First time", "held, checked again"]], link: "yes"},
      {t: "decision", title: "Normal move?", text: `Not more than ${gx} the last good price, not more than ${gp("max_drop_pct", "50%")} below it, and not the first reading after a ${n(GD, "stale_gap_min", "10-minute", " min")} gap`, br: [["ok", "Yes", `act on it now (the −${p(P, "stop_loss_pct", "set")} stop fires immediately)`]], link: "no: extreme or after a gap"},
      ...(gt2 ? [{t: "decision", title: "GeckoTerminal agrees?", text: `Its price for the same pool within ${gTol} (only asked for extreme moves)`, br: [["ok", "Yes", "act on it"], ["wait", "Spike it contradicts", "never acted on"]], link: "no answer"}] : []),
      {t: "decision", title: "Next check agrees?", text: `Within ${gTol}, within ${gWin}. A spike up also needs real trades in the last 5 minutes`, br: [["ok", "Yes", "act on it, at most one check late"]], link: "no"},
      {t: "end", title: "Ignored", text: "No price update and no exit decision this check"},
    ], "The price guard, run on every reading for an open position.")}
    <h3>The rules</h3>
    <ul class="dl">
      <li><b>Wrong source:</b> readings from a different pool or token than the position's are ignored. So are readings where market cap ÷ price implies a supply more than ${gp("max_supply_deviation_pct", "50%")} off.</li>
      <li><b>Extreme moves:</b> a reading more than ${gx} the last good price, or more than ${gp("max_drop_pct", "50%")} below it, is only acted on once ${gt2 ? "GeckoTerminal or " : ""}the next check agrees within ${gTol}, within ${gWin}. Smaller drops aren't extreme, so the normal −${p(P, "stop_loss_pct", "stop")} stop still fires immediately. A real crash sells at most one check late.</li>
      <li><b>Spikes up</b> are never accepted if the pool is empty${gt2 ? " or GeckoTerminal contradicts them. Without a GeckoTerminal answer" : ". Otherwise"}, the spike must repeat with real trades in the last 5 minutes.</li>
      <li><b>Rug close:</b> if pool liquidity falls ${gp("liq_drain_pct", "80%")}+ from entry, or below ${m(GD, "min_exit_liquidity_usd", "$1,000")}, on two checks in a row, the position closes as <i>"rug: liquidity pulled"</i>, valued at what the empty pool could pay (about $0).</li>
      <li><b>After an outage:</b> after ${n(GD, "stale_gap_min", "10", " min")}+ with no reading (an outage or freeze), the first reading isn't acted on; the next one is.</li>
      <li><b>Realistic fills${fillCap ? "" : " (switched off)"}:</b> paper sells are capped by what the pool could pay (a constant-product x·y=k estimate from liquidity). Buys pay whichever is larger: ${p(P, "slippage_pct", "the set")} slippage or the pool's price impact. Open positions are valued the same way.</li>
      <li><b>Volume-decay exit:</b> ${vConfirm ? "needs two checks in a row that agree." : "acts on a single check (confirmation switched off)."}</li>
      <li><b>Pre-buy re-check:</b> right before a buy, the same pool is re-read. The price must be within ${gp("entry_price_tolerance_pct", "20%")} of the scan price, liquidity must meet ${m(F, "min_liquidity_usd", "the minimum")}${gt2 ? " and GeckoTerminal must agree" : ""}.${fbOn ? ` If DexScreener is busy, a Jupiter check (fresh reading plus ${fbT} and ${fbD} test quotes, never executed) is used instead; these buys are labelled 'checked via Jupiter quote'.` : ""}</li>
      <li><b>One position per coin:</b> matched by mint, pool${on(GD, "block_same_symbol") ? " and ticker (case-insensitive)" : ""}. The ${n(P, "reentry_cooldown_hours", "re-entry", " h")} cooldown applies to each. A blocked candidate is logged as <i>"already holding X (position #N)"</i>.</li>
    </ul>
    <p class="muted small">Ignored readings are logged as GUARD in the decision log (at most once every 10 minutes per coin), so the log shows when the guard stepped in. The live prices on this dashboard hide extreme moves the same way until the bot confirms them.</p>`);

  const corrections = card("doc-corrections", "Data corrections", `
    <p class="muted small">Sep 26, 2026</p>
    <ul class="dl">
      <li><b>Two trades re-priced.</b> They had sold at prices with no real trades behind them (rug pulls). <b>DDOS #34</b> went from +$6,547.50 to <span class="neg">−$11.13</span>, and <b>UPTOBER #22</b> from +$982.36 to <span class="neg">−$21.14</span>. Both are labeled <i>"corrected: bad price data"</i> on the Trades screen. After the correction, fake cash was $761.54 and equity about $1,050.</li>
      <li><b>Oversized trades.</b> Trades #43–#52 were sized from the inflated balance, about $170 each instead of about $20. Their results are real paper results but ~8× larger than normal sizing would give.</li>
    </ul>`);

  const risk = card("doc-risk", "Risk controls and health", `
    <h3>Daily loss limits (two tiers)</h3>
    <p>On the first position update after midnight Toronto time (within about ${sec(P, "update_interval_sec", "a minute")}), the bot records the day's starting equity. Today's P&L is measured against that, with open positions included at what they'd fetch now. It's checked on every position update and before any buy, and it sets the day's mode:</p>
    ${flow([
      {t: "start", title: "Today's P&L vs starting equity", text: `Checked every ${sec(P, "update_interval_sec", "update")} and before buys`},
      {t: "decision", title: `Down ${hardC} or more (or already hit today)?`, br: [["no", "Yes", `hard stop: no new buys until midnight Toronto${RT.on ? " (unless a real buy can go through)" : ""}`]], link: "no"},
      {t: "decision", title: `Down ${softC} or more?`, br: [["wait", "Yes", `soft cap: only score ≥ ${capScore}, at ${capSize} size`]], link: "no"},
      {t: "hold", title: "Normal", text: "All entries allowed"},
    ], "Daily loss mode. Open positions are managed the same way in every mode.")}
    <ul class="dl">
      <li><b>Soft cap (−${softC}):</b> new buys continue, but only for coins with a score of ${capScore} or more, at ${capSize} of the normal size. Every other limit and safeguard still applies. It lifts on its own as soon as the day recovers above −${softC}.</li>
      <li><b>Hard stop (−${hardC}):</b> no new buys until midnight Toronto time. It stays on even if the day recovers.</li>
      ${RT.on ? `<li><b>Real-money test:</b> these paper limits don't block it. While a real buy can go through (${RT.unlimited || RT.noBudget ? "" : "budget, "}${RT.maxOpen != null ? "slot, " : ""}enough SOL and the real wallet's own −${rtP(RT.dstop)} daily stop), a candidate is still bought at normal rules and size, paper and real. See Real-money test.</li>` : ""}
      <li><b>Open positions</b> are always managed (stops, take-profits, guards), whatever the mode.</li>
      <li><b>Telegram:</b> an alert when the soft cap starts and another when the hard stop hits. Buy alerts say when the size was reduced. At midnight the limits reset, and if either was active a message says normal entries have resumed.</li>
      <li><b>Decision log:</b> PAUSE when a tier starts, RESUME when the soft cap lifts, and SKIP for blocked candidates.</li>
    </ul>
    <h3>Pause and resume</h3>
    <p><b>/pause</b> in Telegram stops new paper buys; open positions keep their stops and take-profits. <b>/resume</b> allows buys again. The manual pause and the daily loss limits are separate: /resume doesn't clear a hard stop or soft cap.</p>
    <h3>Heartbeats</h3>
    <p>The scanner saves its state (scanning, idle or error) and the time of its last run. The position manager saves a heartbeat after every successful update. The Health screen marks either one as stale if it hasn't reported for about 5 minutes. Each process runs under a supervisor that restarts it 10 seconds after it stops and sends a Telegram alert.</p>
    <h3>When things go wrong</h3>
    <ul class="dl">
      <li><b>API hiccups:</b> GeckoTerminal and DexScreener requests are tried up to 3 times after network errors, "too many requests" (429) and server errors (5xx); other errors such as 404 give up at once. After a 429, GeckoTerminal pauses 60 s. <b>DexScreener</b> is paused for <b class="cv">${mins(dpB)}</b> after a 429, doubling up to <b class="cv">${mins(dpM)}</b> while 429s continue${dpSeq.length > 2 ? ` (${dpSeq.map(s => s / 60 % 1 ? (s / 60).toFixed(1) : s / 60).join(" → ")} min)` : ""}; during a pause the watchlist double-checks wait, open positions are priced from Jupiter and buys use the Jupiter pre-buy check. RPC lookups try each configured node once, skipping nodes that failed in the last 20–30 s.${jupOn ? ` <b>Jupiter</b> (the main coin-data source, spaced to ${n(A, "jupiter_per_min", "40/min", "/min")}) is tried up to 2 times per batch; after a 429 the bot waits until Jupiter's limit resets (at most 60 s), and a batch that still fails is fetched from DexScreener instead. Per-cycle Jupiter, DexScreener and GeckoTerminal calls and 429s are listed on the Health screen.` : ""}</li>
      <li><b>A coin's check crashes:</b> that coin is marked as waiting ("error") and the rest of the scan continues.</li>
      <li><b>A whole scan fails:</b> the error is shown on the Health screen. The next attempt starts 90 seconds after the failed scan began (at least 5 seconds after the failure). This fixed 90 s is used instead of the configured scan interval.</li>
      <li><b>Telegram is down:</b> alerts are queued in the database and retried with growing delays (up to 8 attempts), so Telegram can never block trading. Restarts never resend old alerts.</li>
    </ul>`);

  const G = [["Paper trading", "Simulated trades with fake money. Everything on this dashboard is paper unless marked REAL."],
    ...(d.scoring ? (() => { const I = scInfo(d), th = scThr(d, I); return [
      ["Current score", `The score (0–100) the bot has always bought on: it buys at ${th.f(th.cur)}+, or ${th.f(th.curCap)}+ on a bad day. Real-money trades follow it.`],
      ["New score", `${esc(scModel(I))}: a score trained on the bot's own trade history that favours younger pools. A separate paper account buys at ${th.f(th.nb)}+ (${th.f(th.nCap)}+ at ${th.half} on a bad day). Never real money.`],
      ["Side-by-side test", "Two $1,000 paper accounts that see the same coins and safety checks but buy on different scores, so the scores can be compared fairly."],
      ["Expectancy", "Average profit or loss per closed trade (total realized P&L ÷ number of closed trades)."],
      ["Max drawdown", "The largest drop from a high point of the account's equity to a later low, in dollars and percent."],
      ["Win probability (new)", "The new score's own estimate (prob_new) that a trade ends in profit."]]; })() : []),
    ["New coins / Older coins", "The two paper strategies. New coins buys freshly launched meme coins (the main strategy, $1,000 fake start). Older coins buys established coins after a dip that is starting to recover, with its own fake balance. Neither uses real money; only the real-money test does."],
    ["Organic score", "Jupiter's 0–100 estimate of how much of a coin's trading comes from real wallets rather than bots. The Older coins strategy needs a high one."],
    ["Dip and recover", "The Older coins entry: the price fell over the last 6 hours and has just started rising again in the last hour, so it isn't chasing a pump."],
    ["Real-money test", `A small test with real money: a real ${rtMoney(RT.trade)} buy alongside each paper buy, ${RT.unlimited ? "with no overall spending limit (it pauses only on the real daily stop or low SOL)" : RT.noBudget ? "within set limits" : rtMoney(RT.budget) + " in total at most"}, from a dedicated test wallet.`],
    ["Jupiter", jupOn ? `A Solana swap and token-data service. The bot's main source of coin data for the watchlist re-checks (${JB} coins per request, DexScreener as double-check and fallback), and the real-money test buys and sells through it.` : "A Solana swap service. The real-money test buys and sells through it."],
    ["Pool / pair", "A trading pool on a DEX where the coin trades against SOL or a stablecoin."],
    ["Liquidity", "How much money sits in the pool. More liquidity means you can buy or sell without moving the price much."],
    ["Market cap", "Price × supply: the total value of all the coins (DexScreener's figure, or FDV if missing)."],
    ["Volume", "Dollar value traded over a period (1 h, 6 h, 24 h)."],
    ["Honeypot", "A coin you can buy but not sell. Very few sells compared with buys is a warning sign."],
    ["Mint authority", "A key that can create new coins. If it isn't revoked, supply can be inflated at any time."],
    ["Freeze authority", "A key that can freeze holders' tokens. If it isn't revoked, your coins could be locked."],
    ["Top holder / concentration", "Share of supply held by the biggest wallet(s), excluding pool, LP and burn accounts."],
    ["Holder trend", "How the number of holders changes compared with the price over time."],
    ["Score", "A 0–100 rating of the coin's setup; it must reach the pass mark to be bought."],
    ["Waiting (pending)", "Not decided yet: the coin is re-checked next scan."],
    ["Watchlist", `Every coin the bot has discovered. Each scan round re-checks only the newest ${W} of them.`],
    ["Final reject", "The coin is taken off the watchlist and never checked again."],
    ["Equity", "Cash plus what the open positions would fetch if sold now (after costs, capped by what the pool could pay)."],
    ["TP1 / TP2", `First and second take-profit levels: +${p(P, "take_profit_pct", "TP1")} (sells ${frac}) and +${p(P, "take_profit2_pct", "TP2")} (sells the rest).`],
    ["Stop loss", "Automatic sale when the price falls a set amount below the entry price."],
    ["Trailing stop", `A stop that follows the peak: once in profit enough, it sells if the price drops a set amount from its high. New coins (and the real wallet): ${trailInfo(P, false).txt}.${(() => { const n = estNums(d); return n.trail != null && n.trailAct != null && (d.established || d.strategies) ? ` Older coins: ${n.f(n.trail)}% below the peak after +${n.f(n.trailAct)}%.` : ""; })()}`],
    ["Volume decay", "An exit when trading activity fades well below its normal pace."],
    ["Slippage", "The difference between the quoted price and the price you actually get."],
    ["Cooldown", `After trading a coin, the bot won't buy it again for ${n(P, "reentry_cooldown_hours", "a while", " h")}.`],
    ["Daily soft cap", `At −${softC} on the day, only high-score coins (score ≥ ${capScore}) are bought, at ${capSize} size. Lifts when the day recovers.`],
    ["Daily hard stop", `At −${hardC} on the day, no new buys until midnight Toronto, even if the day recovers (paper limits don't block the real-money test).`],
    ["Real daily stop", `The real wallet's own limit: ${RT.dstop}% below its start-of-day value, no new real buys until midnight Toronto. Real sells keep running.`],
    ["RugCheck", "A free service that scores Solana coins for rug-pull risk. The bot asks it before every buy."],
    ["LP locked / burned", "Pool money (liquidity-provider tokens) that is time-locked or destroyed, so the creator can't pull it out of the pool."],
    ["Price guard", "Checks every price reading for an open position and ignores or holds readings that look wrong until they're confirmed."],
    ["Rug / drained pool", "The pool's liquidity is pulled out, so the coin can no longer be sold for real money."],
    ["Constant-product (x·y=k)", "How most DEX pools price trades. The bigger your trade versus the pool, the worse your average price; a pool can never pay out more than it holds."],
    ["Price impact", "How much your own trade moves the pool's price. Large trades in small pools pay a lot of it."]];
  const glossary = card("doc-glossary", "Glossary", `<dl class="gloss">${G.map(([k, v]) => `<div><dt>${k}</dt><dd>${v}</dd></div>`).join("")}</dl>`);

  $("docs").innerHTML = `<div class="card doc intro"><h2>How Memebot decides</h2><p>This page explains, step by step, how the bot finds coins, what it checks, when it buys and when it sells. It's written from the bot's actual code. <span class="cvkey">Numbers in teal</span> are read from the bot's current settings, so they stay up to date.</p></div>`
    + overview + realtest + sources + filters + score + buy + manage + guards + risk + olderDoc(d, flow, facts) + scoreDoc(d, facts) + corrections + glossary;
}
function docsToc() {
  $("doctoc").innerHTML = DOC_SECTIONS.map(([id, t]) => `<button data-j="${id}">${t}</button>`).join("");
  $("doctoc").addEventListener("click", e => { const id = e.target.dataset && e.target.dataset.j; if (!id || !$(id)) return;
    $(id).scrollIntoView({behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth", block: "start"}); });
}
docsToc();


// ---------------------------------------------------------------- OLDER COINS (second paper-only strategy: data.json "established" + "strategies")
// established.{enabled, paper_only, label, kpi, open[], closed[], equity[], last_round{ts, duration, funnel, candidates[], watchlist}, rules}
// strategies.{new, established}: side-by-side summary. The top-level kpi/open/closed are the new-coins strategy only.
const EST_NA = "Older coins strategy data not available yet";
let ochart = null;
const bigr = (v) => big(v).replace(/\.0+([kMB])$/, "$1");
function estInfo(d) {
  const E = d.established && typeof d.established === "object" ? d.established : null;
  const S = d.strategies && typeof d.strategies === "object" ? d.strategies : {};
  const cfg = (d.config || {}).established || {};
  const ek = (E && E.kpi && typeof E.kpi === "object") ? E.kpi : (S.established && typeof S.established === "object" ? S.established : null);
  return {E, cfg, rules: (E && E.rules && typeof E.rules === "object") ? E.rules : {}, kpi: ek,
    open: E && Array.isArray(E.open) ? E.open.filter(o => o && typeof o === "object") : [],
    closed: E && Array.isArray(E.closed) ? E.closed.filter(o => o && typeof o === "object") : [],
    equity: E && Array.isArray(E.equity) ? E.equity.filter(e => e && num(e.ts) && num(e.equity) != null) : [],
    round: E && E.last_round && typeof E.last_round === "object" ? E.last_round : null,
    sNew: S.new && typeof S.new === "object" ? S.new : null, sEst: S.established && typeof S.established === "object" ? S.established : ek};
}
// live numbers for the strategy text: config.established first, then established.rules, then the text's own values
function estNums(d) {
  const {cfg, rules: R, kpi} = estInfo(d);
  const cn = (k) => num(cfg[k]);
  const rn = (s, i = 0) => { const m = String(s ?? "").match(/[-+]?\d+(\.\d+)?/g); return m && m[i] != null ? Math.abs(+m[i]) : null; };
  const fmt = (v) => Number.isInteger(v) ? String(v) : String(+v.toFixed(2));
  const o = {
    ageH: cn("min_pool_age_hours") ?? rn(R.pool_age) ?? 24, liq: cn("min_liquidity_usd") ?? num(R.min_liquidity_usd) ?? 100000,
    vol24: cn("min_volume_h24_usd") ?? num(R.min_volume_h24_usd), holders: cn("min_holders") ?? num(R.min_holders) ?? 1000,
    organic: cn("min_organic_score") ?? num(R.min_organic_score) ?? 50,
    pbMin: cn("pullback_min_pct") ?? rn(R.entry, 1), pbMax: cn("pullback_max_pct") ?? rn(R.entry, 2),
    bMin: cn("bounce_min_h1_pct") ?? rn(R.entry, 4), bMax: cn("max_h1_pump_pct") ?? rn(R.entry, 5),
    tp1: cn("take_profit_pct") ?? rn(R.take_profit, 0) ?? 12, tp1Frac: cn("take_profit_sell_fraction") ?? (rn(R.take_profit, 1) != null ? rn(R.take_profit, 1) / 100 : null),
    tp2: cn("take_profit2_pct") ?? rn(R.take_profit, 2) ?? 25, sl: cn("stop_loss_pct") ?? rn(R.stop_loss) ?? 8,
    trail: cn("trailing_stop_pct") ?? rn(R.trailing, 0), trailAct: cn("trailing_activate_pct") ?? rn(R.trailing, 1),
    holdH: cn("max_hold_hours") ?? num(R.max_hold_hours) ?? 168, start: cn("starting_balance_usd") ?? num(kpi && kpi.start) ?? 300,
    pos: cn("position_usd") ?? num(kpi && kpi.position_usd), maxOpen: cn("max_open") ?? num(kpi && kpi.max_open), dstop: cn("daily_stop_pct") ?? num(kpi && kpi.daily_stop_pct),
    mcMin: cn("min_market_cap_usd"), mcMax: cn("max_market_cap_usd"), topH: cn("max_top_holders_pct"), every: cn("discovery_interval_min"), upd: cn("update_interval_sec"),
    perRound: cn("max_entries_per_round"), cooldown: cn("reentry_cooldown_hours"), agree: cn("entry_price_agree_pct"), watchH: cn("watch_hours"),
    slip: cn("slippage_pct"), fee: cn("fee_pct"), m5: cn("min_m5_pct"), netBuy: cn("min_net_buyers_h1"),
    fbOn: String(cfg.prebuy_fallback ?? R.prebuy_fallback ?? "jupiter_quote") !== "off", fbCost: cn("fallback_max_trade_impact_pct") ?? num(R.fallback_max_trade_impact_pct) ?? 1.5,
    fbLiq: cn("fallback_min_liq_usd") ?? num(R.fallback_min_liq_usd) ?? 100000, fbTol: cn("fallback_price_tolerance_pct") ?? num(R.fallback_price_tolerance_pct) ?? 5,
    fbDepth: cn("fallback_depth_usd") ?? num(R.fallback_depth_usd), fbGt: (cfg.fallback_gt_check ?? R.fallback_gt_check) === true,
    fromData: Object.keys(cfg).length > 0 || Object.keys(R).length > 0};
  o.f = fmt;
  o.fbTrade = o.pos ?? 15;
  o.ageTxt = o.ageH === 24 ? "more than a day old" : o.ageH % 24 === 0 ? `more than ${o.ageH / 24} days old` : `more than ${fmt(o.ageH)} hours old`;
  o.holdTxt = o.holdH % 24 === 0 ? `${o.holdH / 24} day${o.holdH === 24 ? "" : "s"}` : `${fmt(o.holdH)} hours`;
  return o;
}
function estText(d, cv) {
  const n = estNums(d), b = (s) => cv && n.fromData ? `<b class="cv">${s}</b>` : s;
  return `<b>Older coins (paper only):</b> a second fake-money test that trades established Solana coins (${b(n.ageTxt)}, with at least ${b(bigr(n.liq))} liquidity, ${b(n.holders.toLocaleString("en-US") + "+")} holders and mostly real trading). It buys only after a dip that is starting to recover, never a pump, takes profit at ${b("+" + n.f(n.tp1) + "%")} and ${b("+" + n.f(n.tp2) + "%")}, stops out at ${b("−" + n.f(n.sl) + "%")}, uses a trailing stop, and holds at most ${b(n.holdTxt)}. It has its own ${b(usd(n.start, 0))} fake balance and its own results, and it never uses real money.`;
}
const paperOnly = `<span class="pill paperonly">PAPER ONLY</span>`;
function cmpRows(d) {
  const {sNew, sEst} = estInfo(d), k = d.kpi || {};
  const nw = sNew || (Object.keys(k).length ? {equity: k.equity, start: k.start, pnl: k.pnl, pnl_pct: k.pnl_pct, win_rate: k.win_rate, closed: k.closed, wins: k.wins, open: k.open, max_open: k.max_open} : null);
  return {nw, es: sEst};
}
function stratCmp(d, compact) {
  const {nw, es} = cmpRows(d);
  if (!es) return `<div class="empty">${EST_NA}.</div>`;
  const wr = (s) => num(s && s.win_rate) != null ? num(s.win_rate).toFixed(0) + "%" : "—";
  const tr = (s) => s ? `${num(s.closed) ?? 0}${num(s.wins) != null && num(s.closed) ? ` <span class="muted small">(${num(s.wins)}W)</span>` : ""}` : "—";
  const op = (s) => s ? `${num(s.open) ?? 0}${num(s.max_open) != null ? ` / ${num(s.max_open)}` : ""}` : "—";
  const eq = (s) => s ? `${usd(s.equity)}${num(s.start) != null ? ` <span class="muted small">/ ${usd(s.start, 0)}</span>` : ""}` : "—";
  const pc = (s) => s ? `<span class="${cls(s.pnl_pct)}">${pct(s.pnl_pct, 2)}</span>${!compact && num(s.pnl) != null ? ` <span class="${cls(s.pnl)} small">${usd(s.pnl)}</span>` : ""}` : "—";
  const rows = [["Equity", eq], ["P&amp;L", pc], ["Win rate", wr], ["Closed trades", tr], ["Open", op]];
  return `<div class="cmp"><div class="cmp-h"></div><div class="cmp-h">New coins</div><div class="cmp-h">Older coins</div>
    ${rows.map(([l, f]) => `<div class="cmp-l">${l}</div><div class="cmp-v">${f(nw)}</div><div class="cmp-v">${f(es)}</div>`).join("")}</div>`;
}
function stratCard(d) {
  const el = $("stratcmp"); if (!el) return;
  el.innerHTML = `<h2><span>New coins vs Older coins</span> ${paperOnly}<a href="#older" class="small more">Older coins</a></h2>${stratCmp(d, true)}
    <div class="small muted cmp-note">Two separate fake-money strategies, each with its own balance. The figures above this card are the new-coins strategy.</div>`;
}
function estTargets(p, now) {
  const t = p.targets || {}, bits = [];
  if (num(t.tp1) != null) bits.push(`TP1 <b>${price(t.tp1)}</b>${p.tp1_done ? " ✓" : ""}`);
  if (num(t.tp2) != null) bits.push(`TP2 <b>${price(t.tp2)}</b>`);
  if (num(t.sl) != null) bits.push(`stop <b>${price(t.sl)}</b>`);
  if (num(t.trail) != null) bits.push(`trail <b>${price(t.trail)}</b>`);
  const mh = num(t.max_hold_until);
  return (bits.join(" · ") || "—") + (mh ? `<br>max hold until ${tfmt(mh, true)} <span class="muted">(${mh > now ? dur(mh - now) + " left" : "due"})</span>` : "");
}
const EST_STAGE = {age: "Pool age", liquidity: "Liquidity", volume: "Volume", market_cap: "Market cap", holders: "Holders", organic: "Organic (real) trading",
  contract: "Mint/freeze revoked", timing: "No dip-and-recover yet", rugcheck: "Rug check", price: "Price check", data: "Data", txns: "Real buys and sells", buyers: "Net buyers", pump: "Pumping (not a dip)", cooldown: "Traded recently"};
function olderView(d) {
  const el = $("olderview"); if (!el) return;
  const I = estInfo(d), now = d.generated;
  if (!I.E && !I.sEst) { el.innerHTML = `<div class="card"><h2>Older coins ${paperOnly}</h2><div class="empty">${EST_NA}.</div></div>`; if (ochart) { ochart.destroy(); ochart = null; } return; }
  const k = I.kpi || {}, n = estNums(d);
  const tile = (l, v, sub) => `<div class="stat"><div class="label">${l}</div><div class="value">${v}</div><div class="sub">${sub}</div></div>`;
  const losses = num(k.closed) != null && num(k.wins) != null ? num(k.closed) - num(k.wins) : null;
  const off = I.E && I.E.enabled === false;
  el.innerHTML = `<div class="card stratcmp"><h2><span>New coins vs Older coins</span> ${paperOnly}</h2>${stratCmp(d, false)}</div>
    <div class="card estintro"><h2><span>${esc((I.E && I.E.label) || "Older coins (paper only)")}</span> ${paperOnly}${off ? ` ${pill("", "switched off")}` : ""}<a href="#docs" class="small more" data-doc="doc-older">How it works</a></h2>
      <p>${estText(d, false)}</p></div>
    <div class="stats stats6">
      ${tile("Equity · older coins", usd(k.equity), `vs ${usd(k.start ?? n.start, 0)} start${num(k.cash) != null ? ` · cash ${usd(k.cash)}` : ""}`)}
      ${tile("P&amp;L · older coins", `<span class="${cls(k.pnl)}">${usd(k.pnl)}</span>`, `<span class="${cls(k.pnl_pct)}">${pct(k.pnl_pct, 2)}</span>`)}
      ${tile("Win rate", num(k.win_rate) != null ? num(k.win_rate).toFixed(0) + "%" : "—", num(k.closed) ? `${num(k.closed)} closed` : "no closed trades yet")}
      ${tile("Wins / losses", `${num(k.wins) ?? 0} · ${losses ?? 0}`, num(k.avg_trade_pct) != null ? `avg trade ${pct(k.avg_trade_pct)}` : "wins · losses")}
      ${tile("Open", `${num(k.open) ?? I.open.length}${num(k.max_open) != null ? ` / ${num(k.max_open)}` : ""}`, num(k.position_usd) != null ? `${usd(k.position_usd, 0)} per position` : "positions")}
      ${tile("Today", `<span class="${cls(k.day_pnl)}">${usd(k.day_pnl)}</span>`, k.day_stopped ? `<span class="neg">daily stop hit, no new buys today</span>` : num(k.daily_stop_pct) != null ? `daily stop −${num(k.daily_stop_pct)}%` : "since midnight (Toronto)")}
    </div>
    <div class="card"><h2>Equity curve <span class="muted small">(older coins, fake $)</span></h2><div class="chartbox"><canvas id="oldchart"></canvas></div></div>
    <div class="card"><h2>Open positions <span class="muted small">· older coins</span></h2><div class="tablewrap"><table id="estopen" class="cards"></table></div></div>
    <div class="card"><h2>Closed trades <span class="muted small">· older coins</span></h2><div class="tablewrap"><table id="estclosed" class="cards"></table></div></div>
    <div class="card"><h2>Last round <span class="muted small" id="estroundnote"></span></h2><div id="estfunnel" class="funnel"></div>
      <h3 class="subh">Top candidates</h3><div class="tablewrap"><table id="estcands" class="cards"></table></div></div>`;
  table("estopen", [["token", "Token"], ["entry", "Entry", 1], ["price", "Current", 1], ["pnl", "P&L", 1], ["size", "Size", 1], ["age", "Age"], ["targets", "Exit targets"]],
    I.open.map(p => `<tr><td class="col-token">${tok(p.symbol, p.url, p.token)}${jupChip(p)}</td><td class="num col-entry">${price(p.entry_price)}</td>
      <td class="num col-price">${price(p.last_price)}${num(p.last_update) ? `<br><span class="small muted">${ago(p.last_update, now)} ago</span>` : ""}</td>
      <td class="num col-pnl ${cls(p.pnl_usd)}">${usd(p.pnl_usd)}<br><span class="small">${pct(p.pnl_pct)}</span></td>
      <td class="num col-size">${usd(p.cost_usd)}${p.tp1_done && num(p.qty) ? `<br><span class="small muted">${(num(p.remaining_qty) / num(p.qty) * 100).toFixed(0)}% left</span>` : ""}</td>
      <td class="col-age">${ago(p.opened_at, now)}</td><td class="col-targets">${estTargets(p, now)}</td></tr>`), "No open older-coin positions right now.");
  table("estclosed", [["token", "Token"], ["opened", "Opened"], ["closedat", "Closed"], ["size", "Size", 1], ["entry", "Entry", 1], ["exit", "Exit", 1], ["pnl", "P&L", 1], ["reason", "Exit reason"]],
    I.closed.map(p => `<tr><td class="col-token">${tok(p.symbol, p.url, p.token)}${jupChip(p)}</td><td class="col-opened">${tfmt(p.opened_at, true)}</td><td class="col-closedat">${tfmt(p.closed_at, true)}</td>
      <td class="num col-size">${usd(p.cost_usd)}</td><td class="num col-entry">${price(p.entry_price)}</td><td class="num col-exit">${price(p.last_price)}</td>
      <td class="num col-pnl ${cls(p.pnl_usd)}">${usd(p.pnl_usd)}<br><span class="small">${pct(p.pnl_pct)}</span></td><td class="reasons">${esc(p.exit_reason || "—")}</td></tr>`), "No closed older-coin trades yet.");
  // last round funnel + candidates
  const R = I.round, f = R && R.funnel && typeof R.funnel === "object" ? R.funnel : null;
  $("estroundnote").textContent = R && num(R.ts) ? `(${tfmt(R.ts, true)}${num(R.duration) != null ? ` · took ${dur(R.duration)}` : ""}${num(R.watchlist) != null ? ` · watching ${num(R.watchlist)}` : ""})` : "";
  if (!f) $("estfunnel").innerHTML = `<div class="empty">No round data yet.</div>`;
  else {
    const tot = num(f.evaluated) || num(f.listed) || 1, rej = f.rejected && typeof f.rejected === "object" ? f.rejected : {};
    const row = (l, v, c = "", w = null) => `<div class="frow${c ? " " + c : ""}"><span>${l}</span><b>${v}</b><div class="bar"><i class="${c === "drop" ? "drop" : ""}" style="width:${w ?? 100}%"></i></div></div>`;
    let h = "";
    if (num(f.listed) != null) h += `<div class="frow total"><span>Coins on Jupiter's lists</span><b>${num(f.listed)}</b><div class="bar"><i style="width:100%"></i></div></div>`;
    if (num(f.excluded)) h += `<div class="frow"><span>Excluded (majors, stables, LSTs…)</span><b class="muted">${num(f.excluded)}</b><div class="bar"><i class="pend" style="width:${Math.max(1, num(f.excluded) / (num(f.listed) || tot) * 100)}%"></i></div></div>`;
    Object.entries(rej).filter(([, v]) => num(v) > 0).sort((a, b) => num(b[1]) - num(a[1])).forEach(([s, v]) => {
      h += `<div class="frow"><span>${esc(EST_STAGE[s] || PRETTY(s))}</span><b><span class="${s === "timing" ? "warn" : "neg"}">−${num(v)}</span></b><div class="bar"><i class="${s === "timing" ? "pend" : "drop"}" style="width:${Math.max(1, num(v) / tot * 100)}%"></i></div></div>`; });
    h += `<div class="fpass"><span>Passed all checks</span><span>${num(f.passed) ?? 0}${num(f.opened) != null ? ` · bought ${num(f.opened)}` : ""}</span></div>`;
    const extra = [num(f.quality_ok) != null ? `${num(f.quality_ok)} met the quality bar (age, liquidity, holders, real trading)` : "", num(R.jup_calls) != null ? `${num(R.jup_calls)} Jupiter calls` : "",
      num(f.list_calls_failed) ? `<span class="warn">${num(f.list_calls_failed)} list calls failed</span>` : ""].filter(Boolean);
    h += `<div class="muted small fnote">Red −N = rejected at that check · amber = no dip-and-recover pattern yet (watched again next round).${extra.length ? " " + extra.join(" · ") + "." : ""}</div>`;
    $("estfunnel").innerHTML = h;
  }
  const C = R && Array.isArray(R.candidates) ? R.candidates.filter(c => c && typeof c === "object") : [];
  table("estcands", [["token", "Token"], ["h6", "6h change", 1], ["h1", "1h change", 1], ["organic", "Organic score", 1], ["liq", "Liquidity", 1]],
    C.map(c => `<tr><td class="col-token">${tokLink(c.symbol, c.mint)}</td><td class="num col-h6 ${cls(c.chg_h6)}">${pct(c.chg_h6)}</td><td class="num col-h1 ${cls(c.chg_h1)}">${pct(c.chg_h1)}</td>
      <td class="num col-organic">${num(c.organic) != null ? num(c.organic).toFixed(0) : "—"}</td><td class="num col-liq">${bigr(c.liq)}</td></tr>`), "No candidates in the last round.");
  olderChart(d, I);
}
function olderChart(d, I) {
  if (typeof Chart === "undefined" || !$("oldchart")) return;
  const k = I.kpi || {}, start = num(k.start) ?? estNums(d).start;
  const pts = I.equity.slice();
  if (num(k.equity) != null && (!pts.length || num(pts[pts.length - 1].ts) < num(d.generated) - 30)) pts.push({ts: d.generated, equity: num(k.equity)});
  if (!pts.length) pts.push({ts: d.generated, equity: start});
  const labels = pts.map(p => new Date(p.ts * 1000).toLocaleString("en-CA", {timeZone: TZ, month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false}));
  const T = chartTheme(), grid = T.grid, txt = T.txt, line = T.line;
  const vals = pts.map(p => num(p.equity)), lo = Math.min(...vals, start), hi = Math.max(...vals, start), pad = Math.max(2, (hi - lo) * 0.25);
  if (ochart) { ochart.destroy(); ochart = null; }   // the canvas is re-created with the view on every render
  ochart = new Chart($("oldchart"), {type: "line", data: {labels, datasets: [
      {data: vals, borderColor: line, backgroundColor: T.fill, fill: true, pointRadius: pts.length < 3 ? 3 : 0, borderWidth: 2, tension: 0.2},
      {data: pts.map(() => start), borderColor: T.base, borderDash: [5, 5], pointRadius: 0, borderWidth: 1}]},
    options: {responsive: true, maintainAspectRatio: false, animation: false, plugins: {legend: {display: false}, tooltip: {...T.tip, callbacks: {label: c => usd(c.parsed.y)}}},
      scales: {x: {ticks: {color: txt, maxTicksLimit: 6, maxRotation: 0}, grid: {color: grid}}, y: {min: Math.floor(lo - pad), max: Math.ceil(hi + pad), ticks: {color: txt, callback: v => "$" + v}, grid: {color: grid}}}}});
}
// Docs section (rendered inside docs(); uses its flow()/facts helpers via arguments)
function olderDoc(d, flow, facts) {
  const I = estInfo(d), n = estNums(d), b = (s) => n.fromData ? `<b class="cv">${s}</b>` : s;
  if (!I.E && !I.sEst && !Object.keys(I.cfg).length) return `<section class="card doc" id="doc-older"><h2>Older coins (paper only) ${paperOnly}</h2><p class="muted">${EST_NA}.</p></section>`;
  const R = I.rules, has = (v) => v != null;
  const quality = [`pool ${b(n.ageTxt)}`, `liquidity ≥ ${b(bigr(n.liq))}`, has(n.vol24) ? `24h volume ≥ ${b(bigr(n.vol24))}` : "", `${b(n.holders.toLocaleString("en-US"))}+ holders`,
    `Jupiter organic score ≥ ${b(n.f(n.organic))} (mostly real trading, not bots)`, has(n.mcMin) && has(n.mcMax) ? `market cap ${b(bigr(n.mcMin))}–${b(bigr(n.mcMax))}` : "",
    has(n.topH) ? `top holders ≤ ${b(n.f(n.topH) + "%")}` : "", I.cfg.require_mint_revoked || I.cfg.require_freeze_revoked ? "mint and freeze authority revoked" : ""].filter(Boolean);
  const dip = has(n.pbMin) && has(n.pbMax) && has(n.bMin) && has(n.bMax)
    ? `down ${b(n.f(n.pbMin) + "%")}–${b(n.f(n.pbMax) + "%")} over 6 hours, then up ${b("+" + n.f(n.bMin) + "%")} to ${b("+" + n.f(n.bMax) + "%")} in the last hour` : esc(R.entry || "a 6-hour dip, then a small 1-hour bounce");
  const exits = [`TP1 ${b("+" + n.f(n.tp1) + "%")}${has(n.tp1Frac) ? ` sells ${b(Math.round(n.tp1Frac * 100) + "%")}` : ""}`, `TP2 ${b("+" + n.f(n.tp2) + "%")} sells the rest`, `stop ${b("−" + n.f(n.sl) + "%")}`,
    has(n.trail) ? `trailing ${b(n.f(n.trail) + "%")}${has(n.trailAct) ? ` after ${b("+" + n.f(n.trailAct) + "%")}` : ""}` : "trailing stop", `max hold ${b(n.holdTxt)}`];
  const fl = flow([
    {t: "start", title: "Jupiter's coin lists", text: `Top organic-score and trending lists${has(n.every) ? `, every ${b(n.f(n.every) + " min")}` : ""}. Majors, stablecoins and staking tokens are excluded`},
    {t: "decision", title: "Established coin?", text: quality.join(", "), br: [["no", "No", "rejected this round"]], link: "yes"},
    {t: "decision", title: "Dip starting to recover?", text: `Not a pump: ${dip}${has(n.m5) ? `, last 5 min not below ${b(n.f(n.m5) + "%")}` : ""}${has(n.netBuy) ? ", more buyers than sellers in the last hour" : ""}`, br: [["wait", "Not yet", `watched again next round${has(n.watchH) ? ` (up to ${b(n.f(n.watchH) + " h")})` : ""}`]], link: "yes"},
    {t: "decision", title: "Room and safety?", text: [has(n.maxOpen) ? `fewer than ${b(n.maxOpen)} open` : "", has(n.dstop) ? `daily stop (−${b(n.f(n.dstop) + "%")}) not hit` : "", has(n.cooldown) ? `not traded in the last ${b(n.f(n.cooldown) + " h")}` : "", "RugCheck has no danger flag"].filter(Boolean).join(", "), br: [["no", "No", "skipped"]], link: "yes"},
    ...(n.fbOn ? [{t: "decision", title: "DexScreener busy?", text: "The pre-buy DexScreener price check can't be read (paused or 429)", br: [["alt", "Yes", `→ Jupiter quote check instead of skipping: a ${b(usd(n.fbTrade, 0))} trade costs under ${b(n.f(n.fbCost) + "%")}, pool holds about ${b(bigr(n.fbLiq))}+, price matches within ${b(n.f(n.fbTol) + "%")}${n.fbGt ? " (and GeckoTerminal when it answers)" : ""}; fails → skipped`]], link: "no"}] : []),
    {t: "decision", title: "Price check passes?", text: `${has(n.agree) ? `DexScreener price agrees within ${b(n.f(n.agree) + "%")}` : "DexScreener price agrees with the scan"}${n.fbOn ? " (or the Jupiter quote check passed)" : ""}`, br: [["no", "No", "skipped"]], link: "yes"},
    {t: "buy", title: `Paper buy${has(n.pos) ? ` (${usd(n.pos, 0)})` : ""}`, text: `Fake money only${has(n.perRound) ? `, at most ${n.perRound} new buy per round` : ""}${has(n.slip) && has(n.fee) ? `, ${n.f(n.slip)}% slippage + ${n.f(n.fee)}% fee assumed` : ""}`},
    {t: "hold", title: "Manage the position", text: `Re-priced${has(n.upd) ? ` every ${b(n.f(n.upd) + " s")}` : " regularly"}`},
    {t: "end", title: "Sell", text: exits.join(" · ")},
  ], "Older coins: entry and exit, in order.");
  const fx = facts([["Fake balance", b(usd(n.start, 0))], ...(has(n.pos) ? [["Per position", b(usd(n.pos, 0))]] : []), ...(has(n.maxOpen) ? [["Max open", b(n.maxOpen)]] : []),
    ["Take profit", `${b("+" + n.f(n.tp1) + "%")} / ${b("+" + n.f(n.tp2) + "%")}`], ["Stop loss", b("−" + n.f(n.sl) + "%")], ...(has(n.trail) ? [["Trailing stop", `${b(n.f(n.trail) + "%")}${has(n.trailAct) ? ` after +${n.f(n.trailAct)}%` : ""}`]] : []),
    ["Max hold", b(n.holdTxt)], ...(has(n.dstop) ? [["Daily stop", b("−" + n.f(n.dstop) + "%")]] : []),
    ...(n.fbOn ? [["DexScreener busy", `Jupiter quote check (${b(usd(n.fbTrade, 0))} under ${b(n.f(n.fbCost) + "%")})`]] : []), ["Real money", "Never"]]);
  return `<section class="card doc" id="doc-older"><h2>Older coins (paper only) ${paperOnly}</h2>
    <p class="rt-note jupnote">${estText(d, true)}</p>
    <p>It runs separately from the main (new coins) paper strategy: its own fake balance, positions, results and daily stop. The Home figures, the Trades tab and the main equity curve are the new-coins strategy; the <a href="#older">Older coins</a> tab shows this one, and Home has a side-by-side card.</p>
    ${n.fbOn ? `<p class="rt-note jupnote">When DexScreener is busy, older-coin paper buys are double-checked with live Jupiter quotes instead of being skipped: a ${b(usd(n.fbTrade, 0))} trade must cost under ${b(n.f(n.fbCost) + "%")}, the pool must hold about ${b(bigr(n.fbLiq))} or more, and the price must match within ${b(n.f(n.fbTol) + "%")}.${has(n.fbDepth) ? ` <span class="muted small">(A second ${usd(n.fbDepth, 0)} test quote measures the pool's depth; quotes are never executed.)</span>` : ""} Such trades are marked <span class="pill jupchk"><span class="jc-l">checked via Jupiter quote</span><span class="jc-s">Jup check</span></span> on the <a href="#older">Older coins</a> tab.</p>` : ""}
    ${fl}${fx}</section>`;
}

// ---------------------------------------------------------------- scoring test (current vs new score, side by side)
// data.json "scoring": {model_version, selftest, enabled, started, thresholds, real_money_account, current, new, comparison}
const SC_NA = "Scoring test data not available yet";
const scCharts = {};
function scInfo(d) {
  const S = d.scoring && typeof d.scoring === "object" ? d.scoring : null;
  const arr = (x) => Array.isArray(x) ? x.filter(e => e && typeof e === "object") : [];
  const acct = (k) => { const A = S && S[k] && typeof S[k] === "object" ? S[k] : null; if (!A) return null;
    return {kpi: A.kpi && typeof A.kpi === "object" ? A.kpi : {}, open: arr(A.open), closed: arr(A.closed),
      equity: arr(A.equity).filter(e => num(e.ts) && num(e.equity) != null), decisions: arr(A.decisions)}; };
  const T = S && S.thresholds && typeof S.thresholds === "object" ? S.thresholds : {};
  const cmp = S && S.comparison && typeof S.comparison === "object" ? S.comparison : null;
  return {S, cur: acct("current"), nw: acct("new"), cmp, T, ver: S && typeof S.model_version === "string" && S.model_version ? S.model_version : null,
    st: S && S.selftest && typeof S.selftest === "object" ? S.selftest : null, started: num(S && (S.started ?? (cmp && cmp.since))),
    realAcct: (S && S.real_money_account) || "current", enabled: !!(S && S.enabled !== false),
    start: num(((d.config || {}).scoring || {}).starting_balance_usd) ?? num(S && S.new && S.new.kpi && S.new.kpi.start) ?? 1000};
}
// thresholds, with the announced values as fallback
function scThr(d, I) {
  const T = I.T, f = (v) => Number.isInteger(v) ? String(v) : String(+v.toFixed(2));
  const cf = num((d.kpi || {}).cap_size_factor), half = cf == null || cf === 0.5 ? "half size" : `${Math.round(cf * 100)}% size`;
  const cur = num(T.current_min_score) ?? num((((d.config || {}).filters) || {}).min_score_to_buy) ?? 55, curCap = num(T.current_soft_cap_min) ?? num((d.kpi || {}).cap_min_score) ?? 75;
  const nb = num(T.new_buy) ?? 48.4, nCap = num(T.new_soft_cap_min) ?? 55.5;
  return {cur, curCap, nb, nCap, half, f, fromData: num(T.new_buy) != null,
    curTxt: `${f(cur)}+, or ${f(curCap)}+ at ${half} on a bad day`, newTxt: `${f(nb)}+, or ${f(nCap)}+ at ${half} on a bad day`,
    curB: `<b class="cv">${f(cur)}+</b>, or <b class="cv">${f(curCap)}+</b> at ${half} on a bad day`, newB: `<b class="cv">${f(nb)}+</b>, or <b class="cv">${f(nCap)}+</b> at ${half} on a bad day`};
}
const scWhen = (ts) => ts ? new Date(ts * 1000).toLocaleString("en-US", {timeZone: TZ, month: "short", day: "numeric", hour: "numeric", minute: "2-digit"}) + " ET" : "—";
const scModel = (I) => I.ver ? I.ver.split(/\s+/)[0] : "scoring_v1";
const scTag = (k) => k && k.real_money === true ? `<span class="realtag">REAL MONEY</span>` : paperOnly;
function scSelftest(I) {
  const t = I.st; if (!t) return pill("", "self-test: no data");
  if (t.ok) return `<span class="pill ok" title="${esc(`${num(t.rows) ?? "?"} test rows${num(t.worst) != null ? `, largest difference ${Number(t.worst).toExponential(1)}` : ""}`)}">self-test passed${num(t.rows) != null ? ` · ${num(t.rows)} rows` : ""}</span>`;
  return `<span class="pill bad" title="${esc(t.error || "")}">self-test failed${t.error ? ": " + esc(String(t.error).slice(0, 60)) : ""}</span>`;
}
// coin list field: a count, or an array of symbols / mints / {symbol, token}
function scCoins(v) {
  if (Array.isArray(v)) {
    const names = v.map(x => x && typeof x === "object" ? (x.symbol || x.token || "") : String(x ?? "")).filter(Boolean)
      .map(s => s.length > 16 ? s.slice(0, 4) + "…" + s.slice(-4) : s);
    if (!names.length) return "0";
    const chips = names.map(s => `<span class="pill coinchip">${esc(s)}</span>`).join(" ");
    return names.length > 6 ? `<details class="coinlist"><summary>${names.length} coins</summary><div>${chips}</div></details>` : `${names.length} <span class="coinchips">${chips}</span>`;
  }
  return num(v) != null ? String(num(v)) : "—";
}
function scCmp(d, I, compact) {
  if (!I.S || (!I.cur && !I.nw)) return `<div class="empty">${SC_NA}.</div>`;
  const c = I.cmp || {}, kc = I.cur ? I.cur.kpi : null, kn = I.nw ? I.nw.kpi : null;
  const ss = (k, side) => (c[side] && typeof c[side] === "object" ? c[side] : null) || (k && k.since_start) || null;
  const sc = ss(kc, "current"), sn = ss(kn, "new");
  const wr = (s) => s && num(s.win_rate) != null ? num(s.win_rate).toFixed(0) + "%" : "—";
  const eq = (k) => k ? `${usd(k.equity)}${num(k.start) != null ? ` <span class="muted small">/ ${usd(k.start, 0)}</span>` : ""}` : "—";
  const pc = (k) => k ? `<span class="${cls(k.pnl_pct)}">${pct(k.pnl_pct, 2)}</span>` : "—";
  const sp = (s) => s ? `<span class="${cls(s.pnl)}">${usd(s.pnl)}</span>` : "—";
  const dd = (s) => s ? (num(s.max_drawdown_usd) ? `<span class="neg">${usd(-Math.abs(num(s.max_drawdown_usd)))}</span>${num(s.max_drawdown_pct) != null ? ` <span class="muted small">${pct(-Math.abs(num(s.max_drawdown_pct)))}</span>` : ""}` : "$0.00") : "—";
  const op = (k) => k ? `${num(k.open) ?? 0}${num(k.max_open) != null ? ` / ${num(k.max_open)}` : ""}` : "—";
  const tr = (s) => s ? String(num(s.trades) ?? 0) : "—";
  const rows = compact
    ? [["Equity", eq(kc), eq(kn)], ["P&amp;L, all time", pc(kc), pc(kn)], ["Trades since start", tr(sc), tr(sn)], ["Win rate since start", wr(sc), wr(sn)], ["Open", op(kc), op(kn)]]
    : [["Equity", eq(kc), eq(kn)], ["P&amp;L, all time", pc(kc), pc(kn)], ["Trades since start", tr(sc), tr(sn)], ["Win rate since start", wr(sc), wr(sn)],
       ["P&amp;L since start", sp(sc), sp(sn)], ["Max drawdown since start", dd(sc), dd(sn)], ["Open", op(kc), op(kn)]];
  const head = `<div class="cmp-h"></div><div class="cmp-h">Current score ${kc && kc.real_money ? `<span class="realtag">REAL</span>` : ""}</div><div class="cmp-h">New score ${kn && kn.real_money === true ? `<span class="realtag">REAL</span>` : ""}</div>`;
  const grid = `<div class="cmp">${head}${rows.map(([l, a, b]) => `<div class="cmp-l">${l}</div><div class="cmp-v">${a}</div><div class="cmp-v">${b}</div>`).join("")}</div>`;
  if (!I.nw) return grid + `<div class="empty">The new-score account has not started yet.</div>`;
  if (compact) return grid;
  const th = scThr(d, I);
  const coins = I.cmp ? `<div class="scoins"><div><span class="muted small">Bought by both</span><b>${scCoins(c.both_bought)}</b></div><div><span class="muted small">Only current score</span><b>${scCoins(c.only_current_coins)}</b></div><div><span class="muted small">Only new score</span><b>${scCoins(c.only_new_coins)}</b></div></div>` : "";
  return grid + coins + `<div class="scmeta small"><span>Model <b class="cv">${esc(I.ver || "—")}</b></span>${scSelftest(I)}${I.enabled ? "" : pill("wait", "new account switched off")}
    <span>Current buys at <b class="cv">${th.f(th.cur)}+</b> (<b class="cv">${th.f(th.curCap)}+</b> on a bad day)</span><span>New buys at <b class="cv">${th.f(th.nb)}+</b> (<b class="cv">${th.f(th.nCap)}+</b> at ${th.half} on a bad day)</span>
    <span>Real money follows the <b>${esc(I.realAcct === "new" ? "new" : "current")}</b> score</span></div>`;
}
function scoreCard(d) {
  const el = $("scorecmp"); if (!el) return;
  const I = scInfo(d);
  el.innerHTML = `<h2><span>Current vs New scoring</span> ${paperOnly.replace("PAPER ONLY", "PAPER TEST")}<a href="#scorenew" class="small more">New scoring</a></h2>${scCmp(d, I, true)}
    <div class="small muted cmp-note">${I.S && (I.cur || I.nw) ? `Same coins and safety checks, different buy score, $1,000 each. Since ${scWhen(I.started)}. Real money follows the current score.` : "A side-by-side paper test of a new buy score."}</div>`;
}
function scoreView(d, key) {
  const el = $(key === "cur" ? "scoreview-cur" : "scoreview-new"); if (!el) return;
  if (scCharts[key]) { scCharts[key].destroy(); scCharts[key] = null; }
  const I = scInfo(d), A = key === "cur" ? I.cur : I.nw, now = d.generated, th = scThr(d, I);
  const name = key === "cur" ? "Current scoring" : "New scoring";
  if (!I.S || !A) { el.innerHTML = `<div class="card"><h2>${name} ${key === "cur" ? "" : paperOnly}</h2><div class="empty">${SC_NA}.</div></div>`; return; }
  const k = A.kpi, ss = k.since_start && typeof k.since_start === "object" ? k.since_start : {};
  const real = typeof k.real_money === "boolean" ? k.real_money : I.realAcct === (key === "cur" ? "current" : "new");
  const tile = (l, v, sub) => `<div class="stat"><div class="label">${l}</div><div class="value">${v}</div><div class="sub">${sub}</div></div>`;
  const dm = k.day_mode_label ? (DM_ICON[k.day_mode] ? dayBadge({mode: k.day_mode, label: k.day_mode_label}) : esc(k.day_mode_label)) : "since midnight (Toronto)";
  const intro = key === "cur"
    ? `The existing $1,000 paper account. It buys on the <b>current score</b> (${th.curB}), and ${real ? "<b>the real-money test copies its buys</b>" : "real money does not follow it"}. Its figures are the same as Home and Trades.`
    : `A second <b>$1,000 paper account</b> that sees the same coins and passes the same safety checks, but buys on the <b>new score</b> (${esc(scModel(I))}: ${th.newB}). ${real ? "" : "It never trades real money."}`;
  const cmpCard = `<div class="card stratcmp"><h2><span>Current vs New scoring</span> <span class="muted small">since ${scWhen(I.started)}</span><a href="#docs" class="small more" data-doc="doc-scoring">How it works</a></h2>${scCmp(d, I, false)}</div>`;
  el.innerHTML = cmpCard + `
    <div class="card"><h2><span>${name}</span> ${real ? `<span class="realtag">REAL MONEY</span>` : paperOnly}</h2><p class="scintro">${intro}</p></div>
    <div class="stats stats4">
      ${tile("Equity", usd(k.equity), `vs ${usd(k.start ?? 1000, 0)} start${num(k.cash) != null ? ` · cash ${usd(k.cash)}` : ""}`)}
      ${tile("P&amp;L, all time", `<span class="${cls(k.pnl)}">${usd(k.pnl)}</span>`, `<span class="${cls(k.pnl_pct)}">${pct(k.pnl_pct, 2)}</span>${num(k.realized_pnl) != null ? ` · realized ${usd(k.realized_pnl)}` : ""}`)}
      ${tile("Today", `<span class="${cls(k.day_pnl)}">${usd(k.day_pnl ?? 0)}</span>`, dm)}
      ${tile("Win rate", num(k.win_rate) != null ? num(k.win_rate).toFixed(1) + "%" : "—", num(k.closed) ? `${num(k.wins) ?? 0} wins of ${num(k.closed)} closed` : "no closed trades yet")}
      ${tile("Expectancy", `<span class="${cls(k.expectancy)}">${usd(k.expectancy)}</span>`, "average P&amp;L per closed trade")}
      ${tile("Avg win / loss", `<span class="pos">${usd(k.avg_win)}</span> <span class="muted">/</span> <span class="neg">${usd(k.avg_loss)}</span>`, "per closed trade")}
      ${tile("Open", `${num(k.open) ?? A.open.length}${num(k.max_open) != null ? ` / ${num(k.max_open)}` : ""}`, "positions")}
      ${tile("Trades today", String(num(k.trades_today) ?? 0), "buys since midnight (Toronto)")}
    </div>
    <div class="card"><h2>Since the test started <span class="muted small">${scWhen(I.started)}</span></h2><div class="sstart">
      <div><span class="muted small">Trades</span><b>${num(ss.trades) ?? 0}</b></div>
      <div><span class="muted small">Win rate</span><b>${num(ss.win_rate) != null ? num(ss.win_rate).toFixed(0) + "%" : "—"}</b></div>
      <div><span class="muted small">P&amp;L</span><b class="${cls(ss.pnl)}">${usd(ss.pnl ?? 0)}</b></div>
      <div><span class="muted small">Max drawdown</span><b>${num(ss.max_drawdown_usd) ? `<span class="neg">${usd(-Math.abs(num(ss.max_drawdown_usd)))}</span> <span class="muted small">${pct(-Math.abs(num(ss.max_drawdown_pct) || 0))}</span>` : "$0.00"}</b></div>
    </div></div>
    <div class="card"><h2>Equity curve <span class="muted small">(${key === "cur" ? "current score" : "new score"}, fake $, last 7 days)</span></h2><div class="chartbox"><canvas id="scchart-${key}"></canvas></div></div>
    <div class="card"><h2>Open positions <span class="muted small">· ${key === "cur" ? "current" : "new"} score</span></h2><div class="tablewrap"><table id="scopen-${key}" class="cards"></table></div></div>
    <div class="card"><h2>Closed trades <span class="muted small" id="scclosednote-${key}"></span></h2><div class="tablewrap"><table id="scclosed-${key}" class="cards"></table></div></div>
    <div class="card"><h2>Recent decisions <span class="muted small" id="scdecnote-${key}"></span></h2><div class="tablewrap"><table id="scdec-${key}" class="cards"></table></div></div>`;
  const scores = (p) => { const a = num(p.score_current ?? p.score), b = num(p.score_new);
    return a == null && b == null ? "—" : `<span class="nowrap">cur <b>${a != null ? a.toFixed(1) : "—"}</b></span> · <span class="nowrap">new <b>${b != null ? b.toFixed(1) : "—"}</b></span>`; };
  const prob = (p) => { let v = num(p.prob_new); if (v == null) return "—"; if (v <= 1) v *= 100; return v.toFixed(0) + "%"; };
  table("scopen-" + key, [["token", "Token"], ["entry", "Entry", 1], ["price", "Current", 1], ["pnl", "P&L", 1], ["size", "Size", 1], ["scores", "Scores"], ["prob", "Win prob. (new)", 1], ["targets", "Exit targets"]],
    A.open.map(p => `<tr><td class="col-token">${tok(p.symbol, p.url, p.token)}${jupChip(p)}</td><td class="num col-entry">${price(p.entry_price)}</td>
      <td class="num col-price">${price(p.last_price)}${num(p.last_update) ? `<br><span class="small muted">${ago(p.last_update, now)} ago</span>` : ""}</td>
      <td class="num col-pnl ${cls(p.pnl_usd)}">${usd(p.pnl_usd)}<br><span class="small">${pct(p.pnl_pct)}</span></td><td class="num col-size">${usd(p.cost_usd)}</td>
      <td class="col-scores small">${scores(p)}${p.model_version ? `<br><span class="muted">${esc(p.model_version)}</span>` : ""}</td><td class="num col-prob">${prob(p)}</td><td class="col-targets">${estTargets(p, now)}</td></tr>`),
    "No open positions right now.");
  const CL = A.closed.slice(0, 60);
  $("scclosednote-" + key).textContent = A.closed.length ? `(${A.closed.length > CL.length ? `latest ${CL.length} of ${A.closed.length}` : A.closed.length}, newest first)` : "";
  table("scclosed-" + key, [["token", "Token"], ["opened", "Opened"], ["closedat", "Closed"], ["size", "Size", 1], ["entry", "Entry", 1], ["exit", "Exit", 1], ["pnl", "P&L", 1], ["scores", "Scores"], ["reason", "Exit reason"]],
    CL.map(p => `<tr><td class="col-token">${tok(p.symbol, p.url, p.token)}${jupChip(p)}</td><td class="col-opened">${tfmt(p.opened_at, true)}</td><td class="col-closedat">${tfmt(p.closed_at, true)}</td>
      <td class="num col-size">${usd(p.cost_usd)}</td><td class="num col-entry">${price(p.entry_price)}</td><td class="num col-exit">${price(p.last_price)}</td>
      <td class="num col-pnl ${cls(p.pnl_usd)}">${usd(p.pnl_usd)}<br><span class="small">${pct(p.pnl_pct)}</span></td><td class="col-scores small">${scores(p)}</td><td class="reasons">${esc(p.exit_reason || "—")}</td></tr>`),
    key === "cur" ? "No closed trades yet." : "No closed trades yet: the new-score account started " + scWhen(I.started) + ".");
  // decisions: the current account's list also carries older-coin entries (separate strategy, shown on the Older coins tab)
  const isOlder = (e) => /older[ -]coin/i.test(String(e.message || ""));
  const decs = A.decisions.filter(e => !isOlder(e)), hidden = A.decisions.length - decs.length, DL = decs.slice(0, 30);
  const SKC = {BUY: "ok", SELL: "wait", PAUSE: "bad", RESUME: "info", GUARD: "info", CORRECTION: "bad", SCORE: "score"};
  // score columns: this tab's own method first (New tab: new, old; Current tab: old, new); "-" when the decision has no score
  const sc1 = (v) => num(v) != null ? num(v).toFixed(1) : "-";
  const scCols = key === "new" ? [["scnew", "New score", 1], ["scold", "Old score", 1]] : [["scold", "Old score", 1], ["scnew", "New score", 1]];
  const scCells = (e) => (key === "new" ? [["scnew", e.score_new], ["scold", e.score_old]] : [["scold", e.score_old], ["scnew", e.score_new]])
    .map(([c, v]) => `<td class="num col-${c}${num(v) == null ? " muted" : ""}">${sc1(v)}</td>`).join("");
  $("scdecnote-" + key).textContent = decs.length || hidden ? `(latest ${DL.length}${hidden ? ` · ${hidden} older-coin entr${hidden === 1 ? "y" : "ies"} left out` : ""})` : "";
  table("scdec-" + key, [["time", "Time"], ["kind", "Action"], ["symbol", "Token"], ...scCols, ["message", "Details"]],
    DL.map(e => `<tr class="scdecrow"><td class="col-time">${tfmt(e.ts, true)}</td><td class="col-kind">${pill(SKC[e.kind] ?? "", e.kind || "—")}</td><td class="col-symbol">${esc(e.symbol || "")}</td>${scCells(e)}<td class="reasons">${esc(e.message || "")}</td></tr>`),
    key === "cur" ? "No decisions yet." : "No decisions yet for the new-score account.");
  scChart(d, A, key);
}
function scChart(d, A, key) {
  const cv = $("scchart-" + key); if (typeof Chart === "undefined" || !cv) return;
  const k = A.kpi, start = num(k.start) ?? 1000, pts = A.equity.slice();
  if (num(k.equity) != null && (!pts.length || num(pts[pts.length - 1].ts) < num(d.generated) - 30)) pts.push({ts: d.generated, equity: num(k.equity)});
  if (!pts.length) pts.push({ts: d.generated, equity: start});
  const T = chartTheme(), labels = pts.map(p => new Date(p.ts * 1000).toLocaleString("en-CA", {timeZone: TZ, month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false}));
  const vals = pts.map(p => num(p.equity)), lo = Math.min(...vals, start), hi = Math.max(...vals, start), pad = Math.max(2, (hi - lo) * 0.25);
  scCharts[key] = new Chart(cv, {type: "line", data: {labels, datasets: [
      {data: vals, borderColor: T.line, backgroundColor: T.fill, fill: true, pointRadius: pts.length < 3 ? 3 : 0, borderWidth: 2, tension: 0.2},
      {data: pts.map(() => start), borderColor: T.base, borderDash: [5, 5], pointRadius: 0, borderWidth: 1}]},
    options: {responsive: true, maintainAspectRatio: false, animation: false, plugins: {legend: {display: false}, tooltip: {...T.tip, callbacks: {label: c => usd(c.parsed.y)}}},
      scales: {x: {ticks: {color: T.txt, maxTicksLimit: 6, maxRotation: 0}, grid: {color: T.grid}}, y: {min: Math.floor(lo - pad), max: Math.ceil(hi + pad), ticks: {color: T.txt, callback: v => "$" + v}, grid: {color: T.grid}}}}});
}
// scanner rows: new score + both buy gates, e.g. "new 54.1 ✓"
function scoreNewCell(e) {
  const sn = num(e.score_new), gc = e.gate_current, gn = e.gate_new;
  const mk = (g) => g === "pass" ? "✓" : g === "reject" ? "✗" : "", kc = (g) => g === "pass" ? "ok" : g === "reject" ? "no" : "";
  const out = [];
  if (sn != null || (gn && gn !== "n.a.")) out.push(`<span class="sgate ${kc(gn)}" title="${esc(`New score ${sn != null ? sn.toFixed(1) : "not computed"}${gn ? `; new-score buy gate: ${gn}` : ""}`)}">new ${sn != null ? sn.toFixed(1) : "—"}${mk(gn) ? " " + mk(gn) : ""}</span>`);
  if (gc && gc !== "n.a.") out.push(`<span class="sgate ${kc(gc)}" title="${esc(`Current-score buy gate: ${gc}`)}">cur ${mk(gc) || esc(gc)}</span>`);
  return out.length ? `<div class="sgates">${out.join("")}</div>` : "";
}
function scoreDoc(d, facts) {
  const I = scInfo(d);
  if (!I.S) return `<section class="card doc" id="doc-scoring"><h2>Scoring test (side by side)</h2><p class="muted">${SC_NA}.</p></section>`;
  const th = scThr(d, I), b = (s) => `<b class="cv">${s}</b>`;
  const txt = `<b>Side-by-side test (since ${scWhen(I.started)}):</b> a second ${b(usd(I.start, 0))} paper account sees the same coins and passes the same safety checks, but buys on a new score (${b(esc(scModel(I)))}, trained on our own trade history, favours younger pools; buys at ${b(th.f(th.nb) + "+")}, or ${b(th.f(th.nCap) + "+")} at ${th.half} on a bad day). It never trades real money; real trades still follow the current score.`;
  return `<section class="card doc" id="doc-scoring"><h2>Scoring test (side by side) ${paperOnly}</h2>
    <p class="rt-note jupnote">${txt}</p>
    <p>Both accounts start from ${usd(I.start, 0)}, see every candidate that passes the filters and safety checks, and use the same exits and daily limits. Only the buy decision differs: the <b>current score</b> buys at ${b(th.f(th.cur) + "+")} (${b(th.f(th.curCap) + "+")} at ${th.half} on a bad day); the <b>new score</b> buys at ${b(th.f(th.nb) + "+")} (${b(th.f(th.nCap) + "+")} at ${th.half} on a bad day). The <a href="#scorecur">Current scoring</a> and <a href="#scorenew">New scoring</a> tabs show each account; the comparison at the top counts trades, win rate, P&amp;L and the largest drop (max drawdown) since the test started, and which coins both, or only one, of them bought. On the Scanner tab each coin shows its new score and whether each score would buy it (e.g. <span class="sgate ok">new 54.1 ✓</span> <span class="sgate no">cur ✗</span>).</p>
    <p class="rt-note jupnote"><b>New scoring fix (Oct 6):</b> the new model treats a pool whose liquidity is close to or above the coin's market cap as unreliable and used to score it 0. On Solana meme pools that is almost always the case, so every coin got 0. Those coins are now scored without the liquidity and market cap numbers, the same way Jupiter-sourced coins already were. Paper account only; real money still follows the old scoring.</p>
    <p><b>Decision log:</b> each scoring tab now lists every coin its method scored, including ones it turned down, with the other method's score for comparison.</p>
    ${facts([["Started", b(scWhen(I.started))], ["Balance each", b(usd(I.start, 0))], ["Current score buys", th.curB], ["New score buys", th.newB], ["Model", b(esc(I.ver || "scoring_v1"))],
      ["Self-test", I.st ? (I.st.ok ? `passed${num(I.st.rows) != null ? ` (${num(I.st.rows)} rows)` : ""}` : "failed: the new account stays off") : "—"], ["Real money", `current score only`]])}</section>`;
}

// ---------------------------------------------------------------- bento layout (PREVIEW, Oct 3 2026)
// Focus number + bento tiles on Home, tiny sparklines on Trades / Scanner rows, soft motion (count-up, draw-in, pulse).
// All motion is skipped when the visitor asks for reduced motion.
const RM = () => !!(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches);
let BENTO_FIRST = true, SEEN = null;
// tiny SVG sparkline from a list of prices; returns "" (nothing drawn) when there are fewer than 2 usable points
function spark(vals, o = {}) {
  vals = (vals || []).map(num).filter(v => v != null && isFinite(v) && v > 0);
  if (vals.length < 2) return "";
  const w = o.w || 60, h = o.h || 20, p = 2.5, lo = o.lo ?? Math.min(...vals), hi = o.hi ?? Math.max(...vals), span = hi - lo || 1;
  const pts = vals.map((v, i) => [p + i * (w - 2 * p) / (vals.length - 1), hi === lo ? h / 2 : p + (hi - v) / span * (h - 2 * p)]);
  const d = pts.map((q, i) => (i ? "L" : "M") + q[0].toFixed(1) + " " + q[1].toFixed(1)).join("");
  const last = pts[pts.length - 1], tone = o.tone || (vals[vals.length - 1] >= vals[0] ? "up" : "down");
  return `<span class="spk ${tone}" title="${esc(o.title || "")}"><svg class="spark" viewBox="0 0 ${w} ${h}" width="${w}" height="${h}" aria-hidden="true">`
    + `<path class="sl" pathLength="1" d="${d}"/><circle class="se" cx="${last[0].toFixed(1)}" cy="${last[1].toFixed(1)}" r="2"/></svg>${o.tag ? `<small>${esc(o.tag)}</small>` : ""}</span>`;
}
// trades only carry entry, peak and last/exit prices: draw entry → peak → exit (peak only when it sits above both ends)
function tradeSpark(p) {
  const e = num(p.entry_price), pk = num(p.peak_price), l = num(p.last_price);
  if (!(e > 0) || !(l > 0)) return "";
  const v = [e]; if (pk > 0 && pk > Math.max(e, l) * 1.001) v.push(pk); v.push(l);
  const open = p.status === "open" || !p.closed_at, pv = num(p.pnl_usd);
  return spark(v, {tone: pv == null ? undefined : pv >= 0 ? "up" : "down", title: `entry ${price(e)}${v.length === 3 ? ` → peak ${price(pk)}` : ""} → ${open ? "now" : "exit"} ${price(l)}`});
}
// scanner rows: the feed has the current price and the 1h change, so the line is "1h ago → now"
function scanSpark(m) {
  const pr = num(m && m.price), c = num(m && m.chg_h1);
  if (!(pr > 0) || c == null || c <= -100) return "";
  // fixed scale (at least ±20%) so a tiny move draws a gentle slope instead of a full-height line
  const M = Math.max(20, Math.abs(c));
  return spark([100, 100 + c], {w: 44, lo: 100 - M, hi: 100 + M, tone: Math.abs(c) < 0.05 ? "flat" : c > 0 ? "up" : "down", tag: "1h " + pct(c, Math.abs(c) < 10 ? 1 : 0), title: `price 1h ago → now (${pct(c)})`});
}
// wide trend line (equity) as a responsive SVG
function wideSpark(el, pts, base) {
  if (!el) return;
  const v = pts.map(q => num(q.equity)).filter(x => x != null && isFinite(x));
  if (v.length < 2) { el.innerHTML = ""; el.classList.add("hidden"); return; }
  el.classList.remove("hidden");
  const step = Math.max(1, Math.ceil(v.length / 160)), s = v.filter((_, i) => i % step === 0 || i === v.length - 1);
  const lo = Math.min(...s, base ?? Infinity), hi = Math.max(...s, base ?? -Infinity), sp = hi - lo || 1, W = 1000, H = 100;
  const y = (x) => (4 + (hi - x) / sp * (H - 8)).toFixed(1);
  const d = s.map((x, i) => (i ? "L" : "M") + (i * W / (s.length - 1)).toFixed(1) + " " + y(x)).join("");
  const ly = y(s[s.length - 1]);
  el.innerHTML = `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" aria-hidden="true">`
    + (base != null ? `<path class="fb" d="M0 ${y(base)}H${W}" vector-effect="non-scaling-stroke"/>` : "")
    + `<path class="fa" d="${d}L${W} ${H}L0 ${H}Z"/><path class="sl" pathLength="1" d="${d}" vector-effect="non-scaling-stroke"/></svg>`
    + `<i class="fdot" style="top:${ly}%"></i>${base != null ? `<span class="fbl" style="top:${y(base)}%">start ${usd(base, 0)}</span>` : ""}`;
}
function ring(pctv) {
  const v = Math.max(0, Math.min(100, num(pctv) ?? 0));
  return `<svg viewBox="0 0 36 36" aria-hidden="true"><circle class="rb" cx="18" cy="18" r="15.5"/><circle class="rf" cx="18" cy="18" r="15.5" pathLength="100" style="stroke-dasharray:${v.toFixed(1)} 100"/></svg>`;
}
function focusTop(d) {
  const k = d.kpi, eq = d.equity, tz = (ts) => new Date(ts * 1000).toLocaleDateString("en-CA", {timeZone: TZ});
  const today = tz(d.generated), wk = eq.length ? eq[0] : null, last = eq.length ? eq[eq.length - 1] : null;
  const ch = (l, v, extra) => `<span class="fchip"><span class="muted">${l}</span> <b class="${cls(v)}">${usd(v)}</b>${extra ? ` <span class="${cls(v)} small">${extra}</span>` : ""}</span>`;
  const dm = dayMode(d);
  let chips = ch("Today", k.day_pnl, dm.dpp != null ? pct(dm.dpp, 1) : "");
  if (wk && last && wk !== last) { const w = num(last.equity) - num(wk.equity); chips += ch(d.generated - wk.ts > 6.5 * 86400 ? "7 days" : `Since ${new Date(wk.ts * 1000).toLocaleDateString("en-US", {timeZone: TZ, month: "short", day: "numeric"})}`, w, num(wk.equity) ? pct(w / num(wk.equity) * 100, 1) : ""); }
  $("focuschips").innerHTML = chips;
  wideSpark($("focusspark"), eq, num(k.start));
  // today's mini line in the Today tile (equity points since midnight Toronto)
  const tp = eq.filter(q => tz(q.ts) === today), dayEl = $("kpi-day");
  let ds = dayEl.querySelector(".dspark"); if (!ds) { ds = document.createElement("div"); ds.className = "dspark"; dayEl.appendChild(ds); }
  ds.innerHTML = tp.length >= 2 && new Set(tp.map(q => num(q.equity).toFixed(2))).size > 1 ? spark(tp.map(q => q.equity), {w: 120, h: 26, title: "equity today"}) : `<span class="muted small">no trades today yet</span>`;
}
function winTile(d) {
  const k = d.kpi, S = (d.strategies || {}).new || {};
  $("winring").innerHTML = ring(k.win_rate);
  const bits = [];
  if (num(S.avg_trade_pct) != null) bits.push(`avg trade <b class="${cls(S.avg_trade_pct)}">${pct(S.avg_trade_pct)}</b>`);
  if (num(S.avg_hold_hours) != null) bits.push(`avg hold <b>${dur(S.avg_hold_hours * 3600)}</b>`);
  if (num(S.best_trade_pct) != null) bits.push(`best <b class="pos">${pct(S.best_trade_pct, 0)}</b>`);
  $("winfoot").innerHTML = bits.map(b => `<span>${b}</span>`).join("");
}
function realTile(d) {
  const el = $("realtile"); if (!el) return;
  const r = liveInfo(d);
  const head = `<h2><span>💵 Real money</span> ${realTag}${r.missing ? "" : " " + onOff(r)}<a href="#wallet" class="small more">Wallet</a></h2>`;
  if (r.missing) { el.innerHTML = head + `<div class="empty">Real wallet data unavailable${r.error ? "" : " in this data"}. Paper figures are not affected.</div>`; return; }
  const pc = r.pnl != null ? cls(r.pnl) : "", s = spendTile(r), db = realDayBadge(r);
  el.innerHTML = head + `<div class="rbig"><span class="value ${pc}" data-count>${r.pnl != null ? usd(r.pnl) : "—"}</span>${r.pnlPct != null ? `<span class="chg ${pc}">${pct(r.pnlPct, 1)}</span>` : ""}</div>
    <div class="muted small">total real P&amp;L vs ${r.funded ? usd(r.funded) : "the"} deposit, incl. network fees · ${wl(r)}</div>${db ? `<div class="rdayrow">${db}</div>` : ""}
    <div class="rmini"><div><span class="lb">Wallet total</span><b>${usd(r.total)}</b><span class="muted small">${r.sol != null ? r.sol.toLocaleString("en-US", {maximumFractionDigits: 4}) + " SOL" : "SOL —"}${r.wts ? ` · ${ago(r.wts, d.generated)} ago` : ""}</span></div>
      <div><span class="lb">${s.label}</span><b>${s.value}</b>${s.bar || ""}<span class="muted small">${s.sub}</span></div></div>
    ${r.trades.length ? `<div class="rlist2">${r.trades.slice(0, 4).map(t => `<div class="rl"><span class="muted">${tfmt(num(t.ts), true)}</span>${rtSide(t)}<b>${tokLink(t.symbol, t.mint)}</b><span class="rl-p">${pnlCell(t.position_pnl_usd, t.position_pnl_pct, t.position_open === true)}</span></div>`).join("")}</div>` : `<div class="muted small">No real trades yet.</div>`}`;
}
function scanTile(d) {
  const k = d.kpi, c = d.cycles[0], cyc = d.cycles.slice(0, 20).reverse(), mx = Math.max(1, ...cyc.map(x => num(x.evaluated) || 0));
  const bars = cyc.length >= 2 ? `<div class="bars" title="tokens checked per scan cycle (last ${cyc.length})">${cyc.map(x => `<i style="height:${Math.max(6, (num(x.evaluated) || 0) / mx * 100).toFixed(0)}%"${num(x.passed) ? ' class="hit"' : ""}></i>`).join("")}</div>` : "";
  $("scantile").innerHTML = `<div class="sbig"><b>${(num(k.scanned_today) ?? 0).toLocaleString("en-US")}</b> <span class="muted small">tokens today</span></div>`
    + (c ? `<div class="muted small">cycle #${esc(c.id)} · ${c.evaluated ?? 0} checked · <span class="${c.passed ? "pos" : ""}">${c.passed ?? 0} passed</span></div>` : "") + bars;
}
function healthTile(d) {
  const el = $("healthtile"); if (!el) return;
  const s = scanInfo(d), now = dataNow(d), hb = hbTs(d), [lp, lt] = liveHealth();
  const word = OFFLINE.on ? "Bot offline" : HEALTH === "ok" ? "All running" : HEALTH === "wait" ? "Needs a look" : "Problem";
  el.innerHTML = `<h2><span>Bot health</span><a href="#health" class="small more">Details</a></h2>
    <div class="hbig ${OFFLINE.on ? "off" : HEALTH}"><i></i>${word}</div>
    <div class="hrows"><span>Scanner</span><b>${cycleTs(d, s) ? ago(cycleTs(d, s), now) + " ago" : "—"}</b>
      <span>Trader</span><b class="${hb != null && now - hb > 300 ? "neg" : ""}">${hb != null ? ago(hb, now) + " ago" : "—"}</b>
      <span>Live prices</span><b title="${esc(lt)}">${lp}</b>
      ${OFFLINE.on ? `<span>Offline for</span><b class="warn">${OFFLINE.since != null ? ago(OFFLINE.since, now) : "unknown"}</b>` : `<span>Up for</span><b>${num(d.bot_started) ? ago(d.bot_started, now) : "—"}</b>`}</div>`;
}
// numbers count up from zero on first load (formatting kept: $, commas, decimals, %)
function countUp(root, ms = 850) {
  const w = document.createTreeWalker(root, NodeFilter.SHOW_TEXT), nodes = [];
  while (w.nextNode()) nodes.push(w.currentNode);
  for (const n of nodes) {
    const full = n.nodeValue, m = full.match(/(\d[\d,]*)(\.\d+)?/);
    if (!m) continue;
    const target = parseFloat((m[1] + (m[2] || "")).replace(/,/g, "")), dec = m[2] ? m[2].length - 1 : 0, grp = m[1].includes(",");
    if (!isFinite(target) || target === 0) continue;
    const pre = full.slice(0, m.index), post = full.slice(m.index + m[0].length), t0 = performance.now();
    const fmt = (v) => pre + v.toLocaleString("en-US", {minimumFractionDigits: dec, maximumFractionDigits: dec, useGrouping: grp}) + post;
    let wrote = fmt(0); n.nodeValue = wrote;
    const tick = (t) => {
      if (!n.isConnected || n.nodeValue !== wrote) return;  // re-rendered meanwhile: leave the new value alone
      const f = Math.min(1, (t - t0) / ms), e = 1 - Math.pow(1 - f, 3);
      wrote = f >= 1 ? full : fmt(target * e); n.nodeValue = wrote;
      if (f < 1) requestAnimationFrame(tick);
    };
    requestAnimationFrame(tick);
  }
}
// draw-in: sparklines / ring / bars animate when a screen is shown (class removed afterwards so refreshes stay still)
function drawIn(view) {
  if (RM()) return;
  const v = view || document.querySelector(".view.on"); if (!v) return;
  v.classList.remove("drawin"); void v.offsetWidth; v.classList.add("drawin");
  clearTimeout(v._dt); v._dt = setTimeout(() => v.classList.remove("drawin"), 1600);
}
window.addEventListener("hashchange", () => drawIn());
// soft pulse on trades that are new since the last refresh (first load: opened/closed in the last 10 minutes)
function pulseNew(d) {
  const key = (p) => (p.status === "open" || !p.closed_at ? "o" : "c") + p.id;
  const all = [...d.open, ...d.closed], ids = new Set(all.map(key)), now = d.generated, fresh = [];
  for (const p of all) { const id = key(p); if (SEEN ? !SEEN.has(id) : now - (num(p.closed_at) || num(p.opened_at) || 0) < 600) fresh.push(p); }
  SEEN = ids;
  for (const p of fresh) {
    const sel = p.status === "open" || !p.closed_at ? `tr[data-pos="${CSS.escape(String(p.id))}"]` : `[data-cid="${CSS.escape(String(p.id))}"]`;
    document.querySelectorAll(sel).forEach(el => { el.classList.remove("pulse"); void el.offsetWidth; el.classList.add("pulse"); setTimeout(() => el.classList.remove("pulse"), 4200); });
  }
  if (fresh.some(p => p.status === "open")) { const t = $("t-open"); t && (t.classList.add("pulse"), setTimeout(() => t.classList.remove("pulse"), 4200)); }
  return fresh.length;
}
function bento(d) {
  if (!$("bento")) return;
  focusTop(d); winTile(d); realTile(d); scanTile(d); healthTile(d);
  $("t-open").classList.toggle("has-rows", d.open.length > 0);
  pulseNew(d);
  if (BENTO_FIRST) {
    BENTO_FIRST = false;
    if (!RM()) { document.querySelectorAll("#focus [data-count], #bento [data-count]").forEach(el => countUp(el)); drawIn(); }
  }
}

// ---------------------------------------------------------------- render
function norm(d) {
  return {...d, kpi: d.kpi || {}, status: d.status || {}, open: d.open || [], closed: d.closed || [], cycles: d.cycles || [], feed: d.feed || [],
    deep: d.deep || [], decisions: d.decisions || [], equity: (d.equity || []).filter(e => num(e.ts) && num(e.equity) != null), config: d.config || {}};
}
function render(raw) {
  const d = norm(raw); DATA = d;
  checkOffline(d, false);
  $("updated").textContent = "Data last updated " + new Date(d.generated * 1000).toLocaleString("en-CA", {timeZone: TZ, hour12: false}) + " (Toronto time)"
    + (window.__DATA_URL__ ? " · published right after every trade (else every 5 min)" : "");
  realTestChips(d); kpis(d); stratCard(d); realCard(d); walletView(d); equityChart(d); recent(d); positions(d); scanStats(d); funnel(d); feeds(d); health(d); config(d); docs(d); olderView(d); scoreCard(d); scoreView(d, "cur"); scoreView(d, "new");
  bento(d);
  applyLive();
}

// simple screen router
const VIEWS = {home: "Home", wallet: "Real wallet", trades: "Trades", older: "Older coins", scorecur: "Current scoring", scorenew: "New scoring", scanner: "Scanner", health: "Health", docs: "Docs", settings: "Settings"};
function route() {
  const v = (location.hash || "#home").slice(1), view = VIEWS[v] ? v : "home";
  document.querySelectorAll(".view").forEach(s => s.classList.toggle("on", s.dataset.view === view));
  document.querySelectorAll("#nav a").forEach(a => a.classList.toggle("on", a.dataset.v === view));
  $("tbtitle").textContent = VIEWS[view]; window.scrollTo(0, 0);
  if (view === "home" && chart) chart.resize();
  if (view === "older" && ochart) ochart.resize();
  if (view === "scorecur" && scCharts.cur) scCharts.cur.resize();
  if (view === "scorenew" && scCharts.new) scCharts.new.resize();
  const more = MORE_V.includes(view);
  $("navmore") && $("navmore").classList.toggle("on", more);
  closeMore();
}
const MORE_V = ["scorecur", "scorenew", "health", "docs", "settings"];
// phone "More" sheet: the scoring tabs, Health, Docs and Settings live here on the bottom bar (the desktop rail lists every tab)
function closeMore() { const s = $("moresheet"); if (s) s.classList.add("hidden"); $("navmore") && $("navmore").setAttribute("aria-expanded", "false"); }
(function initMore() {
  const btn = $("navmore"), sheet = $("moresheet"); if (!btn || !sheet) return;
  sheet.innerHTML = [...document.querySelectorAll("#nav a[data-v]")].filter(a => MORE_V.includes(a.dataset.v))
    .map(a => `<a href="#${a.dataset.v}" role="menuitem">${a.querySelector("svg").outerHTML}<span>${(a.querySelector(".lbl-l") || a).textContent.trim()}</span></a>`).join("");
  btn.addEventListener("click", e => { e.preventDefault(); const open = sheet.classList.toggle("hidden") === false; btn.setAttribute("aria-expanded", String(open)); });
  sheet.addEventListener("click", () => closeMore());
  document.addEventListener("click", e => { if (!sheet.classList.contains("hidden") && !sheet.contains(e.target) && !btn.contains(e.target)) closeMore(); });
})();
// "How it works" links that jump to a Docs section
document.addEventListener("click", e => { const a = e.target.closest && e.target.closest("a[data-doc]"); if (!a) return;
  const id = a.dataset.doc; setTimeout(() => { const el = $(id); el && el.scrollIntoView({block: "start"}); }, 60); });
window.addEventListener("hashchange", route); route();
$("tradeseg").addEventListener("click", e => { const t = e.target.dataset.t; if (!t) return;
  $("tradeseg").querySelectorAll("button").forEach(b => b.classList.toggle("on", b.dataset.t === t));
  document.querySelectorAll('.view[data-view="trades"] .card').forEach(c => c.classList.toggle("hidden", c.dataset.t !== t)); });
$("scanseg").addEventListener("click", e => { const t = e.target.dataset.s; if (!t) return;
  $("scanseg").querySelectorAll("button").forEach(b => b.classList.toggle("on", b.dataset.s === t));
  document.querySelectorAll('.view[data-view="scanner"] .card[data-s]').forEach(c => c.classList.toggle("hidden", c.dataset.s !== t)); });
$("feedmore").addEventListener("click", () => { feedAll = !feedAll; DATA && feeds(DATA); });

// ---------------------------------------------------------------- live prices (browser-side)
// Open positions are re-priced straight from DexScreener's public API (CORS-enabled) every 10s,
// using the same exit-cost assumption as the bot: value = qty * price * (1 - slippage) * (1 - fee).
const LIVE = {prices: {}, lastOk: null, lastTry: null, failed: false, timer: null};
const LIVE_MS = 10000, LIVE_STALE_MS = 45000;
const liveEnabled = () => !window.__SNAPSHOT__;

async function pollLive() {
  if (!DATA || document.hidden || !liveEnabled()) return;
  const pairs = [...new Set(DATA.open.map(p => p.pair).filter(Boolean))].slice(0, 30);
  if (!pairs.length) { applyLive(); return; }
  LIVE.lastTry = Date.now();
  try {
    const r = await fetch("https://api.dexscreener.com/latest/dex/pairs/solana/" + pairs.join(","), {cache: "no-store"});
    if (!r.ok) throw new Error("HTTP " + r.status);
    const j = await r.json();
    let got = 0;
    for (const p of (j.pairs || [])) {
      const v = parseFloat(p.priceUsd);
      if (p.pairAddress && v > 0) { LIVE.prices[p.pairAddress] = {price: v, liq: num((p.liquidity || {}).usd), token: (p.baseToken || {}).address, ts: Date.now()}; got++; }
    }
    if (!got) throw new Error("no prices");
    LIVE.lastOk = Date.now(); LIVE.failed = false;
  } catch (e) { LIVE.failed = true; }
  applyLive();
}

function flash(el, up) {
  el.classList.remove("live-flash-up", "live-flash-down");
  void el.offsetWidth;  // restart animation
  el.classList.add(up ? "live-flash-up" : "live-flash-down");
}

// Paper exit value, same formula as the bot (priceguard.sell_value): price minus slippage, capped by what a
// constant-product pool with this liquidity could pay (x·y=k), then minus the fee.
function sellValue(qty, px, P, G, liq) {
  let gross = qty * px * (1 - (num(P.slippage_pct) || 0) / 100);
  if (G.fill_cap !== false && liq != null) { const r = Math.max(liq, 0) / 2, v = qty * px; gross = Math.min(gross, r > 0 && v > 0 ? r * v / (r + v) : 0); }
  return gross * (1 - (num(P.fee_pct) || 0) / 100);
}
function applyLive() {
  if (!DATA) return;
  const P = DATA.config.paper || {}, G = DATA.config.guard || {};
  const jump = num(G.max_jump_up_factor) || 5, dropPct = num(G.max_drop_pct) || 50;
  const fresh = LIVE.lastOk && Date.now() - LIVE.lastOk < LIVE_STALE_MS;
  let openValue = 0, anyLive = false, uncapped = false, held = false;
  for (const p of DATA.open) {
    let lp = fresh && LIVE.prices[p.pair];
    // same idea as the bot's price guard: ignore readings for another coin, and don't show extreme moves
    // (> jump× or more than drop% below the bot's last good price) until the bot itself has confirmed them
    const ref = num(p.last_price) || num(p.entry_price);
    let isHeld = false;
    if (lp && (lp.token && lp.token !== p.token)) lp = null;
    if (lp && G.enabled !== false && ref && (lp.price / ref >= jump || lp.price / ref <= 1 - dropPct / 100)) { lp = null; isHeld = true; held = true; }
    const px = lp ? lp.price : p.last_price;
    let value, pnl, pc;
    if (lp) {
      anyLive = true;
      const liq = lp.liq != null ? lp.liq : num(p.last_liq);
      if (liq == null) uncapped = true;
      value = sellValue(p.remaining_qty, px, P, G, liq);
      pnl = (p.proceeds_usd || 0) + value - p.cost_usd; pc = pnl / p.cost_usd * 100;
    } else if (num(p.pnl_usd) != null) {
      // no live price: the bot's own values (already include the fill cap)
      pnl = num(p.pnl_usd); pc = num(p.pnl_pct) ?? pnl / p.cost_usd * 100; value = p.cost_usd + pnl - (p.proceeds_usd || 0);
    } else {
      value = sellValue(p.remaining_qty, px, P, G, num(p.last_liq)); if (num(p.last_liq) == null) uncapped = true;
      pnl = (p.proceeds_usd || 0) + value - p.cost_usd; pc = pnl / p.cost_usd * 100;
    }
    openValue += value;
    for (const row of document.querySelectorAll(`#open tr[data-pos="${CSS.escape(String(p.id))}"], #open2 tr[data-pos="${CSS.escape(String(p.id))}"]`)) {
      const pe = row.querySelector('[data-f="price"]'), ne = row.querySelector('[data-f="pnl"]');
      const tag = lp ? `<span class="small live-tag">live</span>` : isHeld ? `<span class="small warn" title="Live price moved too far; waiting for the bot's price guard to confirm it">held</span>` : `<span class="small muted">snapshot</span>`;
      const priceHtml = price(px) + `<br>${tag}`;
      if (pe.innerHTML !== priceHtml) pe.innerHTML = priceHtml;
      const pnlHtml = `${usd(pnl)}<br><span class="small">${pct(pc)}</span>`;
      if (ne.innerHTML !== pnlHtml) {
        const prev = parseFloat(ne.dataset.v);
        ne.innerHTML = pnlHtml; ne.className = `num col-pnl ${cls(pnl)}`; ne.dataset.v = pnl;
        if (!isNaN(prev) && Math.abs(prev - pnl) >= 0.005) flash(ne, pnl > prev);
      }
    }
  }
  LIVE.uncapped = uncapped; LIVE.held = held;
  // live hero + P&L tile: equity = cash + exit value of open positions (same formula as the bot, fill cap included)
  const k = DATA.kpi;
  if (DATA.open.length && anyLive) {
    const eq = k.cash + openValue, tp = eq - k.start, tpp = k.start ? tp / k.start * 100 : 0;
    const e = $("kpi-equity"), t = $("kpi-pnl");
    if (e) { e.querySelector(".value").innerHTML = usd(eq) + ` <span class="live-dot" title="live"></span>`;
             $("hero-sub").innerHTML = heroSub(tp, tpp, `all time · live · cash ${usd(k.cash)}`); }
    if (t) { t.querySelector(".value").innerHTML = `<span class="${cls(tp)}">${usd(tp)}</span>`;
             t.querySelector(".sub").innerHTML = `<span class="${cls(tp)}">${pct(tpp, 2)}</span> · live`; }
  }
  liveBadge(anyLive);
}

function liveHealth() {
  if (!DATA || !DATA.open.length) return [pill("", "idle"), "no open positions to price"];
  if (!liveEnabled()) return [pill("wait", "snapshot"), "static snapshot: prices as of the export"];
  if (LIVE.lastOk && Date.now() - LIVE.lastOk < LIVE_STALE_MS) return [pill("ok", "live"), `DexScreener every ${LIVE_MS / 1000}s · updated ${Math.round((Date.now() - LIVE.lastOk) / 1000)}s ago`];
  return LIVE.failed ? [pill("bad", "unavailable"), "DexScreener not reachable, showing last published values"] : [pill("wait", "loading"), "fetching live prices…"];
}
function liveBadge(anyLive) {
  const hl = $("h-live"), hd = $("h-live-d");
  if (hl && DATA) { const [p, t] = liveHealth(); if (hl.innerHTML !== p) hl.innerHTML = p; hd.textContent = t; }
  const b = $("livebadge"); if (!b || !DATA) return;
  if (!DATA.open.length) { b.innerHTML = ""; return; }
  if (!liveEnabled()) { b.innerHTML = `<span class="live-snap">snapshot</span>`; return; }
  if (anyLive && LIVE.lastOk) {
    const s = Math.round((Date.now() - LIVE.lastOk) / 1000);
    const capNote = LIVE.uncapped ? " · live P&L before fill cap" : " · P&L after pool fill cap";
    b.innerHTML = `<span class="live-dot"></span><span class="live-label">LIVE</span> <span class="muted small">price updated ${s}s ago${document.hidden ? " (paused)" : ""}${capNote}${LIVE.held ? " · extreme move held for the bot to confirm" : ""}</span>${OFFLINE.on ? ` <span class="small warn">· prices only: the bot is offline, so exits are not being managed</span>` : ""}`;
  } else {
    b.innerHTML = `<span class="live-snap">snapshot</span> <span class="muted small">${LIVE.failed ? "live prices unavailable, showing last published values" : "loading live prices…"}</span>`;
  }
}

function startLive() {
  if (!liveEnabled()) return;
  clearInterval(LIVE.timer);
  LIVE.timer = setInterval(pollLive, LIVE_MS);
  pollLive();
}
document.addEventListener("visibilitychange", () => {
  if (document.hidden) { clearInterval(LIVE.timer); LIVE.timer = null; liveBadge(true); }
  else startLive();
});
setInterval(() => DATA && liveBadge(LIVE.lastOk && Date.now() - LIVE.lastOk < LIVE_STALE_MS), 1000);

// Modes: live local server (api/state every 12s), published static site (data.json every 15s),
// or a single-file snapshot (data inlined in window.__SNAPSHOT__, no refresh).
const DATA_URL = window.__DATA_URL__ || "api/state";
const REFRESH_MS = window.__DATA_URL__ ? 15000 : 12000;
async function load() {
  try {
    const r = await fetch(DATA_URL + (window.__DATA_URL__ ? "?t=" + Date.now() : ""), {cache: "no-store"});
    if (r.ok) render(await r.json());
    else $("updated").textContent = "Could not load data (HTTP " + r.status + ") — retrying…";
  } catch (e) { $("updated").textContent = "Connection problem — retrying…"; }
}

$("hidePending").onchange = $("hideLiq").onchange = () => DATA && feeds(DATA);
if (window.__SNAPSHOT__) { $("snapnote").classList.remove("hidden"); render(window.__SNAPSHOT__); }
else { load().then(startLive); setInterval(load, REFRESH_MS); }
