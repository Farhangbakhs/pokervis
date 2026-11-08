import base64
import os

os.environ["AUTOPOKER_NO_AUTOSTART"] = "1"

import base58  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from nacl.signing import SigningKey  # noqa: E402

from server.app import create_app  # noqa: E402
from server.chain_client import MockChain  # noqa: E402

SECRET, PIN = "s" * 40, "123456"


class Player:
    def __init__(self, name):
        self.name = name
        self.sk = SigningKey.generate()
        self.pub = base58.b58encode(bytes(self.sk.verify_key)).decode()
        self.token = None
        self.seat = None


@pytest.fixture()
def env():
    chain = MockChain()
    app = create_app(chain=chain, session_secret=SECRET, dealer_pin=PIN)
    c = TestClient(app)
    tok = c.post("/api/dealer/login", json={"pin": PIN}).json()["token"]
    return c, app.state.table, chain, {"Authorization": f"Bearer {tok}"}


_deposits = iter(range(1, 10_000))


def fund(c, table, p, chips: int = 1000):
    """The wallet pays the table SOL (mock chain: a signature 'mock-<chips>-<n>' is a verified payment)."""
    r = c.post("/api/sol/confirm", json={"pubkey": p.pub, "signature": f"mock-{chips}-{next(_deposits)}"})
    assert r.status_code == 200, r.text
    return r


def sign_in(c, table, p: Player, seat: int, code: str | None = None, sign_with=None):
    if table.chain.balance(p.pub) == 0:
        fund(c, table, p)
    ch = c.post("/api/auth/challenge", json={"pubkey": p.pub}).json()
    sig = (sign_with or p.sk).sign(ch["message"].encode()).signature
    r = c.post("/api/auth/login", json={
        "pubkey": p.pub, "nonce": ch["nonce"], "signature": base64.b64encode(sig).decode(),
        "seat": seat, "seat_code": code or table.seat_codes[seat], "name": p.name})
    if r.status_code == 200:
        p.token, p.seat = r.json()["token"], seat
    return r


def hdr(p):
    return {"Authorization": f"Bearer {p.token}"}


def zones(**cards):
    return {"ts": 1.0, "zones": {z: {"code": c, "conf": 0.995, "stable": True} for z, c in cards.items()}, "warnings": []}


def check_down(c, table, players):
    by_seat = {p.seat: p for p in players}
    while table.engine.to_act is not None:
        seat = table.engine.to_act
        kind = "call" if table.engine.to_call(seat) else "check"
        assert c.post("/api/action", json={"type": kind}, headers=hdr(by_seat[seat])).status_code == 200


def test_sign_in_security(env):
    c, table, chain, _ = env
    a, b = Player("A"), Player("B")
    nobody = Player("Nobody")                                                   # no SOL paid -> cannot sit, ever
    ch0 = c.post("/api/auth/challenge", json={"pubkey": nobody.pub}).json()
    sig0 = base64.b64encode(nobody.sk.sign(ch0["message"].encode()).signature).decode()
    r0 = c.post("/api/auth/login", json={"pubkey": nobody.pub, "nonce": ch0["nonce"], "signature": sig0, "seat": 1,
                                         "seat_code": table.seat_codes[1], "name": "N"})
    assert r0.status_code == 400 and "Buy in" in r0.json()["detail"] and not table.engine.players
    assert sign_in(c, table, a, 1, code="wrong").status_code == 400            # bad seat code
    assert sign_in(c, table, a, 1, sign_with=SigningKey.generate()).status_code == 401   # signed by someone else
    ch = c.post("/api/auth/challenge", json={"pubkey": a.pub}).json()
    sig = base64.b64encode(a.sk.sign(ch["message"].encode()).signature).decode()
    body = {"pubkey": a.pub, "nonce": ch["nonce"], "signature": sig, "seat": 1, "seat_code": table.seat_codes[1], "name": "A"}
    assert c.post("/api/auth/login", json=body).status_code == 200
    assert c.post("/api/auth/login", json=body).status_code == 401              # replayed nonce
    assert sign_in(c, table, b, 1).status_code == 400                           # seat taken
    assert c.post("/api/auth/challenge", json={"pubkey": "not-a-key"}).status_code == 400
    assert chain.bal[a.pub] == table.buy_in


def test_token_rules(env):
    c, table, _, dh = env
    a = Player("A")
    sign_in(c, table, a, 1)
    assert c.post("/api/action", json={"type": "fold"}).status_code == 401                       # no token
    assert c.post("/api/dealer/start_hand", headers=hdr(a)).status_code == 403                   # player != dealer
    assert c.post("/api/action", json={"type": "fold"}, headers=dh).status_code == 403           # dealer != player
    assert c.post("/api/action", json={"type": "fold"}, headers={"Authorization": "Bearer x.y"}).status_code == 401
    tampered = a.token[:-2] + ("AA" if not a.token.endswith("AA") else "BB")
    assert c.get("/api/me", headers={"Authorization": f"Bearer {tampered}"}).status_code == 401


