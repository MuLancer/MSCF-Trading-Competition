"""
RIT Algorithmic ETF Arbitrage - strategy implementation.

    python3 etf_strategy.py --check     # verify fields and print the arb, no orders
    python3 etf_strategy.py             # trade (honours DRY_RUN below)

RITC is an ETF quoted in USD whose fair value is BULL + BEAR, both quoted in
CAD. USD is itself a tradable instrument priced in CAD, so the comparison is

    RITC_ask * USD_ask   vs   BULL_bid + BEAR_bid

and the mirror. Tender offers arrive privately and have to be priced against
what unwinding them would actually fetch, not accepted on sight.

The structure follows vol_strategy deliberately, because the same handful of
mistakes cost real money there and every one of them applies here too:
acting once per tick, waiting for ACTIVE, resetting between heats, counting
the position limits the way the server counts them, and never letting a
rejected order abort the tick before the risk step runs.
"""

import argparse
import base64
import os
import signal
from time import sleep

import requests

# ---------------------------------------------------------------- connection
# The ETF case runs on its own ports: 16630 for the Windows Client, 16635 for
# DMA. Practice values; the competition uses different ones.
MODE = os.environ.get("RIT_MODE", "client")
PRACTICE_HOST = "flserver.rotman.utoronto.ca"
DMA_PORT = 16635
CLIENT_PORT = 16630

if MODE == "client":
    API_ENDPOINT = "http://localhost:9999/v1"
    AUTHORIZATION = {"X-API-Key": os.environ.get("RIT_API_KEY", "Rotman")}
else:
    USERNAME = os.environ.get("RIT_USER")
    PASSWORD = os.environ.get("RIT_PASS")
    if not USERNAME or not PASSWORD:
        raise SystemExit("DMA mode needs RIT_USER and RIT_PASS in the environment")
    API_ENDPOINT = f"http://{PRACTICE_HOST}:{DMA_PORT}/v1"
    AUTHORIZATION = {"Authorization": "Basic " + base64.b64encode(
        f"{USERNAME}:{PASSWORD}".encode()).decode()}

# ----------------------------------------------------------------- constants
CAD, USD, BULL, BEAR, RITC = "CAD", "USD", "BULL", "BEAR", "RITC"
STOCKS = (BULL, BEAR)

TOTAL_TICKS = 300
FEE_EQUITY = 0.02            # per share, market orders
REBATE_LIMIT = 0.01          # per share, filled limit orders
MAX_ORDER_EQUITY = 10000
MAX_ORDER_FX = 2500000

# The limits and the weight each ticker carries are READ FROM THE SERVER, not
# assumed. The handout says the ETF counts double; the practice server counts
# it at 0.5 and allows 300,000 gross rather than 250,000. Guessing either way
# would size every trade wrong, and the competition server may differ again.
STOCK_LIMIT_NAME = "LIMIT-STOCK"

# One unit of arbitrage is one RITC against one BULL and one BEAR: three
# market orders, so three fees, before any edge is left over.
ARB_LEG_COST = 3 * FEE_EQUITY
# Demand the edge beat the fees by half again, so a trade that is merely
# break-even on paper does not churn the book for nothing.
ARB_MARGIN = 1.5
ARB_QTY = 5000

# A tender is only worth taking if unwinding it clears the round trip. The
# unwind is the expensive half: it crosses the spread on three legs.
TENDER_MARGIN = 1.5

MAX_POSITION_TICKS = 30      # how long an arb position may sit before unwinding
LOOP_SLEEP = 0.2
DRY_RUN = True

shutdown = False


class ApiException(Exception):
    pass


def signal_handler(signum, frame):
    global shutdown
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    shutdown = True


def api_request(session, method, endpoint, params=None):
    while True:
        url = f"{API_ENDPOINT}/{endpoint}"
        resp = (session.get(url, params=params) if method == "GET"
                else session.post(url, params=params))
        if resp.status_code == 401:
            print(f"401 on /{endpoint}: {resp.text[:160]}")
            if endpoint.startswith(("orders", "tenders")):
                print("  -> case is probably not ACTIVE yet; not shutting down")
                return None
            globals()["shutdown"] = True
            return None
        if resp.status_code == 429:
            sleep(float(resp.headers.get("Retry-After", 1)))
            continue
        if resp.ok:
            return resp.json()
        raise ApiException(f"{endpoint}: {resp.text[:200]}")


def get_case(session):
    case = api_request(session, "GET", "case")
    return (None, None) if case is None else (case["tick"], case["status"])


def get_securities(session):
    return api_request(session, "GET", "securities")


