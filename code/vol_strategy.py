"""
RIT Volatility Trading Case - strategy implementation (Client REST API).

Connects through the RIT *Client* running locally, NOT the DMA server:
  - auth is the X-API-Key header, not Basic auth with TraderID/password
  - the Client buffers requests, so rate limiting is far less aggressive
Every endpoint, parameter and JSON response is otherwise identical to DMA,
so the case logic is unchanged.

Requires the RIT Client to be running and logged in on this machine.
"""

import base64
import os
import re
import signal
from time import sleep

import requests
from py_vollib.black_scholes.greeks.analytical import delta as bs_delta
from py_vollib.black_scholes.implied_volatility import implied_volatility as bs_iv

# ---------------------------------------------------------------- connection
# "dma"    - talks to the Rotman server directly. Any OS, no Client needed.
#            The browser client and the Mac app also use this port; neither of
#            them opens localhost:9999, so DMA is the only option off Windows.
# "client" - talks to the RIT Client's own API on this machine. Requires the
#            Windows desktop Client installed, running and logged in (it is
#            what serves localhost:9999). Rotman recommends this one.
MODE = "dma"

PRACTICE_HOST = "flserver.rotman.utoronto.ca"
DMA_PORT = 16595              # Volatility case, browser/Mac App port
CLIENT_PORT = 16590           # Volatility case, port the Windows Client logs into

if MODE == "client":
    API_ENDPOINT = "http://localhost:9999/v1"
    AUTHORIZATION = {"X-API-Key": os.environ.get("RIT_API_KEY", "Rotman")}
else:
    # Never hard-code these: this repo is public. Set them in the shell first.
    #   macOS/Linux:  export RIT_USER=xxxx-1 RIT_PASS=yyyy
    #   Windows:      set RIT_USER=xxxx-1    (then set RIT_PASS=yyyy)
    USERNAME = "tqdu-1"
    PASSWORD = "invoice"
    if not USERNAME or not PASSWORD:
        raise SystemExit(
            "DMA mode needs credentials. Set RIT_USER and RIT_PASS in the "
            "environment, or switch MODE to 'client'."
        )
    API_ENDPOINT = f"http://{PRACTICE_HOST}:{DMA_PORT}/v1"
    AUTHORIZATION = {
        "Authorization": "Basic "
        + base64.b64encode(f"{USERNAME}:{PASSWORD}".encode()).decode()
    }

# ----------------------------------------------------------------- constants
CONTRACT_SIZE = 100          # shares per option contract
TOTAL_TICKS = 300            # expiry tick
TICKS_PER_YEAR = 3600        # 240 trading days x 15 ticks/day
RISK_FREE = 0.0              # confirm before the heat; instructor may change it

RTM_GROSS_LIMIT = 50000      # shares
RTM_NET_LIMIT = 50000
RTM_MAX_ORDER = 10000

OPT_GROSS_LIMIT = 2500       # contracts
OPT_NET_LIMIT = 1000
OPT_MAX_ORDER = 100

FEE_RTM = 0.02               # per share
FEE_OPT = 2.00               # per contract

DELTA_HARD_LIMIT = 7000      # CRO fine threshold
# These two must not be equal. Sizing orders up to the same number that
# triggers the hedge pins the book just under the trigger, so the hedge never
# fires and the delta is carried naked all heat. Keep the sizing cap above the
# hedge trigger so a tick that takes a position is then flattened.
MAX_TICK_DELTA = 3000        # most delta one tick's option orders may leave
DELTA_BAND = 2500            # hedge trigger; below this, drift is left alone
IV_GAP_THRESHOLD = 0.02      # min |market_iv - forecast| to trade; tune this
MAX_NEW_TRADES_PER_TICK = 2

WEEK_TICKS = 75              # 4 weeks of 75 ticks; vol shifts at each boundary
UNDERLYING = "RTM"
LOOP_SLEEP = 0.25            # Client buffers for us, so this can be tighter than DMA

DRY_RUN = False

shutdown = False


class ApiException(Exception):
    pass


def signal_handler(signum, frame):
    global shutdown
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    shutdown = True


def handle_rate_limit(response):
    if response.status_code == 429:
        wait = float(response.headers.get("Retry-After", response.json().get("wait", 1)))
        print(f"Rate limited, waiting {wait}s")
        sleep(wait)
        return True
    return False


