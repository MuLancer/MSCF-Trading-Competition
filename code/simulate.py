"""
Monte Carlo the whole heat, running the real strategy against a fake server.

    python3 simulate.py                  # 100 heats on the current settings
    python3 simulate.py -n 300           # more paths
    python3 simulate.py --sweep          # compare parameter settings

Nothing here reimplements the strategy. The simulator answers the same
endpoints the RIT server does, and vol_strategy.main() is run against it
unchanged, so what is measured is the code that will trade tomorrow --
including the tick guard, the heat reset, the budgets and the winddown.

Modelled after the live server's observed behaviour:
  - the price path is the case's own P_t = P_t-1 * (1 + r_t), r ~ N(0, s/60)
  - weekly volatility is redrawn inside the range the practice server used
  - the market maker quotes off a LAGGING volatility that converges toward
    the truth over the week, which is the entire source of edge
  - the mid-week announcement gives a range bracketing next week's level

The market maker's learning rate is the one free parameter and it is set so
the mispricing decays the way the live logs showed: about eight volatility
points at a week's open, gone by roughly its midpoint.
"""

import argparse
import random
import statistics
import sys

from py_vollib.black_scholes import black_scholes as bs
from py_vollib.black_scholes.greeks.analytical import delta as bs_delta

import vol_strategy as vs

SPREAD = 0.02              # the market maker's fixed two-cent quote
MM_LEARNING_RATE = 0.07    # calibrated so the gap halves in ~10 ticks
VOL_RANGE = (0.09, 0.40)   # weekly levels seen on the practice server
FORECAST_HALF_WIDTH = 0.025


