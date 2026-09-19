"""
Deterministic tests for vol_strategy.py -- no RIT Client, no network.

Builds a synthetic market where the market maker prices every option with
Black-Scholes at a KNOWN volatility, then checks that the strategy recovers
that volatility, signs the mispricing correctly, and computes delta/limits right.
"""

from py_vollib.black_scholes import black_scholes as bs

import vol_strategy as vs


# ------------------------------------------------------------ synthetic market
def make_market(S, mm_vol, tick, positions=None):
    """Quotes priced by a market maker at mm_vol, with the case's 2-cent spread."""
    positions = positions or {}
    T = vs.time_to_expiry(tick)
    securities = [{
        "ticker": vs.UNDERLYING, "last": S, "bid": S - 0.01, "ask": S + 0.01,
        "position": positions.get(vs.UNDERLYING, 0),
    }]
    for K in (48, 49, 50, 51, 52):
        for right, suffix in (("c", "C"), ("p", "P")):
            ticker = f"RTM{K}{suffix}"
            fair = bs(right, S, K, T, vs.RISK_FREE, mm_vol)
            securities.append({
                "ticker": ticker, "last": fair,
                "bid": fair - 0.01, "ask": fair + 0.01,
                "position": positions.get(ticker, 0),
            })
    return securities


# ------------------------------------------------------------------- the tests
def test_time_to_expiry():
    assert vs.time_to_expiry(0) == 300 / 3600
    assert abs(vs.time_to_expiry(150) - 150 / 3600) < 1e-12
    assert vs.time_to_expiry(300) == 1 / 3600      # floored, never zero
    assert vs.time_to_expiry(999) == 1 / 3600
    print("PASS  time_to_expiry")


def test_parse_option_ticker():
    assert vs.parse_option_ticker("RTM48C") == ("c", 48.0)
    assert vs.parse_option_ticker("RTM52P") == ("p", 52.0)
    assert vs.parse_option_ticker("RTM") is None
    assert vs.parse_option_ticker("RTM100C") == ("c", 100.0)   # 3-digit strike
    print("PASS  parse_option_ticker")


def test_iv_roundtrip_recovers_market_maker_vol():
    """The core check: price at a known vol, invert, get that vol back."""
    S, mm_vol, tick = 50.0, 0.25, 1
    T = vs.time_to_expiry(tick)
    worst = 0.0
    for K in (48, 49, 50, 51, 52):
        for right in ("c", "p"):
            mid = bs(right, S, K, T, vs.RISK_FREE, mm_vol)
            recovered = vs.compute_market_iv(mid, S, K, T, right)
            assert recovered is not None, f"{right}{K} failed to invert"
            worst = max(worst, abs(recovered - mm_vol))
    assert worst < 1e-6, f"worst IV error {worst}"
    print(f"PASS  iv_roundtrip_recovers_market_maker_vol (max err {worst:.2e})")


def test_compute_market_iv_survives_bad_quote():
    """A sub-intrinsic price must return None, not raise."""
    T = vs.time_to_expiry(1)
    assert vs.compute_market_iv(0.0001, 50.0, 40.0, T, "c") is None
    print("PASS  compute_market_iv_survives_bad_quote")


def test_signal_table_prices_rich_market_as_sell():
    """MM at 25%, our forecast 20% -> every option rich by ~5 vol points."""
    securities = make_market(S=50.0, mm_vol=0.25, tick=1)
    rows = vs.build_signal_table(securities, current_vol=0.20, tick=1)

    assert len(rows) == 10, f"expected 10 options, got {len(rows)}"
    for r in rows:
        assert abs(r["iv_gap"] - 0.05) < 1e-4, f"{r['ticker']} gap {r['iv_gap']}"

    orders = vs.select_trades(rows)
    assert len(orders) == 10
    assert all(o["action"] == "SELL" for o in orders)
    print("PASS  signal_table_prices_rich_market_as_sell")


