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
SUMMARY_CSV = os.path.join(HERE, "vol_summary.csv")

TICK_COLUMNS = ["heat", "tick", "week", "pnl", "fines", "delta", "rtm",
                "opt_gross", "opt_net", "forecast", "mkt_iv", "signals",
                "room", "book_delta"]
SUMMARY_COLUMNS = ["heat", "ticks", "pnl", "fines", "pnl_net", "peak_delta",
                   "ticks_over_limit", "fine_from_delta", "max_drawdown",
                   "ticks_blocked", "ticks_idle"]
FINE_RATE = 0.10          # $ per second per unit past the limit

# How long to wait between retries when the server stops answering. Heats end
# and the case host is restarted between them; a recorder that dies in every
# gap records exactly one heat.
RECONNECT_WAIT = 5

# Consecutive ticks of a completely unchanged book, while delta is past the
# fine line, before the monitor says so out loud. The book moves every few
# ticks in a live heat, so a frozen one past the line means nobody is
# trading it -- see frozen_run() for what that cost on the record.
FROZEN_TICK_ALARM = 5


def book_state(row):
    """The part of a row that must change if anything is being traded."""
    return (row["rtm"], row["opt_gross"], row["opt_net"])


def frozen_run(rows):
    """(longest run, first row, last row) with the book frozen past the line.

    This is the failure that cost the most on the record, and it is not a
    hedging decision -- it is the absence of one. Heat 6 froze at tick 248
    with RTM at 33,652 and held every position for 51 ticks while delta ran
    to 59,212 and P&L went 59,158 -> -57,538. Heat 2 froze at 269 for 30
    ticks, 132,515 -> 66,198. In neither case was RTM near its 50,000 limit:
    the hedge had room and did not use it, because nothing was running.
    """
    best, run, start = (0, None, None), 0, None
    for a, b in zip(rows, rows[1:]):
        if (a["heat"] == b["heat"] and book_state(a) == book_state(b)
                and abs(float(b["delta"])) > vs.DELTA_HARD_LIMIT):
            run += 1
            start = start or a
            if run > best[0]:
                best = (run, start, b)
        else:
            run, start = 0, None
    return best


def append(path, columns, row):
    fresh = not os.path.exists(path)
    with open(path, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=columns)
        if fresh:
            w.writeheader()
        w.writerow(row)


def append_summary(summary):
    append(SUMMARY_CSV, SUMMARY_COLUMNS, summary)


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

    # Separate the two causes rather than offering both. "No room left" is
    # checkable: it means RTM sat at its own gross limit. On the record it
    # never did -- every one of these ticks had hedging capacity spare.
    stuck = [b for a, b in zip(ticks, ticks[1:])
             if a["heat"] == b["heat"] and a["rtm"] == b["rtm"]
             and abs(float(b["delta"])) > vs.DELTA_HARD_LIMIT]
    if stuck:
        no_room = [r for r in stuck
                   if abs(float(r["rtm"])) >= vs.RTM_GROSS_LIMIT * 0.99]
        print(f"\n  {len(stuck)} ticks sat past the fine line without the "
              "hedge moving")
        print(f"     {len(no_room):>4} with RTM at its {vs.RTM_GROSS_LIMIT:,} "
              "limit   <- the hedge genuinely had nowhere to go")
        print(f"     {len(stuck) - len(no_room):>4} with room to spare        "
              "     <- nothing was hedging")

    run, first, last = frozen_run(ticks)
    if run >= FROZEN_TICK_ALARM:
        cost = float(first["pnl"]) - float(last["pnl"])
        print(f"\n  worst frozen stretch: heat {first['heat']}, {run} ticks "
              f"from tick {first['tick']}")
        print(f"     book unchanged at RTM {float(first['rtm']):,.0f} while "
              f"delta ran to {float(last['delta']):,.0f}")
        print(f"     P&L {float(first['pnl']):,.0f} -> {float(last['pnl']):,.0f} "
              f"({cost:,.0f})")
        print("     -> a frozen book past the line means the strategy is not "
              "running; restart it")

    print(f"\n  detail: {TICKS_CSV}")