def test_dealer_pin_lockout(env):
    c, *_ = env
    for _ in range(5):
        assert c.post("/api/dealer/login", json={"pin": "000000"}).status_code == 401
    assert c.post("/api/dealer/login", json={"pin": PIN}).status_code == 429


def test_full_hand_vision_to_instant_settlement(env):
    c, table, chain, dh = env
    a, b = Player("Alice"), Player("Bob")
    sign_in(c, table, a, 1)
    sign_in(c, table, b, 3)
    assert c.post("/api/dealer/start_hand", headers=dh).status_code == 200
    assert chain.begun == table.hand_key

    # betting: nobody can act out of turn, seat comes from the token
    wrong = b if table.engine.to_act == 1 else a
    assert c.post("/api/action", json={"type": "check"}, headers=hdr(wrong)).status_code == 400
    check_down(c, table, [a, b])
    assert table.phase == "need_board"

    # camera reads flop (auto-confirms: all stable, conf >= 0.98)
    c.post("/api/dealer/manual_mode", json={"on": False}, headers=dh)
    table.on_vision(zones(B1="As", B2="Kd", B3="7c"))
    assert table.engine.board == ["As", "Kd", "7c"]
    check_down(c, table, [a, b])
    table.on_vision(zones(B1="As", B2="Kd", B3="7c", B4="2h"))
    check_down(c, table, [a, b])
    table.on_vision(zones(B1="As", B2="Kd", B3="7c", B4="2h", B5="9s"))
    check_down(c, table, [a, b])
    assert table.phase == "showdown"

    before = dict(chain.bal)
    # Alice (seat 1) has aces, Bob (seat 3) has queens: Alice must be paid instantly
    table.on_vision(zones(B1="As", B2="Kd", B3="7c", B4="2h", B5="9s", **{"1a": "Ad", "1b": "Ah", "3a": "Qs", "3b": "Qd"}))
    st = c.get("/api/state").json()
    assert st["phase"] == "done" and st["result"]["winners"] == [1]
    assert st["settlement"]["signature"]
    # both only paid the big blind (10) all the way down, so the winner nets +10
    assert chain.bal[a.pub] - before[a.pub] == 10 and chain.bal[b.pub] - before[b.pub] == -10
    assert sum(chain.bal.values()) == 2 * table.buy_in                             # zero-sum, nothing minted/lost
    assert st["result"]["hand_names"]["1"] != st["result"]["hand_names"]["3"]
    assert c.post("/api/dealer/next_hand", headers=dh).status_code == 200


def test_duplicate_card_blocks_auto_confirm(env):
    c, table, _, dh = env
    a, b = Player("A"), Player("B")
    sign_in(c, table, a, 1)
    sign_in(c, table, b, 2)
    c.post("/api/dealer/start_hand", headers=dh)
    check_down(c, table, [a, b])
    table.on_vision(zones(B1="As", B2="As", B3="7c"))                               # same card twice = misread
    assert table.engine.board == []
    assert c.post("/api/dealer/override", json={"zone": "B2", "code": "Kd"}, headers=dh).status_code == 200
    assert table.engine.board == ["As", "Kd", "7c"]                                 # fixed by override, then auto-confirmed


def test_fold_win_pays_without_any_card_reads(env):
    c, table, chain, dh = env
    a, b = Player("A"), Player("B")
    sign_in(c, table, a, 1)
    sign_in(c, table, b, 2)
    c.post("/api/dealer/start_hand", headers=dh)
    folder = a if table.engine.to_act == 1 else b
    winner = b if folder is a else a
    c.post("/api/action", json={"type": "fold"}, headers=hdr(folder))
    st = c.get("/api/state").json()
    assert st["phase"] == "done" and not st["result"]["showdown"]
    assert chain.bal[winner.pub] > table.buy_in > chain.bal[folder.pub]


def test_public_state_leaks_no_secrets(env):
    c, table, *_ = env
    a = Player("A")
    sign_in(c, table, a, 1)
    raw = c.get("/api/state").text
    assert table.seat_codes[1] not in raw and a.token not in raw


def test_vision_and_camera_endpoints_are_dealer_only_and_validate(env):
    c, table, _, dh = env
    a = Player("A")
    sign_in(c, table, a, 1)
    jpg = b"\xff\xd8\xff\xe0" + b"0" * 100
    # no token / player token are refused
    assert c.post("/api/vision/update", json={"zones": {}}).status_code == 401
    assert c.post("/api/vision/update", json={"zones": {}}, headers=hdr(a)).status_code == 403
    assert c.post("/api/cam/frame", content=jpg, headers=hdr(a)).status_code == 403
    assert c.get("/api/cam/latest.jpg", headers=hdr(a)).status_code == 403
    # dealer: valid jpeg roundtrips, junk is rejected
    assert c.post("/api/cam/frame", content=b"not a jpeg", headers=dh).status_code == 400
    assert c.post("/api/cam/frame", content=jpg, headers=dh).status_code == 200
    r = c.get("/api/cam/latest.jpg", headers=dh)
    assert r.status_code == 200 and r.content == jpg and float(r.headers["x-frame-ts"]) > 0
    assert c.post("/api/vision/frame", content=jpg, headers=dh).status_code == 200
    assert c.post("/api/vision/update", json={"ts": 1, "zones": {"B1": {"code": "As", "conf": 0.99, "stable": True}}, "warnings": []}, headers=dh).status_code == 200
    ds = c.get("/api/dealer/state", headers=dh).json()
    assert ds["readings"]["B1"]["code"] == "As" and set(ds["seat_codes"]) == {"1", "2", "3", "4"}
    # public state must never carry dealer-only fields
    assert "seat_codes" not in c.get("/api/state").json()