def test_signal_table_prices_cheap_market_as_buy():
    """MM at 20%, our forecast 25% -> options cheap, we buy."""
    securities = make_market(S=50.0, mm_vol=0.20, tick=1)
    rows = vs.build_signal_table(securities, current_vol=0.25, tick=1)
    for r in rows:
        assert abs(r["iv_gap"] + 0.05) < 1e-4
    orders = vs.select_trades(rows)
    assert all(o["action"] == "BUY" for o in orders)
    print("PASS  signal_table_prices_cheap_market_as_buy")


def test_fairly_priced_market_generates_no_trades():
    securities = make_market(S=50.0, mm_vol=0.22, tick=1)
    rows = vs.build_signal_table(securities, current_vol=0.22, tick=1)
    assert vs.select_trades(rows) == []
    print("PASS  fairly_priced_market_generates_no_trades")


def test_select_trades_sorted_by_edge():
    rows = [   # equal vega, so dollar edge ranks the same as the gap
        {"ticker": "A", "iv_gap": 0.03, "delta": 0.5, "vega": 0.057, "position": 0},
        {"ticker": "B", "iv_gap": -0.09, "delta": -0.5, "vega": 0.057, "position": 0},
        {"ticker": "C", "iv_gap": 0.06, "delta": 0.4, "vega": 0.057, "position": 0},
        {"ticker": "D", "iv_gap": 0.001, "delta": 0.3, "vega": 0.057, "position": 0},
    ]
    orders = vs.select_trades(rows, threshold=0.02)
    assert [o["ticker"] for o in orders] == ["B", "C", "A"]   # D below threshold
    assert orders[0]["action"] == "BUY"
    print("PASS  select_trades_sorted_by_edge")


def test_same_gap_stops_being_tradable_near_expiry():
    """Vega decays, the $2 commission does not."""
    early = vs.build_signal_table(make_market(50.0, 0.25, 1), 0.23, 1)
    late = vs.build_signal_table(make_market(50.0, 0.25, 290), 0.23, 290)

    # identical 2-point mispricing at both ends of the heat
    assert all(abs(r["iv_gap"] - 0.02) < 1e-4 for r in early + late)

    atm_early = next(r for r in early if r["ticker"] == "RTM50C")
    atm_late = next(r for r in late if r["ticker"] == "RTM50C")
    assert vs.expected_edge(atm_early) > 10, vs.expected_edge(atm_early)
    assert vs.expected_edge(atm_late) < 2 * vs.FEE_OPT, vs.expected_edge(atm_late)

    assert vs.select_trades(early), "should trade a 2-point gap at the open"
    assert vs.select_trades(late) == [], "same gap grosses less than the round trip"
    print(f"PASS  same_gap_stops_being_tradable_near_expiry "
          f"(${vs.expected_edge(atm_early):.2f} -> ${vs.expected_edge(atm_late):.2f}/contract)")


def test_atm_deltas_have_expected_sign_and_size():
    securities = make_market(S=50.0, mm_vol=0.25, tick=1)
    rows = vs.build_signal_table(securities, current_vol=0.25, tick=1)
    by_ticker = {r["ticker"]: r["delta"] for r in rows}
    assert 0.45 < by_ticker["RTM50C"] < 0.55
    assert -0.55 < by_ticker["RTM50P"] < -0.45
    assert by_ticker["RTM48C"] > by_ticker["RTM52C"]   # lower strike, higher delta
    print("PASS  atm_deltas_have_expected_sign_and_size")


def test_portfolio_delta_counts_contract_size():
    """Long 10 ATM calls ~= +500 share deltas, then netted against shares."""
    positions = {"RTM50C": 10, vs.UNDERLYING: -300}
    securities = make_market(S=50.0, mm_vol=0.25, tick=1, positions=positions)
    rows = vs.build_signal_table(securities, current_vol=0.25, tick=1)
    total = vs.portfolio_delta(securities, rows)
    call_delta = next(r["delta"] for r in rows if r["ticker"] == "RTM50C")
    assert abs(total - (call_delta * 10 * 100 - 300)) < 1e-9
    assert 190 < total < 260, f"expected roughly +200, got {total}"
    print(f"PASS  portfolio_delta_counts_contract_size (delta {total:.1f})")


