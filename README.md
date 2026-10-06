# Memebot — Solana meme coin scanner with PAPER trading

> **PAPER TRADING ONLY — fake money.** This program has no wallet, no private keys and
> cannot buy or sell anything for real. It only *pretends* to trade so you can see how a
> strategy would have done.

## What it does

1. **Finds new Solana tokens** using free public data: it starts a new scan every 90 seconds;
   when a scan takes longer (free-API slowdowns) the next one starts 5 seconds after it
   finishes, so typically every 1.5–3 minutes. Sources: GeckoTerminal new pools (2 pages) and
   trending pools (6-hour view); the top-volume list is switched off in config.toml. Every
   token it finds goes on the watchlist. Each scan re-checks only the 600 most recently
   discovered tokens. Older ones stay on the list but are no longer checked. A re-checked
   token whose pool is over 24 h old is dropped.
2. **Runs each token through a funnel of filters**, cheapest first, and logs every result
   with the exact reason:
   - listed on DexScreener → pool age 10 min–24 h → liquidity ≥ $15k → 1h/24h volume →
     market-cap range → not already up more than 200% in the last hour → real buys **and**
     sells (near-zero sells = possible honeypot): at least 10 buys and 5 sells in the last
     hour, sells at least 0.2× buys, and at least 150 trades in 24 h
   - then the more expensive safety checks: mint authority revoked, freeze authority
     revoked, honeypot flag, biggest wallet ≤ 5 % and top-10 ≤ 60 % (pool/LP accounts
     excluded, read from the Solana blockchain), minimum holders
   - holder trend: price must not be running up while the holder count stays flat
   - a simple, transparent **score** (0–100; the parts are listed next to each token).
3. **Paper trades the survivors** with a fake $1,000: max 3 positions, 1.5 % assumed
   slippage+fees each way. Size: 2% of current equity (cash plus open positions), never more
   than 2% of pool liquidity or the cash available. Exits are measured on the market price
   versus the entry price, before the ~1.5% costs each way. A +50% take-profit nets about
   +45.6%, and a −25% stop about −27.2%. TP1 at +50% sells half; TP2 at +150% sells the rest.
   TP2 is checked first, so a jump straight past +150% sells everything at once. The stop
   stays at −25% after TP1. Trailing stop: once the price has been +30% above entry, it sells
   on a 20% drop from the peak. Volume-decay exit only from pool age 2 h. Max hold 24 h.
   Daily cap: new entries pause when today's P&L, counting open positions at their latest
   price, reaches −5% of the equity at the start of the Toronto day.
4. **Dashboard** shows balance, P&L, win rate, equity chart, open/closed trades, the filter
   funnel, the live scan feed with reasons, and the current settings.

## Opening the dashboard

- **From any browser, anywhere:** the public web address is in `public_url.txt`
  (a Netlify site with a random, hard-to-guess name; search engines are told not to index it).
  It is a read-only copy that is re-published within about 10 seconds of every paper buy or
  sell (including partial take-profits), and every 5 minutes otherwise. The page re-checks for
  new data every 15 seconds, shows "Data last updated …" in Toronto time, and prices open
  positions live from DexScreener every 10 seconds.
- **On this machine:** http://localhost:8787 (live, refreshes every ~12 seconds).
- Nothing on either page can change the bot.
- `snapshot.html` is a single file with everything inside it — you can email it or open it
  offline (`python export_snapshot.py` makes a fresh one).

## Telegram alerts and commands

The Telegram bot (**@nwarviebot**) sends you a short message for every paper buy, every sell
(including partial take-profits) with P&L and the new fake balance, when the daily soft cap
kicks in (only high-score trades at half size), when the daily hard stop is hit (no new entries), when a new day resets them, and when the bot is started, stopped or restarts itself.

**One-time setup:** open @nwarviebot in Telegram and press **Start**. The bot remembers
your chat (saved in `telegram_chat.json`) and only ever answers you.

Commands:

| Command | What it does |
|---|---|
| `/status` | fake balance, P&L, open positions, when the scanner last ran |
| `/positions` | open paper positions with P&L |
| `/pause` | stop opening new paper positions (open ones are still managed); shows on the dashboard |
| `/resume` | allow new paper positions again |
| `/dashboard` | the dashboard link |
| `/help` | list of commands |

