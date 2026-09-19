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
from py_vollib.black_scholes.greeks.analytical import vega as bs_vega
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
# The option book's own delta must stay inside what the underlying leg can
# offset, with room to spare for gamma near expiry. Past this the hedge runs
# out of position limit and the delta cannot be brought back at all.
MAX_OPTION_DELTA = 0.4 * RTM_GROSS_LIMIT
# Over the final stretch the allowance above is wound down to zero, because
# gamma makes the book unhedgeable exactly when the hedge has least room.
WINDDOWN_TICKS = 90
IV_GAP_THRESHOLD = 0.02      # min |market_iv - forecast| to trade; tune this
MAX_NEW_TRADES_PER_TICK = 4
# Required gross edge per contract, counted in round-trip commissions. Vega
# decays with sqrt(time left) while the commission does not, so this is what
# stops the last stretch of the heat from trading at a loss.
MIN_EDGE_MULTIPLE = 1.5
# A mispricing only pays once the market maker walks toward the forecast, and
# that takes it roughly fifty ticks. A position opened with less time than
# that left may never be repriced before expiry, so it rides on realised
# volatility alone. 0 disables the screen.
MIN_TICKS_TO_WORK = 0

WEEK_TICKS = 75              # 4 weeks of 75 ticks; vol shifts at each boundary
# How many weeks the gross budget takes to be fully released. 4 spreads it a
# quarter at a time; 1 hands over the whole limit at the open.
BUDGET_WEEKS = 4
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
    # `seeded` stays False until a real announcement has been read. Trading
    # before that prices every option off a guessed level.
    return {"current_vol": initial_vol, "next_range": None,
            "risk_free": RISK_FREE, "last_news_id": 0, "seeded": False}


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
        state["seeded"] = True
        if scope == "this":
            state["current_vol"] = value if isinstance(value, float) else sum(value) / 2
            # This confirmation is the week the previous forecast was about, so
            # that forecast is spent. Keeping it would blend a level that has
            # already arrived into the weeks still ahead.
            state["next_range"] = None
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
    """One row per option leg, whether or not its quote inverts.

    Greeks come from our own forecast volatility, which always evaluates, so
    every leg is represented. Only the mispricing fields need the inversion,
    and a leg that will not invert carries iv_gap None: it is untradable but
    still counts toward delta and the position limits.

    Dropping those legs was what broke a live heat. At expiry two deep
    in-the-money calls stopped inverting and took about 49,000 of delta out of
    the total with them, so the book was hedged against a number of the wrong
    sign, the underlying leg ran to its 50,000 limit, and the delta escaped to
    -66,000 for the last forty ticks.
    """
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
        rows.append({
            "ticker": s["ticker"],
            "right": right,
            "strike": K,
            "position": s["position"],
            "mid": mid,
            "market_iv": market_iv,
            "iv_gap": None if market_iv is None else market_iv - current_vol,
            "delta": bs_delta(right, S, K, T, risk_free, current_vol),
            "vega": bs_vega(right, S, K, T, risk_free, current_vol),
        })
    return rows


def expected_edge(row):
    """Gross dollars per contract from the mispricing, before commission.

    py_vollib reports vega per share per volatility point, so a contract is
    100x that and iv_gap has to be read in points rather than absolute units.
    """
    return abs(row["iv_gap"]) * row["vega"] * 10000


def select_trades(rows, threshold=IV_GAP_THRESHOLD, tick=None):
    """iv_gap > 0 means the option is rich -> SELL. Sorted by dollar edge.

    A volatility gap is not worth the same everywhere. Vega falls with the
    square root of time left while the $2 commission does not, so by the last
    fifty ticks the standard two-point gap grosses less than the round trip
    costs: $2.10 against $4.00 at tick 290. Screening on dollars instead of
    vol points tightens the bar automatically as expiry approaches, and ranks
    by what each trade actually earns rather than by how mispriced it looks.
    """
    if tick is not None and TOTAL_TICKS - tick < MIN_TICKS_TO_WORK:
        return []
    floor = MIN_EDGE_MULTIPLE * 2 * FEE_OPT
    picks = [r for r in rows
             if r["iv_gap"] is not None
             and abs(r["iv_gap"]) > threshold and expected_edge(r) > floor]
    picks.sort(key=expected_edge, reverse=True)
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


