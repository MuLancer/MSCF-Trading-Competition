# RITCx CMU 2026 — trading algorithms

Two algorithmic trading cases for the Rotman International Trading Competition
at CMU. Both run against the RIT simulator over a five-minute heat that stands
for one month of trading, 300 ticks at roughly one per second.

- **Volatility Trading** — options on an ETF called RTM. Trade the gap between
  the market maker's stale volatility and the one the analysts announce, and
  keep the delta hedged so the P&L is a bet on volatility rather than on price.
- **Algorithmic ETF Arbitrage** — an ETF (RITC, quoted in USD) whose fair value
  is a basket of two CAD stocks. Trade the dislocation, price private tender
  offers against what unwinding them would really fetch, and hedge the currency.

Scoring is by rank, not by dollars: each heat is ranked, the ranks are averaged,
and the two cases are weighted equally. The handout says this is to stop anyone
betting the house. That shapes every risk decision below — **the worst heat
costs more places than the best heat wins**.

---

## Repository layout

```
code/
  vol_strategy.py          volatility case strategy
  test_vol_strategy.py     30 tests, no network needed
  simulate.py              Monte Carlo: runs the real strategy vs a fake server
  monitor.py               read-only recorder + post-mortem (ticks.csv)
  flatten.py               emergency close-out for the volatility case
  test_news.ipynb          connectivity and field-name check

  etf_strategy.py          ETF arbitrage strategy (--check for a dry inspection)
  test_etf_strategy.py     8 tests, real order-book ladders pinned as fixtures

RUNBOOK.md                 what to do on the day, and when it goes wrong
```

---

## Case 1 — Volatility Trading

### The edge

The market maker prices all ten options with Black-Scholes off a volatility
estimate that **lags** the truth. The analysts tell us the real number first.
Two announcements matter:

| tick | announcement | worth |
| --- | --- | --- |
| 1, 75, 150, 225 | this week's realised volatility | the level to price against |
| 36, 112, 187 | **next week's range** | ~38 ticks of warning before the shift |

Measured on the live server, the mispricing opens at roughly eight volatility
points after each shift and decays to nothing by mid-week. The edge is
concentrated in the first forty ticks of a week.

### Pricing off the right number

Every option expires at tick 300, so Black-Scholes wants the **average**
volatility over the remaining life, not this week's spot level. Once the
mid-week forecast lands, the stretch after the week boundary is priced off it,
weighted by ticks because variance is additive:

```
sigma^2 = (ticks_before * sigma_this^2 + ticks_after * sigma_next^2) / ticks_left
```

In one live heat that turned a 5-point edge into 19: the forecast said week
four would collapse from 35% to 8–13%, and the blend moved the pricing
volatility from 0.350 to 0.212 — 38 ticks before the market maker moved.

Time conversion: one year is 240 days, the heat is 20 days over 300 ticks, so
`T = (300 - tick) / 3600` years, and announced percentages divide by 100.

### Screening on dollars, not volatility points

A gap is not worth the same at every moment. Vega decays with the square root
of time left while the $2 commission does not:

| tick | a 2-point gap grosses | round trip | net |
| --- | --- | --- | --- |
| 1 | $11.49 | $4.00 | +7.49 |
| 240 | $5.15 | $4.00 | +1.15 |
| 290 | $2.10 | $4.00 | **−1.90** |

So trades are screened on `|gap| × vega × 10000 > 1.5 × round trip`. A fixed
volatility threshold keeps trading through that crossover at a loss.

### Risk, in four layers

The CRO fines $0.10 per second for every unit of delta past ±7,000. Each layer
below exists because its absence cost real money in a practice heat.

| layer | value | what it prevents |
| --- | --- | --- |
| per-tick order sizing | `MAX_TICK_DELTA` 3,000 | one tick adding 13,000 of delta the hedge must immediately reverse |
| hedge trigger | `DELTA_BAND` 2,500 | **must differ from the line above** — equal values pin the book just under the trigger so the hedge never fires |
| book delta cap | `MAX_OPTION_DELTA` 20,000 | 2,500 contracts can carry 250,000 of delta while the hedge leg stops at 50,000 shares |
| expiry winddown | `WINDDOWN_TICKS` 90 | gamma makes the book unhedgeable exactly when the hedge has least room |

Budget is released a quarter per week (625 / 1,250 / 1,875 / 2,500 contracts)
because the same opportunity arrives four times. Removing that rationing tested
**5× worse on the floor** (−81,915 against −16,426) for no gain in the mean.

**There is deliberately no P&L stop-loss.** The edge only pays as the market
maker converges, so cutting on a drawdown sells positions that are right but
early, and the options cash-settle anyway. The loss that is certain is the
fine, so that is what gets stopped: when the hedge leg is at its own limit and
cannot help, the offending option leg is cut instead.

### What the Monte Carlo says

`simulate.py` answers the same endpoints the RIT server does and runs
`vol_strategy.main()` against them unchanged, so what is measured is the code
that trades — tick guard, budgets, winddown and all. Two hundred paths take
twenty seconds.