Alerts are queued in the database first, so a Telegram outage never affects the bot; they
are delivered when Telegram is reachable again, and restarts never resend old alerts.

## Daily loss limits (two tiers, changed Sep 26, 2026)
"Today's P&L" is fake equity now minus fake equity at midnight Toronto time.
- **Normal:** all entries allowed.
- **Soft cap:** today's P&L is at or below -5% (`daily_loss_cap_pct`). New buys are still allowed, but only for candidates
  scoring at least 75 (`cap_min_score`), and at half the normal size (`cap_size_factor = 0.5`). All other limits and
  price safeguards still apply. Lower-score candidates are logged as `[SKIP] X score 62 < 75 (daily soft cap active)`,
  and buys as `[BUY] ... reduced size 50% (daily soft cap)`. Soft-cap mode lifts again if today's P&L recovers above -5%.
- **Hard stop:** today's P&L is at or below -10% (`daily_hard_stop_pct`). No new entries at all until midnight Toronto time,
  even if P&L recovers.
Open positions are always managed normally. Telegram alerts you when each tier starts. `/status`, `status.sh` and
the dashboard show the current mode (`kpi.day_mode` in data.json: `normal`, `soft` or `hard`). Tests:
`.venv/bin/python tests/test_daycap.py`.

## Price safeguards (added Sep 26, 2026)
The bot never buys, sells, takes profit, stops out or trails on a price reading it doesn't trust. Every rejected
reading is written to `logs/bot.log` with a `[PRICE-GUARD]` tag, plus a GUARD line in the decision log.
Settings are in `config.toml` under `[guard]`.
- **Wrong pool or coin:** readings from a different pair/pool or token than the one the position was bought on are ignored.
- **Inconsistent data:** if market cap divided by price implies a different token supply (more than 50% off), the reading is ignored.
- **Extreme moves:** a jump of more than 5x up, or a drop of more than 50% from the last good price, has to be confirmed
  by GeckoTerminal or by the next check (within 5 minutes, agreeing within 25%). An up-spike is never accepted if
  GeckoTerminal contradicts it or shows the pool empty, or if it repeats with no trades in the last 5 minutes.
  A real crash still sells, one check later at most.
- **Rug pulls:** if pool liquidity falls below $1,000, or drops 80% or more from what it was at entry, and that shows on two checks in a row,
  the position is closed as "rug: liquidity pulled". It's booked at what the empty pool could actually pay, which is about $0.
- **After a gap or freeze:** after 10 minutes or more without a reading, the first reading is not acted on. The next one is.
- **Realistic fills:** paper sells can't book more than the pool could pay (a constant-product x·y=k estimate from
  pool liquidity). Paper buys pay the larger of the 1% slippage or the pool's price impact. Open positions are valued the same way.
- **Buy re-check:** right before a buy, the pool is re-read. The buy is skipped if the price moved more than 20% from
  the scan, if liquidity is below the minimum, or if GeckoTerminal disagrees.
- **Volume-decay exits** need two checks in a row that agree.
- **One position per coin:** no second position in the same mint, the same pool, or (with `block_same_symbol = true`)
  the same ticker. That covers copycat coins with the same name. The 24h re-entry cooldown applies to all three.

Past records were corrected on Sep 26 for two exits booked on fake prints: DDOS #34 ($0.03061) and UPTOBER #22 ($0.02232).
Every change, with before and after values, is in the `corrections` table in `memebot.db`, and the script is in
`tools/correct_bad_prints_20260926.py`. To check the safeguards, run `.venv/bin/python tests/test_guard.py`, which uses a copy of the DB.

## Start / stop / check

```bash
cd /workspace/memebot
./run.sh        # start (or restart) everything in the background
./status.sh     # is it running? current public URL, last scan
./stop.sh       # stop everything
```

Everything keeps running after you close the terminal. If a part crashes it restarts by
itself within 10 seconds. The paper account, trades and history are stored in
`memebot.db`, so restarts don't lose anything. (If the whole machine reboots, run
`./run.sh` again.)

