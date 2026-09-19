"""
Close every open position, fast. The emergency bail-out.

    python3 flatten.py            # show what it would do, send nothing
    python3 flatten.py --live     # actually send the orders

Connection settings come from vol_strategy, so MODE/ports/credentials are
configured in one place.

This is deliberately NOT wired into the strategy's shutdown. At expiry the
options cash-settle at intrinsic and RTM closes at the last price, so
flattening then only pays commission and spread for nothing. And Ctrl+C is
how you stop to change parameters between heats -- losing the book every time
you did that would be worse than the problem it solves.

Use it when something has gone wrong and you want out now, or to clear a
practice book before a fresh run.
"""

import sys

import requests

import vol_strategy as vs


def flatten(session, live):
    securities = vs.get_securities(session)
    if securities is None:
        print("could not read positions")
        return 1

    open_legs = [s for s in securities if s["position"]]
    if not open_legs:
        print("already flat")
        return 0

    print(f"{'ticker':10}{'position':>10}{'action':>8}{'chunks':>8}")
    for s in open_legs:
        qty = abs(int(s["position"]))
        action = "SELL" if s["position"] > 0 else "BUY"
        is_option = vs.parse_option_ticker(s["ticker"]) is not None
        cap = vs.OPT_MAX_ORDER if is_option else vs.RTM_MAX_ORDER
        chunks = -(-qty // cap)          # ceiling division
        print(f"{s['ticker']:10}{s['position']:>10,.0f}{action:>8}{chunks:>8}")

        if live:
            vs.submit_chunked(session, s["ticker"], action, qty, cap)

    if not live:
        print("\ndry run -- nothing sent. Re-run with --live to execute.")
        return 0

    after = vs.get_securities(session)
    leftover = [s for s in after if s["position"]] if after else []
    if leftover:
        print("\nstill open (rejected, or filled after the check):")
        for s in leftover:
            print(f"  {s['ticker']:10}{s['position']:>10,.0f}")
        return 1
    print("\nflat")
    return 0


if __name__ == "__main__":
    live = "--live" in sys.argv
    if live:
        vs.DRY_RUN = False           # vs.place_order honours this flag
    with requests.Session() as session:
        session.headers.update(vs.AUTHORIZATION)
        tick, status = vs.get_case(session)
        print(f"case tick={tick} status={status}  mode={vs.MODE}\n")
        sys.exit(flatten(session, live))
