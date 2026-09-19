"""
Deterministic tests for etf_strategy.py -- no server needed.

The ladders below were captured from the live practice case during an active
heat, alongside a real tender for 83,000 shares. They are kept verbatim
because the whole point of this case is that the touch price and the price
you actually get are different numbers, and only real depth shows that.
"""

import etf_strategy as es


def ladder(levels):
    return [{"price": p, "quantity": q, "quantity_filled": 0} for p, q in levels]


# captured live, tick ~227
RITC_BIDS = ladder([(24.22, 12200), (24.19, 7910), (24.17, 12000),
                    (24.16, 11700), (24.15, 10200), (24.15, 10100)])
BULL_BIDS = ladder([(10.02, 1818), (10.01, 8700), (10.00, 1951),
                    (10.00, 10700), (9.99, 9300), (9.99, 9500)])
BEAR_BIDS = ladder([(14.89, 9600), (14.84, 11100), (14.81, 8300),
                    (14.80, 8200), (14.79, 11800), (14.77, 4100)])

BOOK = {
    "CAD": {"bid": 1.0, "ask": 1.0, "position": 0},
    "USD": {"bid": 1.017, "ask": 1.018, "position": 0},
    "BULL": {"bid": 10.02, "ask": 10.10, "position": 0},
    "BEAR": {"bid": 14.89, "ask": 14.90, "position": 0},
    "RITC": {"bid": 24.22, "ask": 24.25, "position": 0},
}


def test_sweep_walks_the_ladder():
    price, got = es.sweep(RITC_BIDS, 12200)          # exactly the top level
    assert got == 12200 and abs(price - 24.22) < 1e-9

    price, got = es.sweep(RITC_BIDS, 20110)          # into the second level
    assert got == 20110
    expect = (12200 * 24.22 + 7910 * 24.19) / 20110
    assert abs(price - expect) < 1e-9
    assert price < 24.22, "a bigger order must fill worse than the touch"
    print(f"PASS  sweep_walks_the_ladder (12,200 at 24.220, 20,110 at {price:.4f})")


def test_sweep_reports_what_it_could_not_fill():
    """The ladder holds 64,110; a tender of 83,000 does not fit."""
    price, got = es.sweep(RITC_BIDS, 83000)
    assert got == 64110, got
    assert price < 24.18
    print(f"PASS  sweep_reports_what_it_could_not_fill "
          f"({got:,} of 83,000 at {price:.4f})")


def test_touch_and_depth_disagree_on_a_real_tender():
    """The 83,000-share tender at 24.49: cheap at the touch, dear on the book."""
    price_cad = 24.49 * BOOK["USD"]["bid"]

    at_touch = (BOOK["BULL"]["bid"] + BOOK["BEAR"]["bid"]) - price_cad - es.ARB_LEG_COST

    swept_bull, bull_got = es.sweep(BULL_BIDS, 83000)
    swept_bear, bear_got = es.sweep(BEAR_BIDS, 83000)
    on_depth = swept_bull + swept_bear - price_cad - es.ARB_LEG_COST

    assert at_touch > on_depth, "depth must be the less flattering number"
    assert bull_got < 83000 and bear_got < 83000, "neither book holds the size"
    print(f"PASS  touch_and_depth_disagree_on_a_real_tender "
          f"(touch {at_touch:+.4f}/sh vs depth {on_depth:+.4f}/sh; "
          f"BULL offers {bull_got:,} of 83,000)")


def test_limit_weights_come_from_the_server():
    """The handout says the ETF counts double; the server says half."""
    securities = [
        {"ticker": "BULL", "limits": [{"name": "LIMIT-STOCK", "units": 1.0}]},
        {"ticker": "BEAR", "limits": [{"name": "LIMIT-STOCK", "units": 1.0}]},
        {"ticker": "RITC", "limits": [{"name": "LIMIT-STOCK", "units": 0.5}]},
        {"ticker": "USD", "limits": [{"name": "LIMIT-CASH", "units": 1.0}]},
    ]
    weights = es.limit_weights(securities)
    assert weights == {"BULL": 1.0, "BEAR": 1.0, "RITC": 0.5}
    assert "USD" not in weights, "cash sits under a different limit"

    book = {"BULL": {"position": 10000}, "BEAR": {"position": 10000},
            "RITC": {"position": -10000}}
    gross, net = es.limit_usage(book, weights)
    assert gross == 10000 + 10000 + 0.5 * 10000
    assert net == 10000 + 10000 - 0.5 * 10000
    print(f"PASS  limit_weights_come_from_the_server (gross {gross:,.0f})")