# --------------------------------------------------------------------- live
def wait_for_server(session):
    """Sit out an unreachable server instead of exiting. True if it came back.

    vs.api_request raises SystemExit on a ConnectionError, which is right for
    the strategy -- it holds positions and should not keep trading into a
    server it cannot see. The monitor holds nothing and is meant to outlive a
    heat, so it waits instead. SystemExit derives from BaseException, so the
    live loop's `except Exception` never caught it; that is what ended the
    previous run the moment the case host went down between heats.
    """
    print(f"\n  server unreachable; retrying every {RECONNECT_WAIT}s "
          "(Ctrl+C to stop)")
    while True:
        try:
            time.sleep(RECONNECT_WAIT)
            if session.get(f"{vs.API_ENDPOINT}/case", timeout=5).ok:
                print("  server is back; resuming\n")
                return True
        except KeyboardInterrupt:
            return False
        except Exception:
            continue


def frozen_alarm(samples):
    """Shout when the book stops moving while delta is past the fine line.

    The monitor cannot restart the strategy, but it is the only thing
    watching when the strategy dies. On the record that went unnoticed for
    51 ticks and cost 116,697 -- see frozen_run().
    """
    if len(samples) <= FROZEN_TICK_ALARM:
        return 0
    recent = samples[-(FROZEN_TICK_ALARM + 1):]
    if any(book_state(r) != book_state(recent[0]) for r in recent):
        return 0
    if abs(float(recent[-1]["delta"])) <= vs.DELTA_HARD_LIMIT:
        return 0
    run = 1
    for a, b in zip(reversed(samples), reversed(samples[:-1])):
        if book_state(a) != book_state(b):
            break
        run += 1
    return run


def live(quiet):
    session = requests.Session()
    session.headers.update(vs.AUTHORIZATION)
    state = vs.new_vol_state()
    heat_no = next_heat_number()
    samples = []
    last_tick = None
    alarmed = False

    print(f"monitoring {vs.API_ENDPOINT}   heat #{heat_no}   (Ctrl+C to stop)\n")
    while True:
        try:
            tick, status = vs.get_case(session)
            if tick is None:
                time.sleep(1)
                continue

            if last_tick is not None and tick < last_tick and samples:
                row = close_heat(heat_no, samples)
                append_summary(row)
                print(f"\n=== heat {heat_no}: P&L {row['pnl']:,.0f}  "
                      f"fines {row['fines']:,.0f}  net {row['pnl_net']:,.0f}  "
                      f"drawdown {row['max_drawdown']:,.0f}  "
                      f"{row['ticks_over_limit']} ticks over ===\n")
                heat_no += 1
                samples = []
                state = vs.new_vol_state()
                alarmed = False

            if status != "ACTIVE" or tick == last_tick:
                time.sleep(0.3)
                continue
            last_tick = tick

            snap = sample(session, state, tick)
            if snap is None:
                continue
            samples.append(snap)
            append(TICKS_CSV, TICK_COLUMNS, dict(snap, heat=heat_no))

            frozen = frozen_alarm(samples)
            if frozen and not alarmed:
                alarmed = True
                print("\n" + "!" * 68)
                print("  THE BOOK HAS NOT MOVED FOR "
                      f"{frozen} TICKS AND DELTA IS {snap['delta']:,.0f}")
                print(f"  RTM {snap['rtm']:,.0f} of {vs.RTM_GROSS_LIMIT:,}, so "
                      "the hedge has room. Nothing is using it.")
                print(f"  The fine is {FINE_RATE:.2f}/second/unit past "
                      f"{vs.DELTA_HARD_LIMIT:,}. Check vol_strategy.py is alive.")
                print("!" * 68 + "\n")
            elif not frozen:
                alarmed = False

            if not quiet:
                flag = "  <-- OVER" if abs(snap["delta"]) > vs.DELTA_HARD_LIMIT else ""
                print(f"t={tick:>3} wk={snap['week']} "
                      f"vol={snap['forecast']:.3f} mkt={snap['mkt_iv'] or 0:.3f} "
                      f"pnl={snap['pnl']:>9,.0f} fines={snap['fines']:>7,.0f} "
                      f"Δ={snap['delta']:>8,} sig={snap['signals']:>2} "
                      f"room={snap['room']:>4}{flag}")
        except KeyboardInterrupt:
            break
        except SystemExit:
            # vs.api_request gave up on the connection; the monitor does not
            if not wait_for_server(session):
                break
        except Exception as e:
            print(f"  {type(e).__name__}: {e}")
            time.sleep(1)

    if samples:
        append_summary(close_heat(heat_no, samples))
    print()
    review()


if __name__ == "__main__":
    if "--review" in sys.argv:
        review()
    else:
        live("--quiet" in sys.argv)
