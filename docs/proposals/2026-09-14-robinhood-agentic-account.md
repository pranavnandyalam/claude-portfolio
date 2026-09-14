# Proposal: managing the Robinhood "Agentic" account

**Date:** 2026-09-14 · **Status:** proposal, awaiting human decision · **Author:** Claude session
**Trigger:** user request — "Manage my agentic portfolio in Robinhood", citing
[How AI Runs $200 Million in Portfolios](https://www.ai-street.co/p/how-ai-runs-200-million-in-portfolios).

No orders were placed and no code was changed. Per `CLAUDE.md` rule 5, a new broker, a new
account and live money are all outside bounded adaptation, so this is written as a proposal.

## 1. Account state as found (read-only)

| Field | Value |
|---|---|
| Account | Robinhood individual, cash, nickname **Agentic** (••••0371) |
| Tradable by this agent | yes |
| Total value | **$50.00** |
| Cash / buying power | $50.00 / $50.00 |
| Equity, options, crypto positions | none |
| Equity order history | **empty — the account has never traded** |

There is a second Robinhood account (••••9214, default) that is not accessible to this agent.

**This is real money.** Everything in this repo to date is an Alpaca *paper* account. The
distinction matters more than anything else in this document.

## 2. What the cited article actually establishes

The article covers Alejandro Lopez-Lira (Univ. of Florida) running seven public model portfolios on
Autopilot — built with ChatGPT, Claude, DeepSeek and Grok — with ~52,000 investors and ~$200M
following them. Each portfolio splits investing into macro analysis, company research, portfolio
construction and verification, with a different agent per stage. The DeepSeek portfolio returned
**+56% over the past year vs +16% for the S&P 500**.

What that does *not* establish, and what this project's standards (rule 6) require saying out loud:

- **Best-of-seven is not evidence.** The +56% figure is the top of seven portfolios selected after
  the fact. With seven strategies and one year of data, a spread that wide is the expected outcome
  of noise alone. The six other portfolios are the control group and the article does not lead with them.
- **One year is not a track record.** Lopez-Lira says so himself in the piece: it can take a decade
  to know whether a diversified strategy is beating the market.
- **$200M following it is a popularity fact, not a performance fact.** Assets follow returns.
- **The reported returns are gross of the behavior gap** — the investors mirroring it did not
  necessarily hold through the drawdowns that produced it.

This is the opposite of the standard `README.md` and `docs/EVIDENCE.md` hold themselves to
(2008–2026 after-cost walk-forward, and a stated expectation of *trailing* SPY in bull markets).
Copying the article's approach into this account would mean abandoning that standard, not extending it.

## 3. Why the existing engine cannot run this account today

Four independent blockers, in order of severity:

1. **The risk layer halts on this account size.** `config/portfolio.yaml` sets
   `risk.min_equity_usd: 1000`; `portfolio/risk.py:95` turns that into a hard halt
   (`equity $50 below minimum $1,000`). The engine's own pre-registered risk rules say a $50
   account is too small to run this strategy. That check is doing its job and should not be
   edited to make this work.
2. **The strategy does not fit in $50.** The three sleeves can request 8–10 ETFs at once
   (rotation `top_n: 4`, trend up to 5, gem 1). That is ~$5 per position. Meanwhile
   `rebalance.min_trade_usd: 25` allows at most two trades. The vol-target and drift-band
   machinery is meaningless at this scale — rounding would dominate every signal.
3. **There is no Robinhood broker.** `portfolio/broker.py` defines a `Broker` Protocol
   (11 methods: `get_account`, `get_positions`, `get_clock`, `submit_order`, `get_order`,
   `get_order_by_client_id`, `list_open_orders`, `cancel_all_orders`, `close_position`,
   `is_fractionable`, `latest_price`) with exactly two implementations, `AlpacaBroker` and
   `FakeBroker`. A `RobinhoodBroker` is writable against that Protocol, but it is new code and
   needs its own tests, idempotent client-order-id handling and reconciliation.
4. **The live gate is Alpaca-shaped and deliberately hard to open.** `mode: paper`,
   `PORTFOLIO_ALLOW_LIVE=1` and `state/LIVE_CONFIRMED` gate live trading, and `CLAUDE.md` rule 2
   reserves that decision for a human outside any Claude session. Routing live orders through a
   different broker's MCP tools would bypass that gate rather than satisfy it — the deny-list in
   `.claude/settings.json` covers `mcp__alpaca__place_*` but was written before a second broker existed.

## 4. Options

**A. Leave the account flat; fund and prove on paper first.** *(recommended)*
Keep the $50 in cash. Continue running the Alpaca paper engine until there is a meaningful live
sample. Revisit Robinhood when the account is funded above `min_equity_usd` and the strategy has a
track record worth transplanting. Cost: nothing happens right now. That is the correct outcome for
$50 of untested live infrastructure.

**B. Build a `RobinhoodBroker` and mirror the existing systematic targets.**
The intellectually consistent path: same deterministic sleeves, same risk limits, same reporting, a
second execution venue. Requires the adapter (§3.3), a broker-agnostic live gate extending
`.claude/settings.json`'s deny-list to `mcp__Robinhood__place_*`, and funding past $1,000 —
realistically $5,000+ before the sleeve math stops being dominated by rounding. Worth doing if this
account is meant to become real. Should not be done for $50.

**C. Copy the article — LLM agents picking stocks.** *(not recommended)*
Deliverable would be a macro/research/construction/verification agent pipeline choosing individual
names. No backtest, no after-cost evidence, no risk layer, and a discretionary process that cannot be
replayed or audited — it contradicts the premise in `README.md` ("rules, not vibes") and every hard
rule about deterministic execution. If you want this anyway, it belongs in a *separate* clearly
labelled experiment with an explicit budget you are willing to lose, never inside `claude_portfolio`.

**D. Park the $50 in a single broad-market position.**
One fractional buy of SPY (or BIL to stay risk-off) and leave it. Honest about what $50 is: a
funding test, not a portfolio. Cheapest way to verify the Robinhood execution path end-to-end
before committing real capital. Still a live order, so still a human decision.

## 5. Recommendation

**Option A now, Option B when funded.** The blocker is not capability — it is that $50 is below the
size at which this strategy's own risk rules permit it to operate, and the one-year, best-of-seven
result in the article is not a basis for overriding that. If the goal is to exercise the Robinhood
path, Option D is the small honest version and needs an explicit instruction.

## 6. Decisions needed from a human

1. Is the Robinhood Agentic account intended to be funded (how much, when)? Everything else follows.
2. If yes: Option B (mirror the systematic engine) or Option C (article-style discretionary agents)?
3. ~~Should `.claude/settings.json` deny the Robinhood order tools?~~ **Applied in this change.**
   The deny-list now covers `mcp__Robinhood__place_equity_order`, `place_option_order`,
   `place_crypto_order`, the three cancel tools, `exercise_option` and `cancel_option_exercise`,
   making the "code trades, Claude does not" rule broker-agnostic instead of Alpaca-only. This was
   done without waiting because it only removes capability and is trivially reversible. Remaining
   question: `CLAUDE.md` rule 1 still names `mcp__alpaca__*` specifically — reword it to cover any
   broker? That is an edit to the rules file, so it is left to you.
4. If any live order is ever wanted from a Claude session, the live gate needs a broker-agnostic
   redesign — that is a separate proposal.

## 7. Honest expectations (rule 6)

Any systematic portfolio described here targets, after costs, **CAGR 5–9%, Sharpe 0.5–0.8, max
drawdown −15% to −20%**, and is **expected to trail SPY in bull markets**. There is no live evidence
of edge; this project has fewer than 250 trading days of live data, so the words "edge", "alpha" and
"outperform" do not apply to it. The article's +56% is not this strategy, not after costs, and not a
result this project claims or targets.
