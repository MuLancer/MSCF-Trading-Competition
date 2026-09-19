

import argparse
import base64
import os
import signal
from time import sleep

import requests

MODE = os.environ.get("RIT_MODE", "dma")
PRACTICE_HOST = "flserver.rotman.utoronto.ca"
DMA_PORT = 16635
CLIENT_PORT = 16630

if MODE == "client":
    API_ENDPOINT = "http://localhost:9999/v1"
    AUTHORIZATION = {"X-API-Key": os.environ.get("RIT_API_KEY", "Rotman")}
else:
    USERNAME =  "tqdu"
    PASSWORD = "invoice"
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
# Raising this does NOT help, which is worth stating because it is the
# obvious move. Across 620 recorded entries the realised fill came in 0.37
# CAD a share below the edge quoted at the touch, and the relationship runs
# the wrong way: entries on a quoted edge under 0.05 realised -0.079 a share,
# entries over 0.40 realised -0.518. A wide touch edge is the symptom of a
# thin or stale top of book, so screening harder on it selects worse fills.
# The depth walk in arb_edges_depth is what does the real screening; this
# threshold only keeps the obviously pointless trades out.
ARB_MARGIN = 1.5
ARB_QTY = 5000

# A tender is only worth taking if unwinding it clears the round trip. The
# unwind is the expensive half: it crosses the spread on three legs.
TENDER_MARGIN = 1.5

# The converters swap 10,000 RITC for 10,000 BULL + 10,000 BEAR (or back) for
# 1,500 USD, two ticks each. At 0.151 CAD a share that is dearer than the
# 0.06 of fees, so it is never the cheap route -- it is the route that exists
# when the route does not. Tenders arrive at 80,000 shares against a RITC
# book holding 12,200 at the touch; converting spreads the exit across three
# books instead of one. The API cannot press the button, so a person has to.
CONVERT_LOT = 10000
CONVERT_FEE_USD = 1500.0

# Positions are NOT unwound on a timer. The handout is explicit that open
# positions settle at the end "at the correct price if the stock is
# experiencing a divergence", and that participants "are not required to
# close statistical arbitrage positions". Settlement is therefore a free
# exit at full convergence, while unwinding at market costs the spread and
# the fees a second time.
#
# Measured over 76 recorded heats: holding an open spread earned +0.0041 CAD
# per share per tick, steadily, all heat. Getting in cost about 0.30 a share
# in realised slippage. So a position needs roughly 75 ticks to pay for its
# own entry -- and the old 30-tick forced unwind guaranteed it never did.
HOLD_RETURN_PER_TICK = 0.0041
ENTRY_SLIPPAGE = 0.30
MIN_TICKS_TO_EARN_ENTRY = int(ENTRY_SLIPPAGE / HOLD_RETURN_PER_TICK)   # ~73
LOOP_SLEEP = 0.2
DRY_RUN = False                   # set True to see what would happen, no orders

shutdown = False


class ApiException(Exception):
    pass


def signal_handler(signum, frame):
    global shutdown
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    shutdown = True


def human_alert(message):
    """Print only events that require attention during a live run."""
    print("\n" + "!" * 68)
    print("  HUMAN ACTION REQUIRED")
    print(f"  {message}")
    print("!" * 68)



def explain_connection_failure():
    """Say what to do instead of unwinding twenty frames of urllib3.

    Connection refused on localhost:9999 means the Client is not serving,
    which under time pressure is worth one sentence rather than a traceback.
    """
    print("\n" + "!" * 68)
    print(f"  CANNOT REACH {API_ENDPOINT}")
    if "localhost" in API_ENDPOINT:
        print("  Nothing is listening on the RIT Client's port. Either:")
        print("    - the Windows RIT Client is not running or not logged in")
        print("    - or this is a Mac, where the Client does not exist at all")
        print('  On a Mac set MODE = "dma" and export RIT_USER / RIT_PASS.')
    else:
        print("  The server did not answer. Check the host and port, and that")
        print("  the practice server for this case is still up.")
    print("!" * 68)

def api_request(session, method, endpoint, params=None):
    while True:
        url = f"{API_ENDPOINT}/{endpoint}"
        try:
            resp = (session.get(url, params=params) if method == "GET"
                    else session.post(url, params=params))
        except requests.exceptions.ConnectionError:
            explain_connection_failure()
            raise SystemExit(1)
        if resp.status_code == 401:
            human_alert(f"401 on /{endpoint}: {resp.text[:160]}")
            if endpoint.startswith(("orders", "tenders")):
                print("  The case may not be ACTIVE yet; the request was not sent.")
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