def test_arb_room_uses_the_real_weights():
    caps = {"gross": 300000, "net": 200000}
    weights = {"BULL": 1.0, "BEAR": 1.0, "RITC": 0.5}
    flat = {"BULL": {"position": 0}, "BEAR": {"position": 0}, "RITC": {"position": 0}}
    # one unit of arb costs 1 + 1 + 0.5 = 2.5 of gross
    assert es.arb_room(flat, weights, caps) == 120000

    # the handout's assumed weight of 2 would have said 50,000
    wrong = {"BULL": 1.0, "BEAR": 1.0, "RITC": 2.0}
    assert es.arb_room(flat, wrong, caps) == 75000
    print("PASS  arb_room_uses_the_real_weights (120,000 vs 75,000 if guessed)")


def test_edges_are_net_of_fees_and_use_executable_prices():
    rich, cheap = es.arb_edges(BOOK)
    # selling the ETF pays bid * USD bid; buying the basket costs both asks
    expect_rich = (24.22 * 1.017) - (10.10 + 14.90) - es.ARB_LEG_COST
    assert abs(rich - expect_rich) < 1e-9
    expect_cheap = (10.02 + 14.89) - (24.25 * 1.018) - es.ARB_LEG_COST
    assert abs(cheap - expect_cheap) < 1e-9

    # this snapshot really was dislocated: the ETF traded 22 cents under the
    # basket, which is the trade the case exists to produce
    assert rich < 0, "cannot be rich and cheap at once"
    assert cheap > es.ARB_MARGIN * es.ARB_LEG_COST, cheap
    print(f"PASS  edges_are_net_of_fees_and_use_executable_prices "
          f"(rich {rich:+.4f}, cheap {cheap:+.4f} -> buy the ETF)")


def test_currency_leg_is_not_optional():
    """Converting at a mid rate books an edge the FX leg then gives back."""
    wide = dict(BOOK, USD={"bid": 1.00, "ask": 1.04, "position": 0})
    rich, cheap = es.arb_edges(wide)
    mid = 1.02
    naive_cheap = (10.02 + 14.89) - (24.25 * mid) - es.ARB_LEG_COST
    assert naive_cheap > cheap, "the executable rate must be the worse one"
    print(f"PASS  currency_leg_is_not_optional "
          f"(mid says {naive_cheap:+.4f}, executable says {cheap:+.4f})")


def test_converter_makes_a_large_tender_executable():
    """Without the converter an 83,000 tender cannot be unwound at all."""
    books = {"RITC": {"bids": RITC_BIDS, "asks": []},
             "BULL": {"bids": BULL_BIDS, "asks": []},
             "BEAR": {"bids": BEAR_BIDS, "asks": []}}
    original = es.fetch_book
    es.fetch_book = lambda s, t, depth=40: books[t]
    try:
        # RITC alone holds 64,110 of the 83,000
        _, direct_got = es.sweep(RITC_BIDS, 83000)
        assert direct_got == 64110

        price, lots, stranded = es.unwind_plan(None, BOOK, 83000, buying=True)
        assert stranded == 0, "routing through the converter must clear it"
        assert lots > 0, "and it must actually use the converter"

        # the real tender was priced badly and is still refused
        bad = {"tender_id": 1, "quantity": 83000, "price": 24.49,
               "action": "BUY", "is_fixed_bid": True}
        edge, _, _ = es.tender_edge(None, BOOK, bad)
        assert edge < 0, edge

        # the same size at a fair price is taken
        good = dict(bad, price=24.00)
        edge_good, lots_good, stranded_good = es.tender_edge(None, BOOK, good)
        assert stranded_good == 0
        assert edge_good > es.TENDER_MARGIN * es.ARB_LEG_COST, edge_good
        print(f"PASS  converter_makes_a_large_tender_executable "
              f"({lots} lots; 24.49 gives {edge:+.3f}, 24.00 gives {edge_good:+.3f})")
    finally:
        es.fetch_book = original


