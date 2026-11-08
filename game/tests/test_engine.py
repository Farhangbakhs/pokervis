import pytest

from game.engine import EngineError, PokerEngine


def table(stacks, blinds=(5, 10)):
    e = PokerEngine(blinds)
    for seat, st in stacks.items():
        e.add_player(seat, f"P{seat}", f"pk{seat}", st)
    return e


def check_down(e):
    """Everyone checks/calls to the end of the current street(s) as long as someone is to act."""
    while e.to_act is not None:
        seat = e.to_act
        e.act(seat, "call" if e.to_call(seat) else "check")


def test_everyone_folds_last_player_wins_no_showdown():
    e = table({0: 1000, 1: 1000, 2: 1000})
    e.start_hand()
    while e.to_act is not None and e.in_hand:
        e.act(e.to_act, "fold")
    r = e.result
    assert r is not None and not r.showdown
    assert sum(r.deltas.values()) == 0
    assert len(r.winners) == 1
    assert r.deltas[e.players[r.winners[0]].pubkey] > 0


def test_split_pot_even_chips():
    e = table({0: 1000, 1: 1000})
    e.start_hand()
    check_down(e)
    e.deal_board("AsKdQh")
    check_down(e)
    e.deal_board("2c")
    check_down(e)
    e.deal_board("3d")
    check_down(e)
    assert e.awaiting_showdown
    # both hold J-T for the same broadway straight -> exact split
    r = e.showdown({0: "JsTs", 1: "JdTd"})
    assert sorted(r.winners) == [0, 1]
    assert r.deltas == {"pk0": 0, "pk1": 0}


def test_three_way_all_in_side_pots():
    # short stack wins main pot, middle wins side pot, big stack loses
    e = table({0: 100, 1: 300, 2: 1000})
    e.start_hand()
    for _ in range(6):
        if e.to_act is None:
            break
        lim = e.raise_limits()
        e.act(e.to_act, "raise", lim[1]) if lim else e.act(e.to_act, "call")
    assert e.board_needed == 3
    e.deal_board("2c7d9h")
    e.deal_board("Jc")
    e.deal_board("3s")
    assert e.awaiting_showdown
    live = e.live_seats()
    assert sorted(live) == [0, 1, 2]
    # seat0 best (set of aces), seat1 second (pair of kings), seat2 worst
    r = e.showdown({0: "AsAd", 1: "KsKd", 2: "4h5d"})
    assert sum(r.deltas.values()) == 0
    # big stack can only lose what the other two matched: seat0 100, seat1 300
    assert r.deltas["pk2"] == -300
    assert r.deltas["pk0"] == 200          # wins main pot 3x100 minus own 100
    assert r.deltas["pk1"] == 100          # wins side pot 2x200 minus own extra 200... net +100


def test_showdown_board_is_best_hand_chops():
    e = table({0: 500, 1: 500})
    e.start_hand()
    check_down(e)
    e.deal_board("AsKsQs")
    check_down(e)
    e.deal_board("Js")
    check_down(e)
    e.deal_board("Ts")        # royal flush on board
    check_down(e)
    r = e.showdown({0: "2c3d", 1: "4h5h"})
    assert sorted(r.winners) == [0, 1]
    assert r.deltas == {"pk0": 0, "pk1": 0}


def test_validation_errors():
    e = table({0: 1000, 1: 1000})
    e.start_hand()
    with pytest.raises(EngineError):
        e.act(99, "fold")                      # not your turn
    with pytest.raises(EngineError):
        e.act(e.to_act, "raise", 1)            # below min raise
    with pytest.raises(EngineError):
        e.deal_board("AsKdQh")                 # betting not finished
    with pytest.raises(EngineError):
        e.add_player(5, "late", "pk5", 100)    # no joining mid-hand


def test_button_rotates_and_hand_ids_increment():
    e = table({0: 1000, 1: 1000, 2: 1000})
    buttons = []
    for _ in range(3):
        e.start_hand()
        buttons.append(e.button)
        while e.in_hand and e.to_act is not None:
            e.act(e.to_act, "fold")
    assert buttons == [0, 1, 2]
    assert e.hand_id == 3


# ---------- settle_now: pay from whatever has been bet so far, no streets needed ----------
def test_settle_now_pays_the_pot_straight_after_the_blinds():
    e = table({0: 1000, 1: 1000})
    e.start_hand()                                        # only the blinds (15) are in; no betting, no board dealt to the engine
    r = e.settle_now("AsKdQh2c9s", {0: "AdAh", 1: "QsQd"})
    assert sum(r.deltas.values()) == 0 and r.showdown
    assert r.winners == [0] and r.deltas["pk0"] == 5 and r.deltas["pk1"] == -5     # button had the 10 big blind in, the other the 5
    assert e.board == ["As", "Kd", "Qh", "2c", "9s"]


def test_settle_now_counts_bets_of_the_current_round_and_folded_chips():
    e = table({0: 1000, 1: 1000, 2: 1000})
    e.start_hand()
    seats_in_order = []
    while len(seats_in_order) < 3 and e.to_act is not None:
        seat = e.to_act
        seats_in_order.append(seat)
        lim = e.raise_limits()
        if len(seats_in_order) == 1 and lim:
            e.act(seat, "raise", 100)
        elif len(seats_in_order) == 2:
            e.act(seat, "fold")
        else:
            e.act(seat, "call")
    live = e.live_seats()
    assert len(live) == 2
    hole = {live[0]: "AdAh", live[1]: "7c2d"}
    r = e.settle_now("KsQh9c4d3s", hole)
    assert sum(r.deltas.values()) == 0
    assert r.winners == [live[0]]
    folder = next(s for s in e.players if s not in live)
    assert r.deltas[e.players[folder].pubkey] < 0          # the folder's chips stayed in the pot


def test_settle_now_builds_side_pots_from_unequal_stacks():
    e = table({0: 100, 1: 300, 2: 1000})
    e.start_hand()
    for _ in range(6):
        if e.to_act is None:
            break
        lim = e.raise_limits()
        e.act(e.to_act, "raise", lim[1]) if lim else e.act(e.to_act, "call")
    r = e.settle_now("2c7d9hJc3s", {0: "AsAd", 1: "KsKd", 2: "4h5d"})
    assert r.deltas == {"pk2": -300, "pk0": 200, "pk1": 100}


def test_settle_now_chops_and_validates():
    e = table({0: 500, 1: 500})
    e.start_hand()
    with pytest.raises(EngineError):
        e.settle_now("AsKdQh2c", {0: "JsTs", 1: "JdTd"})                 # needs 5 board cards
    with pytest.raises(EngineError):
        e.settle_now("AsKdQh2c9s", {0: "JsTs"})                          # needs every live player's hand
    with pytest.raises(EngineError):
        e.settle_now("AsKdQh2c9s", {0: "AsTs", 1: "JdTd"})               # a card used twice
    r = e.settle_now("AsKdQhJc9s", {0: "Ts2c", 1: "Td3c"})              # both play the same straight
    assert sorted(r.winners) == [0, 1] and r.deltas == {"pk0": 0, "pk1": 0}
