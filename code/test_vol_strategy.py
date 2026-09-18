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
    rows = [
        {"ticker": "A", "iv_gap": 0.03, "delta": 0.5, "position": 0},
        {"ticker": "B", "iv_gap": -0.09, "delta": -0.5, "position": 0},
        {"ticker": "C", "iv_gap": 0.06, "delta": 0.4, "position": 0},
        {"ticker": "D", "iv_gap": 0.001, "delta": 0.3, "position": 0},
    ]
    orders = vs.select_trades(rows, threshold=0.02)
    assert [o["ticker"] for o in orders] == ["B", "C", "A"]   # D below threshold
    assert orders[0]["action"] == "BUY"
    print("PASS  select_trades_sorted_by_edge")


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
        assert vs.hedge_delta(None, 4000) is False and not calls    # inside band
        assert vs.hedge_delta(None, -4999) is False and not calls

        vs.hedge_delta(None, 6000)                                  # long -> sell
        assert len(calls) == 1
        assert calls[0]["action"] == "SELL" and calls[0]["quantity"] == 6000

        calls.clear()
        vs.hedge_delta(None, -25000)                                # chunked at 10k
        assert [c["quantity"] for c in calls] == [10000, 10000, 5000]
        assert all(c["action"] == "BUY" for c in calls)
    finally:
        vs.api_request = original
        vs.DRY_RUN = original_dry
    print("PASS  hedge_delta_respects_band_and_chunks")


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