def test_positions_are_held_to_settlement():
    """There is no timer unwind: settlement is a free exit at fair value."""
    assert not hasattr(es, "MAX_POSITION_TICKS"), "the timer unwind is gone"

    # a position needs enough ticks left to earn back what entry costs
    assert es.MIN_TICKS_TO_EARN_ENTRY == int(es.ENTRY_SLIPPAGE
                                             / es.HOLD_RETURN_PER_TICK)
    assert 60 < es.MIN_TICKS_TO_EARN_ENTRY < 90, es.MIN_TICKS_TO_EARN_ENTRY

    # entering at tick 150 pays for itself twice over; at 250 it does not
    earns_from = lambda t: (es.TOTAL_TICKS - t) * es.HOLD_RETURN_PER_TICK
    assert earns_from(150) > es.ENTRY_SLIPPAGE
    assert earns_from(250) < es.ENTRY_SLIPPAGE
    print(f"PASS  positions_are_held_to_settlement "
          f"(needs {es.MIN_TICKS_TO_EARN_ENTRY} ticks of runway; "
          f"tick 150 earns {earns_from(150):.2f}/sh, tick 250 only {earns_from(250):.2f})")


def test_passive_prices_never_cross():
    """A crossing order is marketable: it pays the fee and gives back the spread."""
    for ticker in ("RITC", "BULL", "BEAR"):
        bid, ask = BOOK[ticker]["bid"], BOOK[ticker]["ask"]
        buy = es.passive_price(BOOK, ticker, "BUY")
        sell = es.passive_price(BOOK, ticker, "SELL")
        assert bid <= buy < ask, f"{ticker} buy {buy} not inside {bid}/{ask}"
        assert bid < sell <= ask, f"{ticker} sell {sell} not inside {bid}/{ask}"
        assert buy < sell, "the two sides must not cross each other"

    # a one-tick-wide market leaves no room to improve; stay at the touch
    tight = {"X": {"bid": 10.00, "ask": 10.01}}
    assert 10.00 <= es.passive_price(tight, "X", "BUY") <= 10.01
    print("PASS  passive_prices_never_cross")


def test_posting_beats_crossing_by_the_spread_and_the_rebate():
    rebate = 0.03                      # the server pays this, not the 0.01 quoted
    market_rich, market_cheap = es.arb_edges(BOOK)
    passive_rich, passive_cheap = es.passive_edges(BOOK, rebate)

    # three legs swing by fee + rebate each, plus the spread no longer crossed
    assert passive_rich > market_rich
    assert passive_cheap > market_cheap
    gain = passive_cheap - market_cheap
    # the rebate part is kept whole; the spread part is discounted
    assert gain >= 3 * rebate, gain
    full = (es.passive_price(BOOK, "BULL", "SELL")
            + es.passive_price(BOOK, "BEAR", "SELL")
            - es.passive_price(BOOK, "RITC", "BUY") * BOOK["USD"]["ask"]
            + 3 * rebate)
    assert passive_cheap < full, "spread capture must not be assumed certain"
    print(f"PASS  posting_beats_crossing_by_the_spread_and_the_rebate "
          f"(cheap {market_cheap:+.3f} -> {passive_cheap:+.3f}, +{gain:.3f}/sh)")


def test_uneven_fills_are_squared_not_left_alone():
    """A half-filled spread is a directional position, which this case is not."""
    clean = {"RITC": {"position": -5000}, "BULL": {"position": 5000},
             "BEAR": {"position": 5000}}
    assert abs(es.spread_imbalance(clean)) < 1e-9

    # the ETF leg filled but the basket did not
    lopsided = {"RITC": {"position": -5000}, "BULL": {"position": 0},
                "BEAR": {"position": 0}}
    assert es.spread_imbalance(lopsided) == -5000

    sent = []
    original, orig_dry = es.api_request, es.DRY_RUN
    es.api_request = lambda s, m, e, params=None: sent.append(params) or {}
    es.DRY_RUN = False
    try:
        es.flatten_imbalance(None, lopsided)
        assert sent and sent[0]["ticker"] == "RITC"
        assert sent[0]["action"] == "BUY", "short 5,000 ETF must be bought back"
        assert sum(o["quantity"] for o in sent) == 5000

        sent.clear()
        assert es.flatten_imbalance(None, clean) is False
        assert sent == [], "a balanced spread must not be touched"
    finally:
        es.api_request, es.DRY_RUN = original, orig_dry
    print("PASS  uneven_fills_are_squared_not_left_alone")


