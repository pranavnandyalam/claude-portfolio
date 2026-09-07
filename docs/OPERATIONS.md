# Operations runbook

## 0. One-time setup (human)
1. **Paper keys.** Generate at https://app.alpaca.markets/paper/dashboard/overview and put them where the
   engine looks (first match wins): env `ALPACA_API_KEY`/`ALPACA_SECRET_KEY`, or `./.env`, or
   `~/.claude/state/alpaca/.env`. **Status 2026-09-07:** the keys in `~/.claude/state/alpaca/.env` are valid
   (paper, $100,000 equity, ACTIVE); the copy inside the `alpaca` MCP entry in `~/.claude.json` is stale and
   returns 401 — update it if you want the Alpaca MCP read tools in Claude sessions.
2. `.venv/bin/python scripts/run_daily.py --preflight` → must print `alpaca: OK paper=True ...`.
3. First real run (any weekday 09:35–15:45 ET): `.venv/bin/python scripts/run_daily.py` (dry-run), read the
   plan, then `--execute`. The first run of a month deploys the full target basket.
4. Schedules: the three desktop scheduled tasks are created (see below). Optional launchd fallback so the
   engine trades even with the Claude app closed:
   ```bash
   cp ops/com.prn.claude-portfolio.*.plist ~/Library/LaunchAgents/ && launchctl load ~/Library/LaunchAgents/com.prn.claude-portfolio.trade.plist ~/Library/LaunchAgents/com.prn.claude-portfolio.report.plist
   ```
   and keep the Mac awake for it: `sudo pmset repeat wakeorpoweron MTWRF 10:15:00`.
5. Recommended: archive the old LLM-driven trading fleet so no session can spawn it:
   `mkdir -p ~/.claude/agents/_archive && mv ~/.claude/agents/trade-*.md ~/.claude/agents/_archive/`.

## 1. Daily commands
```bash
.venv/bin/python scripts/run_daily.py --preflight
.venv/bin/python scripts/run_daily.py            # dry-run
.venv/bin/python scripts/run_daily.py --execute  # paper orders
.venv/bin/python scripts/report.py               # reports/YYYY-MM-DD.md + state/equity_history.csv
.venv/bin/python scripts/run_daily.py --json     # full machine-readable summary
```
Exit codes: 0 ok · 2 halted or plan blocked · 3 error · 4 lock held.
Offline rehearsal on real data without a broker: add `--fake` (in-memory $100k account).

## 2. Kill switch
Set: `scripts/run_daily.py --set-kill-switch --reason "..."`. The engine also sets it at −15% drawdown from
peak (`state/equity_history.csv`), after moving targets to 100% BIL. **Reset only by a human** after reading
`reports/` and `logs/audit.jsonl`: `scripts/run_daily.py --reset-kill-switch --reason "..."`.

## 3. Reading the state
- `state/targets.json` current period's targets and per-sleeve breakdown (recomputed monthly).
- `state/last_run.json` last engine pass; `logs/audit.jsonl` every decision/order (append-only).
- `state/account.json` pinned broker account — delete only if you intentionally switched accounts.
- `reports/` daily & weekly markdown.

## 4. When something is wrong
| symptom | action |
|---|---|
| `alpaca: AUTH FAILED` | regenerate paper keys (step 0.1); nothing trades meanwhile |
| `data: stale` | yfinance outage/holiday quirk; engine halts itself; retry later |
| `price disagreement` halt | compare broker quote vs yfinance close for the symbol; likely a bad bar → wait a day |
| `mid-period turnover ... exceeds` | stored targets changed unexpectedly → inspect `state/targets.json` and audit log |
| `broker account changed` | keys now point at a different account; confirm, then delete `state/account.json` |
| reconcile issues after execute | partial fills or rejected orders; the next run's drift bands finish the job |
| lock held | a previous run is still going or crashed < 2h ago; check `state/run_lock` |

## 5. Changing the strategy
Only `config/portfolio.yaml`. Claude may tune `allocation`/`vol_target` inside `adapt` bounds (weekly).
Anything else: edit, run `pytest -q`, run `scripts/backtest.py`, commit with a decision note in
`docs/decisions/`. Never let a live session change `mode`, `risk`, or `rebalance`.

## 6. Going live (not automatic, not recommended before ≥6 months of paper data)
Requires all of: `mode: live` in config · live keys · `PORTFOLIO_ALLOW_LIVE=1` · a file
`state/LIVE_CONFIRMED` containing `I UNDERSTAND THIS TRADES REAL MONEY` · deleting `state/account.json`
so the live account can be pinned. Claude sessions are denied writing that file.

## 7. Scheduled Claude tasks (desktop app; created via the scheduled-tasks MCP)
| taskId | cron (local) | prompt |
|---|---|---|
| claude-portfolio-premarket | `52 9 * * 1-5` | pre-market run per CLAUDE.md |
| claude-portfolio-postclose | `23 16 * * 1-5` | post-close report per CLAUDE.md |
| claude-portfolio-weekly | `3 18 * * 0` | weekly review per CLAUDE.md |
The prompts live in `~/.claude/scheduled-tasks/<taskId>/SKILL.md`; they defer to this repo's `CLAUDE.md`.
