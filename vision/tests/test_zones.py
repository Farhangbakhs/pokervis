from vision.zones import ZoneReading, seat_hole_cards, seat_of_zone


def test_seat_of_zone():
    assert seat_of_zone("3b") == 3
    assert seat_of_zone("B2") is None
    assert seat_of_zone("MUCK") is None


def test_hole_cards_need_both_cards_stable():
    r = {"2a": ZoneReading("Kd", 0.98, True), "2b": ZoneReading("Kh", 0.97, True)}
    assert seat_hole_cards(r, [2]) == {2: ["Kd", "Kh"]}


def test_unstable_or_back_cards_block_showdown():
    r = {"1a": ZoneReading("As", 0.99, True), "1b": ZoneReading("Kd", 0.99, False)}
    assert seat_hole_cards(r, [1]) is None
    r["1b"] = ZoneReading("BACK", 0.99, True)
    assert seat_hole_cards(r, [1]) is None
