# Design: a Claude-supervised, deterministic paper portfolio

## One-paragraph summary
Three published, low-turnover ETF rule sets (multi-asset momentum rotation, Faber trend following,
Antonacci dual momentum) are combined into monthly target weights, de-risked to a 10% volatility target,
and executed on an Alpaca **paper** account by deterministic Python with hard risk limits. Claude Code
sessions run on a schedule to operate, audit, report and adapt the allocation within pre-registered bounds.
Claude never computes signals or places orders itself. Expected after costs: CAGR 5–9%, Sharpe 0.5–0.8,
max drawdown −15% to −20% (see `docs/EVIDENCE.md`).

## Strategy spec (executable from this text)
Universe of liquid ETFs only; cash/risk-off = **BIL** (1–3 month T-bills). Signals computed from the
last close, once per calendar month (first engine run of the month), traded that day between 09:35–15:45 ET.

| sleeve | alloc | rule |
|---|---:|---|
| rotation | 50% | Universe SPY QQQ IWM EFA EEM VNQ TLT IEF GLD DBC. Score = mean of 3/6/12-month total returns. Keep the top 4 with score > 0 **and** close > 200-day SMA. Weight ∝ 1/63-day realized vol, each slot = 25%. Empty slots → BIL. |
| trend | 30% | SPY EFA IEF VNQ GLD, 20% each; hold if close > 200-day SMA else that 20% → BIL (Faber GTAA-5). |
| gem | 20% | If SPY 12m return > BIL 12m return: hold SPY unless VEU 12m > SPY 12m (then VEU); else AGG (Antonacci GEM). |
| overlay | — | Portfolio 63-day realized vol of the risky sleeve mix; if > 10% scale risky weights by 10%/vol (never > 1); freed weight → BIL. |

Rebalance day: |target − current| > 2% of equity. Other days: only if drift > 5% (drift bands). Long-only,
no leverage, fractional notional market orders (DAY), sells before buys.

## Architecture
```
yfinance ──► portfolio/data.py ──► signals/{rotation,trend,dual_momentum}.py ──► engine.combine_sleeves
                                                                                     │ vol target
state/targets.json ◄─────────────────────────────────────────────────────────────────┘
        │
        ▼
portfolio/risk.py (kill switch, drawdown breaker, daily loss, data sanity, account pin, price agreement)
        │ RiskVerdict
        ▼
portfolio/rebalance.py (targets − positions → OrderPlan; bands, cash feasibility, whole-share rounding)
        │ OrderPlan
        ▼
portfolio/executor.py (market-open + window check, cancel stale, sells → wait → buys → wait → reconcile)
        │
        ▼
portfolio/broker.py  AlpacaBroker(paper) | FakeBroker      logs/audit.jsonl · state/last_run.json
```
Scripts: `run_daily.py` (preflight / dry-run / execute / kill switch), `report.py`, `backtest.py`,
`research_backtests.py`. Tests: `tests/` (72 tests, FakeBroker, synthetic data, no network).

### Deterministic vs LLM boundary
| deterministic engine | Claude session | human |
|---|---|---|
| signals, targets, vol targeting | runs the CLI, reads JSON output | Alpaca keys, live gate |
| risk limits, circuit breaker, kill switch set | anomaly triage, incident notes | kill switch reset |
| order sizing, submission, idempotency, reconcile | reports, Obsidian notes, push notifications | universe / rule / risk-limit changes |
| account pin, paper/live gate | bounded allocation & vol-target tuning (weekly, logged) | schedules, code |

### Safety layer (all in code, none in prompts)
- Paper by default; live requires `mode: live` **and** `PORTFOLIO_ALLOW_LIVE=1` **and**
  `state/LIVE_CONFIRMED` with the exact phrase, and the client URL is re-verified.
- Account pinned on first run (`state/account.json`); a different account/mode refuses to run.
- Idempotent `client_order_id = cp-YYYYMMDD-SYMBOL-SIDE` with lookup-before-submit; run lock file.
- Circuit breaker at −15% from peak equity → targets 100% BIL, kill switch set, human reset required.
  Daily loss ≥ 3% → no trading that day. Warn at −10%.
- Data: stale (>3 business days), missing cash symbol, ±50% one-day move, broker-vs-close price
  disagreement > 15% → halt. Market closed or outside 09:35–15:45 ET → skip execution.
- Turnover bug-catcher: > 30% mid-period is blocked (rebalance days may turn over fully).
- Position cap 45%, gross ≤ 100%, ≤ 20 symbols, min equity $1,000.
- Secrets loaded from env / `.env` / `~/.claude/state/alpaca/.env` inside the Python process only;
  Claude sessions are denied reading those files (`.claude/settings.json`) and denied Alpaca write tools.

## Claude's role and bounded autonomy
See `CLAUDE.md`. Allowed unilaterally: sleeve allocations within [10%, 60%], ≤ 10 pp change per week,
vol target within [6%, 14%], only when tests pass and the walk-forward backtest does not worsen on Sharpe
and max drawdown, with a decision note. Everything else becomes a proposal for the human.
`portfolio.config.check_adaptation` enforces the bounds in code.

## Schedule (America/New_York)
| when | who | what |
|---|---|---|
| weekdays 09:50 | Claude scheduled task `claude-portfolio-premarket` | preflight → dry-run → triage → `--execute` → note + push |
| weekdays 10:20 | launchd `com.prn.claude-portfolio.trade` (optional fallback) | `--execute` without Claude if the app was closed; idempotent, so a double run is harmless |
| weekdays 16:20 | Claude `claude-portfolio-postclose` | report → Obsidian daily note → push |
| weekdays 16:35 | launchd `…report` (optional) | report without Claude |
| Sunday 18:00 | Claude `claude-portfolio-weekly` | tests, backtest, live-vs-expected review, bounded tuning, dashboard |
Desktop scheduled tasks only run while the Claude app is open (else on next launch); the launchd pair keeps
the paper account trading regardless. Both paths call the same idempotent engine.

## Divergences from the research agents' recommendations (and why)
- GEM keeps AGG as its bond leg (published rule) even though the strategy researcher preferred BIL as the
  only risk-off asset; GEM is 20% and the other 80% already uses BIL.
- Signals are recomputed on the first engine run of each month rather than at month-end close; the
  execution happens ~10:05 ET the same day (overnight gap risk accepted, documented in EVIDENCE.md).
- No shadow-mode period: dry-run is the default and execution is an explicit flag, which serves the same
  purpose; the account is paper.
- The LLM-driven `trade-*` agents and `/trade` command in `~/.claude` are left in place but unused;
  archiving them is recommended (see OPERATIONS.md).

## Known limitations
- yfinance is an unofficial data source; the engine tolerates one missing symbol but halts on the cash
  symbol or stale data. Alpaca bars can replace it once keys work (`AlpacaBroker` already has a data client).
- Paper fills are optimistic. Implementation shortfall is not yet measured (weekly review compares fill
  price to prior close manually).
- Vol targeting does not protect against slow grinds (2015–16); the drawdown control comes from the
  multi-asset structure and cash risk-off.
- No tax awareness; not investment advice.
