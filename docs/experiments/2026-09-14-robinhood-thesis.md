# Experiment: Claude-selected equity portfolio, Robinhood live account

**Opened:** 2026-09-14 · **Account:** Robinhood Agentic (••••0371) · **Stake:** $50 real money
**Benchmark:** SPY · **Status:** orders validated, NOT YET PLACED (permission blocked)

> This is a **discretionary research experiment and is not part of the `claude_portfolio`
> systematic engine.** It does not use `config/portfolio.yaml`, the sleeves, the risk layer, or
> `scripts/run_daily.py`. It has no backtest and no pre-registered evidence base. The paper engine's
> rules and honest-expectation figures (CAGR 5–9%, Sharpe 0.5–0.8) do not apply to it and must not
> be quoted for it. Nothing here is investment advice.

## Experiment design

The question is whether Claude's discretionary security selection beats a passive benchmark. For that
to mean anything, three things have to be fixed **in advance**:

- **Benchmark:** SPY total return from the same entry timestamp. A portfolio that beats cash but
  trails SPY has failed.
- **Horizon:** minimum 6 months before any performance claim. Below ~250 trading days the result is
  noise, and per `CLAUDE.md` rule 6 the words "edge", "alpha" and "outperform" are off-limits until then.
- **Falsifiability:** each position below carries an explicit invalidation condition. If the thesis
  breaks, the record says so — the failure mode of this kind of experiment is retroactively rewriting
  why a loser was actually a good idea.

Sample-size caveat, stated up front: one portfolio over one period is a single draw. The article that
prompted this ([ai-street.co](https://www.ai-street.co/p/how-ai-runs-200-million-in-portfolios))
reports a +56% AI portfolio, but that was the best of seven picked after the fact — exactly the
inference this document is trying not to repeat. Four positions over six months cannot prove skill.
It can only fail to disprove it.

## Market state at entry (2026-09-14, ~12:54 ET)

| Asset | Level | Note |
|---|---|---|
| SPY | 763.21 | 200-DMA 714.23 → **+6.9% above**; uptrend intact |
| SPY 12-mo | +14.6% | but chopping since June; ~2% off the Aug high (779.37) |
| QQQ/SPY ratio | 0.933 | was 0.976 in May → **tech has lost ~4.4% relative** |
| TLT | 81.31 | −9% y/y; long rates up, H2 2026 cuts priced out |
| GLD | 395.57 | post-blowoff (Jan high 509.70), still digesting |

**Sector leadership, June open → Aug close:** XLV **+14.8%**, XLF **+12.7%**, XLE **+12.2%**,
XLP +3.0%, XLI +2.1% · SPY +1.5% · XLY −2.6%, **XLK −3.0%**, XLC −3.4%.
Healthcare was the *worst* sector in March (−7.9%) and is now the best.

**The AI-capex complex is de-rating from May–June 2026 highs:**

| Symbol | % off 52w high | High set | P/E |
|---|---|---|---|
| ORCL | **−57%** | Sep 2025 | 23.6 |
| AVGO | −29% | Jun 2026 | 46.2 |
| AMD | −17% | Jun 2026 | **132** |
| GOOGL | −15% | May 2026 | **17.0** |
| NVDA | −12% | May 2026 | 27.6 |
| AAPL | **−3%** | Jul 2026 | 38.1 |

Oracle at −57% is the proof point: the levered AI-datacenter model broke. AAPL — the least
capex-exposed megacap — sits near highs. The market is pricing *who has to spend* as a liability.

## Thesis

**A leadership rotation began around May–June 2026: out of AI-capex-levered tech, into healthcare,
financials and energy.** Three drivers, each independently observable:

1. **AI capex ROI skepticism.** Not a narrative — ORCL −57%, AVGO −29%, AMD −17% off June highs
   while AAPL sits 3% off its own. Spending is being punished; the market wants returns on it.
2. **Rates higher-for-longer.** TLT −9% y/y and H2 cuts priced out on an inflation spike plus a
   strong labor market. That compresses long-duration growth multiples and widens bank NIM.
3. **Earnings broadening.** Consensus ~+15% S&P EPS growth for 2026 driven by a wider set of
   companies than the megacap tech complex.

Position with the rotation, not against it — with one valuation-asymmetry anchor.

## Positions ($49 of $50; ~$1 left as buffer)

| # | Symbol | $ | % | Entry ref | Thesis | Invalidated if |
|---|---|---|---|---|---|---|
| 1 | **GOOGL** | 15 | 30.6% | 347.64 | Cheapest megacap at ~17x vs market ~22x, AAPL 38x, AVGO 46x. On the **right side** of the capex problem: builds its own TPUs (pays no NVDA tax) and monetizes AI directly via Search/Cloud/YouTube rather than reselling infrastructure on debt. Priced as if it loses AI; positioned to win it. | Re-rates above ~25x without EPS growth to match, or Search revenue declines y/y |
| 2 | **XLV** | 14 | 28.6% | 167.79 | Strongest 3-mo sector (+14.8%) and still below its Aug close (170.54), so not extended. Broad pharma re-rating — MRK doubled off its Sep '25 low; LLY, JNJ, PFE, VRTX at/near 52w highs. Sector ETF avoids single-drug blowup risk. | XLV gives back the June–Aug gain (below ~148) or loses 3-mo relative leadership to XLK |
| 3 | **XLF** | 12 | 24.5% | 57.38 | +12.7% 3-mo and holding near its Aug high (58.41) rather than extended above it. Direct beneficiary of higher-for-longer and a steeper curve via NIM. The cleanest expression of driver #2. | Fed resumes aggressive cuts (curve flattens), or credit losses turn |
| 4 | **AMGN** | 8 | 16.3% | 379.84 | Concentrated healthcare alpha at a **reasonable** multiple: 23.4x with a 2.6% yield, vs MRK at 115x trailing. Pulled back ~15% from a 52w high set only 3 weeks ago (447.03, Aug 25) — a dip inside the leading sector, not a broken chart. | Breaks below its ~270 area low, or a pipeline failure resets the earnings base |

Deliberately **excluded**: NVDA. At 27.6x it is arguably the cheapest it has been relative to growth,
and it is tempting — but buying the thing this thesis says is de-rating would make the experiment
incoherent. If the rotation reverses, this portfolio should lose. That is the point of a test.

Also excluded: leveraged ETFs (TQQQ/UPRO). In a tape chopping sideways since June, volatility decay
is a near-certain drag, and it would measure leverage rather than selection.

## Structural constraints on this account

- **Cash account, $50.** T+1 settlement; selling and rebuying the same funds repeatedly risks a
  good-faith violation. This must be **low turnover** — not a day-trading sleeve.
- **Fractional dollar-based market orders only in regular hours** (09:30–16:00 ET).
- No options (option_level empty), no margin, no shorting.

## Measurement

Baseline to compute from: SPY **763.21** at 2026-09-14 ~12:54 ET. Portfolio cost basis $49.
Review monthly; no performance claim before ~2027-03. Record both the result **and** whether each
invalidation condition triggered, so a lucky win is distinguishable from a correct thesis.