def fetch_book(session, ticker, depth=40):
    """The visible ladder for one ticker."""
    return api_request(session, "GET", "securities/book",
                       params={"ticker": ticker, "limit": depth})


def sweep(levels, qty):
    """(average price, shares actually available) for taking `qty` in one go.

    Top of book is a lie for anything large. RITC shows 12,200 at its best
    bid against tenders of 83,000, and the BULL ladder does not hold 83,000
    at any price. Pricing a tender off the touch says take it; pricing it off
    the ladder says it loses a third of a dollar a share.
    """
    taken, spend = 0, 0.0
    for lvl in levels:
        available = lvl["quantity"] - lvl.get("quantity_filled", 0)
        if available <= 0:
            continue
        lot = min(available, qty - taken)
        spend += lot * lvl["price"]
        taken += lot
        if taken >= qty:
            break
    return (spend / taken if taken else 0.0), taken


def sweep_basket(session, qty, side):
    """CAD per share to buy or sell `qty` of BULL+BEAR through the ladder."""
    total, short_by = 0.0, 0
    for ticker in STOCKS:
        ladder = fetch_book(session, ticker)
        levels = ladder["bids"] if side == "sell" else ladder["asks"]
        price, got = sweep(levels, qty)
        total += price
        short_by = max(short_by, qty - got)
    return total, short_by


def book(securities):
    """-> {ticker: {bid, ask, position}} for the tickers this case trades."""
    return {s["ticker"]: s for s in securities}


# --------------------------------------------------------------- the pricing
def basket_value(b, side):
    """What BULL+BEAR fetches ('sell') or costs ('buy'), in CAD."""
    if side == "sell":
        return b[BULL]["bid"] + b[BEAR]["bid"]
    return b[BULL]["ask"] + b[BEAR]["ask"]


def etf_value_cad(b, side):
    """RITC converted to CAD at the rate we could actually transact.

    RITC is quoted in USD and USD is itself an instrument priced in CAD, so
    selling the ETF also means selling the dollars it pays out. Using a mid
    rate here would book an edge the currency leg then gives back.
    """
    if side == "sell":
        return b[RITC]["bid"] * b[USD]["bid"]
    return b[RITC]["ask"] * b[USD]["ask"]


def arb_edges(b):
    """(etf_rich, etf_cheap) in CAD per share, net of the three fees.

    etf_rich  > 0: sell RITC, buy the basket.
    etf_cheap > 0: buy RITC, sell the basket.
    """
    etf_rich = etf_value_cad(b, "sell") - basket_value(b, "buy") - ARB_LEG_COST
    etf_cheap = basket_value(b, "sell") - etf_value_cad(b, "buy") - ARB_LEG_COST
    return etf_rich, etf_cheap


def limit_weights(securities):
    """{ticker: units it consumes} straight from the securities feed."""
    weights = {}
    for s in securities:
        for lim in s.get("limits") or []:
            if lim["name"] == STOCK_LIMIT_NAME:
                weights[s["ticker"]] = lim["units"]
    return weights


def limit_usage(b, weights):
    """(gross, net) computed the way the server computes them."""
    gross = sum(weights.get(t, 0) * abs(b[t]["position"]) for t in weights)
    net = sum(weights.get(t, 0) * b[t]["position"] for t in weights)
    return gross, net


def arb_room(b, weights, caps):
    """Shares of arbitrage that still fit inside the gross limit.

    One unit is one RITC against one BULL and one BEAR, so it consumes the
    sum of those three weights -- whatever the server says they are.
    """
    gross, _ = limit_usage(b, weights)
    per_unit = sum(weights.get(t, 0) for t in (RITC, BULL, BEAR))
    if per_unit <= 0:
        return 0
    return max(0, int((caps["gross"] - gross) / per_unit))


def get_limits(session):
    """{gross, net} for the stock limit, as configured on this server."""
    for lim in api_request(session, "GET", "limits") or []:
        if lim["name"] == STOCK_LIMIT_NAME:
            return {"gross": lim["gross_limit"], "net": lim["net_limit"]}
    raise ApiException(f"no {STOCK_LIMIT_NAME} in /limits")


# ----------------------------------------------------------------- execution
def place(session, ticker, action, qty):
    if qty <= 0:
        return False
    if DRY_RUN:
        print(f"    [DRY] {action:4} {int(qty):>7} {ticker}")
        return True
    try:
        resp = api_request(session, "POST", "orders",
                           params={"ticker": ticker, "type": "MARKET",
                                   "quantity": int(qty), "action": action})
    except ApiException as e:
        print(f"    rejected {action} {int(qty)} {ticker}: {str(e)[-80:]}")
        return False
    return resp is not None