def handle_auth_failure(response, endpoint=""):
    global shutdown
    if response.status_code == 401:
        print(f"401 on /{endpoint}: {response.text[:200]}")
        if endpoint.startswith("orders"):
            # Orders are refused with 401 while the case is not ACTIVE. That is
            # a timing problem, not a credentials problem, so do not kill the
            # run over it -- the heat may simply not have started yet.
            print("  -> case is probably not ACTIVE yet; not shutting down")
            return True
        print("  -> check credentials (MODE, RIT_USER/RIT_PASS, or the API key)")
        shutdown = True
        return True
    return False


def api_request(session, method, endpoint, params=None):
    while True:
        url = f"{API_ENDPOINT}/{endpoint}"
        if method == "GET":
            resp = session.get(url, params=params)
        elif method == "POST":
            resp = session.post(url, params=params)
        else:
            raise ValueError(f"Unsupported HTTP method: {method}")
        if handle_auth_failure(resp, endpoint):
            return None
        if handle_rate_limit(resp):
            continue
        if resp.ok:
            return resp.json()
        raise ApiException(f"API request failed: {resp.text}")


def get_case(session):
    """-> (tick, status). Status matters: orders are refused until ACTIVE."""
    case = api_request(session, "GET", "case")
    if case is None:
        return None, None
    return case["tick"], case["status"]


def get_tick(session):
    return get_case(session)[0]


def get_securities(session):
    return api_request(session, "GET", "securities")


# ------------------------------------------------- module A: volatility state
# Both patterns anchor on the word "volatility" and stay inside its sentence.
# Without that anchor the opening announcement ("The current risk free rate is
# 0%. ... realized volatility is 18%") matches the risk-free rate and sets the
# forecast to zero. The live server writes ranges as "between 21% and 26%",
# not the "21-26%" form used in the case handout, so accept both.
VOL_RANGE_RE = re.compile(
    r"volatilit\w*[^.]*?between\s+(\d+(?:\.\d+)?)\s*%?\s*(?:and|to|-)\s*(\d+(?:\.\d+)?)\s*%",
    re.I)
VOL_SINGLE_RE = re.compile(
    r"volatilit\w*[^.]*?(?:is|be)\s+(\d+(?:\.\d+)?)\s*%", re.I)

# The opening announcement states the rate ("The current risk free rate is 0%").
# The handout warns the instructor may change it from 0, and every price and
# implied vol depends on it, so read it rather than assuming.
RF_RE = re.compile(
    r"risk[-\s]*free\s+(?:interest\s+)?rate\s+(?:is|of|be)\s+(\d+(?:\.\d+)?)\s*%", re.I)


def new_vol_state(initial_vol=0.20):
    return {"current_vol": initial_vol, "next_range": None,
            "risk_free": RISK_FREE, "last_news_id": 0}


def get_new_news(session, last_news_id):
    news = api_request(session, "GET", "news",
                       params={"after": last_news_id, "limit": 20})
    if not news:
        return [], last_news_id
    return news, max(n["news_id"] for n in news)


def parse_vol_from_news(item):
    """-> ('this', 0.29) | ('next', (0.27, 0.30)) | None. Already divided by 100."""
    headline = item.get("headline") or ""
    body = item.get("body") or ""
    scope = "next" if re.search(r"next\s+week", f"{headline} {body}", re.I) else "this"
    # Search the fields separately. Concatenated, the opening item's headline
    # ("...annualized volatility of RTM") bridges into the body's first
    # sentence ("The current risk free rate is 0%") and yields 0%.
    for text in (body, headline):
        m = VOL_RANGE_RE.search(text)
        if m:
            return scope, (float(m.group(1)) / 100, float(m.group(2)) / 100)
        m = VOL_SINGLE_RE.search(text)
        if m:
            return scope, float(m.group(1)) / 100
    return None


def parse_risk_free_from_news(item):
    """-> 0.02 for 'risk free rate is 2%', else None. Already divided by 100."""
    m = RF_RE.search(f"{item.get('headline') or ''} {item.get('body') or ''}")
    return float(m.group(1)) / 100 if m else None


def apply_news_to_state(items, state):
    """Pure: fold parsed news into state. Separated from I/O so it is testable."""
    for item in sorted(items, key=lambda n: n["news_id"]):
        rf = parse_risk_free_from_news(item)
        if rf is not None:
            state["risk_free"] = rf

        parsed = parse_vol_from_news(item)
        if parsed is None:
            continue
        scope, value = parsed
        if scope == "this":
            state["current_vol"] = value if isinstance(value, float) else sum(value) / 2
        else:
            state["next_range"] = value if isinstance(value, tuple) else (value, value)
    return state


