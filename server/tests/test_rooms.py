"""Hosted tables: isolated rooms on one shared CHIP ledger, optional bots, and the safeguards."""
import base64
import os
import time

os.environ["AUTOPOKER_NO_AUTOSTART"] = "1"

import base58  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from nacl.signing import SigningKey  # noqa: E402

from server.app import create_app  # noqa: E402
from server.auth import Auth  # noqa: E402
from server.chain_client import MockChain  # noqa: E402

SECRET = "s" * 40
JPG = b"\xff\xd8\xff\xe0" + b"0" * 100
PAYMENTS = iter(range(1, 100_000))


@pytest.fixture()
def site():
    app = create_app(chain=MockChain(), session_secret=SECRET, hosted=True, max_rooms=2)
    return TestClient(app), app.state.rooms


def bearer(tok):
    return {"Authorization": f"Bearer {tok}"}


def new_room(c):
    r = c.post("/api/rooms")
    assert r.status_code == 200, r.text
    j = r.json()
    return j["room"], bearer(j["dealer_token"])


def guest_sits(c, rooms, rid, seat=1):
    sk = SigningKey.generate()
    pub = base58.b58encode(bytes(sk.verify_key)).decode()
    paid = c.post("/api/sol/confirm", json={"pubkey": pub, "signature": f"mock-1000-{next(PAYMENTS)}", "room": rid})
    assert paid.status_code == 200, paid.text
    ch = c.post("/api/auth/challenge", json={"pubkey": pub}).json()
    sig = base64.b64encode(sk.sign(ch["message"].encode()).signature).decode()
    r = c.post("/api/auth/login", json={"pubkey": pub, "nonce": ch["nonce"], "signature": sig, "seat": seat,
                                        "seat_code": rooms.get(rid).table.seat_codes[seat], "name": "Guest", "room": rid})
    assert r.status_code == 200, r.text
    return pub, bearer(r.json()["token"])


def zones(**cards):
    return {"ts": time.time(), "zones": {z: {"code": c, "conf": .9, "stable": True} for z, c in cards.items()}, "warnings": []}


def test_public_site_has_no_permanent_table_and_no_pin(site):
    c, _ = site
    assert c.get("/api/state").status_code == 404
    assert c.post("/api/dealer/login", json={"pin": "123456"}).status_code == 404
    assert c.post("/api/sol/confirm", json={"pubkey": "x", "signature": "y"}).status_code == 404   # names no table
    assert "landing" in c.get("/", follow_redirects=False).headers["location"]
    assert c.get("/healthz").json()["ok"]


def test_a_new_table_is_empty_and_a_wallet_with_no_chips_cannot_sit(site):
    c, rooms = site
    rid, _ = new_room(c)
    st = c.get("/api/state", params={"room": rid}).json()
    assert st["bots"] == [] and not any(x["occupied"] for x in st["seats"])     # bots only if the dealer asks
    sk = SigningKey.generate()
    pub = base58.b58encode(bytes(sk.verify_key)).decode()
    ch = c.post("/api/auth/challenge", json={"pubkey": pub}).json()
    sig = base64.b64encode(sk.sign(ch["message"].encode()).signature).decode()
    r = c.post("/api/auth/login", json={"pubkey": pub, "nonce": ch["nonce"], "signature": sig, "seat": 1,
                                        "seat_code": rooms.get(rid).table.seat_codes[1], "name": "N", "room": rid})
    assert r.status_code == 400 and "Buy in" in r.json()["detail"]


def test_one_wallet_cannot_sit_at_two_tables(site):
    c, rooms = site
    a, _ = new_room(c)
    b, _ = new_room(c)
    sk = SigningKey.generate()
    pub = base58.b58encode(bytes(sk.verify_key)).decode()
    c.post("/api/sol/confirm", json={"pubkey": pub, "signature": f"mock-1000-{next(PAYMENTS)}", "room": a})

    def sit(rid):
        ch = c.post("/api/auth/challenge", json={"pubkey": pub}).json()
        sig = base64.b64encode(sk.sign(ch["message"].encode()).signature).decode()
        return c.post("/api/auth/login", json={"pubkey": pub, "nonce": ch["nonce"], "signature": sig, "seat": 1,
                                               "seat_code": rooms.get(rid).table.seat_codes[1], "name": "N", "room": rid})
    assert sit(a).status_code == 200
    r = sit(b)
    assert r.status_code == 400 and "another table" in r.json()["detail"]


