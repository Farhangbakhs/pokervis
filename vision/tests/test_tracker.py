from vision.tracker import ZoneTracker


def feed(t, n, **zones):
    for _ in range(n):
        t.update({z: v for z, v in zones.items()})


def test_stable_after_enough_agreeing_frames_only():
    t = ZoneTracker(window=5, need=4)
    feed(t, 3, B1=("As", 0.99))
    assert t.message()["zones"]["B1"]["stable"] is False          # not enough frames yet
    feed(t, 1, B1=("As", 0.99))
    z = t.message()["zones"]["B1"]
    assert z["stable"] and z["code"] == "As"


def test_flip_flopping_never_goes_stable():
    t = ZoneTracker()
    for i in range(10):
        t.update({"B1": ("As" if i % 2 else "Ah", 0.99)})
    assert t.message()["zones"]["B1"]["stable"] is False


def test_low_confidence_not_stable():
    t = ZoneTracker()
    feed(t, 5, B1=("As", 0.70))
    assert t.message()["zones"]["B1"]["stable"] is False


def test_motion_blocks_stability_then_recovers():
    t = ZoneTracker()
    feed(t, 5, B1=("As", 0.99))
    t.set_motion(True)
    assert t.message()["zones"]["B1"]["stable"] is False
    t.set_motion(False)
    assert t.message()["zones"]["B1"]["stable"] is True


def test_one_bad_frame_is_outvoted():
    t = ZoneTracker()
    feed(t, 4, B1=("Kd", 0.99))
    t.update({"B1": ("Kh", 0.99)})
    z = t.message()["zones"]["B1"]
    assert z["code"] == "Kd" and z["stable"]


def test_card_removed_clears_zone():
    t = ZoneTracker()
    feed(t, 5, B1=("As", 0.99))
    for _ in range(4):
        t.update({}, zone_names=["B1"])
    assert t.message()["zones"]["B1"] is None


from vision.tracker import CardTracker  # noqa: E402


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