## Settings

All thresholds are in **`config.toml`** (plain text with comments). Edit and save; the bot
picks up changes on its next cycle, no restart needed. To start the fake account over,
stop the bot, delete `memebot.db`, and run `./run.sh`.

## Files

| File | What it is |
|---|---|
| `config.toml` | all settings |
| `bot.py`, `scanner.py`, `trader.py`, `common.py` | the scanner + paper trader |
| `web.py`, `dashboard_data.py`, `static/` | dashboard (look & feel: `static/style.css`) |
| `export_snapshot.py` | writes `snapshot.html` (single shareable file) or `site/` (the public website) |
| `notify.py`, `telegram_bot.py` | Telegram alert queue and command bot |
| `publish.py` | publishes `site/` to Netlify ~10 s after each paper trade and every 5 min otherwise (touch `logs/publish_now` to force one); site id in `netlify_site.json` |
| `screenshot.py` | saves `dashboard.png` |
| `logs/` | logs for bot, web, publisher and telegram |

## Honest limitations

- Free APIs have gaps: brand-new tokens often have no holder count yet, and the public
  Solana RPC sometimes refuses the "top holders" lookup. When that happens the check is
  retried on each scan, up to 6 such retries per scan, and only while the token is among the
  600 re-checked, and the token waits ("pending") until the on-chain check succeeds.
  GeckoTerminal only refreshes holder counts every so often, so the "holders vs price" check
  waits for a real refresh; when a token has no holder count at all the check is skipped
  and the token scores lower. Every such case is written in the reasons column.
- The honeypot flag from GeckoTerminal is usually "unknown" for Solana; the buys-and-sells
  check is the main honeypot guard.
- Paper fills use the DexScreener price plus an assumed 1.5 % cost. Real meme-coin fills can
  be much worse, and prices are sampled every ~45 s, so fast crashes can go past the stop
  loss before the simulator sees them.
- Volume decay only applies from pool age 2 h. For 2–12 h it compares the last hour with the
  6 h hourly pace; after 12 h it compares the last 6 h with 24 h volume × 6 ÷ pool age, which
  is 24 h ÷ 4 once the pool is 24 h old.
- An unknown honeypot flag costs 3 score points; 'yes' is rejected outright.
- Unknown mint or freeze authority is treated as not revoked (rejected). If both data sources
  fail, the token waits and is re-checked.
- More than +150% in the last hour costs 10 points; more than +200% is rejected, so the
  penalty only matters between +150% and +200%.
- There is no dip wait: a token that passes is paper-bought at the current price straight away.
- None of this is proof that the strategy makes money. It's a way to watch and learn.

## Real-money test mode (added 2026-09-26, OFF by default)
- `live.py` copies the paper bot's trades with small real swaps through Jupiter on Solana. Paper trading is unchanged.
- It uses only the dedicated test wallet (public address `5Tdn5tRELLVAVBj5i7DK3BherBWKx82K4siovd6tjbFm`). The key is stored outside the project, in `/home/box/agent-data/memebot_wallet.json`.
- Config is in `[live]`: `enabled` (master switch), `trade_usd` $5 per buy, and `budget_usd` $50. That is a hard lifetime limit on real dollars spent on buys. Also `max_open` 3, and slippage 3% on buys (sells retry at 3%, then 8%, then 15%).
- A real buy happens only when the paper bot buys. A real sell sells the same fraction as the paper sell, and it still runs if live mode was switched off since.
- Emergency stop is Telegram `/livestop` (or create `logs/live_kill`). Status is Telegram `/live`. Everything is recorded in the DB table `live_trades`.

### Real wallet daily stop (added 2026-09-26)
- The real wallet has its own daily stop: `[live] daily_stop_pct = 10`. Once the wallet's value (SOL + estimated coins) is 10% below its value at the start of the Toronto day, no new real buys until midnight. Real sells still run.
- The paper soft/hard daily caps no longer block the real test: while paper is capped, an entry is still taken (normal rules and size) only if the real buy would go through (live on, budget left, slot free, real daily stop not hit).
- Telegram buy alerts end with "💵 REAL MONEY: yes…" or "📝 Paper only, no real money (reason)".
- On 2026-09-26 at 2:48 PM the paper daily loss count was reset to 0 at Sammy's request (day-start equity $897.87).
- Tests: `tests/test_livecap.py` (5/5). All tests force `[live] enabled = false` so they never touch the real wallet.
- 2:51 PM: re-based again to exclude bPay (bought before the reset, closed 2:50 PM, -$5.53). Day-start equity $881.84.