Current settings over 250 paired paths: **mean net ≈ 62,000, median ≈ 59,000,
p10 ≈ 15,000, worst ≈ −16,000, losing heats 7/250.**

Paired tests (same seeds, so the comparison is not swamped by path noise)
justified three changes: four orders a tick rather than two (+3,564), the book
delta cap at 40% of the hedge rather than 70%, and a 90-tick winddown. A wider
hedge band tested better on the mean but pushed the worst path from −15,417 to
−19,946, so it was left alone.

---

## Case 2 — Algorithmic ETF Arbitrage

### The edge

In equilibrium `RITC × USD/CAD = BULL + BEAR`. Shocks break it. The comparison
must use prices that can actually be transacted, including the currency:

```
sell the ETF:  RITC_bid × USD_bid      buy the basket:  BULL_ask + BEAR_ask
buy the ETF:   RITC_ask × USD_ask      sell the basket: BULL_bid + BEAR_bid
```

**The currency leg is not optional.** RITC is quoted in USD and USD is itself
an instrument priced in CAD, so holding the ETF is a bet on the exchange rate
unless it is hedged. On a wide quote, converting at the mid rate books +0.115 a
share where the executable rate gives −0.370.

Three market orders cost $0.06 a share, and the trade must clear that by half
again before it is worth putting on.

### Tenders are a liquidity problem, not a pricing one

Private tenders arrive large. One captured live: **83,000 shares of RITC at
24.49**. The books at that moment:

```
RITC  best bid 24.22 for 12,200      total depth 64,110
BULL  total depth 41,969             — less than half the tender
BEAR  total depth 72,600
```

Priced at the touch that tender looks like −0.056 a share. Walked down the
actual ladder it is **−0.146**, and a third of the position could not have been
unwound at any visible price. The provided template accepts every tender on
sight; that is the single most expensive thing it does.

### The converters

The web client's Assets tab swaps 10,000 RITC plus $1,500 USD for 10,000 BULL
and 10,000 BEAR, or the reverse, in two ticks. At **0.151 CAD a share** against
0.06 of fees it is never the cheap route — it is the route that exists when no
route does. Its value is spreading the exit across three order books instead of
one.

With it, the same 83,000-share tender unwinds with **nothing stranded**. It is
still refused at 24.49 (−0.329 a share); at 24.00 it clears +0.169 and is
taken. The point is that a well-priced large tender is no longer rejected
merely for being large.

**The API cannot press the button.** When a plan needs conversions the script
prints how many and which one, and a person has to click.

### Limits are read, not assumed

The handout says the ETF counts double toward the position limits. The live
server weights it at **0.5** and allows 300,000 gross rather than 250,000.
Taking the handout at its word would have sized every trade wrong — arbitrage
capacity of 75,000 shares instead of the real 120,000. Weights and caps are
read from `/securities` and `/limits` at runtime, so a differently configured
competition server is handled too.

---

## Five things that cost the most to learn

Each was found by running against the live server, not by reading the handout.
All of them apply to both cases.

1. **A heat restarts `news_id` at 1.** Carrying the previous heat's cursor
   filtered out every announcement, and one heat ran 224 ticks on the last
   heat's final volatility with ten meaningless signals a tick.

2. **Anything dropped from the risk table takes its risk with it.** Two deep
   in-the-money calls stopped inverting at expiry and left the table carrying
   about 50,000 of delta. The hedge was then sized against a number of the
   wrong sign; the underlying leg ran to its limit and the fine reached
   $91,627 against $108,693 of P&L.

3. **Stopping mid-heat with an open book is the most expensive single
   mistake.** One heat was interrupted at tick 148 and left alone: nothing
   hedged for 150 ticks, the delta parked around −30,000, and the fine reached
   $74,309 against $65,716 of P&L. A profitable heat became a loss with no
   trade involved. Both scripts now warn loudly on the way out.

4. **The touch price is not the price.** True of tender unwinds, true of
   currency conversion, true of anything sized above the top level.

5. **Editing a file does not change a running process.** One whole heat was
   spent debugging behaviour that had already been fixed nine minutes earlier.
   Restart after every change.

---

## Running it

```bash
pip install -r code/requirements.txt

python code/test_vol_strategy.py     # 30 tests, no server needed
python code/test_etf_strategy.py     #  8 tests, no server needed

python code/etf_strategy.py --check  # inspect the live ETF case, place nothing
python -u code/monitor.py            # follow a heat, record to ticks.csv
python code/monitor.py --review      # post-mortem across recorded heats
python code/simulate.py -n 200       # Monte Carlo the volatility strategy
python code/simulate.py --sweep      # compare settings on paired paths
```

Both strategies default to the RIT Client REST API on `localhost:9999`, which
needs the Windows client running and logged in. `MODE = "dma"` talks to the
server directly and works from any machine; it reads credentials from
`RIT_USER` and `RIT_PASS` rather than the file, because this repository is
public.

`DRY_RUN` logs orders instead of sending them. Leave it on until `--check` and
a full dry heat both look right.

See [RUNBOOK.md](RUNBOOK.md) for ports, credentials, the competition-day
checklist and what to do when something breaks.