def place_chunked(session, ticker, action, qty, cap):
    remaining = int(abs(qty))
    while remaining > 0:
        lot = min(remaining, cap)
        if not place(session, ticker, action, lot):
            return False
        remaining -= lot
    return True


def trade_arb(session, b, direction, qty):
    """Put on one side of the basket-versus-ETF trade, all three legs."""
    if direction == "sell_etf":
        place_chunked(session, RITC, "SELL", qty, MAX_ORDER_EQUITY)
        for t in STOCKS:
            place_chunked(session, t, "BUY", qty, MAX_ORDER_EQUITY)
    else:
        place_chunked(session, RITC, "BUY", qty, MAX_ORDER_EQUITY)
        for t in STOCKS:
            place_chunked(session, t, "SELL", qty, MAX_ORDER_EQUITY)
    hedge_currency(session, b)


def hedge_currency(session, b):
    """Flatten the USD the ETF leg leaves behind.

    Every RITC share is a USD-denominated asset. Left alone the book is a bet
    on the exchange rate, which is not the trade and is not what the edge was
    measured in.
    """
    exposure = b[RITC]["position"] * b[RITC]["bid"]        # in USD
    held = b[USD]["position"]
    needed = -exposure - held
    if abs(needed) < 1000:
        return False
    action = "BUY" if needed > 0 else "SELL"
    return place_chunked(session, USD, action, abs(needed), MAX_ORDER_FX)


# -------------------------------------------------------------------- tenders
def tender_edge(session, b, tender):
    """(CAD per share after unwinding at real depth, shares we could not place).

    The unwind is priced by walking the ladder for the tender's whole size,
    not at the touch, because these arrive at 80,000 shares against books
    that show ten thousand at the best price. The cheaper of the two exits is
    used: sell the basket, or sell the ETF straight back.
    """
    qty = int(tender["quantity"])
    price_cad = tender["price"] * b[USD]["bid"]       # tenders quote RITC in USD
    buying = tender["action"].upper() == "BUY"

    basket, basket_short = sweep_basket(session, qty, "sell" if buying else "buy")
    ladder = fetch_book(session, RITC)
    etf_usd, etf_got = sweep(ladder["bids"] if buying else ladder["asks"], qty)
    etf = etf_usd * (b[USD]["bid"] if buying else b[USD]["ask"])

    if buying:
        exit_value = max(basket, etf)                 # sell whichever pays more
        shortfall = basket_short if basket >= etf else qty - etf_got
        return exit_value - price_cad - ARB_LEG_COST, shortfall
    entry_cost = min(basket, etf) if etf else basket  # buy whichever is cheaper
    shortfall = basket_short if basket <= etf else qty - etf_got
    return price_cad - entry_cost - ARB_LEG_COST, shortfall


def handle_tenders(session, b, weights, caps):
    tenders = api_request(session, "GET", "tenders")
    if not tenders:
        return
    for t in tenders:
        edge, shortfall = tender_edge(session, b, t)
        qty = int(t["quantity"])
        floor = TENDER_MARGIN * ARB_LEG_COST
        room = arb_room(b, weights, caps)
        if edge < floor or qty > room or shortfall:
            why = ("too big for the limits" if qty > room else
                   f"{shortfall:,} shares of it could not be unwound"
                   if shortfall else f"edge {edge:+.3f} < {floor:.3f}")
            print(f"    tender {t['tender_id']} declined ({qty:,} sh): {why}")
            continue
        print(f"    tender {t['tender_id']} accepted: {edge:+.3f} CAD/sh "
              f"on {qty:,} -> {edge * qty:,.0f} CAD")
        if DRY_RUN:
            continue
        params = None if t.get("is_fixed_bid") else {"price": t["price"]}
        api_request(session, "POST", f"tenders/{t['tender_id']}", params=params)


# ------------------------------------------------------------------ main loop
def unwind(session, b):
    """Close the arb back to flat once the spread has done its work."""
    for ticker in (RITC, BULL, BEAR):
        pos = b[ticker]["position"]
        if pos:
            place_chunked(session, ticker, "SELL" if pos > 0 else "BUY",
                          abs(pos), MAX_ORDER_EQUITY)
    hedge_currency(session, b)