def test_pages_and_seat_qr(env):
    c, _, _, dh = env
    for path in ("/dealer", "/table", "/cam", "/play/1"):
        assert c.get(path).status_code == 200
    r = c.get("/api/dealer/seat_qr/2.png", headers=dh)
    assert r.status_code == 200 and r.content[:4] == b"\x89PNG" and "/play/2?code=" in r.headers["x-join-url"]
    assert c.get("/api/dealer/seat_qr/2.png").status_code == 401


def test_areas_endpoints_validate_and_are_dealer_only(env, tmp_path, monkeypatch):
    from vision import areas as va
    monkeypatch.setattr(va, "AREAS_FILE", tmp_path / "areas.json")
    monkeypatch.setattr(va.save, "__defaults__", (tmp_path / "areas.json",))
    monkeypatch.setattr(va.load, "__defaults__", (tmp_path / "areas.json",))
    c, table, _, dh = env
    a = Player("A")
    sign_in(c, table, a, 1)
    assert c.get("/api/vision/areas").status_code == 401
    assert c.post("/api/vision/areas", json={"areas": {}}, headers=hdr(a)).status_code == 403
    assert c.post("/api/vision/areas", json={"areas": {"BOARD": [0.5, 0.5, 0.4, 0.9]}}, headers=dh).status_code == 400
    assert c.post("/api/vision/areas", json={"areas": {"HACK": [0, 0, .5, .5]}}, headers=dh).status_code == 400
    assert c.post("/api/vision/areas", json={"areas": {"BOARD": [0.1, 0.2, 0.5, 0.9]}}, headers=dh).status_code == 200
    assert c.get("/api/vision/areas", headers=dh).json()["areas"]["BOARD"] == [0.1, 0.2, 0.5, 0.9]
    assert c.get("/areas").status_code == 200


def test_board_is_a_set_in_any_order_and_rejoin_midhand_and_no_extra_chips(env):
    c, table, chain, dh = env
    a, b = Player("A"), Player("B")
    sign_in(c, table, a, 1)
    sign_in(c, table, b, 2)
    c.post("/api/dealer/start_hand", headers=dh)
    check_down(c, table, [a, b])
    # flop laid out in a different slot order than dealt; labels are positional so any order is fine
    table.on_vision(zones(B1="7c", B2="As", B3="Kd"))
    assert sorted(table.engine.board) == ["7c", "As", "Kd"]
    # a phone reload mid-hand must be able to sign back in (same wallet), and must not mint more chips
    before = dict(chain.bal)
    assert sign_in(c, table, a, 1).status_code == 200
    assert chain.bal == before
    # turn: the three confirmed cards stay in the board area; only the NEW card is proposed
    check_down(c, table, [a, b])
    table.on_vision(zones(B1="7c", B2="As", B3="Kd", B4="2h"))
    assert table.engine.board[-1] == "2h" and len(table.engine.board) == 4
    # a phantom extra card in the board area is flagged instead of guessed
    check_down(c, table, [a, b])
    table.on_vision(zones(B1="7c", B2="As", B3="Kd", B4="2h", B5="9s", B6="4d"))
    assert table.phase == "showdown" and not table.prompts           # 5+ cards out: now we wait for the players' cards


def _to_showdown(c, table, dh, a, b):
    c.post("/api/dealer/start_hand", headers=dh)
    check_down(c, table, [a, b])
    board = dict(B1="As", B2="Kd", B3="7c")
    table.on_vision(zones(**board))
    check_down(c, table, [a, b])
    board.update(B4="2h")
    table.on_vision(zones(**board))
    check_down(c, table, [a, b])
    board.update(B5="9s")
    table.on_vision(zones(**board))
    assert table.phase == "showdown"                                   # five cards are out


