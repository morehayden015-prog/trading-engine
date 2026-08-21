# What the bot has actually learned — Jul 24 → Aug 7, 2026

Built from `trade_export.csv` (183 closed trades, the complete live DB). Read-only analysis; nothing on the bot was touched.

---

## 1. The outcomes are idealized, not real fills

Every one of the 183 trades resolves to a near-exact R value:

| Realized R | Count |
|---|---|
| exactly −1.0000 | 96 |
| exactly +1.5 / +2.0 / +2.5 / +3.0 (the target) | 58 |
| partial values (+0.25, +0.75, +1.0) | 29 |

There is no spread. No −0.87R, no +1.34R, no slippage, no gap. A real fill distribution is continuous; this one has 16 distinct values across 183 trades.

**Why it matters:** whatever is labeling outcomes resolves each trade to a clean "stop hit" or "target hit." The learning loop — weight adjustment, auto-disable, Sharpe rankings — is training on synthetic labels. Every conclusion below inherits that caveat, and so does every conclusion the bot itself draws.

## 2. The strategy leaderboard is ranking RR assignment, not edge

`rr` is constant per strategy/symbol pair — it's the planned target, set once, never varying. Win rate is essentially flat across targets:

| Target R | Trades | Win rate | Expectancy |
|---|---|---|---|
| 1.5 | 66 | 39.4% | +0.023R |
| 2.0 | 27 | 33.3% | +0.000R |
| 2.5 | 43 | 44.2% | +0.791R |
| 3.0 | 47 | 42.6% | +0.915R |

Win rate varies by 11 points across targets — noise at these sample sizes. Expectancy varies 40×. That gap is arithmetic, not skill: at a flat ~40% win rate, a 3.0R target mechanically prints money and a 1.5R target mechanically breaks even.

So `ict_5step` topping the table at +$3,634 is substantially a statement about it being assigned 2.5–3.0R targets on ES and NQ. `rp_profits` at −$94 sits on mostly 1.5–2.0R pairs. **The ranking the auto-manager acts on is largely an artifact of the RR table.**

## 3. The breakeven/partial logic is the one mechanic clearly adding value

> **Correction (see `outcome_labeling_trace.md`):** this section is wrong. The partial path books `risk_usd * (rr/2)` at TP1 — half the *planned* target, paid in full regardless of where price actually went — then halves the risk on the runner. The reduced average loss below is that formula, not trade management.

| | Trades | Win rate | Net | Avg R |
|---|---|---|---|---|
| `be_moved=0` | 89 | 0.0% | −$7,387.97 | −1.000 |
| `be_moved=1` | 94 | 78.7% | +$13,784.81 | +1.782 |

The 0% figure is not predictive — BE only moves after price runs in favor, so it's an outcome, not a signal. The real evidence is in the losses: **20 trades had BE moved and still lost, but lost less than 1R.** That's what drags average loss on the 2.5R and 3.0R pairs down to −0.56R and −0.63R while 1.5R and 2.0R pairs sit at −0.94R and −1.00R.

Partial banking contributed **$4,423.83** across 29 TP1 hits. Strip that mechanic out and the book's edge largely disappears.

## 4. One session carries the entire book

| Session | Trades | Win rate | Net | Avg R |
|---|---|---|---|---|
| NY_OPEN | 134 | 44.0% | +$6,787.70 | +0.608 |
| NY_AFTERNOON | 9 | 22.2% | +$52.22 | +0.111 |
| LONDON | 23 | 34.8% | +$23.49 | −0.000 |
| ASIAN | 17 | 29.4% | −$466.57 | −0.235 |

NY_OPEN's +$6,788 exceeds the book's entire +$6,397. Everything else nets negative. ASIAN is the clearest loser — 29% win rate, negative expectancy, 17 trades. There is no session filter anywhere in the codebase; `_get_session()` exists in `ai_brain.py` but only feeds the AI prompt, and nothing gates entries on it.

## 5. Signal score has weak but monotonic information

| Score | Trades | Win rate | Net | Avg R |
|---|---|---|---|---|
| < 7.0 | 24 | 29.2% | −$277.40 | −0.167 |
| 7.0–7.5 | 73 | 41.1% | +$1,161.70 | +0.349 |
| 7.5–8.0 | 80 | 41.2% | +$4,817.78 | +0.572 |
| > 8.0 | 6 | 66.7% | +$694.76 | +1.875 |

Sub-7.0 signals are the only bucket that loses money. The >8.0 bucket is 6 trades — encouraging, uninterpretable.

## 6. Auto-management fires on win rate alone, and the threshold is doing odd work

Two pairs disabled, both correctly stopped (0 trades after the cutoff):

- `supply_demand::USDJPY` — disabled Jul 31 at 27% WR, **−$371.25**. Defensible.
- `supply_demand::EURUSD` — disabled Aug 3 at 33% WR, **−$25.60**. Essentially flat, killed for win rate.

Meanwhile `orb_scalp::NQ` is 0-for-4 at **−$275.87** and remains enabled, because `STRATEGY_MIN_TRADES=10` hasn't been met.

The rule reads win rate and ignores P&L, so a high-RR strategy that wins 33% and makes money looks identical to one that wins 33% and loses money. Given §2, that's backwards — the pairs carrying 3.0R targets *should* be expected to win well under 50%.

## 7. Sample size and drift

- **15 days, 183 trades.** `MIN_TRADES=10` triggers auto-disable on 10 observations. At a true 40% win rate, 10 trades gives roughly a ±30-point confidence band — the disable rule is largely firing on noise.
- **Max 9 consecutive losses** in the window; currently 4, which has the circuit breaker at yellow and sizing at 0.55×.
- **Risk per trade drifted 0.25% → 1.21%** over two weeks while the account grew. Position sizing is scaling with equity, so late trades carry ~4× the dollar risk of early ones. Both of the two biggest winning stretches (+$2,825 and +$2,024) land in that high-risk tail.

---

## What I'd actually conclude

1. **Fix the outcome labeling before trusting anything else.** Clean ±R results mean the feedback loop isn't learning from market behavior. Every number above is downstream of this.
2. **The measured edge is concentrated in two places** — the BE/partial mechanic (+$4,424 banked, losses cut on high-RR pairs) and the NY open (+$6,788 of +$6,397). Neither is a strategy.
3. **The strategy ranking is mostly the RR table.** Comparing strategies on net P&L while their targets differ 2× compares target assignment.
4. **Auto-disable should weigh expectancy, not win rate**, and 10 trades is too few to act on.

Nothing here is a recommendation to change thresholds — you asked for read-only and this is read-only. These are the places the data points at.
