"""Read-only monitor for ETF arbitrage.

Use ``python3 -u etf_monitor.py`` to record etf_ticks.csv or
``python3 etf_monitor.py --review`` to review it.
"""

import csv
import os
import sys
import time

import requests

import etf_strategy as es

HERE = os.path.dirname(os.path.abspath(__file__))
TICKS_CSV = os.path.join(HERE, "etf_ticks.csv")
SUMMARY_CSV = os.path.join(HERE, "etf_summary.csv")
COLUMNS = ["heat", "tick", "pnl", "fines", "pnl_net", "rich", "cheap",
           "gross", "net", "room", "ritc", "bull", "bear", "usd",
           "open_legs"]
SUMMARY_COLUMNS = ["heat", "ticks", "pnl", "fines", "net", "drawdown",
                   "opportunities"]


def read_ticks():
    if not os.path.exists(TICKS_CSV):
        return []
    with open(TICKS_CSV, newline="") as file:
        return list(csv.DictReader(file))


def append(row):
    fresh = not os.path.exists(TICKS_CSV)
    with open(TICKS_CSV, "a", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=COLUMNS)
        if fresh:
            writer.writeheader()
        writer.writerow(row)


def append_summary(row):
    fresh = not os.path.exists(SUMMARY_CSV)
    with open(SUMMARY_CSV, "a", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=SUMMARY_COLUMNS)
        if fresh:
            writer.writeheader()
        writer.writerow(row)


def next_heat():
    rows = read_ticks()
    return int(rows[-1]["heat"]) + 1 if rows else 1


def snapshot(session, tick, caps):
    trader = es.api_request(session, "GET", "trader")
    securities = es.get_securities(session)
    if trader is None or securities is None:
        return None
    positions = es.book(securities)
    weights = es.limit_weights(securities)
    rich, cheap = es.arb_edges(positions)
    gross, net = es.limit_usage(positions, weights)
    fines = trader.get("total_fines", 0)
    return {
        "tick": tick,
        "pnl": round(trader["nlv"], 2),
        "fines": round(fines, 2),
        "pnl_net": round(trader["nlv"] - fines, 2),
        "rich": round(rich, 4),
        "cheap": round(cheap, 4),
        "gross": round(gross),
        "net": round(net),
        "room": es.arb_room(positions, weights, caps),
        "ritc": int(positions[es.RITC]["position"]),
        "bull": int(positions[es.BULL]["position"]),
        "bear": int(positions[es.BEAR]["position"]),
        "usd": int(positions[es.USD]["position"]),
        "open_legs": sum(1 for ticker in (es.RITC, es.BULL, es.BEAR)
                          if positions[ticker]["position"]),
    }


def summary(rows):
    pnls = [float(row["pnl_net"]) for row in rows]
    peak, drawdown = pnls[0], 0.0
    for pnl in pnls:
        peak = max(peak, pnl)
        drawdown = max(drawdown, peak - pnl)
    floor = es.ARB_MARGIN * es.ARB_LEG_COST
    opportunities = sum(1 for row in rows
                        if max(float(row["rich"]), float(row["cheap"])) > floor)
    return {"pnl": float(rows[-1]["pnl"]), "fines": float(rows[-1]["fines"]),
            "net": pnls[-1], "drawdown": drawdown,
            "opportunities": opportunities, "ticks": len(rows)}


def heat_summary(heat, rows):
    result = summary(rows)
    return {"heat": heat, **result}


def review():
    ticks = read_ticks()
    if not ticks:
        print("nothing recorded yet -- run etf_monitor.py through a heat first")
        return
    heats = {}
    for row in ticks:
        heats.setdefault(int(row["heat"]), []).append(row)
    print(f"{'#':>3}{'ticks':>7}{'P&L':>11}{'fines':>9}{'net':>11}"
          f"{'drawdn':>10}{'opp':>6}")
    for heat, rows in heats.items():
        result = heat_summary(heat, rows)
        partial = "  (partial)" if result["ticks"] <= 250 else ""
        print(f"{heat:>3}{result['ticks']:>7}{result['pnl']:>11,.0f}"
              f"{result['fines']:>9,.0f}{result['net']:>11,.0f}"
              f"{result['drawdown']:>10,.0f}{result['opportunities']:>6}{partial}")
    print(f"\n  detail: {TICKS_CSV}")


def live(quiet):
    session = requests.Session()
    session.headers.update(es.AUTHORIZATION)
    caps = es.get_limits(session)
    heat, last_tick, samples = next_heat(), None, []
    print(f"monitoring {es.API_ENDPOINT}   heat #{heat}   (Ctrl+C to stop)\n")
    while True:
        try:
            tick, status = es.get_case(session)
            if tick is None or status != "ACTIVE" or tick == last_tick:
                time.sleep(0.3)
                continue
            if last_tick is not None and tick < last_tick:
                if samples:
                    append_summary(heat_summary(heat, samples))
                heat += 1
                samples = []
            last_tick = tick
            row = snapshot(session, tick, caps)
            if row is None:
                continue
            row["heat"] = heat
            append(row)
            if not quiet:
                print(f"t={tick:>3} rich={row['rich']:+.3f} cheap={row['cheap']:+.3f} "
                      f"pnl={row['pnl_net']:>10,.0f} gross={row['gross']:>8,} "
                      f"net={row['net']:>8,} room={row['room']:>7,}")
        except KeyboardInterrupt:
            break
        except Exception as error:
            print(f"  {type(error).__name__}: {error}")
            time.sleep(1)
    if samples:
        append_summary(heat_summary(heat, samples))
    review()


if __name__ == "__main__":
    review() if "--review" in sys.argv else live("--quiet" in sys.argv)
