"""
Watch heats go by and keep a per-heat scorecard. Read-only: places no orders.

    python3 monitor.py                 # follow live, append to heats.csv
    python3 monitor.py --quiet         # only the per-heat summary lines

Run it in a second terminal beside vol_strategy.py. It samples /trader for
P&L and fines and /securities for the book, notices when a heat rolls over,
and writes one row per finished heat so the practice runs can be compared
instead of remembered.

The delta columns are the ones that explain the fines: the CRO charges $0.10
a second for every unit past 7,000, so `over` (ticks spent past the line) and
`peak` say where a fine came from, which the P&L alone never does.
"""

import csv
import os
import sys
import time
from datetime import datetime

import requests

import vol_strategy as vs

CSV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "heats.csv")
COLUMNS = ["finished", "ticks", "pnl", "fines", "pnl_net",
           "peak_delta", "ticks_over_limit", "max_opt_gross", "max_opt_net"]


class HeatRecord:
    def __init__(self):
        self.ticks = 0
        self.pnl = 0.0
        self.fines = 0.0
        self.peak_delta = 0.0
        self.ticks_over = 0
        self.max_gross = 0
        self.max_net = 0

    def observe(self, pnl, fines, delta, gross, net):
        self.ticks += 1
        self.pnl, self.fines = pnl, fines
        if abs(delta) > abs(self.peak_delta):
            self.peak_delta = delta
        if abs(delta) > vs.DELTA_HARD_LIMIT:
            self.ticks_over += 1
        self.max_gross = max(self.max_gross, int(abs(gross)))
        self.max_net = max(self.max_net, int(abs(net)))

    def row(self):
        return {
            "finished": datetime.now().strftime("%H:%M:%S"),
            "ticks": self.ticks,
            "pnl": round(self.pnl, 2),
            "fines": round(self.fines, 2),
            "pnl_net": round(self.pnl - self.fines, 2),
            "peak_delta": int(self.peak_delta),
            "ticks_over_limit": self.ticks_over,
            "max_opt_gross": self.max_gross,
            "max_opt_net": self.max_net,
        }


def append_csv(row):
    fresh = not os.path.exists(CSV_PATH)
    with open(CSV_PATH, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        if fresh:
            w.writeheader()
        w.writerow(row)


def print_summary():
    if not os.path.exists(CSV_PATH):
        return
    with open(CSV_PATH) as f:
        rows = list(csv.DictReader(f))
    if not rows:
        return
    print(f"\n{'#':>3}{'ticks':>7}{'P&L':>12}{'fines':>10}{'net':>12}"
          f"{'peak Δ':>10}{'over':>6}")
    for i, r in enumerate(rows, 1):
        print(f"{i:>3}{r['ticks']:>7}{float(r['pnl']):>12,.0f}"
              f"{float(r['fines']):>10,.0f}{float(r['pnl_net']):>12,.0f}"
              f"{int(r['peak_delta']):>10,}{r['ticks_over_limit']:>6}")
    nets = [float(r["pnl_net"]) for r in rows]
    fines = [float(r["fines"]) for r in rows]
    print(f"{'':>3}{'':>7}{'':>12}{'':>10}{'-'*11:>12}")
    print(f"{'avg':>3}{'':>7}{'':>12}{sum(fines)/len(fines):>10,.0f}"
          f"{sum(nets)/len(nets):>12,.0f}")
    print(f"{'min':>3}{'':>7}{'':>12}{'':>10}{min(nets):>12,.0f}"
          "   <- ranking is by average heat rank, so the floor matters")


def main(quiet):
    session = requests.Session()
    session.headers.update(vs.AUTHORIZATION)
    state = vs.new_vol_state()
    heat = HeatRecord()
    last_tick = None

    print(f"monitoring {vs.API_ENDPOINT}  (Ctrl+C to stop)\n")
    while True:
        try:
            tick, status = vs.get_case(session)
            if tick is None:
                time.sleep(1)
                continue

            if last_tick is not None and tick < last_tick and heat.ticks:
                row = heat.row()
                append_csv(row)
                print(f"\n=== heat done: P&L {row['pnl']:,.0f}  "
                      f"fines {row['fines']:,.0f}  net {row['pnl_net']:,.0f}  "
                      f"peak Δ {row['peak_delta']:,}  "
                      f"{row['ticks_over_limit']} ticks over ===\n")
                heat = HeatRecord()
                state = vs.new_vol_state()

            if status != "ACTIVE" or tick == last_tick:
                time.sleep(0.3)
                continue
            last_tick = tick

            trader = vs.api_request(session, "GET", "trader")
            securities = vs.get_securities(session)
            if trader is None or securities is None:
                continue
            vs.update_vol_state(session, state)

            forecast = vs.blended_vol(state, tick) if state["seeded"] else 0.2
            rows = vs.build_signal_table(securities, forecast, tick,
                                         state["risk_free"])
            delta = vs.portfolio_delta(securities, rows)
            legs = vs.option_legs(securities)
            gross = sum(abs(x["position"]) for x in legs)
            net = sum(x["position"] for x in legs)
            heat.observe(trader["nlv"], trader["total_fines"], delta, gross, net)

            if not quiet:
                flag = "  <-- OVER" if abs(delta) > vs.DELTA_HARD_LIMIT else ""
                print(f"t={tick:>3} vol={forecast:.3f} pnl={trader['nlv']:>10,.0f} "
                      f"fines={trader['total_fines']:>8,.0f} "
                      f"delta={delta:>9,.0f} opt={int(net):>5}{flag}")
        except KeyboardInterrupt:
            break
        except Exception as e:
            print(f"  {type(e).__name__}: {e}")
            time.sleep(1)

    print_summary()


if __name__ == "__main__":
    main("--quiet" in sys.argv)
