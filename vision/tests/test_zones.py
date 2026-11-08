from vision.zones import (Detection, ZoneReading, assign, board_cards, default_zones, find_conflicts,
                          seat_hole_cards, seat_of_zone, zone_of)

Z = default_zones(4)


def centre(name):
    xs, ys = zip(*Z[name])
    return (sum(xs) / len(xs), sum(ys) / len(ys))


def test_every_zone_centre_maps_to_itself_and_zones_do_not_overlap():
    for name in Z:
        assert zone_of(centre(name), Z) == name


def test_seat_of_zone():
    assert seat_of_zone("3b") == 3
    assert seat_of_zone("B2") is None
    assert seat_of_zone("MUCK") is None


def test_assign_separates_board_from_each_seat():
    dets = [Detection(centre("B1"), "As", 0.99), Detection(centre("2a"), "Kd", 0.98),
            Detection(centre("2b"), "Kh", 0.97), Detection((5, 5), "2c", 0.9)]   # last is off-zone
    r, w = assign(dets, Z)
    assert set(r) == {"B1", "2a", "2b"} and not w
    assert seat_hole_cards(r, [2]) == {2: ["Kd", "Kh"]}


def test_two_cards_in_one_zone_is_dropped_with_warning():
    c = centre("1a")
    r, w = assign([Detection(c, "As", 0.99), Detection((c[0] + 3, c[1]), "Ks", 0.99)], Z)
    assert "1a" not in r and w


def test_unstable_or_back_cards_block_showdown():
    r = {"1a": ZoneReading("As", 0.99, True), "1b": ZoneReading("Kd", 0.99, False)}
    assert seat_hole_cards(r, [1]) is None
    r["1b"] = ZoneReading("BACK", 0.99, True)
    assert seat_hole_cards(r, [1]) is None


def test_board_cards_in_order_and_min_conf():
    r = {f"B{i}": ZoneReading(c, 0.99, True) for i, c in enumerate(["As", "Kd", "Qh"], 1)}
    assert board_cards(r, 3, 0) == ["As", "Kd", "Qh"]
    r["B4"] = ZoneReading("2c", 0.80, True)
    assert board_cards(r, 1, 3, min_conf=0.9) is None


def test_duplicate_card_is_a_conflict():
    r = {"1a": ZoneReading("As", 0.99, True), "B1": ZoneReading("As", 0.99, True)}
    assert find_conflicts(r)
    assert find_conflicts({"1a": ZoneReading("As", 0.9, True)}, already_used={"As"})