### Rug check before every buy (added 2026-09-26, 11:50 PM)
- 9 of 63 closed trades were real rug pulls (confirmed empty pools on GeckoTerminal); real money lost on ROO, 7 and HEISENBERG.
- Before any buy (paper and real) the bot asks RugCheck (`rugcheck.py`, `[rugcheck]` in config): the pool we buy in must have >= 90% of its LP locked/burned, RugCheck must show no "danger" risk (big single holder, top-10 too high, low liquidity, already rugged), and if RugCheck can't be reached the coin is skipped.
- `min_liquidity_usd` raised from 15000 to 30000.
- Backtest on the last 19 coins: all 9 rugs would have been blocked; 3 of 19 coins would still have been bought (so expect far fewer trades).

## Data source for watchlist re-checks (Oct 2 2026)
- `[apis] scan_source = "jupiter"`: each cycle re-checks up to `max_watchlist_eval` (600) newest watchlist coins with
  Jupiter Tokens API v2 (`tokens/v2/search`, 100 coins per call, Sammy's key, limiter `jup` = `jupiter_per_min`, max 50/min;
  the key's 60/min is shared with real-money quotes, so scanning leaves headroom). Backs off on 429 and when
  `x-ratelimit-remaining` is nearly empty.
- Jupiter alone settles coins that are too young, too old, or whose liquidity is far below the minimum
  (Jupiter liquidity x3 still below `min_liquidity_usd`; "dead" needs x4 below 25%). Jupiter's liquidity is about half of
  DexScreener's pool figure, and the thresholds were set on DexScreener numbers, so every other coin is checked on
  DexScreener pool data with the unchanged filters and score (this also gives the pool used for trading).
- A Jupiter batch that fails falls back to DexScreener. A coin whose DexScreener call failed (429) is "pending: API busy",
  never "not listed" (before, a 429 for 30 min could permanently reject a coin as "not listed on DexScreener").
- `scan_source = "dexscreener"` restores the old behaviour. Optional `[scanner] include_jupiter_trending = true` adds
  Jupiter top-trending (1h) coins to discovery (1 Jupiter call per cycle; off by default).
- Comparison scripts: `tools/compare_jup_ds.py` (read-only), `tools/compare_hybrid.py`.

## Pre-buy fallback when DexScreener is busy (Oct 2 2026)
Every buy re-reads the pool on DexScreener first. If that re-read fails (429 / error - not "pool not found") and
`[entry] prebuy_fallback = "jupiter_quote"`, `jupcheck.py` checks the coin with Jupiter instead (read-only quotes, never executed):
fresh Tokens API v2 reading through the same cheap filters (incl. the 75% 1h pump limit; Jupiter liquidity must be
>= 0.5 x `min_liquidity_usd`), holders >= `min_holders`, price within `entry_price_tolerance_pct` of the scan price,
a $5 (`[live] trade_usd`) quote costing <= 2% vs Jupiter's price (and Jupiter's own impact <= 2% at $5 and $30), and a
$30 depth quote sent at the same moment: the $30-vs-$5 extra cost (fees cancel) must imply >= `min_liquidity_usd`
constant-product liquidity (L = 2*D*(1-i)/i; <= 0.2% at $30 for $30k). On Oct 2 this estimate matched DexScreener's
pool liquidity within ~3-30% on 6 coins. RugCheck is forced on + fail-closed on this path; the GeckoTerminal cross-check,
one-position-per-coin, daily caps and the real-money limits are unchanged. Fallback entries are tagged
`positions.entry_source = 'jup_fallback'` (normal: 'dexscreener') with the numbers in `positions.entry_check` (JSON,
also in data.json), the BUY decision carries `[entry_source=jup_fallback ...]`, and the Telegram alert says
"(DexScreener busy, checked via Jupiter quote)". Test: `.venv/bin/python tests/test_jupfallback.py`.