def test_entries_do_not_stack_without_limit():
    """ARB_QTY sizes one entry; something has to size the book."""
    flat = {"RITC": {"position": 0}}
    assert es.position_room(flat, "sell_etf") == es.MAX_SPREAD_SHARES
    assert es.position_room(flat, "buy_etf") == es.MAX_SPREAD_SHARES

    # short 18,000 ETF: room for 2,000 more of the same, not another 5,000
    deep = {"RITC": {"position": -18000}}
    assert es.position_room(deep, "sell_etf") == 2000
    assert min(es.ARB_QTY, es.position_room(deep, "sell_etf")) == 2000

    full = {"RITC": {"position": -es.MAX_SPREAD_SHARES}}
    assert es.position_room(full, "sell_etf") == 0
    over = {"RITC": {"position": -95000}}      # the worst heat on record
    assert es.position_room(over, "sell_etf") == 0
    print(f"PASS  entries_do_not_stack_without_limit "
          f"(cap {es.MAX_SPREAD_SHARES:,}; 95,000 was reached without one)")


def test_an_opposite_signal_reduces_but_never_flips():
    """Crossing through zero cost 17,707 CAD a time across 120 recorded flips."""
    long_etf = {"RITC": {"position": 30000}}
    # the opposite signal may take the position off, down to flat
    assert es.position_room(long_etf, "sell_etf") == 30000
    # but each entry is still ARB_QTY, so it walks down, never reverses
    assert min(es.ARB_QTY, es.position_room(long_etf, "sell_etf")) == es.ARB_QTY
    # and once flat, the cap -- not the old position -- is what is left
    assert es.position_room({"RITC": {"position": 0}}, "sell_etf") == es.MAX_SPREAD_SHARES

    short_etf = {"RITC": {"position": -30000}}
    assert es.position_room(short_etf, "buy_etf") == 30000
    assert es.position_room(short_etf, "sell_etf") == 0, "already past the cap"
    print("PASS  an_opposite_signal_reduces_but_never_flips")


def test_the_book_is_squared_every_quiet_tick():
    """The old check only ran when a working order retired; market mode never."""
    calls = []
    clean = {"RITC": {"position": -5000, "bid": 24.22},
             "BULL": {"position": 5000}, "BEAR": {"position": 5000},
             "USD": {"position": 5000 * 24.22}}
    # a clean, hedged book costs nothing: no refresh, no orders
    orig_refresh, orig_flat, orig_hedge = (es.refresh_book,
                                           es.flatten_imbalance, es.hedge_currency)
    es.refresh_book = lambda s: calls.append("refresh") or clean
    es.flatten_imbalance = lambda s, b: calls.append("flatten") or False
    es.hedge_currency = lambda s, b: calls.append("hedge") or False
    try:
        es.square_up(None, clean)
        assert calls == [], calls

        # a leg missing: the book is refreshed, squared and re-hedged
        lopsided = dict(clean, BULL={"position": 0}, BEAR={"position": 0})
        es.square_up(None, lopsided)
        assert "flatten" in calls and "hedge" in calls, calls

        # unhedged currency alone is enough to act on
        calls.clear()
        es.square_up(None, dict(clean, USD={"position": 0}))
        assert "hedge" in calls, calls
    finally:
        (es.refresh_book, es.flatten_imbalance,
         es.hedge_currency) = orig_refresh, orig_flat, orig_hedge

    # and the currency test itself is the one hedge_currency acts on
    assert abs(es.currency_gap(clean)) < 1000
    assert es.currency_gap(dict(clean, USD={"position": 0})) > 1000
    print("PASS  the_book_is_squared_every_quiet_tick")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("\nAll ETF tests passed.")
