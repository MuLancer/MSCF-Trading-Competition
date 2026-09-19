"""
Watch heats and keep enough of a record to argue with afterwards. Read-only.

    python3 -u monitor.py          # follow live, recording to ticks.csv
    python3 monitor.py --quiet     # same, only the per-heat lines
    python3 monitor.py --review    # post-mortem over everything recorded

Run the live mode in a second terminal beside vol_strategy.py. It never
places an order; it samples /trader, /securities and /news and writes a row
per tick, so a heat can be taken apart later instead of remembered.

The review mode is the point. P&L alone never says why a heat went the way it
did: it cannot tell a fine from a loss, a missed trade from an absent one, or
a position that drifted from one that was never hedged. The recorded columns
are chosen so those are all separable.
"""

import csv
import os
import sys
import time

import requests

import vol_strategy as vs

HERE = os.path.dirname(os.path.abspath(__file__))
# ticks.csv is the only thing written, one row as each tick happens. Heat
# summaries are derived from it on demand rather than written at shutdown:
# a monitor that is killed rather than asked to stop still leaves its record.
TICKS_CSV = os.path.join(HERE, "ticks.csv")

TICK_COLUMNS = ["heat", "tick", "week", "pnl", "fines", "delta", "rtm",
                "opt_gross", "opt_net", "forecast", "mkt_iv", "signals",
                "room", "book_delta"]
FINE_RATE = 0.10          # $ per second per unit past the limit


def append(path, columns, row):
    fresh = not os.path.exists(path)
    with open(path, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=columns)
        if fresh:
            w.writeheader()
        w.writerow(row)


def next_heat_number():
    for r in reversed(read_ticks()):
        return int(r["heat"]) + 1
    return 1


def read_ticks():
    if not os.path.exists(TICKS_CSV):
        return []
    with open(TICKS_CSV) as f:
        return list(csv.DictReader(f))


def summarise(heat_no, rows):
    """Derive a heat's scorecard from its ticks, causes kept separate."""
    pnls = [float(r["pnl"]) for r in rows]
    peak, drawdown = pnls[0], 0.0
    for p in pnls:
        peak = max(peak, p)
        drawdown = max(drawdown, peak - p)

    deltas = [float(r["delta"]) for r in rows]
    over = [d for d in deltas if abs(d) > vs.DELTA_HARD_LIMIT]
    return {
        "heat": heat_no,
        "ticks": len(rows),
        "pnl": pnls[-1],
        "fines": float(rows[-1]["fines"]),
        "pnl_net": pnls[-1] - float(rows[-1]["fines"]),
        "peak_delta": max(deltas, key=abs, default=0),
        "ticks_over_limit": len(over),
        "fine_from_delta": sum((abs(d) - vs.DELTA_HARD_LIMIT) * FINE_RATE
                               for d in over),
        "max_drawdown": drawdown,
        # edge was there but the budget was not
        "ticks_blocked": sum(1 for r in rows
                             if int(r["signals"]) and not int(r["room"])),
        # no edge to act on at all
        "ticks_idle": sum(1 for r in rows if not int(r["signals"])),
    }


def sample(session, state, tick):
    """One read-only snapshot of everything worth keeping."""
    trader = vs.api_request(session, "GET", "trader")
    securities = vs.get_securities(session)
    if trader is None or securities is None:
        return None
    vs.update_vol_state(session, state)

    forecast = vs.blended_vol(state, tick) if state["seeded"] else 0.0
    rows = vs.build_signal_table(securities, forecast, tick, state["risk_free"])
    legs = vs.option_legs(securities)
    quoted = [r["market_iv"] for r in rows if r["market_iv"]]

    return {
        "tick": tick,
        "week": vs.week_of(tick),
        "pnl": round(trader["nlv"], 2),
        "fines": round(trader["total_fines"], 2),
        "delta": round(vs.portfolio_delta(securities, rows)),
        "rtm": int(next(x["position"] for x in securities
                        if x["ticker"] == vs.UNDERLYING)),
        "opt_gross": int(sum(abs(x["position"]) for x in legs)),
        "opt_net": int(sum(x["position"] for x in legs)),
        "forecast": round(forecast, 4),
        "mkt_iv": round(sum(quoted) / len(quoted), 4) if quoted else "",
        "signals": len(vs.select_trades(rows)),
        "room": int(max(vs.option_room(legs, tick)[0], 0)),
        "book_delta": round(sum(r["delta"] * r["position"] * vs.CONTRACT_SIZE
                                for r in rows)),
    }


def close_heat(heat_no, samples):
    """Report a finished heat. Nothing to persist -- the ticks already are."""
    if not samples:
        return None
    return summarise(heat_no, [{k: str(v) for k, v in s.items()}
                               for s in samples])