def test_private_peeking_is_never_read_stored_or_shown_before_showdown(env):
    c, table, _, dh = env
    a, b = Player("Alice"), Player("Bob")
    sign_in(c, table, a, 1)
    sign_in(c, table, b, 2)
    c.post("/api/dealer/start_hand", headers=dh)
    # during betting a player lifts a card and it is seen face up in their area: it must be discarded
    table.on_vision(zones(**{"1a": "Ad", "1b": "Kc"}))
    assert not any(z for z in table.readings if z[0].isdigit())
    assert c.get("/api/state").json()["showdown_cards"] == {}
    st = c.get("/api/state").json()
    ds = c.get("/api/dealer/state", headers=dh).json()
    assert st["showdown_cards"] == {} and "Ad" not in st["board_camera"] and "Kc" not in st["board_camera"]
    assert not any(z[0].isdigit() for z in ds["readings"])                  # no seat reading is stored before five cards are out
    # a peeked card that equals a board card must not raise a phantom duplicate conflict either
    check_down(c, table, [a, b])
    table.on_vision(zones(B1="Ad", B2="7c", B3="Kd", **{"1a": "Ad"}))
    assert sorted(table.engine.board) == ["7c", "Ad", "Kd"]


def test_showdown_waits_for_every_player_to_place_both_cards(env):
    c, table, chain, dh = env
    a, b = Player("Alice"), Player("Bob")
    sign_in(c, table, a, 1)
    sign_in(c, table, b, 2)
    _to_showdown(c, table, dh, a, b)
    st = c.get("/api/state").json()["status"]
    assert st["code"] == "showdown" and "ALICE" in st["detail"] and "BOB" in st["detail"]
    board = dict(B1="As", B2="Kd", B3="7c", B4="2h", B5="9s")
    table.on_vision(zones(**board, **{"1a": "Ad"}))                       # one card down: still waiting
    assert table.phase == "showdown"
    table.on_vision(zones(**board, **{"1a": "Ad", "1b": "Ah"}))            # Alice done, Bob has not placed
    st = c.get("/api/state").json()
    assert table.phase == "showdown" and "BOB" in st["status"]["detail"] and "ALICE" not in st["status"]["detail"]
    assert st["showdown_cards"]["1"] == ["Ad", "Ah"]                       # placed cards are public once down
    table.on_vision(zones(**board, **{"1a": "Ad", "1b": "Ah", "2a": "Qs", "2b": "Qd"}))
    st = c.get("/api/state").json()
    assert st["phase"] == "done" and st["result"]["winners"] == [1] and st["settlement"]["signature"]
    assert "ALICE WINS" in st["status"]["headline"] and "PAID ON SOLANA" in st["status"]["detail"]


def test_status_line_for_every_phase_and_last_actions(env):
    c, table, _, dh = env
    s = lambda: c.get("/api/state").json()  # noqa: E731
    assert s()["status"]["headline"] == "WAITING FOR PLAYERS"
    a, b = Player("Alice"), Player("Bob")
    sign_in(c, table, a, 1)
    sign_in(c, table, b, 2)
    assert s()["status"]["code"] == "ready"
    c.post("/api/dealer/start_hand", headers=dh)
    turn = s()
    assert turn["status"]["code"] == "turn" and "'S TURN" in turn["status"]["headline"]
    acting = {1: a, 2: b}[turn["to_act"]]
    c.post("/api/action", json={"type": "call"}, headers=hdr(acting))
    other = {1: a, 2: b}[s()["to_act"]]
    c.post("/api/action", json={"type": "check"}, headers=hdr(other))
    st = s()
    assert st["status"]["headline"] == "DEALER: PLACE THE FLOP" and "3 cards" in st["status"]["detail"]
    acts = {x["last_action"] for x in st["seats"] if x["occupied"]}
    assert "CHECK" in acts and any(a.startswith("CALL") for a in acts)       # everyone sees who did what
    roles = st["roles"]
    assert set(roles) == {"dealer", "sb", "bb"} and roles["sb"] != roles["bb"]



def test_screens_always_mirror_the_camera_and_the_game_takes_the_cards_as_betting_closes(env):
    c, table, _, dh = env
    a, b = Player("Alice"), Player("Bob")
    sign_in(c, table, a, 1)
    sign_in(c, table, b, 2)
    assert c.get("/api/state").json()["board_camera"] == []
    c.post("/api/dealer/start_hand", headers=dh)
    assert table.phase == "betting"
    # flop goes down while preflop betting is still open: every screen shows it immediately
    table.on_vision(zones(B1="As", B2="Kd", B3="7c"))
    st = c.get("/api/state").json()
    assert st["board_camera"] == ["As", "Kd", "7c"] and st["board"] == [] and st["phase"] == "betting"
    # only stable reads are shown (the tracker has already folded confidence into "stable")
    table.on_vision({"ts": 1, "zones": {"B1": {"code": "As", "conf": 0.99, "stable": True},
                                         "B2": {"code": "Kd", "conf": 0.99, "stable": False},
                                         "B3": {"code": "7c", "conf": 0.9, "stable": True}}, "warnings": []})
    assert c.get("/api/state").json()["board_camera"] == ["As", "Kd", "7c"]       # Kd dropped out for a moment: held, no flicker
    table.on_vision(zones(B1="As", B2="Kd", B3="7c"))
    check_down(c, table, [a, b])                                           # betting closes -> the game takes the flop
    st = c.get("/api/state").json()
    assert st["board"] == ["As", "Kd", "7c"] and st["board_camera"] == ["As", "Kd", "7c"] and st["phase"] == "need_board"
    # the screens mirror the camera even when nothing is being played: cards removed -> board empty
    for code in list(table._board_seen_at):
        table._board_seen_at[code] -= 10                                    # the hold time passes
    table.on_vision({"ts": 2, "zones": {}, "warnings": []})
    assert c.get("/api/state").json()["board_camera"] == []