def test_option_room():
    rows = [{"position": 400}, {"position": -300}, {"position": 100}]
    gross_room, net_room = vs.option_room(rows)
    assert gross_room == 2500 - 800
    assert net_room == 1000 - 200
    print("PASS  option_room")


def test_limits_count_legs_that_did_not_price():
    """Deep ITM puts do not invert, and dropping them hid their positions."""
    positions = {"RTM52P": -400, "RTM50C": -200}
    securities = make_market(S=50.0, mm_vol=0.25, tick=295, positions=positions)
    # At tick 0 the book is not quoted yet, so an in-the-money leg shows a mid
    # below its intrinsic value and the inversion refuses it.
    for s in securities:
        if s["ticker"] == "RTM52P":
            s["bid"], s["ask"] = 0.0, 0.0

    rows = vs.build_signal_table(securities, current_vol=0.25, tick=295)
    legs = vs.option_legs(securities)

    assert len(legs) == 10
    assert len(rows) < len(legs), "this fixture must actually drop a leg"

    from_rows = sum(abs(r["position"]) for r in rows)
    from_legs = sum(abs(x["position"]) for x in legs)
    assert from_legs == 600
    assert from_rows < from_legs, "the old accounting understated the book"
    print(f"PASS  limits_count_legs_that_did_not_price "
          f"({len(rows)}/10 priced; gross {from_rows} vs {from_legs})")


def test_option_budget_released_per_week():
    """Week one must not be able to spend the whole gross limit."""
    assert [vs.week_of(t) for t in (0, 74, 75, 149, 150, 224, 225, 299)] == \
           [1, 1, 2, 2, 3, 3, 4, 4]

    empty = []
    caps = [vs.option_room(empty, t)[0] for t in (1, 80, 160, 240)]
    assert caps == [625, 1250, 1875, 2500], caps

    # already holding the whole week-one allowance -> no room left that week
    held = [{"position": 625}]
    assert vs.option_room(held, tick=1)[0] == 0
    # ...but the next shift releases more
    assert vs.option_room(held, tick=80)[0] == 625
    print("PASS  option_budget_released_per_week")


def test_state_is_unseeded_until_news_arrives():
    """Never price off the placeholder volatility."""
    state = vs.new_vol_state()
    assert state["seeded"] is False
    assert state["last_news_id"] == 0

    vs.apply_news_to_state(
        [{"news_id": 1, "headline": "", "body": "no numbers here"}], state)
    assert state["seeded"] is False, "unparseable news must not count as seeded"

    vs.apply_news_to_state(
        [{"news_id": 2, "headline": "", "body": "volatility this week will be 19%"}], state)
    assert state["seeded"] is True and state["current_vol"] == 0.19
    print("PASS  state_is_unseeded_until_news_arrives")


def test_stale_cursor_would_swallow_a_new_heat():
    """Why the heat reset exists: news_id restarts at 1 every heat.

    Carrying the previous heat's cursor makes get_new_news ask for ids greater
    than one the new heat has not reached yet, so every announcement up to it
    is filtered out and the whole heat prices off the last heat's final level.
    """
    fresh_heat = [
        {"news_id": i, "headline": "", "body": b}
        for i, b in enumerate(
            ["risk free rate is 0%. realized volatility is 19%",
             "volatility next week will be between 21% and 26%",
             "volatility this week will be 24%"], start=1)
    ]
    carried = vs.new_vol_state()
    carried["last_news_id"] = 6            # left over from the previous heat
    carried["current_vol"] = 0.09          # that heat's final level

    survivors = [n for n in fresh_heat if n["news_id"] > carried["last_news_id"]]
    assert survivors == [], "the stale cursor drops the entire heat"

    reset = vs.new_vol_state()
    kept = [n for n in fresh_heat if n["news_id"] > reset["last_news_id"]]
    assert len(kept) == 3
    vs.apply_news_to_state(kept, reset)
    assert reset["current_vol"] == 0.24 and reset["seeded"]
    print("PASS  stale_cursor_would_swallow_a_new_heat")