def test_two_tables_can_play_hands_at_the_same_time(site):
    c, rooms = site
    a, ah = new_room(c)
    b, bh = new_room(c)
    for rid in (a, b):
        guest_sits(c, rooms, rid)
    for h in (ah, bh):
        assert c.post("/api/dealer/add_bot", headers=h).status_code == 200
        assert c.post("/api/dealer/start_round", headers=h).status_code == 200
    assert rooms.get(a).table.engine.in_hand and rooms.get(b).table.engine.in_hand
    assert rooms.get(a).table.hand_key.startswith(f"{a}.") and rooms.get(b).table.hand_key.startswith(f"{b}.")


def test_closing_an_idle_room_calls_off_its_open_hand(site):
    c, rooms = site
    a, ah = new_room(c)
    guest_sits(c, rooms, a)
    c.post("/api/dealer/add_bot", headers=ah)
    c.post("/api/dealer/start_round", headers=ah)
    chain = rooms.get(a).table.chain
    assert chain.open
    new_room(c)
    rooms.get(a).last_active -= rooms.IDLE_LIMIT + 1
    new_room(c)
    assert rooms.get(a) is None and not chain.open


def test_bots_never_share_an_address_while_seated(site):
    c, rooms = site
    a, ah = new_room(c)
    b, bh = new_room(c)
    for h in (ah, ah, bh):
        assert c.post("/api/dealer/add_bot", headers=h).status_code == 200
    keys = [p.pubkey for r in rooms.all() for p in r.table.engine.players.values()]
    assert len(keys) == len(set(keys)) == 3


def test_rooms_are_isolated(site):
    c, rooms = site
    a, ah = new_room(c)
    b, bh = new_room(c)
    _, player_a = guest_sits(c, rooms, a)
    assert c.post("/api/dealer/add_bot", headers=ah).status_code == 200
    assert c.post("/api/dealer/start_round", headers=ah).status_code == 200
    assert rooms.get(a).table.engine.in_hand and not rooms.get(b).table.engine.in_hand
    c.post("/api/cam/frame", content=JPG, headers=ah)
    assert c.get("/api/cam/latest.jpg", headers=ah).status_code == 200
    assert c.get("/api/cam/latest.jpg", headers=bh).status_code == 404    # b's dealer cannot see a's camera
    # areas are per room and never touch the file used by the local table
    assert c.post("/api/vision/areas", json={"areas": {"BOARD": [.1, .2, .5, .9]}}, headers=ah).status_code == 200
    assert c.get("/api/vision/areas", headers=ah).json()["areas"] == {"BOARD": [.1, .2, .5, .9]}
    assert "S1" in c.get("/api/vision/areas", headers=bh).json()["areas"]
    assert c.get("/api/me", headers=player_a).status_code == 200


def test_room_cap_says_busy_and_idle_rooms_free_up(site):
    c, rooms = site
    a, _ = new_room(c)
    new_room(c)
    r = c.post("/api/rooms")
    assert r.status_code == 503 and "busy" in r.json()["detail"]
    rooms.get(a).last_active -= rooms.IDLE_LIMIT + 1
    new_room(c)                                                            # the idle one was closed to make room
    assert rooms.get(a) is None
    assert c.get("/api/state", params={"room": a}).status_code == 404


def test_camera_token_only_pushes_frames(site):
    c, rooms = site
    rid, dh = new_room(c)
    url = c.get("/api/dealer/cam_qr.png", headers=dh).headers["x-join-url"]
    assert f"room={rid}" in url and "#t=" in url
    cam = bearer(url.split("#t=")[1])
    assert c.post("/api/cam/frame", content=JPG, headers=cam).status_code == 200
    assert c.get("/api/cam/latest.jpg", headers=cam).status_code == 403
    assert c.post("/api/dealer/start_round", headers=cam).status_code == 403
    assert c.post("/api/vision/update", json={"zones": {}}, headers=cam).status_code == 403


def test_worker_serves_named_rooms_and_gets_told_which_seats_to_read(site):
    c, rooms = site
    rid, dh = new_room(c)
    wh = bearer(Auth(SECRET).issue({"role": "worker"}))
    assert c.get("/api/worker/rooms").status_code == 401
    assert c.get("/api/worker/rooms", headers=dh).status_code == 403
    assert c.get("/api/worker/rooms", headers=wh).json()["rooms"] == []    # no camera yet, nothing to do
    c.post("/api/cam/frame", content=JPG, headers=dh)
    r = c.get("/api/worker/rooms", headers=wh).json()["rooms"][0]
    assert r["room"] == rid and r["read_seats"] == [] and "BOARD" in r["areas"]
    assert c.get("/api/cam/latest.jpg", params={"room": rid}, headers=wh).content == JPG
    u = c.post("/api/vision/update", params={"room": rid}, json=zones(B1="As"), headers=wh).json()
    assert u["read_seats"] == [] and u["preview"] is False
    assert rooms.get(rid).table._board_view() == ["As"]