def _live(table, zones_=None, warnings=None):
    table.on_vision({"ts": __import__("time").time(), "zones": zones_ or {}, "warnings": warnings or []})




def test_failed_payout_is_retried_automatically_and_blocks_the_next_hand(env, monkeypatch):
    from server.chain_client import ChainError
    c, table, chain, dh = env
    a, b = Player("Alice"), Player("Bob")
    sign_in(c, table, a, 1)
    sign_in(c, table, b, 2)
    real = chain.settle
    calls = {"n": 0}

    def flaky(*args, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ChainError("rpc down")
        return real(*args, **kw)
    monkeypatch.setattr(chain, "settle", flaky)
    c.post("/api/dealer/start_hand", headers=dh)
    first_hand = table.hand_key
    folder = a if table.engine.to_act == 1 else b
    c.post("/api/action", json={"type": "fold"}, headers=hdr(folder))
    assert table.phase == "settling" and table.settle_error
    table.tick()
    assert table.phase == "settling"                                       # too soon to retry; and no new hand
    table._settle_try -= 6
    table.tick()
    assert calls["n"] == 2 and first_hand in chain.settled                 # the payout landed on the retry
    assert table.settle_error is None and table.phase == "done"
    table._settled_at -= 10
    table.tick()
    assert table.phase == "waiting" and table.hand_key == first_hand       # ...then it WAITS for the dealer (no automatic round)




def test_cards_left_from_the_last_hand_are_ignored_until_they_are_picked_up(env):
    c, table, chain, dh = env
    a, b = Player("Alice"), Player("Bob")
    sign_in(c, table, a, 1)
    sign_in(c, table, b, 2)
    c.post("/api/dealer/start_hand", headers=dh)
    check_down(c, table, [a, b])
    table.on_vision(zones(B1="As", B2="Kd", B3="7c"))                       # last hand's flop is on the table...
    assert table.engine.board == ["As", "Kd", "7c"]
    c.post("/api/dealer/abort_hand", headers=dh)
    c.post("/api/dealer/start_hand", headers=dh)                            # ...and the next hand starts over them
    check_down(c, table, [a, b])
    table.on_vision(zones(B1="As", B2="Kd", B3="7c"))
    assert table.engine.board == [] and table.phase == "need_board"                      # leftovers do not count for this hand...
    assert c.get("/api/state").json()["board_camera"] == ["As", "Kd", "7c"]               # ...but the screens still show what is there
    table.on_vision({"ts": 1, "zones": {}, "warnings": []})                 # picked up
    table.on_vision(zones(B1="Td", B2="3h", B3="Qs"))                       # the new flop
    assert sorted(table.engine.board) == ["3h", "Qs", "Td"]
    # a card that was a leftover but is dealt again later (picked up first) counts normally
    table.on_vision(zones(B1="Td", B2="3h", B3="Qs", B4="As"))
    assert "As" in table._fresh_codes() and "As" in c.get("/api/state").json()["board_camera"]   # now it counts


def test_seat_areas_are_read_only_once_five_cards_are_out_then_the_winner_is_paid(env):
    c, table, chain, dh = env
    a, b = Player("Alice"), Player("Bob")
    sign_in(c, table, a, 1)
    sign_in(c, table, b, 2)
    c.post("/api/dealer/start_hand", headers=dh)
    check_down(c, table, [a, b])
    board = dict(B1="As", B2="Kd", B3="7c")
    seats = {"1a": "Ad", "1b": "Ah", "2a": "Qs", "2b": "Qd"}
    table.on_vision(zones(**board, **seats))                                # hole cards in view too early: ignored
    assert not any(z[0].isdigit() for z in table.readings) and table.phase == "betting"
    check_down(c, table, [a, b])
    board.update(B4="2h")
    table.on_vision(zones(**board, **seats))
    check_down(c, table, [a, b])
    board.update(B5="9s")
    table.on_vision(zones(**board))                                          # five cards out, seats still empty
    check_down(c, table, [a, b])
    assert table.phase == "showdown"
    before = dict(chain.bal)
    table.on_vision(zones(**board, **seats))                                 # now both players' cards are read and paid
    assert table.phase == "done" and table.engine.result.winners == [1]
    assert chain.bal[a.pub] > before[a.pub] and chain.bal[b.pub] < before[b.pub]


def test_a_hand_left_open_by_an_earlier_session_is_closed_automatically(env):
    from server.chain_client import ChainError
    c, table, chain, dh = env
    a, b = Player("Alice"), Player("Bob")
    sign_in(c, table, a, 1)
    sign_in(c, table, b, 2)
    chain.begun = "OLDSESSION-7"                                           # left open when the server restarted mid-hand
    with __import__("pytest").raises(ChainError):
        chain.begin_hand("x-1")                                            # the chain refuses to open a second hand...
    assert c.post("/api/dealer/start_hand", headers=dh).status_code == 200  # ...but the table closes the old one and goes on
    assert table.phase == "betting" and chain.begun == table.hand_key



def test_cards_are_held_on_screen_briefly_when_the_camera_loses_them_and_keep_their_place(env):
    c, table, _, dh = env
    board = lambda: c.get("/api/state").json()["board_camera"]  # noqa: E731
    table.on_vision(zones(B1="As", B2="Kd", B3="7c"))
    assert board() == ["As", "Kd", "7c"]
    table.on_vision(zones(B1="As", B2="Kd", B3="7c", B4="2h"))              # a new card is appended; the others do not move
    assert board() == ["As", "Kd", "7c", "2h"]
    table.on_vision(zones(B1="As", B2="7c", B3="2h"))                       # Kd is hidden for a moment (a hand in the way)
    assert board() == ["As", "Kd", "7c", "2h"]                              # ...and does NOT flicker off the screen
    table.on_vision(zones(B1="As", B2="Kd", B3="7c", B4="2h"))
    assert board() == ["As", "Kd", "7c", "2h"]                              # it comes back in its old place
    table.on_vision({"ts": 1, "zones": {}, "warnings": []})                 # everything really removed
    assert board() == ["As", "Kd", "7c", "2h"]                              # still held for a moment
    for code in list(table._board_seen_at):
        table._board_seen_at[code] -= 10                                    # the hold time passes
    table.on_vision({"ts": 2, "zones": {}, "warnings": []})
    assert board() == []


def test_seat_codes_are_permanent_and_the_roster_survives_a_server_restart(tmp_path):
    from server.table import Table
    chain = MockChain()
    path = tmp_path / "table_state.json"
    t1 = Table(chain, state_path=path)
    t1.restore()
    codes = dict(t1.seat_codes)
    chain._credit("A" * 43 + "1", 1000); chain._credit("B" * 43 + "2", 1000)
    t1.join(1, "Alice", "A" * 43 + "1")
    t1.join(2, "Bob", "B" * 43 + "2")
    t1.start_hand()                                                        # a running hand is not persisted...
    # ...the process "restarts": a brand new Table reads the same file
    t2 = Table(chain, state_path=path)
    t2.restore()
    assert t2.seat_codes == codes                                          # every QR already scanned or printed still works
    assert {s: (p.name, p.pubkey, p.stack) for s, p in t2.engine.players.items()} == {
        1: ("Alice", "A" * 43 + "1", 1000), 2: ("Bob", "B" * 43 + "2", 1000)}
    assert t2.phase == "waiting" and not t2.engine.in_hand                 # the hand itself is simply gone, a new one starts
    # stacks come from the chain, the source of truth for chips
    chain.bal["A" * 43 + "1"] = 1234
    t3 = Table(chain, state_path=path)
    t3.restore()
    assert t3.engine.players[1].stack == 1234


def test_cashing_out_does_not_change_the_seat_code_so_the_same_qr_works_again(env):
    c, table, chain, _ = env
    a = Player("A")
    code_before = table.seat_codes[1]
    assert sign_in(c, table, a, 1).status_code == 200
    assert c.post("/api/cashout", headers=hdr(a)).status_code == 200
    assert table.seat_codes[1] == code_before
    assert sign_in(c, table, a, 1, code=code_before).status_code == 200    # rescan the very same QR and sit down again
    assert table.engine.players[1].stack == table.buy_in


def test_rescanning_from_the_same_phone_returns_the_same_seat_even_mid_hand(env):
    c, table, chain, dh = env
    a, b = Player("A"), Player("B")
    sign_in(c, table, a, 1)
    sign_in(c, table, b, 2)
    c.post("/api/dealer/start_hand", headers=dh)
    before = dict(chain.bal)
    r = sign_in(c, table, a, 1)                                            # phone lost its token / page reloaded mid-hand
    assert r.status_code == 200 and r.json()["seat"] == 1
    assert chain.bal == before                                             # and nobody got extra chips
    assert c.post("/api/action", json={"type": "fold"}, headers=hdr(a)).status_code in (200, 400)   # new token works (400 = not my turn)



def test_a_round_never_starts_by_itself_the_dealer_starts_it_and_players_can_join_and_buy_in_first(env):
    c, table, chain, dh = env
    a, b = Player("Alice"), Player("Bob")
    sign_in(c, table, a, 1)
    assert table.can_start() == {"ok": False, "reason": "Need at least 2 players with chips"}
    sign_in(c, table, b, 2)
    for _ in range(5):
        table._last_join -= 30
        table.tick()
        _live(table)
    assert table.phase == "waiting" and table.can_start()["ok"]            # two players, but nothing starts by itself
    st = c.get("/api/state").json()
    assert st["status"]["code"] == "ready" and "START THE ROUND" in st["status"]["headline"] and st["can_start"]["ok"]
    # a third player can still sit down and buy in while we wait for the dealer
    third = Player("Carol")
    assert sign_in(c, table, third, 3).status_code == 200
    assert table.engine.players[3].stack == table.buy_in
    # the dealer presses START ROUND
    assert c.post("/api/dealer/start_round").status_code == 401            # dealers only
    assert c.post("/api/dealer/start_round", headers=hdr(a)).status_code == 403
    assert c.post("/api/dealer/start_round", headers=dh).status_code == 200
    assert table.phase == "betting"
    assert sign_in(c, table, Player("Late"), 4).status_code == 400         # no joining mid-round


def test_after_a_payout_the_table_waits_for_the_dealer_and_the_board_must_be_clear(env):
    c, table, chain, dh = env
    a, b = Player("Alice"), Player("Bob")
    sign_in(c, table, a, 1)
    sign_in(c, table, b, 2)
    assert c.post("/api/dealer/start_round", headers=dh).status_code == 200
    folder = a if table.engine.to_act == 1 else b
    c.post("/api/action", json={"type": "fold"}, headers=hdr(folder))
    assert table.phase == "done" and table.last_settlement
    first = table.hand_key
    msg = zones(B1="As", B2="Kd", B3="7c", B4="2h", B5="9s")
    msg["ts"] = __import__("time").time()                                   # a live camera (not the helper's fake timestamp)
    table.on_vision(msg)                                                    # the old board is still on the table
    table.tick()
    assert table.phase == "done"                                           # the result stays on screen for a few seconds
    table._settled_at -= 9
    table.tick()
    assert table.phase == "waiting" and table.hand_key == first            # now waiting: nothing has restarted
    # the board is not clear: START ROUND is refused, and everybody is told why
    r = c.post("/api/dealer/start_round", headers=dh)
    assert r.status_code == 400 and "Clear the board" in r.json()["detail"] and "5 cards" in r.json()["detail"]
    st = c.get("/api/state").json()
    assert st["status"]["code"] == "clear_board" and not st["can_start"]["ok"]
    # cards picked up (and the short display hold passes) -> the dealer can start
    table.on_vision({"ts": __import__("time").time(), "zones": {}, "warnings": []})
    for code in list(table._board_seen_at):
        table._board_seen_at[code] -= 10
    table.on_vision({"ts": __import__("time").time(), "zones": {}, "warnings": []})
    assert table.can_start()["ok"]
    assert c.post("/api/dealer/start_round", headers=dh).status_code == 200
    assert table.hand_key != first and table.phase == "betting"


def test_with_the_camera_offline_the_dealer_can_still_start_a_round(env):
    c, table, chain, dh = env
    sign_in(c, table, Player("Alice"), 1)
    sign_in(c, table, Player("Bob"), 2)
    table.on_vision(zones(B1="As"))                                         # a card was seen...
    table.vision_ts -= 60                                                   # ...but the camera has been silent for a minute
    assert table.can_start()["ok"]                                          # no live camera: we cannot insist on a clear board


def test_a_failing_start_refuses_cleanly_without_burning_hand_ids(env, monkeypatch):
    from server.chain_client import ChainError
    c, table, chain, dh = env
    sign_in(c, table, Player("Alice"), 1)
    sign_in(c, table, Player("Bob"), 2)

    def down(*a_, **k_):
        raise ChainError("rpc down")
    monkeypatch.setattr(chain, "begin_hand", down)
    before = (table.hand_key, table.engine.hand_id)
    for _ in range(3):
        r = c.post("/api/dealer/start_round", headers=dh)
        assert r.status_code == 400 and "rpc down" in r.json()["detail"]
    assert (table.hand_key, table.engine.hand_id) == before and table.phase == "waiting"


def test_bankroll_is_kept_between_visits_and_more_chips_only_come_from_a_new_sol_payment(env):
    c, table, chain, dh = env
    a, b = Player("Alice"), Player("Bob")
    sign_in(c, table, a, 1)
    sign_in(c, table, b, 2)
    assert not hasattr(table, "rebuy")                                          # no free refill exists
    chain.bal[a.pub] += 300                                                     # Alice wins 300 (on chain)
    table.engine.players[1].stack += 300
    assert c.post("/api/cashout", headers=hdr(a)).status_code == 200            # log out: chips stay in the bankroll
    assert chain.bal[a.pub] == table.buy_in + 300
    sign_in(c, table, a, 1)
    assert table.engine.players[1].stack == table.buy_in + 300                  # same stack, no new chips minted
    fund(c, table, a, 500)                                                      # buying more = a new verified SOL payment
    assert table.engine.players[1].stack == table.buy_in + 800
    assert c.post("/api/rebuy", headers=hdr(a)).status_code in (404, 405)


def test_hands_and_grants_are_recorded_for_the_database_and_history_endpoint_is_safe(env):
    c, table, chain, dh = env
    calls = {"hand": [], "entry": [], "player": []}

    class FakeDb:
        enabled = True
        def upsert_player(self, *a): calls["player"].append(a)
        def record_entry(self, *a): calls["entry"].append(a)
        def record_hand(self, *a): calls["hand"].append(a)
        def history(self, pk): return {"enabled": True, "stats": {}, "series": [], "recent": []}

    table.db = FakeDb()
    a, b = Player("Alice"), Player("Bob")
    sign_in(c, table, a, 1)
    sign_in(c, table, b, 2)
    assert [e[2] for e in calls["entry"]] == ["deposit", "deposit"]            # both buy-ins recorded, nothing else
    assert c.post("/api/dealer/start_hand", headers=dh).status_code == 200
    seat = table.engine.to_act
    folder = a if seat == 1 else b
    assert c.post("/api/action", json={"type": "fold"}, headers=hdr(folder)).status_code == 200
    assert len(calls["hand"]) == 1
    hand_id, pot, board, sh, sig, url, rows = calls["hand"][0]
    assert sorted(r["delta"] for r in rows) == [-5, 5] or sorted(r["delta"] for r in rows) == [-10, 10]
    assert sum(r["delta"] for r in rows) == 0 and {r["pubkey"] for r in rows} == {a.pub, b.pub}
    assert sum(1 for r in rows if r["won"]) == 1
    assert c.get("/api/history", params={"pubkey": a.pub}).status_code == 200
    assert c.get("/api/history", params={"pubkey": "nope"}).status_code == 400


def test_a_busted_player_keeps_their_seat_across_a_restart_and_buys_more_with_sol(tmp_path):
    from server.table import Table
    chain = MockChain()
    path = tmp_path / "table_state.json"
    t1 = Table(chain, state_path=path)
    t1.restore()
    chain._credit("A" * 43 + "1", 1000)
    chain._credit("B" * 43 + "2", 1000)
    t1.join(1, "Alice", "A" * 43 + "1")
    t1.join(2, "Bob", "B" * 43 + "2")
    chain.bal["A" * 43 + "1"] = 0                                          # Alice lost everything
    t1.engine.players[1].stack = 0
    t1._bump()
    t2 = Table(chain, state_path=path)
    t2.restore()
    assert t2.engine.players[1].stack == 0 and t2.engine.players[1].name == "Alice"   # still seated after the restart
    t2.confirm_buyin("A" * 43 + "1", "mock-1000-77")                         # and buys back in with a real payment
    assert t2.engine.players[1].stack == 1000


def test_dealer_can_kick_a_player_mid_hand_and_chips_go_back(env):
    c, table, chain, dh = env
    a, b = Player("A"), Player("B")
    sign_in(c, table, a, 1)
    sign_in(c, table, b, 2)
    assert c.post("/api/dealer/start_hand", headers=dh).status_code == 200
    old_code = table.seat_codes[2]
    assert c.post("/api/dealer/kick", json={"seat": 2}, headers=hdr(a)).status_code == 403    # players cannot kick
    assert c.post("/api/dealer/kick", json={"seat": 2}, headers=dh).status_code == 200
    assert 2 not in table.engine.players and not table.engine.in_hand
    assert table.engine.players[1].stack == table.buy_in                                     # nobody lost chips
    assert table.seat_codes[2] != old_code                                                    # old QR no longer sits down
    assert c.post("/api/dealer/kick", json={"seat": 2}, headers=dh).status_code in (400, 404, 409)  # empty seat


def test_dealer_gets_a_camera_qr(env):
    c, _, _, dh = env
    r = c.get("/api/dealer/cam_qr.png", headers=dh)
    assert r.status_code == 200 and r.content[:4] == b"\x89PNG" and r.headers["x-join-url"].endswith("/cam")
    assert c.get("/api/dealer/cam_qr.png").status_code in (401, 403)


def test_only_a_wallet_that_paid_sol_can_sit_and_cashing_out_pays_sol_back(env):
    c, table, chain, dh = env
    w = Player("Wallet")
    assert c.post("/api/sol/prepare", json={"pubkey": w.pub, "chips": 500}).json()["lamports"] == 500 * 10_000
    assert c.post("/api/sol/confirm", json={"pubkey": w.pub, "signature": "garbage"}).status_code == 400   # not a payment
    assert c.post("/api/sol/confirm", json={"pubkey": w.pub, "signature": "mock-500-1"}).json()["balance"] == 500
    assert c.post("/api/sol/confirm", json={"pubkey": w.pub, "signature": "mock-500-1"}).json()["balance"] == 500   # replay: no double credit
    assert sign_in(c, table, w, 1).status_code == 200
    assert table.engine.players[1].stack == 500
    r = c.post("/api/cashout_sol", headers=hdr(w))
    assert r.status_code == 200 and r.json()["lamports"] == 500 * 10_000
    assert 1 not in table.engine.players and chain.bal[w.pub] == 0
