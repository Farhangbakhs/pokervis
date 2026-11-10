"""FastAPI app: REST + WebSockets for phones, the dealer console, the big screen and the vision readers.

Run:  python -m uvicorn server.app:app --host 0.0.0.0 --port 8000
Env:  see tools/init_env.py. USE_MOCK_CHAIN=1 runs with an in-memory ledger (no Solana).
      HOSTED=1 runs the public site: no permanent table, visitors open short-lived tables on the shared CHIP ledger.
"""
from __future__ import annotations

import asyncio
import json
import os
import secrets
import time
from collections import defaultdict, deque
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from server import config
from server.auth import Auth, AuthError, decode_pubkey
from server.chain_client import Chain, HttpChain, MockChain
from server.db import make_db
from server.rooms import MAIN, Room, RoomManager, RoomsFull
from server.table import Table, TableError

STATIC = Path(__file__).with_name("static")
WEB = Path(__file__).resolve().parent.parent / "web"
ROOT_DIR = WEB.parent
MAX_JPG = 4_000_000
MIN_FRAME_GAP = 0.1                 # a camera may not push frames faster than this
MAX_WS_PER_ROOM = 16
MAX_WS_TOTAL = 300
HEARTBEAT = 2.0                     # seconds between state pushes when nothing changed (keeps "camera live" current)


class Limiter:
    """Sliding-window limiter keyed by (bucket, client)."""

    def __init__(self) -> None:
        self.hits: dict[tuple[str, str], deque] = defaultdict(deque)

    def check(self, bucket: str, who: str, limit: int, per: float = 60.0) -> None:
        q, now = self.hits[(bucket, who)], time.time()
        while q and q[0] < now - per:
            q.popleft()
        if len(q) >= limit:
            raise HTTPException(429, "Too many requests, slow down")
        q.append(now)

    def prune(self, older_than: float = 600.0) -> None:
        now = time.time()
        for k in [k for k, q in self.hits.items() if not q or q[-1] < now - older_than]:
            del self.hits[k]


class LoginBody(BaseModel):
    pubkey: str
    nonce: str
    signature: str
    seat: int
    seat_code: str
    name: str = Field(default="", max_length=20)
    room: str = ""


class ActionBody(BaseModel):
    type: str
    amount: int | None = None


class PinBody(BaseModel):
    pin: str


class CardsBody(BaseModel):
    cards: list[str] | None = None


class ShowdownBody(BaseModel):
    reveal: dict[int, str] | None = None


class KickBody(BaseModel):
    seat: int


class OverrideBody(BaseModel):
    zone: str
    code: str


def make_chain() -> Chain:
    if os.environ.get("USE_MOCK_CHAIN") == "1":
        return MockChain()
    return HttpChain(config.get("CHAIN_URL", "http://127.0.0.1:8787"), config.get("CHAIN_API_KEY"))


def public_base(req: Request) -> str:
    """The address PHONES can open. Order: PUBLIC_URL env, the tunnel address saved by tools/cam_link.py,
    then the address the dealer is using (unless that is localhost, which a phone cannot reach: use the LAN IP)."""
    import socket
    env = os.environ.get("PUBLIC_URL", "").rstrip("/")
    if env:
        return env
    f = ROOT_DIR / "data" / "public_url.txt"
    if f.exists() and f.read_text().strip().startswith("https://"):
        return f.read_text().strip().rstrip("/")
    host = req.headers.get("host", req.url.netloc)
    if host.split(":")[0] in ("localhost", "127.0.0.1", "[::1]"):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("10.255.255.255", 1))
            host = f"{s.getsockname()[0]}:{host.split(':')[1] if ':' in host else 8000}"
        except OSError:
            pass
        finally:
            s.close()
    return f"{req.headers.get('x-forwarded-proto', req.url.scheme)}://{host}"


def qr_png(url: str, **kw) -> bytes:
    import io

    import qrcode
    buf = io.BytesIO()
    qr = qrcode.QRCode(box_size=kw.get("box_size", 6), border=4)
    qr.add_data(url)
    qr.make(fit=True)
    qr.make_image().save(buf, format="PNG")
    return buf.getvalue()


