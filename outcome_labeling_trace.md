# Root cause: why every trade resolves to an exact ±R

Read-only trace. No code changed.

## The one-line answer

**P&L is computed from the label and the *planned* RR — the exit price is stored but never used in the arithmetic.**

`paper_executor.py:172-177`

```python
if result == "WIN":
    pnl = round(risk_usd * rr, 2)      # exactly the planned target, always
elif result == "LOSS":
    pnl = round(-risk_usd, 2)          # exactly -1R, always
else:
    pnl = 0.0
```

`exit_price` arrives as a parameter on line 160, gets written to the DB on line 180, and never appears in the P&L calculation. Same pattern in `trade_monitor_agent.py:218-231` for the partial/runner path.

## The full chain

1. **`auto_labeler.auto_label_loop`** polls every 60s, pulls a yfinance price, and calls `_check_trade` (line 107-118), which compares that price to a fixed TP/SL distance and returns the string `"WIN"`, `"LOSS"`, or `None`. This step is real — it reads actual market prices.
2. **`executor.close_trade(trade_id, result, exit_price=price)`** (`auto_labeler.py:154`) takes that label and books `risk_usd * rr` or `-risk_usd`.
3. The real price travelled is discarded at step 2.

A trade that gapped three times past its stop books exactly −1R. A trade that tagged its target by a tick books the full planned R. That's the flat distribution in the export.

## The partial path does the same thing

`trade_monitor_agent.py:218-231`

```python
if partial:
    pnl = round(risk_usd * (rr / 2), 2)      # TP1 banks half the target, guaranteed
else:
    remaining_risk = risk_usd / 2 if already_partial else risk_usd
    leg_pnl = round(remaining_risk * rr, 2) if result == "WIN" else round(-remaining_risk, 2)
    pnl = round(banked + leg_pnl, 2)
```

This reproduces every partial value in the export: for `rr=1.5`, TP1 banks +0.75R and a runner loss costs −0.5R → +0.25R (2 trades). For `rr=3.0`, TP1 banks +1.5R, runner loss −0.5R → +1.0R (5 trades).

**This corrects my earlier conclusion.** I credited the BE/partial mechanic with the book's edge. It isn't management skill — `risk_usd * (rr/2)` pays half the planned target in full whenever TP1 is tagged, then halves the risk on the remainder. The "losses cut to −0.56R on high-RR pairs" is that formula, not the market.

## What is and isn't trustworthy

| Column | Real? |
|---|---|
| `result` (WIN/LOSS) | **Yes** — driven by actual yfinance prices |
| `win_rate` | **Yes**, with the biases below |
| `exit_price` | Yes, recorded — just unused |
| `pnl`, `r_achieved`, expectancy, Sharpe, net P&L | **No** — derived from the planned RR |

So the ~40% win rate is a real measurement. The +$6,396.84 is not — it's `Σ(wins × planned RR) − Σ(losses)`, which is what you'd get by assuming every trade resolved perfectly.

## Two biases in the label itself

- **TP is checked before SL.** `_check_trade` (line 107-116) tests the target first, so a candle that touched both books a WIN. Optimistic.
- **60-second polling.** Anything that happens between polls is invisible; the trade is evaluated at whatever price the next poll returns.

## Three smaller things found on the way

1. **`outcome_labeler.check_price_based` is dead code** — nothing calls it. It also lacks forex entries in its `TP_DISTANCES` and falls back to the XAUUSD row, so a EURUSD trade at 1.16 would get a 4.0-price-unit stop it could never reach. Harmless while unreachable; a trap if anyone wires it up.
2. **`main.py:419`** calls `labeler.label_manual(trade_id=trade_id, result=result, exit_price=pnl)` — the `/outcome` endpoint passes a P&L figure into the `exit_price` parameter.
3. **`label_manual` never writes `pnl` or `exit_time`** — it sets `result`, `exit_price`, `status='CLOSED'` only. Trades survive with a P&L because `auto_labeler` calls `close_trade` first, on line 154, one line before it.
