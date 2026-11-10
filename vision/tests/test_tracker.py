from vision.tracker import CardTracker


def feed_cards(t, n, **areas):
    for _ in range(n):
        t.update(areas)


def test_area_tracker_orders_cards_and_labels_them():
    t = CardTracker()
    t.axis["BOARD"] = 1                                   # board laid out as a vertical column
    feed_cards(t, 5, BOARD=[("8d", .8, (10, 300)), ("5s", .8, (10, 100)), ("6h", .8, (10, 500))],
               S1=[("Kd", .7, (50, 5)), ("Qh", .7, (10, 5))])
    z = t.message()["zones"]
    assert [z[f"B{i}"]["code"] for i in (1, 2, 3)] == ["5s", "8d", "6h"]
    assert z["B1"]["stable"] and z["1a"]["code"] == "Qh" and z["1b"]["code"] == "Kd"


def test_corner_duplicates_collapse_and_phantom_never_stable():
    t = CardTracker()
    for i in range(5):                                    # same card seen as two corner boxes; a phantom flickers
        dets = [("8d", .79, (10, 10)), ("8d", .77, (12, 40))] + ([("4d", .5, (30, 30))] if i == 2 else [])
        t.update({"BOARD": dets})
    z = t.message()["zones"]
    assert list(z) == ["B1"] and z["B1"]["code"] == "8d" and z["B1"]["stable"]


def test_card_removed_disappears_and_too_many_cards_warns():
    t = CardTracker()
    feed_cards(t, 5, BOARD=[("As", .8, (1, 1))])
    for _ in range(5):
        t.update({"BOARD": []})
    assert t.message()["zones"] == {}
    feed_cards(t, 5, S2=[("As", .8, (1, 1)), ("Kd", .8, (2, 1)), ("2c", .8, (3, 1))])
    assert any("Seat 2" in w for w in t.message()["warnings"])


def test_motion_and_low_conf_block_stability():
    t = CardTracker(min_conf=0.6)
    feed_cards(t, 5, BOARD=[("As", .5, (1, 1))])
    assert t.message()["zones"]["B1"]["stable"] is False   # confidence too low
    t2 = CardTracker()
    feed_cards(t2, 5, BOARD=[("As", .9, (1, 1))])
    t2.set_motion(True)
    assert t2.message()["zones"]["B1"]["stable"] is False


def test_card_across_two_areas_counts_once_and_board_wins():
    t = CardTracker()
    feed_cards(t, 4, BOARD=[("4c", .74, (1, 1))], S1=[("4c", .79, (9, 1)), ("3d", .79, (9, 5))])
    m = t.message()
    assert m["zones"]["B1"]["code"] == "4c" and m["zones"]["1a"]["code"] == "3d" and "1b" not in m["zones"]
    assert any("4c" in w for w in m["warnings"])


def test_unstable_reading_does_not_steal_a_card_from_a_stable_area():
    t = CardTracker()
    feed_cards(t, 4, S1=[("4c", .79, (9, 1))])
    t.update({"BOARD": [("4c", .3, (1, 1))]})
    z = t.message()["zones"]
    assert z["1a"]["code"] == "4c" and "B1" not in z


def test_message_carries_the_box_of_each_card_and_forget_clears_an_area():
    t = CardTracker()
    feed_cards(t, 4, S2=[("As", .8, (5, 5), (1, 2, 3, 4))])
    assert t.message()["zones"]["2a"]["box"] == (1, 2, 3, 4)
    t.forget("S2")
    assert t.message()["zones"] == {}


def test_motion_does_not_make_already_stable_cards_vanish_but_blocks_new_ones():
    t = CardTracker()
    feed_cards(t, 4, BOARD=[("As", .8, (1, 1))])
    assert t.message()["zones"]["B1"]["stable"] is True
    t.set_motion(True)                                     # a hand reaches in to put down another card
    feed_cards(t, 4, BOARD=[("As", .8, (1, 1)), ("Kd", .8, (2, 1))])
    z = t.message()["zones"]
    assert z["B1"]["stable"] is True                       # the card that was already there does NOT flicker off
    assert z["B2"]["stable"] is False                      # the new one waits until the hand is out
    t.set_motion(False)
    assert t.message()["zones"]["B2"]["stable"] is True


def test_a_hand_over_one_area_only_holds_back_that_area():
    t = CardTracker()
    t.set_area_motion({"S1"})
    feed_cards(t, 4, BOARD=[("As", .8, (1, 1))], S1=[("Kd", .8, (2, 1))])
    z = t.message()["zones"]
    assert z["B1"]["stable"] is True and z["1a"]["stable"] is False


def test_settled_only_when_every_card_has_its_votes_and_none_went_missing():
    t = CardTracker()
    assert t.settled("BOARD")
    feed_cards(t, 2, BOARD=[("As", .8, (1, 1))])
    assert not t.settled("BOARD")
    feed_cards(t, 1, BOARD=[("As", .8, (1, 1))])
    assert t.settled("BOARD")
    t.update({"BOARD": []})
    assert not t.settled("BOARD")