# ------------------------------------------------------------------- review
def review():
    ticks = read_ticks()
    if not ticks:
        print("nothing recorded yet -- run the live mode through a heat first")
        return

    heats, order = {}, []
    for r in ticks:
        h = int(r["heat"])
        if h not in heats:
            heats[h] = []
            order.append(h)
        heats[h].append(r)
    summaries = [summarise(h, heats[h]) for h in order]

    print(f"{'#':>3}{'ticks':>7}{'P&L':>11}{'fines':>9}{'net':>11}"
          f"{'drawdn':>9}{'peak d':>9}{'over':>6}{'blocked':>9}{'idle':>6}")
    for x in summaries:
        partial = "" if x["ticks"] > 250 else "  (partial)"
        print(f"{x['heat']:>3}{x['ticks']:>7}{x['pnl']:>11,.0f}"
              f"{x['fines']:>9,.0f}{x['pnl_net']:>11,.0f}"
              f"{x['max_drawdown']:>9,.0f}{x['peak_delta']:>9,.0f}"
              f"{x['ticks_over_limit']:>6}{x['ticks_blocked']:>9}"
              f"{x['ticks_idle']:>6}{partial}")

    full = [x for x in summaries if x["ticks"] > 250]
    if full:
        nets = [x["pnl_net"] for x in full]
        print(f"\n  complete heats: {len(full)}")
        print(f"  average net {sum(nets)/len(nets):>12,.0f}")
        print(f"  worst heat  {min(nets):>12,.0f}   "
              "<- ranking averages heat ranks, so the floor costs places")

    print("\n--- where it went ---")
    for x in summaries:
        if x["fines"] >= 1:
            share = 100 * x["fines"] / max(abs(x["pnl"]), 1)
            print(f"  heat {x['heat']}: fines {x['fines']:,.0f} "
                  f"= {share:.0f}% of P&L over {x['ticks_over_limit']} ticks "
                  f"past the line (delta accounts for ~{x['fine_from_delta']:,.0f})")
    if all(x["fines"] < 1 for x in summaries):
        print("  no fines recorded")

    blocked = sum(x["ticks_blocked"] for x in summaries)
    idle = sum(x["ticks_idle"] for x in summaries)
    total = sum(x["ticks"] for x in summaries)
    print(f"\n  {blocked} of {total} ticks ({100*blocked//max(total,1)}%) had "
          "signals and no budget   <- capacity, not the model")
    print(f"  {idle} of {total} ticks ({100*idle//max(total,1)}%) had no signal "
          "at all          <- no edge; idling was right")

    stuck = sum(1 for a, b in zip(ticks, ticks[1:])
                if a["heat"] == b["heat"] and a["rtm"] == b["rtm"]
                and abs(float(b["delta"])) > vs.DELTA_HARD_LIMIT)
    if stuck:
        print(f"\n  {stuck} ticks sat past the fine line without the hedge moving")
        print("     -> the strategy was stopped, or the hedge had no room left")

    print(f"\n  detail: {TICKS_CSV}")


# --------------------------------------------------------------------- live
def live(quiet):
    session = requests.Session()
    session.headers.update(vs.AUTHORIZATION)
    state = vs.new_vol_state()
    heat_no = next_heat_number()
    samples = []
    last_tick = None

    print(f"monitoring {vs.API_ENDPOINT}   heat #{heat_no}   (Ctrl+C to stop)\n")
    while True:
        try:
            tick, status = vs.get_case(session)
            if tick is None:
                time.sleep(1)
                continue

            if last_tick is not None and tick < last_tick and samples:
                row = close_heat(heat_no, samples)
                print(f"\n=== heat {heat_no}: P&L {row['pnl']:,.0f}  "
                      f"fines {row['fines']:,.0f}  net {row['pnl_net']:,.0f}  "
                      f"drawdown {row['max_drawdown']:,.0f}  "
                      f"{row['ticks_over_limit']} ticks over ===\n")
                heat_no += 1
                samples = []
                state = vs.new_vol_state()

            if status != "ACTIVE" or tick == last_tick:
                time.sleep(0.3)
                continue
            last_tick = tick

            snap = sample(session, state, tick)
            if snap is None:
                continue
            samples.append(snap)
            append(TICKS_CSV, TICK_COLUMNS, dict(snap, heat=heat_no))

            if not quiet:
                flag = "  <-- OVER" if abs(snap["delta"]) > vs.DELTA_HARD_LIMIT else ""
                print(f"t={tick:>3} wk={snap['week']} "
                      f"vol={snap['forecast']:.3f} mkt={snap['mkt_iv'] or 0:.3f} "
                      f"pnl={snap['pnl']:>9,.0f} fines={snap['fines']:>7,.0f} "
                      f"Δ={snap['delta']:>8,} sig={snap['signals']:>2} "
                      f"room={snap['room']:>4}{flag}")
        except KeyboardInterrupt:
            break
        except Exception as e:
            print(f"  {type(e).__name__}: {e}")
            time.sleep(1)

    if samples:
        close_heat(heat_no, samples)
    print()
    review()


if __name__ == "__main__":
    if "--review" in sys.argv:
        review()
    else:
        live("--quiet" in sys.argv)