def option_legs(securities):
    """Every option leg, whether or not its implied volatility inverted.

    The signal table drops legs whose quote will not invert -- deep in- or
    out-of-the-money ones, which near expiry is most of them. Counting the
    position limits off that table hides those legs' positions, so the book
    looks smaller than it is and the server rejects the next order.
    """
    return [s for s in securities if parse_option_ticker(s["ticker"])]


def week_of(tick):
    """1..4. Volatility shifts at the start of each week."""
    return min(4, tick // WEEK_TICKS + 1)


def blended_vol(state, tick):
    """Expected average volatility over the option's remaining life.

    Black-Scholes wants the average volatility from now to expiry, not just
    this week's level. Every option here expires at tick 300, so once the
    mid-week announcement gives next week's range the back half of that life
    should be priced off it instead of off a level that is about to change.
    Variance is additive in time, so the two stretches are weighted by ticks.

    This is where the mid-week forecast earns its keep: it lands around 38
    ticks before the shift, and until the next confirmation arrives the market
    maker is still quoting the old level.
    """
    remaining = max(TOTAL_TICKS - tick, 1)
    this_week = state["current_vol"]
    rng = state.get("next_range")
    if not rng:
        return this_week

    boundary = min(week_of(tick) * WEEK_TICKS, TOTAL_TICKS)
    ticks_before = max(0, min(boundary - tick, remaining))
    ticks_after = remaining - ticks_before
    if ticks_after <= 0:
        return this_week

    next_vol = sum(rng) / 2
    var = (ticks_before * this_week ** 2 + ticks_after * next_vol ** 2) / remaining
    return var ** 0.5


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
    cap = (OPT_GROSS_LIMIT if tick is None else
           OPT_GROSS_LIMIT * min(week_of(tick), BUDGET_WEEKS) // BUDGET_WEEKS)
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


def option_delta_budget(tick):
    """How much delta the option book may carry at this point in the heat.

    Gamma rises as expiry nears: the same book's delta swings further on the
    same move, and the underlying leg cannot follow because it has its own
    limit. A live heat held its size to the end, stopped trading at tick 267
    with signals gone, and still watched delta drift to 33,555 on gamma alone
    while the hedge sat pinned at -50,000. Shrinking the allowance over the
    last stretch means the book is already small when it becomes hardest to
    hold, and the options settle at intrinsic anyway.
    """
    remaining = max(TOTAL_TICKS - tick, 0)
    if remaining >= WINDDOWN_TICKS:
        return MAX_OPTION_DELTA
    return MAX_OPTION_DELTA * remaining / WINDDOWN_TICKS


def reduce_option_delta(session, rows, opt_delta, tick, max_orders=4):
    """Trim the book back inside its delta budget, largest offender first.

    Cutting one 100-lot a tick could not keep up: that is about 5,000 of
    delta against a 33,000 gap, so the breach simply persisted and the fine
    ran the whole time. Several legs may be trimmed in one tick.
    """
    budget = option_delta_budget(tick)
    if abs(opt_delta) <= budget:
        return opt_delta

    book = {r["ticker"]: r["position"] for r in rows}
    for _ in range(max_orders):
        if abs(opt_delta) <= budget:
            break
        offenders = [r for r in rows
                     if book[r["ticker"]]
                     and (r["delta"] * book[r["ticker"]]) * opt_delta > 0]
        if not offenders:
            break
        worst = max(offenders,
                    key=lambda r: abs(r["delta"] * book[r["ticker"]]))
        held = book[worst["ticker"]]
        per_contract = abs(worst["delta"]) * CONTRACT_SIZE
        qty = min(abs(held), OPT_MAX_ORDER,
                  max(1, int((abs(opt_delta) - budget) / max(per_contract, 1e-9))))
        action = "SELL" if held > 0 else "BUY"
        print(f"    book delta {opt_delta:,.0f} over budget {budget:,.0f}; "
              f"cutting {qty} {worst['ticker']}")
        if not place_order(session, worst["ticker"], action, qty):
            break
        book[worst["ticker"]] = held - qty if held > 0 else held + qty
        opt_delta -= (qty if held > 0 else -qty) * worst["delta"] * CONTRACT_SIZE
    return opt_delta


def hedgeable_qty(order, option_delta, tick):
    """Largest size that keeps the option book inside the hedge's reach.

    2,500 contracts can carry 250,000 of delta while the underlying leg stops
    at 50,000 shares, so a book built purely to the contract limit can grow a
    delta no hedge can flatten. Each tick looked fine -- the net was trimmed
    back every time -- but the underlying accumulated in one direction until
    it hit its limit, and then the delta ran free.
    """
    per_contract = order_delta(order, 1)
    if per_contract == 0:
        return OPT_MAX_ORDER
    cap = option_delta_budget(tick)
    edge = cap if per_contract > 0 else -cap
    return max(0, int((edge - option_delta) / per_contract))


def net_room_for(action, net_pos, tick=None):
    """Contracts still addable in this direction under the net contract limit.

    The limit is on the signed net, so direction matters: short 900 contracts
    leaves 100 to sell but 1900 to buy. Using abs() for both sides both blocks
    trades that would reduce the position and, worse, lets a second order in
    the same tick reuse headroom the first already spent.

    The cap is the week's gross budget or the hard net limit, whichever binds
    first: a net position cannot exceed the gross one, and past that the
    case's own limit governs. Rationing net per week as well as gross was
    double counting. It left 60% of the gross budget unusable -- a live heat
    sat at the net cap for forty-five ticks with ten signals showing and 750
    contracts of gross room it could not touch.
    """
    if tick is None:
        cap = OPT_NET_LIMIT
    else:
        cap = min(OPT_NET_LIMIT,
                  OPT_GROSS_LIMIT * min(week_of(tick), BUDGET_WEEKS) // BUDGET_WEEKS)
    return cap - net_pos if action == "BUY" else cap + net_pos


def emergency_unwind(session, rows, net_delta):
    """Shrink the option leg carrying the most offending delta.

    This is the stop-loss that matters here. A mark-to-market stop would cut
    positions that are usually right but early, since the edge only pays as
    the market maker converges. The fine is the loss that is certain: $0.10
    per second per unit over 7,000, so a book sitting at 30,000 burns $2,300
    a second. Once the underlying leg is at its own limit the hedge cannot
    help, and the only way out is to carry fewer options.
    """
    if abs(net_delta) <= DELTA_HARD_LIMIT:
        return False
    # the leg whose delta pushes the same way as the breach, largest first
    offenders = [r for r in rows
                 if r["position"] and
                 (r["delta"] * r["position"]) * net_delta > 0]
    if not offenders:
        return False
    worst = max(offenders, key=lambda r: abs(r["delta"] * r["position"]))
    excess = abs(net_delta) - DELTA_BAND
    per_contract = abs(worst["delta"]) * CONTRACT_SIZE
    qty = min(abs(worst["position"]), OPT_MAX_ORDER,
              max(1, int(excess / max(per_contract, 1e-9))))
    action = "SELL" if worst["position"] > 0 else "BUY"
    print(f"    delta {net_delta:,.0f} past the fine line; "
          f"cutting {qty} {worst['ticker']}")
    return place_order(session, worst["ticker"], action, qty)


def portfolio_delta(securities, rows):
    """Option delta x 100 x position, plus the RTM share position."""
    rtm_pos = next(s["position"] for s in securities if s["ticker"] == UNDERLYING)
    opt_delta = sum(r["delta"] * r["position"] * CONTRACT_SIZE for r in rows)
    return opt_delta + rtm_pos


def hedge_delta(session, net_delta, rtm_pos=0):
    """Trim the book back toward flat, within RTM's own position limit.

    The hedge is a position too. Ignoring its limit means the server rejects
    the order once the underlying leg is full and the delta then goes wholly
    unhedged -- the one state that reliably costs the fine.
    """
    if abs(net_delta) <= DELTA_BAND:
        return False
    action = "SELL" if net_delta > 0 else "BUY"
    headroom = (RTM_GROSS_LIMIT - rtm_pos if action == "BUY"
                else RTM_GROSS_LIMIT + rtm_pos)
    qty = min(int(round(abs(net_delta))), max(0, int(headroom)))
    if qty <= 0:
        print(f"    hedge blocked: RTM at {rtm_pos:,.0f} of {RTM_GROSS_LIMIT:,}; "
              f"delta {net_delta:,.0f} left unhedged")
        return False
    return submit_chunked(session, UNDERLYING, action, qty, RTM_MAX_ORDER)


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

                # A heat restarts news_id at 1, so the cursor carried over from
                # the previous heat filters out every announcement whose id it
                # already passed. That silently ran a whole heat on the last
                # heat's final volatility. A tick going backwards is the signal.
                if last_tick is not None and tick < last_tick:
                    print(f"new heat (tick {last_tick} -> {tick}); resetting state")
                    vol_state = new_vol_state()
                last_tick = tick

                update_vol_state(session, vol_state)

                securities = get_securities(session)
                if securities is None:
                    break

                if not vol_state["seeded"]:
                    print(f"tick={tick} waiting for the opening announcement")
                    sleep(LOOP_SLEEP)
                    tick, status = get_case(session)
                    continue

                forecast = blended_vol(vol_state, tick)
                rows = build_signal_table(securities, forecast,
                                          tick, vol_state["risk_free"])
                orders = select_trades(rows, tick=tick)

                # Limits count every leg, not just the ones that priced.
                legs = option_legs(securities)
                gross_room, _ = option_room(legs, tick)
                # Track delta and both limits as the orders go in: fills will
                # not show up in /securities before the next order is sized or
                # the hedge is placed, all within this same tick.
                net_delta = portfolio_delta(securities, rows)
                net_pos = sum(x["position"] for x in legs)
                opt_delta = sum(r["delta"] * r["position"] * CONTRACT_SIZE
                                for r in rows)
                for o in orders[:MAX_NEW_TRADES_PER_TICK]:
                    room = min(OPT_MAX_ORDER, gross_room,
                               net_room_for(o["action"], net_pos, tick),
                               hedgeable_qty(o, opt_delta, tick))
                    qty = delta_capped_qty(o, net_delta, room)
                    if qty <= 0:
                        continue
                    if place_order(session, o["ticker"], o["action"], qty):
                        impact = order_delta(o, qty)
                        net_delta += impact
                        opt_delta += impact
                        net_pos += qty if o["action"] == "BUY" else -qty
                        gross_room -= qty

                opt_delta = reduce_option_delta(session, rows, opt_delta, tick)
                net_delta = opt_delta + next(x["position"] for x in securities
                                             if x["ticker"] == UNDERLYING)

                rtm_pos = next(x["position"] for x in securities
                               if x["ticker"] == UNDERLYING)
                hedged = hedge_delta(session, net_delta, rtm_pos)
                if not hedged:
                    # Either inside the band, or the underlying leg is full.
                    # Only the second case is dangerous, and it is the one
                    # where the delta stays past the fine line.
                    emergency_unwind(session, rows, net_delta)

                print(f"tick={tick} wk={week_of(tick)} "
                      f"vol={vol_state['current_vol']:.3f}->{forecast:.3f} "
                      f"r={vol_state['risk_free']:.3f} "
                      f"delta={net_delta:,.0f} signals={len(orders)} "
                      f"room={max(gross_room, 0)}")

                sleep(LOOP_SLEEP)
                tick, status = get_case(session)
            except ApiException as e:
                print(f"API error: {e}")
                sleep(1)

        warn_if_leaving_a_live_book(session)


def warn_if_leaving_a_live_book(session):
    """Stopping mid-heat with positions open costs more than anything else.

    Nothing hedges once this process exits, so the delta sits wherever it was
    and the CRO charges $0.10 a second for every unit past 7,000. One heat was
    stopped at tick 148 with the book open: the delta parked between -20,000
    and -35,000 for the remaining 150 ticks and the fine reached $74,309,
    more than the $65,716 the heat had made.

    This only warns. Flattening automatically would be wrong -- Ctrl+C is also
    how you stop between heats to change parameters, and the positions there
    settle by themselves.
    """
    try:
        tick, status = get_case(session)
        if status != "ACTIVE":
            return
        securities = get_securities(session)
        open_legs = [s for s in securities if s["position"]] if securities else []
        if not open_legs:
            return
        print("\n" + "!" * 68)
        print(f"  STOPPED AT TICK {tick} WITH {len(open_legs)} POSITIONS STILL OPEN.")
        print("  Nothing is hedging them now and the delta fine keeps running.")
        print("  Either restart this script, or close out:")
        print("      python3 flatten.py --live")
        print("!" * 68)
    except Exception:
        pass        # never let the warning itself break the shutdown


if __name__ == "__main__":
    signal.signal(signal.SIGINT, signal_handler)
    main()