def test_forecast_blends_in_next_week():
    """The real heat that motivated this: week 3 at 35%, week 4 forecast 8-13%."""
    state = vs.new_vol_state()
    state["current_vol"] = 0.35

    # before the mid-week announcement there is nothing to blend
    assert vs.blended_vol(state, 192) == 0.35

    state["next_range"] = (0.08, 0.13)
    blended = vs.blended_vol(state, 192)
    # 33 ticks left at 35%, then 75 at ~10.5%, variance-weighted
    assert 0.20 < blended < 0.23, blended
    assert blended < state["current_vol"], "must pull toward the coming level"

    # market was quoting ~0.40; the blend turns a thin edge into a wide one
    assert (0.40 - state["current_vol"]) < 0.06
    assert (0.40 - blended) > 0.17

    # right at expiry there is no 'after' stretch left to blend
    assert vs.blended_vol(state, 299) == 0.35
    print(f"PASS  forecast_blends_in_next_week (0.350 -> {blended:.3f})")


def test_confirmation_clears_the_spent_forecast():
    """A 'this week' announcement supersedes the forecast it confirms."""
    state = vs.new_vol_state()
    vs.apply_news_to_state([
        {"news_id": 6, "headline": "", "body": "volatility of RTM next week will be between 8% and 13%"},
        {"news_id": 7, "headline": "", "body": "volatility of RTM this week will be 9%"},
    ], state)
    assert state["current_vol"] == 0.09
    assert state["next_range"] is None, "stale forecast would blend a level already here"
    assert vs.blended_vol(state, 240) == 0.09
    print("PASS  confirmation_clears_the_spent_forecast")


def test_net_budget_released_per_week():
    assert [vs.net_room_for("SELL", 0, t) for t in (1, 80, 160, 240)] == [250, 500, 750, 1000]
    # already short the week-one allowance: nothing left to sell, room to buy back
    assert vs.net_room_for("SELL", -250, tick=1) == 0
    assert vs.net_room_for("BUY", -250, tick=1) == 500
    print("PASS  net_budget_released_per_week")


def test_order_size_capped_by_delta_impact():
    """A full-size order in a deep-delta option must be cut down."""
    deep_call = {"ticker": "RTM48C", "action": "BUY", "delta": 0.9}

    # flat book: 100 contracts would add 9,000 delta, past the 5,000 band
    qty = vs.delta_capped_qty(deep_call, running_delta=0, max_qty=100)
    assert qty == 33, qty
    assert abs(vs.order_delta(deep_call, qty)) <= vs.MAX_TICK_DELTA

    # already leaning the same way -> much less room
    qty = vs.delta_capped_qty(deep_call, running_delta=2000, max_qty=100)
    assert qty == 11, qty
    assert 2000 + vs.order_delta(deep_call, qty) <= vs.MAX_TICK_DELTA

    # already past the band in that direction -> add nothing
    assert vs.delta_capped_qty(deep_call, running_delta=3500, max_qty=100) == 0

    # an order that pulls delta back toward zero is not restricted
    assert vs.delta_capped_qty(deep_call, running_delta=-6000, max_qty=100) == 100

    # selling flips the sign, so the same option is capped on the short side
    short_call = dict(deep_call, action="SELL")
    qty = vs.delta_capped_qty(short_call, running_delta=0, max_qty=100)
    assert qty == 33 and vs.order_delta(short_call, qty) < 0
    print("PASS  order_size_capped_by_delta_impact")


def test_net_room_is_directional():
    """Short 900 leaves 100 to sell but 1900 to buy."""
    assert vs.net_room_for("SELL", -900) == 100
    assert vs.net_room_for("BUY", -900) == 1900
    assert vs.net_room_for("BUY", 900) == 100
    assert vs.net_room_for("SELL", 900) == 1900
    assert vs.net_room_for("SELL", 0) == vs.OPT_NET_LIMIT
    print("PASS  net_room_is_directional")