def update_vol_state(session, state):
    items, state["last_news_id"] = get_new_news(session, state["last_news_id"])
    return apply_news_to_state(items, state)


# ------------------------------------------------ module B: pricing & signals
def time_to_expiry(tick):
    """Years remaining. Floors at one tick so Black-Scholes never sees T=0."""
    return max(TOTAL_TICKS - tick, 1) / TICKS_PER_YEAR


def parse_option_ticker(ticker):
    """'RTM48C' -> ('c', 48.0); 'RTM' -> None"""
    if len(ticker) < 5 or ticker[-1].upper() not in ("C", "P"):
        return None
    try:
        return ticker[-1].lower(), float(ticker[3:-1])
    except ValueError:
        return None


def compute_market_iv(mid, S, K, T, right, risk_free=RISK_FREE):
    try:
        return bs_iv(mid, S, K, T, risk_free, right)
    except Exception:
        return None     # deep ITM/OTM quotes may not invert; skip them


def build_signal_table(securities, current_vol, tick, risk_free=RISK_FREE):
    T = time_to_expiry(tick)
    S = next(s["last"] for s in securities if s["ticker"] == UNDERLYING)
    rows = []
    for s in securities:
        parsed = parse_option_ticker(s["ticker"])
        if parsed is None:
            continue
        right, K = parsed
        mid = (s["bid"] + s["ask"]) / 2
        market_iv = compute_market_iv(mid, S, K, T, right, risk_free)
        if market_iv is None:
            continue
        rows.append({
            "ticker": s["ticker"],
            "right": right,
            "strike": K,
            "position": s["position"],
            "mid": mid,
            "market_iv": market_iv,
            "iv_gap": market_iv - current_vol,
            "delta": bs_delta(right, S, K, T, risk_free, current_vol),
        })
    return rows


def select_trades(rows, threshold=IV_GAP_THRESHOLD):
    """iv_gap > 0 means the option is rich -> SELL. Sorted by |iv_gap| desc."""
    picks = [r for r in rows if abs(r["iv_gap"]) > threshold]
    picks.sort(key=lambda r: abs(r["iv_gap"]), reverse=True)
    return [{"ticker": r["ticker"],
             "action": "SELL" if r["iv_gap"] > 0 else "BUY",
             "iv_gap": r["iv_gap"],
             "delta": r["delta"]} for r in picks]


# ------------------------------------------- module C: execution & delta risk
def place_order(session, ticker, action, qty):
    if qty <= 0:
        return False
    if DRY_RUN:
        print(f"    [DRY] {action:4} {int(qty):>6} {ticker}")
        return True
    try:
        resp = api_request(session, "POST", "orders",
                           params={"ticker": ticker, "type": "MARKET",
                                   "quantity": int(qty), "action": action})
    except ApiException as e:
        # A rejected order (risk limits, tick boundary) must not abort the
        # tick: the hedge runs after this and skipping it leaves the delta
        # exposed for a whole tick.
        print(f"    order rejected {action} {int(qty)} {ticker}: {str(e)[-90:]}")
        return False
    return resp is not None


def submit_chunked(session, ticker, action, qty, max_size):
    remaining = int(abs(qty))
    while remaining > 0:
        lot = min(remaining, max_size)
        if not place_order(session, ticker, action, lot):
            return False
        remaining -= lot
    return True


