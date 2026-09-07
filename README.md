# claude_portfolio

A fully autonomous, Claude-supervised systematic portfolio on an Alpaca **paper** account.

- **Rules, not vibes.** Three published ETF strategies (momentum rotation, Faber trend, dual momentum) →
  monthly targets → 10% vol target → deterministic execution with hard risk limits.
- **Claude supervises, code trades.** Scheduled Claude Code sessions run the engine, triage anomalies,
  write reports and tune allocations inside pre-registered bounds. Orders are only ever placed by
  `scripts/run_daily.py --execute`.
- **Honest numbers.** Backtest 2008–2026 after 10 bps costs: CAGR 7.7%, Sharpe 0.84, max drawdown −13.4%
  (SPY: 11.7% / 0.66 / −51.5%). Expect less live: CAGR 5–9%, Sharpe 0.5–0.8, DD −15% to −20%.
  Details and caveats in [docs/EVIDENCE.md](docs/EVIDENCE.md).

## Quickstart
```bash
uv venv .venv --python 3.12 && uv pip install --python .venv/bin/python -e ".[dev]"
.venv/bin/python -m pytest -q                       # 72 tests, no network
.venv/bin/python scripts/backtest.py                # after-cost backtest of config/portfolio.yaml
.venv/bin/python scripts/run_daily.py --fake        # dry-run on real data with a fake $100k broker
.venv/bin/python scripts/run_daily.py --preflight   # needs valid Alpaca PAPER keys
.venv/bin/python scripts/run_daily.py --execute     # paper orders, 09:35–15:45 ET
```

## Layout
`portfolio/` engine (data, signals, risk, rebalance, executor, broker, state, backtest) ·
`config/portfolio.yaml` the only tunable file · `scripts/` CLIs · `tests/` · `docs/` design, evidence,
runbook, research · `ops/` launchd fallback · `CLAUDE.md` rules for Claude sessions.

Paper account. Systematic rules. Not investment advice.