def test_emergency_unwind_cuts_the_offending_leg():
    """When the hedge is maxed out, shrink the options instead of paying."""
    calls = []
    original, orig_dry = vs.api_request, vs.DRY_RUN
    vs.api_request = lambda s, m, e, params=None: calls.append(params) or {}
    vs.DRY_RUN = False
    try:
        rows = [
            {"ticker": "RTM48C", "delta": 0.9, "position": 400},   # +36,000
            {"ticker": "RTM52P", "delta": -0.8, "position": 50},   # -4,000
            {"ticker": "RTM50C", "delta": 0.5, "position": 20},    # +1,000
        ]
        # inside the fine threshold -> leave it alone
        assert vs.emergency_unwind(None, rows, 5000) is False and not calls

        vs.emergency_unwind(None, rows, 32000)
        assert len(calls) == 1
        assert calls[0]["ticker"] == "RTM48C", "must cut the biggest offender"
        assert calls[0]["action"] == "SELL", "long leg, positive delta -> sell"

        calls.clear()
        # long puts breaching downward: selling them lifts delta back up
        short_rows = [{"ticker": "RTM48P", "delta": -0.9, "position": 400}]
        vs.emergency_unwind(None, short_rows, -32000)
        assert calls[0]["ticker"] == "RTM48P" and calls[0]["action"] == "SELL"

        calls.clear()
        # nothing pushing the same way -> nothing to cut
        assert vs.emergency_unwind(None, short_rows, +32000) is False
        assert calls == []
    finally:
        vs.api_request, vs.DRY_RUN = original, orig_dry
    print("PASS  emergency_unwind_cuts_the_offending_leg")


def test_hedge_respects_underlying_limit():
    """A full RTM leg must not keep firing orders the server will reject."""
    calls = []
    original, orig_dry = vs.api_request, vs.DRY_RUN
    vs.api_request = lambda s, m, e, params=None: calls.append(params) or {}
    vs.DRY_RUN = False
    try:
        # long 49,000 of a 50,000 limit, needs to buy 5,000 more -> only 1,000 fits
        vs.hedge_delta(None, -5000, rtm_pos=49000)
        assert sum(c["quantity"] for c in calls) == 1000, calls

        calls.clear()
        # completely full in that direction -> no order at all
        assert vs.hedge_delta(None, -5000, rtm_pos=50000) is False
        assert calls == []

        calls.clear()
        # ...but selling is still allowed from a long book
        vs.hedge_delta(None, 5000, rtm_pos=50000)
        assert sum(c["quantity"] for c in calls) == 5000
    finally:
        vs.api_request, vs.DRY_RUN = original, orig_dry
    print("PASS  hedge_respects_underlying_limit")


def test_hedge_delta_respects_band_and_chunks(monkeypatched=None):
    calls = []

    def fake_api_request(session, method, endpoint, params=None):
        calls.append(params)
        return {}

    original = vs.api_request
    original_dry = vs.DRY_RUN
    vs.api_request = fake_api_request
    vs.DRY_RUN = False          # exercise the real order path, not the dry-run stub
    try:
        inside = vs.DELTA_BAND - 1
        assert vs.hedge_delta(None, inside) is False and not calls
        assert vs.hedge_delta(None, -inside) is False and not calls

        over = vs.DELTA_BAND + 1000
        vs.hedge_delta(None, over)                                  # long -> sell
        assert len(calls) == 1
        assert calls[0]["action"] == "SELL" and calls[0]["quantity"] == over

        calls.clear()
        vs.hedge_delta(None, -25000)                                # chunked at 10k
        assert [c["quantity"] for c in calls] == [10000, 10000, 5000]
        assert all(c["action"] == "BUY" for c in calls)
    finally:
        vs.api_request = original
        vs.DRY_RUN = original_dry
    print("PASS  hedge_delta_respects_band_and_chunks")