## Jupiter trigger stop-loss for REAL positions (trial, Oct 2 2026)
`[live] trigger_stops = true`, `trigger_stops_trial_max = 1`: after the next confirmed real buy, `trigstop.py` places a
Jupiter Trigger V2 sell-below order (`jtrigger.py`: wallet-signed auth challenge, vault, deposit, order) for the full
amount at entry x (1 - stop_loss_pct), expiring 2h after the 24h max hold. The bot's trailing rule PATCHes it up (never
down; >= 5% steps, at most every 5 min). Every bot exit first cancels the order and withdraws the coins
(`release_before_sell`), then sells; if Jupiter already filled it, the sell is skipped and the fill is booked as a real
sell 'stop loss (Jupiter trigger)' (shows in live.closed[]). A failed placement leaves the bot's own stop in charge (Telegram).
**Jupiter's minimum order is $10**, so with `trade_usd = 5` no order can be placed: the buy gets a Telegram note and
the trial is not used up. Cost per placed order: vault token-account rent ~0.002-0.003 SOL (deposit, measured by
simulation) + network fees for the deposit and withdrawal; vault registration is a free API call.
Tests: `tests/test_trigstop.py` (mocked).

## Older-coin strategy ("established", PAPER ONLY) - added Oct 2 2026

A second fake-money strategy next to the new-coin one, for Solana coins whose first pool is **older than 24 hours**
(days to months old). Code: `established.py`; settings: `[established]` in `config.toml`; tests: `tests/test_established.py`.

* **Never real money.** `live.py` refuses any position whose `strategy` is not `new` (`live.paper_only_strategy`,
  checked in `on_buy`, `on_sell` and again inside `_do_buy` before any swap), and `established.py` never calls live.py.
* **Separate pot and stats.** Own fake balance (`starting_balance_usd` 300, $15 per trade, max 3 open), own daily stop
  (-5% of its own balance), positions tagged `strategy='established'` (all older rows are `new`). The new-coin strategy's
  cash, equity, daily soft/hard caps, max-open and cooldown only count `new` rows.
* **Discovery** every 10 min: Jupiter `toporganicscore` + `toptrending` (1h/6h/24h) = 6 calls, plus one batch re-check of
  coins that passed the quality filters in the last 24h. Open positions: one Jupiter batch call per minute. No GeckoTerminal
  calls (except the price guard's rare confirmation of an extreme move); one DexScreener call per entry (deepest pool + a
  second price), skipped while DexScreener is paused after a 429.
* **Excluded:** SOL, stablecoins, wrapped majors (BTC/ETH...), liquid-staking SOL, tokenized stocks (fixed mint list,
  Jupiter tags, symbol pattern, $1-peg check).
* **Quality:** pool > 24h, liquidity >= $100k (Jupiter, all pools), 24h volume >= $250k, 1h volume >= $5k, market cap
  $1M-$500M, 1000+ holders, Jupiter organic score >= 50, top holders <= 50%, mint + freeze authority revoked, 20+ buys and
  10+ sells in the last hour.
* **Entry (pullback and bounce, never a pump):** 6h change -3% to -25%, 1h change +0.5% to +8%, 5m change >= -1%,
  24h change between -35% and +100%, more net buyers than sellers in the last hour. Then RugCheck (not rugged, no danger flags,
  no transfer fee, at least one pool with >= 90% LP locked - concentrated-liquidity pools can't be locked), and Jupiter's price
  must match DexScreener's deepest pool within 5%. One entry per round.
* **Exits:** take profit +12% (sell half) and +25% (rest), stop -8%, trailing 5% after +8%, max hold 7 days; the price guard
  checks every reading.
* **Telegram:** "PAPER (older coin) BUY/SELL ...". **data.json:** `established` (kpi, open, closed, equity, last_round, rules)
  and `strategies` (`new` vs `established` stats). Existing fields (`kpi`, `open`, `closed`) stay new-coin only.
* **Jupiter backup for older-coin buys (Oct 3):** when DexScreener is paused or returns a 429 at entry, the buy is checked
  with Jupiter instead (`established.jup_fallback`, using `jupcheck.quote_check`). It takes a fresh Jupiter reading that must
  pass the same older-coin rules and be within 5% of the scan price. Then a $15 quote and a $150 quote: the $15 quote costs
  <= 1.5% (fees included), Jupiter's impact is <= 1.5% at both sizes, and the extra cost of $150 over $15 must imply >= $100k
  of constant-product liquidity (<= 0.3% impact). GeckoTerminal's price must agree within 5% when it answers (skipped while it
  is rate-limited). RugCheck still runs first. These entries are tagged `entry_source='jup_fallback'` and the Telegram alert
  says "(checked via Jupiter quote)". `[established] prebuy_fallback = "off"` restores the old skip.