def main():
    with requests.Session() as session:
        session.headers.update(AUTHORIZATION)
        tick, status = get_case(session)
        caps = get_limits(session)
        last_tick, held_since = None, None

        while tick is not None and tick < TOTAL_TICKS and not shutdown:
            try:
                if status != "ACTIVE":
                    print(f"waiting for case to start (tick={tick} {status})")
                    sleep(1)
                    tick, status = get_case(session)
                    continue
                if tick == last_tick:
                    sleep(LOOP_SLEEP)
                    tick, status = get_case(session)
                    continue
                if last_tick is not None and tick < last_tick:
                    print(f"new heat (tick {last_tick} -> {tick}); resetting")
                    held_since = None
                last_tick = tick

                securities = get_securities(session)
                if securities is None:
                    break
                b = book(securities)
                weights = limit_weights(securities)

                handle_tenders(session, b, weights, caps)

                etf_rich, etf_cheap = arb_edges(b)
                floor = ARB_MARGIN * ARB_LEG_COST
                room = arb_room(b, weights, caps)
                qty = min(ARB_QTY, room)

                if etf_rich > floor and qty:
                    print(f"    ETF rich by {etf_rich:.3f} CAD/sh -> "
                          f"sell {qty:,} RITC, buy the basket")
                    trade_arb(session, b, "sell_etf", qty)
                    held_since = held_since or tick
                elif etf_cheap > floor and qty:
                    print(f"    ETF cheap by {etf_cheap:.3f} CAD/sh -> "
                          f"buy {qty:,} RITC, sell the basket")
                    trade_arb(session, b, "buy_etf", qty)
                    held_since = held_since or tick
                elif held_since and tick - held_since > MAX_POSITION_TICKS:
                    print("    spread has closed; unwinding")
                    unwind(session, b)
                    held_since = None

                gross, net = limit_usage(b, weights)
                print(f"tick={tick} rich={etf_rich:+.3f} cheap={etf_cheap:+.3f} "
                      f"gross={gross:,.0f}/{caps['gross']:,} net={net:,.0f} "
                      f"room={room:,}")

                sleep(LOOP_SLEEP)
                tick, status = get_case(session)
            except ApiException as e:
                print(f"API error: {e}")
                sleep(1)

        warn_if_leaving_a_live_book(session)


def warn_if_leaving_a_live_book(session):
    """Stopping mid-heat with an open book leaves it unhedged and unwatched."""
    try:
        tick, status = get_case(session)
        if status != "ACTIVE":
            return
        securities = get_securities(session)
        open_legs = [s for s in securities if s["position"]] if securities else []
        if not open_legs:
            return
        print("\n" + "!" * 68)
        print(f"  STOPPED AT TICK {tick} WITH {len(open_legs)} POSITIONS OPEN.")
        print("  The basket and ETF legs are no longer being kept against each")
        print("  other. Restart, or close out from the RIT Client.")
        print("!" * 68)
    except Exception:
        pass


def check():
    """Print what the server actually returns, before trusting any of it."""
    with requests.Session() as session:
        session.headers.update(AUTHORIZATION)
        tick, status = get_case(session)
        print(f"case tick={tick} status={status}   mode={MODE}  {API_ENDPOINT}\n")

        securities = get_securities(session)
        if not securities:
            print("no securities returned")
            return
        print("fields:", sorted(securities[0].keys()), "\n")
        print(f"{'ticker':8}{'bid':>10}{'ask':>10}{'position':>10}")
        for s in securities:
            print(f"{s['ticker']:8}{s['bid']:>10.3f}{s['ask']:>10.3f}"
                  f"{s['position']:>10,.0f}")

        missing = [t for t in (CAD, USD, BULL, BEAR, RITC)
                   if t not in {s["ticker"] for s in securities}]
        print("\nmissing tickers:", missing or "none")

        b = book(securities)
        weights = limit_weights(securities)
        caps = get_limits(session)
        rich, cheap = arb_edges(b)
        gross, net = limit_usage(b, weights)
        print("\nlimit weights the server applies:", weights)
        print(f"caps: gross {caps['gross']:,}  net {caps['net']:,}")
        print(f"\nbasket buy  {basket_value(b, 'buy'):.3f} CAD   "
              f"sell {basket_value(b, 'sell'):.3f} CAD")
        print(f"ETF    buy  {etf_value_cad(b, 'buy'):.3f} CAD   "
              f"sell {etf_value_cad(b, 'sell'):.3f} CAD")
        print(f"\nedge after {ARB_LEG_COST:.2f} of fees:  "
              f"ETF rich {rich:+.4f}   ETF cheap {cheap:+.4f}")
        print(f"threshold {ARB_MARGIN * ARB_LEG_COST:.3f} -> "
              f"{'TRADE' if max(rich, cheap) > ARB_MARGIN * ARB_LEG_COST else 'no trade'}")
        print(f"\nusage: gross {gross:,.0f}/{caps['gross']:,}  net {net:,.0f}  "
              f"arb room {arb_room(b, weights, caps):,} shares")
        print("tenders:", api_request(session, "GET", "tenders"))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()
    if args.check:
        check()
    else:
        signal.signal(signal.SIGINT, signal_handler)
        main()