def test_solo_hand_against_a_bot_runs_to_showdown_with_virtual_bot_cards(site):
    c, rooms = site
    rid, dh = new_room(c)
    pub, ph = guest_sits(c, rooms, rid)
    t = rooms.get(rid).table
    assert c.post("/api/dealer/add_bot", headers=dh).status_code == 200
    assert c.post("/api/dealer/start_round", headers=dh).status_code == 200

    def play_street():
        for _ in range(12):
            if t.engine.to_act is None:
                return
            if t.engine.to_act in t.bots:
                t._bot_turn = (t.engine.to_act, 0.0)                  # the bot has thought long enough
                t.tick()
            else:
                kind = "call" if t.engine.to_call(1) else "check"
                c.post("/api/action", json={"type": kind}, headers=ph)
            if not t.engine.in_hand:
                return
    board = dict(B1="As", B2="Kd", B3="7c", B4="2h", B5="9s")
    for n in (3, 4, 5):
        play_street()
        if not t.engine.in_hand:
            pytest.skip("the bot folded; the showdown path needs it to stay in")   # bots are random by design
        t.on_vision(zones(**{k: board[k] for k in list(board)[:n]}))
    play_street()
    if not t.engine.in_hand:
        pytest.skip("the bot folded")
    assert t.seats_to_read() == [1]                                       # only the human's area is read, never the bot's
    assert t.phase == "showdown" and "GUEST" in t.public_state()["status"]["detail"]
    t.on_vision(zones(**board, **{"1a": "Qs", "1b": "Qd"}))
    st = t.public_state()
    assert st["phase"] == "done" and set(st["result"]["revealed"]) == {1} | t.bots
    seen = list(board.values()) + [x for cs in st["result"]["revealed"].values() for x in cs]
    assert len(seen) == len(set(seen))                                    # bot cards never collide with anything on the table
    assert "PAID ON SOLANA" in st["status"]["detail"]


def test_unchanged_vision_messages_do_not_redraw_every_screen(site):
    c, rooms = site
    rid, _ = new_room(c)
    t = rooms.get(rid).table
    t.on_vision(zones(B1="As"))
    v = t.version
    for _ in range(5):
        t.on_vision(zones(B1="As"))
    assert t.version == v
    t.on_vision(zones(B1="As", B2="Kd"))
    assert t.version > v


def test_a_card_being_voted_on_is_announced_before_it_is_confirmed(site):
    c, rooms = site
    rid, _ = new_room(c)
    t = rooms.get(rid).table
    t.on_vision({"ts": time.time(), "zones": {"B1": {"code": "As", "conf": .6, "stable": False}}, "warnings": []})
    st = t.public_state()
    assert st["board_pending"] == 1 and st["board_camera"] == []
    t.on_vision(zones(B1="As"))
    st = t.public_state()
    assert st["board_pending"] == 0 and st["board_camera"] == ["As"]


def test_websocket_pushes_state_and_reports_a_closed_table(site):
    c, rooms = site
    rid, _ = new_room(c)
    with c as live:
        with live.websocket_connect(f"/ws/table?room={rid}") as ws:
            assert "board_pending" in ws.receive_json()
        with live.websocket_connect("/ws/table?room=nope") as ws:
            from starlette.websockets import WebSocketDisconnect
            with pytest.raises(WebSocketDisconnect) as e:
                ws.receive_json()
            assert e.value.code == 4404


def test_a_slow_cash_out_does_not_hold_the_table_lock(monkeypatch):
    import threading

    from server.chain_client import MockChain
    from server.table import Table, TableError
    chain, release, entered = MockChain(), threading.Event(), threading.Event()
    t = Table(chain)
    chain._credit("A" * 44, 1000)
    t.join(1, "A", "A" * 44)
    real = chain.cashout_sol

    def slow(*a, **k):
        entered.set()
        release.wait(5)
        return real(*a, **k)
    monkeypatch.setattr(chain, "cashout_sol", slow)
    th = threading.Thread(target=t.cash_out_sol, args=(1,))
    th.start()
    assert entered.wait(2)
    done = threading.Event()
    threading.Thread(target=lambda: (t.public_state(), done.set()), daemon=True).start()
    assert done.wait(1), "the table froze during a cash-out"
    with pytest.raises(TableError):
        t.start_hand()                                                     # but no hand can start on chips that are leaving
    release.set()
    th.join(5)
    assert 1 not in t.engine.players