## Recording only (Oct 3): price path per position + actual real fills
No trading decision reads these tables, and they are never cleaned up. Code: `fills.py`; tests: `tests/test_history.py`.
* `position_ticks` (id, pos_id, strategy, is_real, ts, price, source, liq, pnl_pct, peak, verdict): one row per price check
  of every open paper position (new-coin: `dexscreener` / `jupiter` backup readings; older-coin: `jupiter_batch`).
  `is_real=1` = also an open REAL position. Readings the price guard rejected are kept, with verdict `reject`
  (`drained` = rug reading); `pnl_pct` = price vs entry; `peak` = peak after this check.
* `live_fills` (one row per REAL trade, `trade_id` = live_trades.id): sig, block_time, source (`tx` = read from the confirmed
  transaction, `balance_diff` = fallback), token_decimals, tokens_in/out, sol_in/out (SOL into/out of the swap),
  sol_wallet_delta, fee_sol, other_sol (token-account rent etc. on buys), sol_price (+ sol_price_source), usd_in/out,
  fill_price_usd, quoted_price_usd, quote_in_raw/out_raw, slippage_allowed_bps, slippage_pct (actual vs quote, + = worse),
  paper_price, vs_paper_pct (real fill vs the paper price, + = worse), error, recorded_at. Filled in the background a few
  seconds after each real buy/sell/Jupiter-stop fill. `tools/backfill_fills.py` filled in the 40 trades before Oct 3
  (no quote was stored then). data.json `live.trades[]` carries the main fill columns.
* Config change Oct 3: `[paper] trailing_activate_pct` 30 -> 20 (trailing stop 20% below the peak once a trade is up 20%);
  the real wallet's Jupiter stop trailing reads the same setting.

## Side-by-side scoring test (Oct 3 2026, Sammy approved)
Spec: `/home/box/analysis-bot/scoring/SIDE_BY_SIDE_SPEC.md`. A second **paper-only** $1,000 account (`scoring='new'`)
trades the same new-coin candidates as the existing account (`scoring='current'`), using the new score `scoring_v1 1.0`
(copied, not linked, into `scoring_v1/`; wrapper `newscore.py`) instead of the current score:
- buy at new score >= 48.4; while its own daily soft cap is on only >= 55.5, at half size (thresholds come from the model file).
- same filters, RugCheck, price guard / pre-buy re-read / Jupiter fallback, sizing, exits and fill model; separate cash,
  day caps, max open, 24h same-coin block (state keys `new:*`, equity rows `scoring='new'`). Telegram `/pause` stops both.
- both scores are computed at the score gate (`scanner.evaluate`) and logged on every evaluation (`score_new`, `prob_new`,
  `model_version`, `gate_current`, `gate_new`). Only DexScreener pool liquidity goes to the scorer (Jupiter-sourced metrics
  are scored without liq/mcap, note `jupiter_metrics_no_liq`).
- **never real money**: `live.on_buy` and `live._do_buy` refuse anything with `scoring != 'current'`; `live.on_sell` and
  `trigstop` are only called for current positions. Tests: `tests/test_scoring.py`.
- startup self-test re-scores the model's 10 stored rows; if it fails the new account stays off (`[scoring] enabled` also turns it off).
- data.json: `scoring.{model_version, selftest, enabled, started, thresholds, current, new, comparison}`; the top-level
  kpi/open/closed/equity/decisions fields stay the current account only.