def test_news_parsing_real_server_text():
    """Verbatim items captured from the live server, headlines included.

    Both traps here were invisible to synthetic fixtures: the opening item
    states the risk-free rate before the volatility, and its headline also
    contains the word "volatility"; the range item spells the range with
    "and", not the hyphen used in the case handout.
    """
    opening = {
        "headline": "Risk free rate and current annualized volatility of RTM",
        "body": "The current risk free rate is 0%. RTM is an ETF that mimics one of "
                "the major indices in the simulated world and its current annualized "
                "realized volatility is 12%. This simulation consists of 20 trading "
                "days that are each 15 ticks in length.",
    }
    forecast = {
        "headline": "News 1",
        "body": "The analysts have informed you that the realized volatility of RTM "
                "next week will be between 31% and 36%",
    }
    weekly = {
        "headline": "Announcement 1",
        "body": "The analysts have informed you that the realized volatility of RTM "
                "this week will be 31%",
    }

    assert vs.parse_vol_from_news(opening) == ("this", 0.12), "grabbed the risk-free rate"
    assert vs.parse_vol_from_news(forecast) == ("next", (0.31, 0.36))
    assert vs.parse_vol_from_news(weekly) == ("this", 0.31)

    # handout also documents the hyphen form
    assert vs.parse_vol_from_news(
        {"headline": "", "body": "realized volatility next week will be between 27-30%"}
    ) == ("next", (0.27, 0.30))
    print("PASS  news_parsing_real_server_text")


def test_risk_free_rate_read_from_news():
    opening = {
        "headline": "Risk free rate and current annualized volatility of RTM",
        "body": "The current risk free rate is 0%. RTM is an ETF that mimics one of "
                "the major indices in the simulated world and its current annualized "
                "realized volatility is 19%. This simulation consists of 20 trading "
                "days that are each 15 ticks in length.",
    }
    assert vs.parse_risk_free_from_news(opening) == 0.0
    # the handout warns the instructor may move it off zero
    assert vs.parse_risk_free_from_news(
        {"headline": "", "body": "The current risk free rate is 2.5%."}) == 0.025
    assert vs.parse_risk_free_from_news(
        {"headline": "", "body": "volatility this week will be 31%"}) is None

    state = vs.new_vol_state()
    vs.apply_news_to_state([dict(opening, news_id=1)], state)
    assert state["risk_free"] == 0.0
    assert state["current_vol"] == 0.19, "rate parsing must not eat the vol"
    print("PASS  risk_free_rate_read_from_news")


def test_nonzero_rate_changes_implied_vol():
    """A rate the script ignored would silently bias every IV."""
    S, K, T, mm_vol, r = 50.0, 50.0, vs.time_to_expiry(1), 0.25, 0.05
    mid = bs("c", S, K, T, r, mm_vol)

    correct = vs.compute_market_iv(mid, S, K, T, "c", risk_free=r)
    assuming_zero = vs.compute_market_iv(mid, S, K, T, "c", risk_free=0.0)

    assert abs(correct - mm_vol) < 1e-6, "correct rate must recover the vol"
    assert abs(assuming_zero - mm_vol) > 1e-4, "a wrong rate must visibly bias IV"
    print(f"PASS  nonzero_rate_changes_implied_vol "
          f"(bias {assuming_zero - mm_vol:+.4f} vol pts if rate ignored)")


def test_news_parsing_and_state():
    assert vs.parse_vol_from_news(
        {"headline": "Vol", "body": "The realized volatility of RTM for this week will be 20%"}
    ) == ("this", 0.20)
    assert vs.parse_vol_from_news(
        {"headline": "", "body": "The realized volatility of RTM for next week will be between 27-30%"}
    ) == ("next", (0.27, 0.30))
    assert vs.parse_vol_from_news({"headline": "Earnings", "body": "Beat expectations."}) is None

    state = vs.new_vol_state()
    vs.apply_news_to_state([
        {"news_id": 2, "headline": "", "body": "volatility for next week will be between 27-30%"},
        {"news_id": 1, "headline": "", "body": "volatility for this week will be 20%"},
    ], state)
    # applied in news_id order, so 'this week' does not get clobbered by the later item
    assert state["current_vol"] == 0.20
    assert state["next_range"] == (0.27, 0.30)
    print("PASS  news_parsing_and_state")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("\nAll deterministic tests passed.")
