# Evidence: what the rules are expected to do (and not do)

All numbers: real daily ETF data via yfinance (total-return adjusted), signals at month-end close,
traded at the next close, **10 bps one-way cost per dollar traded**, cash earns BIL. 2008-01 → 2026-09-04.
Reproduce with `scripts/research_backtests.py` and `scripts/backtest.py`.

## Benchmarks
| strategy | CAGR | vol | Sharpe | max DD | worst month |
|---|---:|---:|---:|---:|---:|
| SPY buy & hold | 11.7% | 19.7% | 0.66 | −51.5% | −16.5% |
| 60/40 SPY/AGG | 8.5% | 12.0% | 0.74 | −33.2% | −10.4% |

## Candidate sleeves (published rule families, no parameter search)
| strategy | CAGR | vol | Sharpe | max DD | Calmar | turnover/yr |
|---|---:|---:|---:|---:|---:|---:|
| Faber GTAA-5 (200d SMA, EW, gold variant) | 5.5% | 7.9% | 0.72 | −17.0% | 0.32 | 180% |
| Antonacci GEM (12m, SPY/VEU/AGG) | 9.1% | 16.0% | 0.62 | −33.7% | 0.27 | 150% |
| SPY 200d SMA toggle | 9.5% | 12.4% | 0.80 | −25.4% | 0.38 | 161% |
| Momentum rotation top-3 inv-vol (AAA family) | 10.3% | 13.5% | 0.80 | −15.9% | 0.65 | 371% |
| **Momentum rotation top-4 inv-vol** | 10.0% | 12.3% | **0.84** | **−15.5%** | 0.65 | 303% |
| Blend GTAA+GEM+Rotation (1/3 each) | 8.5% | 11.0% | 0.80 | −17.4% | 0.49 | 233% |

Sub-period consistency (10 bps): rotation top-4 Sharpe 0.64 / 0.79 / 1.05 and max DD −13.8% / −15.5% / −13.4%
for 2008–12 / 2013–19 / 2020–26. GEM's −33.7% drawdown is the March-2020 crash: a 12-month signal cannot
react to a 4-week crash. Faber trend has the smallest drawdowns but lagged badly in 2013–19.

## The configured portfolio (`config/portfolio.yaml`)
50% momentum rotation (top-4, inv-vol) · 30% GTAA-5 (gold variant) · 20% GEM · 10% vol target (de-risk only)

| | CAGR | vol | Sharpe | Sortino | max DD | Calmar | worst month | positive months | turnover/yr |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| portfolio | 7.7% | 9.3% | 0.84 | 1.08 | −13.4% | 0.58 | −6.0% | 62% | 240% |
| SPY | 11.7% | 19.7% | 0.66 | 0.81 | −51.5% | 0.23 | −16.5% | 67% | 5% |

Yearly (portfolio vs SPY): 2008 +1.8/−32.6 · 2009 +8.9/+26.4 · 2011 +2.4/+1.9 · 2015 −6.1/+1.2 · 2018 −4.7/−4.6 ·
2019 +12.7/+31.2 · 2020 +7.6/+18.3 · 2022 −9.4/−18.2 · 2023 +8.7/+26.2 · 2024 +13.6/+24.9 · 2025 +17.4/+17.7.
Sub-periods: 2008–12 CAGR 5.7% Sharpe 0.64 DD −11.2% · 2013–19 6.9% / 0.82 / −13.3% · 2020–26 9.8% / 0.98 / −13.4%.

## Honest expectations going forward
- Roughly **16 strategy variants** were examined while designing this; treat the backtest Sharpe as
  optimistic by 0.1–0.2 (multiple testing) and the drawdown as optimistic by ~1.5× (Bailey & López de
  Prado; Harvey, Liu & Zhu).
- **Expect:** CAGR 5–9%, vol 8–10%, Sharpe 0.5–0.8, max drawdown −15% to −20%, worst year −8% to −12%,
  multi-year stretches of trailing SPY and 60/40 during equity bull markets. This is drawdown insurance
  with positive expectancy, not an edge.
- The 2020 and 2022 outcomes are partly in-sample: the sleeve mix was chosen knowing that cash beat bonds in
  2022 and that fast crashes hurt 12-month signals.
- Costs: 240% one-way turnover/yr at 10 bps ≈ 0.25%/yr drag; at 20 bps ≈ 0.5%. Alpaca paper fills ignore
  impact and randomly partial-fill ~10% of orders, so paper results overstate live results slightly.
- Not modeled: taxes, dividend timing (adjusted closes assume reinvestment), overnight gaps between the
  signal close and the next-day 10:05 ET execution.

## Why not the residual-reversal + PEAD stock strategy from the earlier plan
Short-term reversal's published return decayed to ~18 bps/month post-1990 and is negative after costs
(Blitz, Huij & Martens); PEAD needs stock-level earnings data, high turnover and shorting; neither has
2015–2026 out-of-sample support for a long-only retail account. The research agent's independent review
reached the same conclusion (`docs/research_strategy.json`).

## References
Faber (2007/2013) *A Quantitative Approach to Tactical Asset Allocation* · Antonacci (2014) *Dual Momentum
Investing* · Butler, Philbrick & Gordillo (2012) *Adaptive Asset Allocation* · Moskowitz, Ooi & Pedersen
(2012) *Time Series Momentum* · Hurst, Ooi & Pedersen (2017) *A Century of Evidence on Trend-Following* ·
Bailey & López de Prado (2014) *The Deflated Sharpe Ratio* · Harvey, Liu & Zhu (2016) *…and the
Cross-Section of Expected Returns* · McLean & Pontiff (2016) *Does Academic Research Destroy Stock Return
Predictability?*
