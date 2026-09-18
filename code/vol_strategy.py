"""
RIT Volatility Trading Case - strategy implementation (Client REST API).

Connects through the RIT *Client* running locally, NOT the DMA server:
  - auth is the X-API-Key header, not Basic auth with TraderID/password
  - the Client buffers requests, so rate limiting is far less aggressive
Every endpoint, parameter and JSON response is otherwise identical to DMA,
so the case logic is unchanged.

Requires the RIT Client to be running and logged in on this machine.
"""

import re
import signal
from time import sleep

import requests
from py_vollib.black_scholes.greeks.analytical import delta as bs_delta
from py_vollib.black_scholes.implied_volatility import implied_volatility as bs_iv

# ---------------------------------------------------------------- connection
API_ENDPOINT = "http://localhost:9999/v1"
AUTHORIZATION = {"X-API-Key": "Rotman"}

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
DELTA_BAND = 5000            # our own hedge trigger; tune this
IV_GAP_THRESHOLD = 0.02      # min |market_iv - forecast| to trade; tune this
MAX_NEW_TRADES_PER_TICK = 2

UNDERLYING = "RTM"
LOOP_SLEEP = 0.25            # Client buffers for us, so this can be tighter than DMA

# Log what would be sent instead of sending it. Run the first heat with this
# on, confirm the signals and hedges look sane, then flip it off.
DRY_RUN = True

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


def handle_auth_failure(response):
    global shutdown
    if response.status_code == 401:
        print("Auth failed. Is the RIT Client running and the API key correct?")
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
        if handle_auth_failure(resp):
            return None
        if handle_rate_limit(resp):
            continue
        if resp.ok:
            return resp.json()
        raise ApiException(f"API request failed: {resp.text}")


def get_tick(session):
    case = api_request(session, "GET", "case")
    return None if case is None else case["tick"]


def get_securities(session):
    return api_request(session, "GET", "securities")


# ------------------------------------------------- module A: volatility state
VOL_RANGE_RE = re.compile(r"between\s+(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)\s*%", re.I)
VOL_SINGLE_RE = re.compile(r"(?:be|is)\s+(\d+(?:\.\d+)?)\s*%", re.I)


def new_vol_state(initial_vol=0.20):
    return {"current_vol": initial_vol, "next_range": None, "last_news_id": 0}


def get_new_news(session, last_news_id):
    news = api_request(session, "GET", "news",
                       params={"after": last_news_id, "limit": 20})
    if not news:
        return [], last_news_id
    return news, max(n["news_id"] for n in news)


def parse_vol_from_news(item):
    """-> ('this', 0.29) | ('next', (0.27, 0.30)) | None. Already divided by 100."""
    text = f"{item.get('headline', '')} {item.get('body', '')}"
    scope = "next" if re.search(r"next\s+week", text, re.I) else "this"
    m = VOL_RANGE_RE.search(text)
    if m:
        return scope, (float(m.group(1)) / 100, float(m.group(2)) / 100)
    m = VOL_SINGLE_RE.search(text)
    if m:
        return scope, float(m.group(1)) / 100
    return None


def apply_news_to_state(items, state):
    """Pure: fold parsed news into state. Separated from I/O so it is testable."""
    for item in sorted(items, key=lambda n: n["news_id"]):
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


def compute_market_iv(mid, S, K, T, right):
    try:
        return bs_iv(mid, S, K, T, RISK_FREE, right)
    except Exception:
        return None     # deep ITM/OTM quotes may not invert; skip them


def build_signal_table(securities, current_vol, tick):
    T = time_to_expiry(tick)
    S = next(s["last"] for s in securities if s["ticker"] == UNDERLYING)
    rows = []
    for s in securities:
        parsed = parse_option_ticker(s["ticker"])
        if parsed is None:
            continue
        right, K = parsed
        mid = (s["bid"] + s["ask"]) / 2
        market_iv = compute_market_iv(mid, S, K, T, right)
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
            "delta": bs_delta(right, S, K, T, RISK_FREE, current_vol),
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
    resp = api_request(session, "POST", "orders",
                       params={"ticker": ticker, "type": "MARKET",
                               "quantity": int(qty), "action": action})
    return resp is not None


def submit_chunked(session, ticker, action, qty, max_size):
    remaining = int(abs(qty))
    while remaining > 0:
        lot = min(remaining, max_size)
        if not place_order(session, ticker, action, lot):
            return False
        remaining -= lot
    return True


def option_room(rows):
    """Remaining contract headroom -> (gross_room, net_room)."""
    gross = sum(abs(r["position"]) for r in rows)
    net = sum(r["position"] for r in rows)
    return OPT_GROSS_LIMIT - gross, OPT_NET_LIMIT - abs(net)


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

        tick = get_tick(session)
        while tick is not None and tick < TOTAL_TICKS and not shutdown:
            try:
                update_vol_state(session, vol_state)

                securities = get_securities(session)
                if securities is None:
                    break

                rows = build_signal_table(securities, vol_state["current_vol"], tick)
                orders = select_trades(rows)

                gross_room, net_room = option_room(rows)
                pending_delta = 0.0
                for o in orders[:MAX_NEW_TRADES_PER_TICK]:
                    qty = min(OPT_MAX_ORDER, gross_room, net_room)
                    if qty <= 0:
                        break
                    if place_order(session, o["ticker"], o["action"], qty):
                        sign = 1 if o["action"] == "BUY" else -1
                        pending_delta += sign * qty * CONTRACT_SIZE * o["delta"]
                        gross_room -= qty

                net_delta = portfolio_delta(securities, rows) + pending_delta
                hedge_delta(session, net_delta)

                print(f"tick={tick} vol={vol_state['current_vol']:.3f} "
                      f"delta={net_delta:,.0f} signals={len(orders)}")

                sleep(LOOP_SLEEP)
                tick = get_tick(session)
            except ApiException as e:
                print(f"API error: {e}")
                sleep(1)


if __name__ == "__main__":
    signal.signal(signal.SIGINT, signal_handler)
    main()