def create_app(chain: Chain | None = None, session_secret: str | None = None, dealer_pin: str | None = None,
               persist: bool = False, hosted: bool | None = None, max_rooms: int | None = None) -> FastAPI:
    from contextlib import asynccontextmanager

    public = os.environ.get("HOSTED") == "1" if hosted is None else hosted
    rooms = RoomManager(max_rooms or int(os.environ.get("MAX_ROOMS", "3")))
    auth = Auth(session_secret or config.get("SESSION_SECRET"))
    limiter = Limiter()
    shared_chain = chain or make_chain()                # one ledger: a wallet's chips are the same at every table
    shared_db = make_db(os.environ.get("DATABASE_URL") if persist else None)
    buy_in = int(config.get("BUY_IN", "1000"))
    pin_fail: dict[str, deque] = defaultdict(deque)

    def reap_rooms() -> None:
        """Close idle rooms. A hand still open is called off: stacks only move when a hand settles, so nobody loses chips."""
        for room in rooms.sweep():
            try:
                room.table.abort_hand()
            except TableError as e:
                print(f"closing {room.id}:", e)

    @asynccontextmanager
    async def lifespan(app_: FastAPI):
        async def ticker():
            """Once a second: bots, payout retries, returning finished tables to waiting. Now and then: close idle rooms."""
            last_sweep = 0.0
            while True:
                for room in rooms.all():
                    try:
                        await asyncio.to_thread(room.table.tick)
                    except Exception as e:  # noqa: BLE001 - a bad tick must never stop the loop
                        print(f"tick error in {room.id}:", e)
                if time.time() - last_sweep > 30:
                    last_sweep = time.time()
                    await asyncio.to_thread(reap_rooms)
                    limiter.prune()
                await asyncio.sleep(1)

        async def broadcaster():
            """One state snapshot per change per room, built off the event loop so a busy table can never stall the server."""
            while True:
                for room in rooms.all():
                    version = room.table.version
                    if room.ws_count and (version != room.snap_version or time.time() - room.snap_at >= HEARTBEAT):
                        try:
                            state = await asyncio.to_thread(room.table.public_state)
                            room.publish(version, json.dumps(state))
                        except Exception as e:  # noqa: BLE001
                            print(f"state error in {room.id}:", e)
                await asyncio.sleep(0.05)
        tasks = [asyncio.create_task(ticker()), asyncio.create_task(broadcaster())]
        yield
        for t in tasks:
            t.cancel()

    app = FastAPI(title="AutoPoker server", lifespan=lifespan)
    pin = None
    app.state.table = None
    app.state.rooms = rooms
    app.state.auth = auth
    if not public:
        pin = dealer_pin or config.get("DEALER_PIN")
        table = Table(shared_chain, buy_in=buy_in,
                      min_conf=float(config.get("AUTO_MIN_CONF", "0.35")),
                      state_path=(ROOT_DIR / "data" / "table_state.json") if persist else None,
                      db=shared_db, settle_async=persist)
        table.restore()
        rooms.add(Room(MAIN, table))
        app.state.table = table

    app.add_middleware(
        CORSMiddleware, allow_credentials=False, allow_methods=["*"], allow_headers=["*"],
        # localhost + private LAN ranges only (the friend's dashboard dev server, phones on the LAN)
        allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1|192\.168\.\d+\.\d+|10\.\d+\.\d+\.\d+|172\.(1[6-9]|2\d|3[01])\.\d+\.\d+|[\w-]+\.trycloudflare\.com)(:\d+)?$",
    )

    ip_header = os.environ.get("CLIENT_IP_HEADER", "").lower()      # set by the host's proxy, which clients cannot forge

    def client(req: Request) -> str:
        return (ip_header and req.headers.get(ip_header)) or (req.client.host if req.client else "?")

    def claims_from(authorization: str | None, *roles: str) -> dict:
        tok = authorization[7:] if authorization and authorization.startswith("Bearer ") else None
        try:
            c = auth.check(tok)
        except AuthError as e:
            raise HTTPException(401, str(e)) from e
        if c.get("role") not in roles:
            raise HTTPException(403, f"{roles[0]} access required")
        return c

    def player(authorization: str | None = Header(default=None)) -> dict:
        return claims_from(authorization, "player")

    def need_room(room_id: str | None) -> Room:
        room = rooms.get(room_id)
        if room is None:
            raise HTTPException(404, "This table has closed")
        return room

    def claim_room(c: dict) -> Room:
        return need_room(c.get("room") or MAIN)

    def dealer_room(authorization: str | None = Header(default=None)) -> Room:
        return claim_room(claims_from(authorization, "dealer"))

    def camera_room(authorization: str | None = Header(default=None)) -> Room:
        """The phone on the stand has a token that can push frames and nothing else."""
        return claim_room(claims_from(authorization, "dealer", "camera"))

    def vision_room(req: Request, authorization: str | None = Header(default=None)) -> Room:
        """The card reader: the local one signs in as the dealer, the hosted worker names the room it is serving."""
        c = claims_from(authorization, "dealer", "worker")
        return need_room(req.query_params.get("room")) if c["role"] == "worker" else claim_room(c)

    def worker(authorization: str | None = Header(default=None)) -> dict:
        return claims_from(authorization, "worker")

    def guarded(fn):
        try:
            return fn()
        except TableError as e:
            raise HTTPException(400, str(e)) from e

    # ---------- public ----------
    @app.get("/healthz")
    def healthz():
        return {"ok": True, "rooms": len(rooms.rooms)}

    @app.get("/api/state")
    def state(room: str = MAIN):
        return need_room(room).table.public_state()

    @app.post("/api/rooms")
    def create_room(req: Request):
        """Open a new table. The creator's browser becomes its dealer screen."""
        if not public:
            raise HTTPException(404, "Live tables are not enabled on this server")
        limiter.check("rooms", client(req), 6)
        reap_rooms()
        try:
            rooms.ensure_capacity()
        except RoomsFull as e:
            raise HTTPException(503, "All live tables are busy right now. Try again in a few minutes.") from e
        from vision.areas import HOSTED_AREAS
        rid = rooms.new_id()
        table = Table(shared_chain, buy_in=buy_in, min_conf=float(config.get("AUTO_MIN_CONF", "0.35")), db=shared_db,
                      settle_async=persist, hand_prefix=f"{rid}.")
        room = rooms.add(Room(rid, table, hosted=True, areas={k: list(v) for k, v in HOSTED_AREAS.items()}))
        return {"room": room.id, "dealer_token": auth.issue({"role": "dealer", "room": room.id}, ttl=RoomManager.AGE_LIMIT + 60)}

    # ---------- the pixel-art UI (everyone's phone + the big screen) ----------
    @app.get("/")
    def root():
        return RedirectResponse("/ui/?landing=1" if public else "/ui/")

    @app.get("/play/{seat}")
    def play_page(seat: int, req: Request):
        """Old seat-QR links keep working: /play/2?code=abc -> /ui/?seat=2&code=abc"""
        q = req.url.query
        return RedirectResponse(f"/ui/?seat={seat}" + (f"&{q}" if q else ""))

    @app.get("/table")
    def table_page():
        return RedirectResponse("/ui/?landing=1" if public else "/ui/")

    # ---------- player sign-in ----------
    @app.post("/api/auth/challenge")
    def challenge(body: dict, req: Request):
        limiter.check("challenge", client(req), 30)
        try:
            return auth.challenge(str(body.get("pubkey", "")))
        except AuthError as e:
            raise HTTPException(400, str(e)) from e

    @app.post("/api/auth/login")
    def login(b: LoginBody, req: Request):
        """Verify wallet signature + physical seat code, seat the player, return a session token."""
        limiter.check("login", client(req), 10)
        try:
            auth.verify_login(b.pubkey, b.nonce, b.signature)
        except AuthError as e:
            raise HTTPException(401, str(e)) from e
        room = need_room(b.room or MAIN)
        if rooms.seated_elsewhere(b.pubkey, room.id):
            raise HTTPException(400, "This wallet is already sitting at another table")

        def go():
            room.table.verify_seat_code(b.seat, b.seat_code)
            info = room.table.join(b.seat, b.name, b.pubkey)
            return {"token": auth.issue({"role": "player", "pubkey": b.pubkey, "seat": b.seat, "room": room.id}), **info}
        return guarded(go)

    @app.get("/api/me")
    def me(c: dict = Depends(player)):
        table = claim_room(c).table
        s = table.public_state()
        seat = next((x for x in s["seats"] if x.get("seat") == c["seat"] and x.get("pubkey") == c["pubkey"]), None)
        if seat is None:
            raise HTTPException(404, "You are not seated")
        return {"seat": seat, "to_call": table.to_call_for(c["seat"]), "phase": s["phase"],
                "min_raise_to": s["min_raise_to"], "max_raise_to": s["max_raise_to"], "pot": s["pot"]}

    @app.post("/api/action")
    def action(b: ActionBody, c: dict = Depends(player)):
        # the seat comes from the signed token, never from the request body
        room = claim_room(c)
        room.touch()
        return guarded(lambda: (room.table.act(c["seat"], b.type, b.amount), {"ok": True})[1])

    @app.get("/api/history")
    def history(pubkey: str, req: Request):
        """Past performance for a wallet: stats, balance-per-turn series and recent chip movements (public data)."""
        limiter.check("history", client(req), 60)
        try:
            decode_pubkey(pubkey)
        except AuthError as e:
            raise HTTPException(400, str(e)) from e
        try:
            return shared_db.history(pubkey)
        except Exception:  # noqa: BLE001 - a sick database must not break the page
            return {"enabled": True, "stats": {"hands": 0, "wins": 0, "net": 0, "best": 0, "balance": None},
                    "series": [], "recent": [], "error": "history unavailable"}

    # ---------- real devnet SOL: the wallet pays the table to buy chips, and is paid SOL back when it cashes out ----------
    @app.post("/api/sol/prepare")
    def sol_prepare(body: dict, req: Request):
        """Unsigned buy-in transaction (player -> house) for the player's own wallet to sign."""
        limiter.check("sol", client(req), 30)
        table = need_room(str(body.get("room") or MAIN)).table
        return guarded(lambda: table.prepare_buyin(str(body.get("pubkey", "")), int(body.get("chips", 0) or 0)))

    @app.post("/api/sol/confirm")
    def sol_confirm(body: dict, req: Request):
        """Proof of payment is the on-chain transaction itself, so no login is needed: chips go to the payer's bankroll."""
        limiter.check("sol", client(req), 30)
        table = need_room(str(body.get("room") or MAIN)).table
        return guarded(lambda: table.confirm_buyin(str(body.get("pubkey", "")), str(body.get("signature", ""))))

    @app.get("/api/bankroll")
    def bankroll(pubkey: str, req: Request):
        """Chips this wallet holds on chain (public data): lets the join screen show the bankroll before sitting."""
        limiter.check("bankroll", client(req), 60)
        try:
            decode_pubkey(pubkey)
            return {"chips": int(shared_chain.balance(pubkey))}
        except AuthError as e:
            raise HTTPException(400, str(e)) from e
        except Exception as e:  # noqa: BLE001
            raise HTTPException(503, "chain unavailable") from e

    @app.post("/api/sync")
    def sync(c: dict = Depends(player)):
        return guarded(lambda: claim_room(c).table.sync_stack(c["seat"]))

    @app.post("/api/cashout_sol")
    def cashout_sol(c: dict = Depends(player)):
        return guarded(lambda: claim_room(c).table.cash_out_sol(c["seat"]))

    @app.post("/api/cashout")
    def cashout(c: dict = Depends(player)):
        return guarded(lambda: claim_room(c).table.cash_out(c["seat"]))

    # ---------- dealer ----------
    @app.post("/api/dealer/login")
    def dealer_login(b: PinBody, req: Request):
        if pin is None:
            raise HTTPException(404, "This server has no PIN table")
        who, now = client(req), time.time()
        q = pin_fail[who]
        while q and q[0] < now - 300:
            q.popleft()
        if len(q) >= 5:
            raise HTTPException(429, "Too many wrong PINs; wait 5 minutes")
        if not secrets.compare_digest(b.pin, pin):
            q.append(now)
            raise HTTPException(401, "Wrong PIN")
        return {"token": auth.issue({"role": "dealer", "room": MAIN}, ttl=12 * 3600)}

    @app.get("/api/dealer/state")
    def dealer_state(room: Room = Depends(dealer_room)):
        return {**room.table.public_state(), **room.table.dealer_state()}

    @app.post("/api/dealer/start_hand")
    def start_hand(room: Room = Depends(dealer_room)):
        return guarded(lambda: {"hand": room.table.start_hand()})

    @app.post("/api/dealer/confirm_board")
    def confirm_board(b: CardsBody, room: Room = Depends(dealer_room)):
        return guarded(lambda: (room.table.confirm_board(b.cards), {"ok": True})[1])

    @app.post("/api/dealer/confirm_showdown")
    def confirm_showdown(b: ShowdownBody, room: Room = Depends(dealer_room)):
        return guarded(lambda: (room.table.confirm_showdown(b.reveal), {"ok": True})[1])

    @app.post("/api/dealer/override")
    def override(b: OverrideBody, room: Room = Depends(dealer_room)):
        return guarded(lambda: (room.table.override(b.zone, b.code), {"ok": True})[1])

    @app.post("/api/dealer/abort_hand")
    def abort_hand(room: Room = Depends(dealer_room)):
        return guarded(lambda: (room.table.abort_hand(), {"ok": True})[1])

    @app.post("/api/dealer/kick")
    def kick(b: KickBody, room: Room = Depends(dealer_room)):
        return guarded(lambda: room.table.kick(b.seat))

    @app.post("/api/dealer/next_hand")
    def next_hand(room: Room = Depends(dealer_room)):
        return guarded(lambda: (room.table.next_hand(), {"ok": True})[1])

    @app.post("/api/dealer/retry_settlement")
    def retry_settlement(room: Room = Depends(dealer_room)):
        return guarded(lambda: (room.table.retry_settlement(), {"ok": True})[1])

    @app.post("/api/dealer/manual_mode")
    def manual_mode(body: dict, room: Room = Depends(dealer_room)):
        room.table.manual_mode = bool(body.get("on"))
        room.table._bump()
        return {"manual_mode": room.table.manual_mode}

    @app.post("/api/dealer/start_round")
    def start_round(room: Room = Depends(dealer_room)):
        """START ROUND?: only when 2+ players have chips and the camera sees a clear board."""
        room.touch()
        return guarded(lambda: {"hand": room.table.request_start()})

    @app.post("/api/dealer/add_bot")
    def add_bot(room: Room = Depends(dealer_room)):
        """Only if the dealer asks: no table ever starts with a bot."""
        if not room.hosted:
            raise HTTPException(404, "Run tools/bot_player.py for a bot at the local table")
        room.touch()
        key = rooms.lease_bot(room.id)
        if key is None:
            raise HTTPException(503, "No bots are free right now")
        return guarded(lambda: room.table.add_bot(key))

    # ---------- vision + phone camera ----------
    async def read_jpeg(req: Request) -> bytes:
        if int(req.headers.get("content-length") or 0) > MAX_JPG:      # refuse before reading the body
            raise HTTPException(413, "expected a JPEG (<= 4 MB)")
        data = await req.body()
        if not data or len(data) > MAX_JPG or data[:2] != b"\xff\xd8":
            raise HTTPException(400, "expected a JPEG (<= 4 MB)")
        return data

    @app.post("/api/vision/update")
    def vision_update(body: dict, room: Room = Depends(vision_room)):
        room.table.on_vision(body)
        return {"ok": True, "read_seats": room.table.seats_to_read(), "preview": time.time() < room.preview_until}

    @app.post("/api/vision/frame")
    async def vision_frame_put(req: Request, room: Room = Depends(vision_room)):
        room.vframe = {"jpg": await read_jpeg(req), "ts": time.time()}
        return {"ok": True}

    @app.get("/api/vision/frame.jpg")
    def vision_frame_get(room: Room = Depends(dealer_room)):
        room.preview_until = time.time() + 3
        if not room.vframe["jpg"]:
            raise HTTPException(404, "no frame yet")
        return Response(room.vframe["jpg"], media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    @app.post("/api/cam/frame")
    async def cam_frame_put(req: Request, room: Room = Depends(camera_room)):
        jpg = await read_jpeg(req)
        seq = float(req.headers.get("x-seq", "0") or 0)
        now = time.time()
        if (seq and seq < room.cam_seq) or now - room.cam["ts"] < MIN_FRAME_GAP:   # overtaken by a newer upload, or flooding: drop it
            return {"ok": True, "dropped": True}
        room.cam_seq = seq
        room.cam = {"jpg": jpg, "ts": now}
        room.touch()
        return {"ok": True}

    @app.get("/api/cam/latest.jpg")
    async def cam_frame_get(after: float = 0.0, room: Room = Depends(vision_room)):
        """Latest phone frame. With ?after=<ts> it waits (up to 1 s) for a NEWER frame, so the reader gets frames
        the instant they arrive instead of polling on a timer. 204 = nothing newer yet."""
        deadline = time.time() + 1.0
        while not room.cam["jpg"] or (after and room.cam["ts"] <= after):
            if time.time() > deadline:
                if not room.cam["jpg"]:
                    raise HTTPException(404, "no camera frame yet")
                return Response(status_code=204)
            await asyncio.sleep(0.01)
        return Response(room.cam["jpg"], media_type="image/jpeg",
                        headers={"x-frame-ts": str(room.cam["ts"]), "Cache-Control": "no-store"})

    @app.get("/api/worker/rooms")
    def worker_rooms(_: dict = Depends(worker)):
        now = time.time()
        return {"rooms": [{"room": r.id, "frame_ts": r.cam["ts"], "age": now - r.cam["ts"], "areas": r.areas,
                           "read_seats": r.table.seats_to_read(), "preview": now < r.preview_until}
                          for r in rooms.hosted_rooms() if r.cam["jpg"]]}

    def phone_url(base: str, path: str, room: Room, query: str = "", camera: bool = False) -> str:
        url = f"{base}{path}"
        q = [query] if query else []
        if room.hosted:
            q.append(f"room={room.id}")
        url += ("?" + "&".join(q)) if q else ""
        if camera and room.hosted:                                  # in the fragment: never sent to a server or logged
            url += "#t=" + auth.issue({"role": "camera", "room": room.id}, ttl=RoomManager.AGE_LIMIT + 60)
        return url

    @app.get("/api/dealer/cam_qr.png")
    def cam_qr(req: Request, room: Room = Depends(dealer_room)):
        """QR that opens the camera page (/cam) on the phone that will read the cards."""
        url = phone_url(public_base(req), "/cam", room, camera=True)
        return Response(qr_png(url), media_type="image/png", headers={"Cache-Control": "no-store", "x-join-url": url})

    @app.get("/api/vision/areas")
    def areas_get(room: Room = Depends(vision_room)):
        from vision import areas as va
        return {"areas": room.areas if room.hosted else va.load(), "names": va.NAMES}

    @app.post("/api/vision/areas")
    def areas_put(body: dict, room: Room = Depends(dealer_room)):
        """Save the drawn play areas (fractions of the camera image). The reader picks them up live."""
        from vision import areas as va
        try:
            if room.hosted:
                room.areas = va.validate(body.get("areas") or {})
                return {"areas": room.areas}
            return {"areas": va.save(body.get("areas") or {})}
        except ValueError as e:
            raise HTTPException(400, str(e)) from e

    @app.get("/areas")
    def areas_page():
        return FileResponse(STATIC / "areas.html")

    @app.get("/api/dealer/seat_qr/{seat}.png")
    def seat_qr(seat: int, req: Request, room: Room = Depends(dealer_room)):
        """QR for the seat's join link, built from the host the dealer is using (LAN IP or tunnel)."""
        table = room.table
        if seat not in table.seat_codes:
            raise HTTPException(404, "no such seat")
        url = phone_url(public_base(req), f"/play/{seat}", room, query=f"code={table.seat_codes[seat]}")
        return Response(qr_png(url, box_size=10), media_type="image/png", headers={"Cache-Control": "no-store", "x-join-url": url})

    @app.get("/dealer")
    def dealer_page():
        return RedirectResponse("/ui/?landing=1" if public else "/ui/?dealer=1")

    @app.get("/cam")
    def cam_page():
        return FileResponse(STATIC / "cam.html")

    @app.middleware("http")
    async def no_store(request: Request, call_next):
        resp = await call_next(request)
        if request.url.path.startswith(("/ui", "/dealer", "/areas", "/cam")):
            resp.headers["Cache-Control"] = "no-store"          # always serve the latest UI
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "no-referrer"         # seat codes are in URLs: never leak them to other sites
        return resp

    app.mount("/ui", StaticFiles(directory=str(WEB), html=True), name="ui")

    # ---------- websockets ----------
    @app.websocket("/ws/table")
    async def ws_table(ws: WebSocket):
        await ws.accept()
        room = rooms.get(ws.query_params.get("room"))
        if room is None:
            await ws.close(code=4404)                           # the browser shows "this table has closed"
            return
        if room.ws_count >= MAX_WS_PER_ROOM or sum(r.ws_count for r in rooms.all()) >= MAX_WS_TOTAL:
            await ws.close(code=4429)
            return
        room.ws_count += 1
        last = 0
        try:
            while not room.closed:
                if room.snap_seq != last:                       # only this socket waits on a slow phone
                    last = room.snap_seq
                    await ws.send_text(room.snap_text)
                await asyncio.sleep(0.03)
            await ws.close(code=4404)
        except (WebSocketDisconnect, RuntimeError):
            return
        finally:
            room.ws_count -= 1

    return app


app = create_app(persist=True) if os.environ.get("AUTOPOKER_NO_AUTOSTART") != "1" else None  # type: ignore[assignment]