def week_of(tick):
    """1..4. Volatility shifts at the start of each week."""
    return min(4, tick // WEEK_TICKS + 1)


def option_room(rows, tick=None):
    """Remaining contract headroom -> (gross_room, net_room).

    The gross budget is released a quarter per week instead of all at once.
    The largest edges appear right after each volatility shift, and the market
    maker then learns the new level within the week, so signals are strongest
    early in a week and fade. Spending the whole limit on week one leaves
    nothing for the three later shifts, which are the same opportunity again.
    """
    gross = sum(abs(r["position"]) for r in rows)
    net = sum(r["position"] for r in rows)
    cap = OPT_GROSS_LIMIT if tick is None else OPT_GROSS_LIMIT * week_of(tick) // 4
    return cap - gross, OPT_NET_LIMIT - abs(net)


def order_delta(order, qty):
    """Share-delta that filling this order adds to the book."""
    sign = 1 if order["action"] == "BUY" else -1
    return sign * qty * CONTRACT_SIZE * order["delta"]


def delta_capped_qty(order, running_delta, max_qty):
    """Largest size whose delta impact still leaves the book inside the band.

    Sizing on the contract limit alone let one tick add 8,000-13,000 of delta
    against a 7,000 fine threshold: the book blew through the limit and the
    hedge had to undo it immediately, paying the spread in both directions.
    Size the option leg so the hedge is a trim rather than a reversal.
    """
    per_contract = order_delta(order, 1)
    if per_contract == 0:
        return max_qty
    edge = MAX_TICK_DELTA if per_contract > 0 else -MAX_TICK_DELTA
    allowed = (edge - running_delta) / per_contract
    return max(0, min(max_qty, int(allowed)))


def net_room_for(action, net_pos):
    """Contracts still addable in this direction under the net contract limit.

    The limit is on the signed net, so direction matters: short 900 contracts
    leaves 100 to sell but 1900 to buy. Using abs() for both sides both blocks
    trades that would reduce the position and, worse, lets a second order in
    the same tick reuse headroom the first already spent.
    """
    return OPT_NET_LIMIT - net_pos if action == "BUY" else OPT_NET_LIMIT + net_pos


def portfolio_delta(securities, rows):
    """Option delta x 100 x position, plus the RTM share position."""
    rtm_pos = next(s["position"] for s in securities if s["ticker"] == UNDERLYING)
    opt_delta = sum(r["delta"] * r["position"] * CONTRACT_SIZE for r in rows)
    return opt_delta + rtm_pos


def hedge_delta(session, net_delta):
    if abs(net_delta) <= DELTA_BAND:
        return False
    action = "SELL" if net_delta > 0 else "BUY"
    return submit_chunked(session, UNDERLYING, action,
                          int(round(abs(net_delta))), RTM_MAX_ORDER)


# ------------------------------------------------------------------ main loop
def main():
    with requests.Session() as session:
        session.headers.update(AUTHORIZATION)
        vol_state = new_vol_state()

        tick, status = get_case(session)
        last_tick = None
        while tick is not None and tick < TOTAL_TICKS and not shutdown:
            try:
                # Between heats the case still answers GETs but refuses orders,
                # so wait rather than firing signals into a stopped market.
                if status != "ACTIVE":
                    print(f"waiting for case to start (tick={tick} status={status})")
                    sleep(1)
                    tick, status = get_case(session)
                    continue

                # A tick lasts about a second but this loop polls four times a
                # second. Acting on every pass sent the same orders three or
                # four times over and re-hedged a delta that the earlier fills
                # had not yet been reflected in /securities, so the hedge
                # overshot into the opposite sign. Act once per tick only.
                if tick == last_tick:
                    sleep(LOOP_SLEEP)
                    tick, status = get_case(session)
                    continue
                last_tick = tick

                update_vol_state(session, vol_state)

                securities = get_securities(session)
                if securities is None:
                    break

                rows = build_signal_table(securities, vol_state["current_vol"],
                                          tick, vol_state["risk_free"])
                orders = select_trades(rows)

                gross_room, _ = option_room(rows, tick)
                # Track delta and both limits as the orders go in: fills will
                # not show up in /securities before the next order is sized or
                # the hedge is placed, all within this same tick.
                net_delta = portfolio_delta(securities, rows)
                net_pos = sum(r["position"] for r in rows)
                for o in orders[:MAX_NEW_TRADES_PER_TICK]:
                    room = min(OPT_MAX_ORDER, gross_room,
                               net_room_for(o["action"], net_pos))
                    qty = delta_capped_qty(o, net_delta, room)
                    if qty <= 0:
                        continue
                    if place_order(session, o["ticker"], o["action"], qty):
                        net_delta += order_delta(o, qty)
                        net_pos += qty if o["action"] == "BUY" else -qty
                        gross_room -= qty

                hedge_delta(session, net_delta)

                print(f"tick={tick} wk={week_of(tick)} "
                      f"vol={vol_state['current_vol']:.3f} "
                      f"r={vol_state['risk_free']:.3f} "
                      f"delta={net_delta:,.0f} signals={len(orders)} "
                      f"room={max(gross_room, 0)}")

                sleep(LOOP_SLEEP)
                tick, status = get_case(session)
            except ApiException as e:
                print(f"API error: {e}")
                sleep(1)


if __name__ == "__main__":
    signal.signal(signal.SIGINT, signal_handler)
    main()