class Simulator:
    """A whole heat: price path, market maker, order book and the scorecard."""

    def __init__(self, rng):
        self.rng = rng
        self.tick = 0
        self.spot = 50.0
        self.weekly_vol = [rng.uniform(*VOL_RANGE) for _ in range(4)]
        # the maker starts the heat knowing week one, then lags every shift
        self.mm_vol = self.weekly_vol[0]
        self.positions = {t: 0 for t in self._tickers()}
        self.cash = 0.0
        self.fines = 0.0
        self.news = []
        self.peak_delta = 0.0
        self.ticks_over = 0
        self._publish_news()

    @staticmethod
    def _tickers():
        out = [vs.UNDERLYING]
        for k in (48, 49, 50, 51, 52):
            out += [f"RTM{k}C", f"RTM{k}P"]
        return out

    # ----------------------------------------------------------- the market
    def true_vol(self):
        return self.weekly_vol[min(self.tick // vs.WEEK_TICKS, 3)]

    def _fair(self, ticker, vol):
        right, K = vs.parse_option_ticker(ticker)
        T = vs.time_to_expiry(self.tick)
        return max(bs(right, self.spot, K, T, 0.0, vol), 0.0)

    def quote(self, ticker):
        """(bid, ask) as the maker shows them."""
        if ticker == vs.UNDERLYING:
            return self.spot - SPREAD / 2, self.spot + SPREAD / 2
        mid = self._fair(ticker, self.mm_vol)
        return max(mid - SPREAD / 2, 0.0), mid + SPREAD / 2

    def portfolio_delta(self):
        """Delta under the true volatility -- the exposure that actually bites."""
        T = vs.time_to_expiry(self.tick)
        total = float(self.positions[vs.UNDERLYING])
        for ticker, pos in self.positions.items():
            if not pos or ticker == vs.UNDERLYING:
                continue
            right, K = vs.parse_option_ticker(ticker)
            total += bs_delta(right, self.spot, K, T, 0.0,
                              self.true_vol()) * pos * vs.CONTRACT_SIZE
        return total

    def step(self):
        """Advance one tick: fine the book, move the price, teach the maker."""
        delta = self.portfolio_delta()
        if abs(delta) > vs.DELTA_HARD_LIMIT:
            self.fines += (abs(delta) - vs.DELTA_HARD_LIMIT) * 0.10
            self.ticks_over += 1
        if abs(delta) > abs(self.peak_delta):
            self.peak_delta = delta

        self.tick += 1
        per_tick = self.true_vol() / 60.0          # annual -> per tick
        self.spot *= 1.0 + self.rng.gauss(0.0, per_tick)
        self.spot = max(self.spot, 1.0)
        self.mm_vol += (self.true_vol() - self.mm_vol) * MM_LEARNING_RATE
        self._publish_news()

    def _publish_news(self):
        """Weekly confirmations and the mid-week range, as the server sends them."""
        t, wk = self.tick, self.tick // vs.WEEK_TICKS
        if t in (1, 75, 150, 225):
            pct = self.weekly_vol[min(wk, 3)] * 100
            body = (f"The current risk free rate is 0%. RTM is an ETF. Its "
                    f"realized volatility is {pct:.0f}%.") if t == 1 else (
                    f"The analysts have informed you that the realized "
                    f"volatility of RTM this week will be {pct:.0f}%")
            self._add(body)
        elif t in (36, 112, 187) and wk + 1 < 4:
            nxt = self.weekly_vol[wk + 1] * 100
            lo = max(nxt - FORECAST_HALF_WIDTH * 100, 1)
            hi = nxt + FORECAST_HALF_WIDTH * 100
            self._add("The analysts have informed you that the realized "
                      f"volatility of RTM next week will be between "
                      f"{lo:.0f}% and {hi:.0f}%")

    def _add(self, body):
        self.news.append({"news_id": len(self.news) + 1, "tick": self.tick,
                          "ticker": None, "headline": "Announcement",
                          "body": body})

    # ------------------------------------------------------------ the fills
    def execute(self, ticker, action, qty):
        bid, ask = self.quote(ticker)
        price = ask if action == "BUY" else bid
        signed = qty if action == "BUY" else -qty
        fee = (vs.FEE_OPT * qty if ticker != vs.UNDERLYING
               else vs.FEE_RTM * qty)
        self.cash -= signed * price * (vs.CONTRACT_SIZE
                                       if ticker != vs.UNDERLYING else 1)
        self.cash -= fee
        self.positions[ticker] += signed


    def settle(self):
        """Options cash-settle at intrinsic, RTM closes at the last price."""
        total = self.cash + self.positions[vs.UNDERLYING] * self.spot
        for ticker, pos in self.positions.items():
            if not pos or ticker == vs.UNDERLYING:
                continue
            right, K = vs.parse_option_ticker(ticker)
            intrinsic = max(self.spot - K, 0) if right == "c" else max(K - self.spot, 0)
            total += pos * intrinsic * vs.CONTRACT_SIZE
        return total

    # ------------------------------------------------- the server's answers
    def securities(self):
        out = []
        for ticker in self._tickers():
            bid, ask = self.quote(ticker)
            out.append({"ticker": ticker, "bid": round(bid, 2),
                        "ask": round(ask, 2), "last": round((bid + ask) / 2, 2),
                        "position": self.positions[ticker]})
        return out


def run_one(seed):
    """One heat, start to finish, driving the real vol_strategy.main()."""
    sim = Simulator(random.Random(seed))

    def fake_get_case(session):
        # Every poll advances the clock by one tick. The strategy asks once at
        # the end of each pass, so this is the heat's heartbeat.
        if sim.tick < vs.TOTAL_TICKS:
            sim.step()
        return sim.tick, "ACTIVE"

    def fake_api_request(session, method, endpoint, params=None):
        if endpoint == "securities":
            return sim.securities()
        if endpoint == "case":
            return {"tick": sim.tick, "status": "ACTIVE"}
        if endpoint == "news":
            after = int((params or {}).get("after", 0))
            return [n for n in sim.news if n["news_id"] > after]
        if endpoint == "trader":
            return {"nlv": sim.settle(), "total_fines": sim.fines}
        if endpoint == "orders" and method == "POST":
            sim.execute(params["ticker"], params["action"], int(params["quantity"]))
            return {"order_id": 1}
        return None

    saved = (vs.get_case, vs.get_securities, vs.api_request,
             vs.LOOP_SLEEP, vs.DRY_RUN)
    printed = []
    original_print = print
    try:
        vs.get_case = fake_get_case
        vs.get_securities = lambda s: sim.securities()
        vs.api_request = fake_api_request
        vs.LOOP_SLEEP = 0
        vs.DRY_RUN = False
        vs.shutdown = False
        import builtins
        builtins.print = lambda *a, **k: printed.append(a)
        vs.main()
    finally:
        import builtins
        builtins.print = original_print
        (vs.get_case, vs.get_securities, vs.api_request,
         vs.LOOP_SLEEP, vs.DRY_RUN) = saved

    return {
        "pnl": sim.settle(),
        "fines": sim.fines,
        "net": sim.settle() - sim.fines,
        "peak_delta": sim.peak_delta,
        "ticks_over": sim.ticks_over,
    }


def report(label, results):
    nets = sorted(r["net"] for r in results)
    fines = [r["fines"] for r in results]
    over = [r["ticks_over"] for r in results]
    n = len(nets)
    print(f"\n=== {label}  ({n} heats) ===")
    print(f"  net P&L   mean {statistics.mean(nets):>12,.0f}   "
          f"median {statistics.median(nets):>12,.0f}")
    print(f"            p10  {nets[n//10]:>12,.0f}   "
          f"p90    {nets[-max(n//10,1)]:>12,.0f}")
    print(f"            worst{nets[0]:>12,.0f}   "
          f"best   {nets[-1]:>12,.0f}")
    print(f"  losing heats {sum(1 for x in nets if x < 0)}/{n}")
    print(f"  fines     mean {statistics.mean(fines):>12,.0f}   "
          f"max {max(fines):>12,.0f}")
    print(f"  ticks over the delta line: mean {statistics.mean(over):.1f}")
    return statistics.mean(nets), statistics.median(nets), nets[0]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-n", type=int, default=100)
    ap.add_argument("--sweep", action="store_true")
    args = ap.parse_args()

    if not args.sweep:
        results = [run_one(s) for s in range(args.n)]
        report("current settings", results)
        return

    # same paths for every setting, so the comparison is paired
    variants = [
        ("baseline", {}),
        ("threshold 0.01", {"IV_GAP_THRESHOLD": 0.01}),
        ("threshold 0.03", {"IV_GAP_THRESHOLD": 0.03}),
        ("hedge band 1500", {"DELTA_BAND": 1500}),
        ("hedge band 4000", {"DELTA_BAND": 4000}),
        ("1 trade/tick", {"MAX_NEW_TRADES_PER_TICK": 1}),
        ("4 trades/tick", {"MAX_NEW_TRADES_PER_TICK": 4}),
        ("budget over 2wk", {"BUDGET_WEEKS": 2}),
        ("no rationing", {"BUDGET_WEEKS": 1}),
    ]
    rows = []
    for label, overrides in variants:
        saved = {k: getattr(vs, k) for k in overrides}
        for k, v in overrides.items():
            setattr(vs, k, v)
        try:
            results = [run_one(s) for s in range(args.n)]
            rows.append((label,) + report(label, results))
        finally:
            for k, v in saved.items():
                setattr(vs, k, v)

    print(f"\n{'setting':>18}{'mean net':>13}{'median':>13}{'worst':>13}")
    for label, mean, med, worst in rows:
        print(f"{label:>18}{mean:>13,.0f}{med:>13,.0f}{worst:>13,.0f}")


if __name__ == "__main__":
    main()