def unwind_plan(session, b, qty, buying):
    """Best way out of `qty` RITC, possibly through the converter.

    Returns (CAD per share realised, lots to convert, shares left stranded).
    Exiting entirely through one book is what makes a large tender
    unsellable; routing part of it through the converter uses the BULL and
    BEAR books as well, which between them hold far more than RITC alone.
    """
    ritc = fetch_book(session, RITC)
    ritc_levels = ritc["bids"] if buying else ritc["asks"]
    bull = fetch_book(session, BULL)
    bear = fetch_book(session, BEAR)
    side = "bids" if buying else "asks"
    fx = b[USD]["bid"] if buying else b[USD]["ask"]
    convert_cad = CONVERT_FEE_USD / CONVERT_LOT * b[USD]["ask"]

    best = None
    for lots in range(0, qty // CONVERT_LOT + 2):
        via_convert = min(lots * CONVERT_LOT, qty)
        direct = qty - via_convert

        etf_px, etf_got = sweep(ritc_levels, direct)
        bull_px, bull_got = sweep(bull[side], via_convert)
        bear_px, bear_got = sweep(bear[side], via_convert)
        basket_got = min(bull_got, bear_got)

        placed = etf_got + basket_got
        if not placed:
            continue
        # value is signed: selling realises, buying costs
        etf_value = etf_px * fx * etf_got
        basket_value = (bull_px + bear_px) * basket_got
        cost = convert_cad * basket_got
        total = (etf_value + basket_value - cost if buying
                 else etf_value + basket_value + cost)
        per_share = total / placed
        score = per_share if buying else -per_share
        stranded = qty - placed
        # a plan that strands shares is worse than one that does not
        ranked = (-stranded, score)
        if best is None or ranked > best[0]:
            best = (ranked, per_share, min(lots, basket_got // CONVERT_LOT), stranded)
    if best is None:
        return 0.0, 0, qty
    return best[1], best[2], best[3]


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

def arb_edges_depth(session, b, qty):
    """Price an arbitrage by walking every book for the whole order size."""
    ritc = fetch_book(session, RITC)
    bull = fetch_book(session, BULL)
    bear = fetch_book(session, BEAR)

    ritc_bid, ritc_bid_got = sweep(ritc["bids"], qty)
    ritc_ask, ritc_ask_got = sweep(ritc["asks"], qty)
    bull_bid, bull_bid_got = sweep(bull["bids"], qty)
    bull_ask, bull_ask_got = sweep(bull["asks"], qty)
    bear_bid, bear_bid_got = sweep(bear["bids"], qty)
    bear_ask, bear_ask_got = sweep(bear["asks"], qty)

    rich_ready = min(ritc_bid_got, bull_ask_got, bear_ask_got) >= qty
    cheap_ready = min(ritc_ask_got, bull_bid_got, bear_bid_got) >= qty
    rich = (ritc_bid * b[USD]["bid"] - bull_ask - bear_ask - ARB_LEG_COST
            if rich_ready else float("-inf"))
    cheap = (bull_bid + bear_bid - ritc_ask * b[USD]["ask"] - ARB_LEG_COST
             if cheap_ready else float("-inf"))
    return rich, cheap


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


def arb_room(b, weights, caps, direction=None):
    """Shares of arbitrage that fit inside gross and, when specified, net.

    One unit is one RITC against one BULL and one BEAR, so it consumes the
    sum of those three weights -- whatever the server says they are.
    """
    gross, net = limit_usage(b, weights)
    per_unit = sum(weights.get(t, 0) for t in (RITC, BULL, BEAR))
    if per_unit <= 0:
        return 0
    room = max(0, int((caps["gross"] - gross) / per_unit))
    if direction is None or not caps.get("net"):
        return room

    net_change = (weights.get(RITC, 0) - weights.get(BULL, 0)
                  - weights.get(BEAR, 0)
                  if direction == "buy_etf" else
                  -weights.get(RITC, 0) + weights.get(BULL, 0)
                  + weights.get(BEAR, 0))
    if net_change > 0:
        net_room = int((caps["net"] - net) / net_change)
    else:
        net_room = int((caps["net"] + net) / -net_change)
    return max(0, min(room, net_room))


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
        human_alert(f"Order rejected: {action} {int(qty)} {ticker}: {str(e)[-120:]}")
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


def refresh_book(session):
    securities = get_securities(session)
    return None if securities is None else book(securities)


def trade_arb(session, b, direction, qty):
    """Put on one side of the basket-versus-ETF trade, all three legs."""
    legs_ok = True
    if direction == "sell_etf":
        legs_ok = place_chunked(session, RITC, "SELL", qty, MAX_ORDER_EQUITY)
        for t in STOCKS:
            legs_ok = (place_chunked(session, t, "BUY", qty, MAX_ORDER_EQUITY)
                       and legs_ok)
    else:
        legs_ok = place_chunked(session, RITC, "BUY", qty, MAX_ORDER_EQUITY)
        for t in STOCKS:
            legs_ok = (place_chunked(session, t, "SELL", qty, MAX_ORDER_EQUITY)
                       and legs_ok)

    current = refresh_book(session)
    if current is None:
        human_alert("Could not refresh positions after the arbitrage orders.")
        return False
    if not legs_ok:
        human_alert("Arbitrage legs were incomplete; flattening the remaining position.")
        unwind(session, current)
        return False
    hedge_currency(session, current)
    return True


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

    realised, lots, stranded = unwind_plan(session, b, qty, buying)
    edge = (realised - price_cad if buying else price_cad - realised) - ARB_LEG_COST
    return edge, lots, stranded


def handle_tenders(session, b, weights, caps, attempted_tenders):
    tenders = api_request(session, "GET", "tenders")
    if not tenders:
        return
    for t in tenders:
        tender_id = t["tender_id"]
        if tender_id in attempted_tenders:
            continue
        edge, lots, shortfall = tender_edge(session, b, t)
        qty = int(t["quantity"])
        floor = TENDER_MARGIN * ARB_LEG_COST
        room = arb_room(b, weights, caps)
        if edge < floor or qty > room or shortfall:
            continue
        attempted_tenders.add(tender_id)
        if DRY_RUN:
            if lots:
                verb = "ETF-Redemption" if t["action"].upper() == "BUY" else "ETF-Creation"
                human_alert(f"Press {verb} {lots} times in the Assets tab; "
                            f"{lots * CONVERT_LOT:,} shares require conversion.")
            continue
        params = None if t.get("is_fixed_bid") else {"price": t["price"]}
        try:
            response = api_request(session, "POST", f"tenders/{tender_id}",
                                   params=params)
        except ApiException as e:
            human_alert(f"Tender {tender_id} was not confirmed by the API: {e}")
            continue
        if response is None:
            human_alert(f"Tender {tender_id} was not confirmed by the API.")
            continue
        if lots:
            verb = "ETF-Redemption" if t["action"].upper() == "BUY" else "ETF-Creation"
            human_alert(f"Press {verb} {lots} times in the Assets tab; "
                        f"{lots * CONVERT_LOT:,} shares require conversion.")


# ------------------------------------------------------------------ main loop
def unwind(session, b):
    """Close the arb back to flat once the spread has done its work."""
    for ticker in (RITC, BULL, BEAR):
        pos = b[ticker]["position"]
        if pos:
            place_chunked(session, ticker, "SELL" if pos > 0 else "BUY",
                          abs(pos), MAX_ORDER_EQUITY)
    current = refresh_book(session)
    if current is not None:
        hedge_currency(session, current)


def main():
    with requests.Session() as session:
        session.headers.update(AUTHORIZATION)
        tick, status = get_case(session)
        caps = get_limits(session)
        last_tick = None
        attempted_tenders = set()
        reported_status = None

        while tick is not None and tick < TOTAL_TICKS and not shutdown:
            try:
                if status != "ACTIVE":
                    if status != reported_status:
                        print(f"case status={status}; waiting for ACTIVE")
                        reported_status = status
                    sleep(1)
                    tick, status = get_case(session)
                    continue
                reported_status = status
                if tick == last_tick:
                    sleep(LOOP_SLEEP)
                    tick, status = get_case(session)
                    continue
                if last_tick is not None and tick < last_tick:
                    attempted_tenders.clear()
                last_tick = tick

                securities = get_securities(session)
                if securities is None:
                    break
                b = book(securities)
                weights = limit_weights(securities)

                handle_tenders(session, b, weights, caps, attempted_tenders)

                touch_rich, touch_cheap = arb_edges(b)
                floor = ARB_MARGIN * ARB_LEG_COST
                direction = ("sell_etf" if touch_rich > floor else
                             "buy_etf" if touch_cheap > floor else None)
                room = arb_room(b, weights, caps, direction)
                qty = min(ARB_QTY, room)
                if direction and qty:
                    etf_rich, etf_cheap = arb_edges_depth(session, b, qty)
                else:
                    etf_rich, etf_cheap = touch_rich, touch_cheap

                # A new position must have time left to earn back what it
                # costs to put on. Near the close there is no runway, and the
                # entry slippage is simply a donation.
                runway = TOTAL_TICKS - tick
                if runway < MIN_TICKS_TO_EARN_ENTRY:
                    qty = 0

                if etf_rich > floor and qty:
                    trade_arb(session, b, "sell_etf", qty)
                elif etf_cheap > floor and qty:
                    trade_arb(session, b, "buy_etf", qty)
                # nothing else: an open spread is held to settlement

                sleep(LOOP_SLEEP)
                tick, status = get_case(session)
            except ApiException as e:
                human_alert(f"API error: {e}")
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
