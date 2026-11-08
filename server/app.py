"""FastAPI app: REST + WebSockets for phones, the dealer console, the big screen and the vision service.

Run:  python -m uvicorn server.app:app --host 0.0.0.0 --port 8000
Env:  see tools/init_env.py. USE_MOCK_CHAIN=1 runs with an in-memory ledger (no Solana).
"""
from __future__ import annotations

import asyncio
import os
import secrets
import time
from collections import defaultdict, deque
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from server import config
from server.auth import Auth, AuthError
from server.chain_client import Chain, HttpChain, MockChain
from server.table import Table, TableError

STATIC = Path(__file__).with_name("static")
WEB = Path(__file__).resolve().parent.parent / "web"
ROOT_DIR = WEB.parent


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


class LoginBody(BaseModel):
    pubkey: str
    nonce: str
    signature: str
    seat: int
    seat_code: str
    name: str = Field(default="", max_length=20)


class ActionBody(BaseModel):
    type: str
    amount: int | None = None


class PinBody(BaseModel):
    pin: str


class CardsBody(BaseModel):
    cards: list[str] | None = None


class ShowdownBody(BaseModel):
    reveal: dict[int, str] | None = None


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


def create_app(chain: Chain | None = None, session_secret: str | None = None, dealer_pin: str | None = None,
               persist: bool = False) -> FastAPI:
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def lifespan(app_: FastAPI):
        """Once a second: auto-start hands, deal the next one, retry failed payouts (no dealer clicks needed)."""
        async def loop():
            while True:
                try:
                    await asyncio.to_thread(app_.state.table.tick)
                except Exception as e:  # noqa: BLE001 - a bad tick must never stop the loop
                    print("tick error:", e)
                await asyncio.sleep(1)
        task = asyncio.create_task(loop())
        yield
        task.cancel()

    app = FastAPI(title="AutoPoker server", lifespan=lifespan)
    auth = Auth(session_secret or config.get("SESSION_SECRET"))
    pin = dealer_pin or config.get("DEALER_PIN")
    table = Table(chain or make_chain(), buy_in=int(config.get("BUY_IN", "1000")),
                  min_conf=float(config.get("AUTO_MIN_CONF", "0.35")),
                  state_path=(ROOT_DIR / "data" / "table_state.json") if persist else None)
    table.restore()
    limiter = Limiter()
    pin_fail: dict[str, deque] = defaultdict(deque)
    app.state.table, app.state.auth = table, auth

    app.add_middleware(
        CORSMiddleware, allow_credentials=False, allow_methods=["*"], allow_headers=["*"],
        # localhost + private LAN ranges only (the friend's dashboard dev server, phones on the LAN)
        allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1|192\.168\.\d+\.\d+|10\.\d+\.\d+\.\d+|172\.(1[6-9]|2\d|3[01])\.\d+\.\d+|[\w-]+\.trycloudflare\.com)(:\d+)?$",
    )

    def client(req: Request) -> str:
        return req.client.host if req.client else "?"

    def claims_from(authorization: str | None, role: str) -> dict:
        tok = authorization[7:] if authorization and authorization.startswith("Bearer ") else None
        try:
            c = auth.check(tok)
        except AuthError as e:
            raise HTTPException(401, str(e)) from e
        if c.get("role") != role:
            raise HTTPException(403, f"{role} access required")
        return c

    def player(authorization: str | None = Header(default=None)) -> dict:
        return claims_from(authorization, "player")

    def dealer(authorization: str | None = Header(default=None)) -> dict:
        return claims_from(authorization, "dealer")

    def guarded(fn):
        try:
            return fn()
        except TableError as e:
            raise HTTPException(400, str(e)) from e

    # ---------- public ----------
    @app.get("/api/state")
    def state():
        return table.public_state()

    # ---------- the pixel-art UI (everyone's phone + the big screen) ----------
    @app.get("/")
    def root():
        return RedirectResponse("/ui/")

    @app.get("/play/{seat}")
    def play_page(seat: int, req: Request):
        """Old seat-QR links keep working: /play/2?code=abc -> /ui/?seat=2&code=abc"""
        q = req.url.query
        return RedirectResponse(f"/ui/?seat={seat}" + (f"&{q}" if q else ""))

    @app.get("/table")
    def table_page():
        return RedirectResponse("/ui/")

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

        def go():
            table.verify_seat_code(b.seat, b.seat_code)
            info = table.join(b.seat, b.name, b.pubkey)
            return {"token": auth.issue({"role": "player", "pubkey": b.pubkey, "seat": b.seat}), **info}
        return guarded(go)

    @app.get("/api/me")
    def me(c: dict = Depends(player)):
        s = table.public_state()
        seat = next((x for x in s["seats"] if x.get("seat") == c["seat"] and x.get("pubkey") == c["pubkey"]), None)
        if seat is None:
            raise HTTPException(404, "You are not seated")
        return {"seat": seat, "to_call": table.to_call_for(c["seat"]), "phase": s["phase"],
                "min_raise_to": s["min_raise_to"], "max_raise_to": s["max_raise_to"], "pot": s["pot"]}

    @app.post("/api/action")
    def action(b: ActionBody, c: dict = Depends(player)):
        # the seat comes from the signed token, never from the request body
        return guarded(lambda: (table.act(c["seat"], b.type, b.amount), {"ok": True})[1])

    @app.post("/api/cashout")
    def cashout(c: dict = Depends(player)):
        return guarded(lambda: table.cash_out(c["seat"]))

    @app.post("/api/rebuy")
    def rebuy(c: dict = Depends(player)):
        return guarded(lambda: table.rebuy(c["seat"]))

    # ---------- dealer ----------
    @app.post("/api/dealer/login")
    def dealer_login(b: PinBody, req: Request):
        who, now = client(req), time.time()
        q = pin_fail[who]
        while q and q[0] < now - 300:
            q.popleft()
        if len(q) >= 5:
            raise HTTPException(429, "Too many wrong PINs; wait 5 minutes")
        if not secrets.compare_digest(b.pin, pin):
            q.append(now)
            raise HTTPException(401, "Wrong PIN")
        return {"token": auth.issue({"role": "dealer"}, ttl=12 * 3600)}

    @app.get("/api/dealer/state")
    def dealer_state(_: dict = Depends(dealer)):
        return {**table.public_state(), **table.dealer_state()}

    @app.post("/api/dealer/start_hand")
    def start_hand(_: dict = Depends(dealer)):
        return guarded(lambda: {"hand": table.start_hand()})

    @app.post("/api/dealer/confirm_board")
    def confirm_board(b: CardsBody, _: dict = Depends(dealer)):
        return guarded(lambda: (table.confirm_board(b.cards), {"ok": True})[1])

    @app.post("/api/dealer/confirm_showdown")
    def confirm_showdown(b: ShowdownBody, _: dict = Depends(dealer)):
        return guarded(lambda: (table.confirm_showdown(b.reveal), {"ok": True})[1])

    @app.post("/api/dealer/override")
    def override(b: OverrideBody, _: dict = Depends(dealer)):
        return guarded(lambda: (table.override(b.zone, b.code), {"ok": True})[1])

    @app.post("/api/dealer/abort_hand")
    def abort_hand(_: dict = Depends(dealer)):
        return guarded(lambda: (table.abort_hand(), {"ok": True})[1])

    @app.post("/api/dealer/next_hand")
    def next_hand(_: dict = Depends(dealer)):
        return guarded(lambda: (table.next_hand(), {"ok": True})[1])

    @app.post("/api/dealer/retry_settlement")
    def retry_settlement(_: dict = Depends(dealer)):
        return guarded(lambda: (table.retry_settlement(), {"ok": True})[1])

    @app.post("/api/dealer/manual_mode")
    def manual_mode(body: dict, _: dict = Depends(dealer)):
        table.manual_mode = bool(body.get("on"))
        table._bump()
        return {"manual_mode": table.manual_mode}

    @app.post("/api/dealer/start_round")
    def start_round(_: dict = Depends(dealer)):
        """START ROUND?: only when 2+ players have chips and the camera sees a clear board."""
        return guarded(lambda: {"hand": table.request_start()})

    # ---------- vision + phone camera (all dealer-token protected) ----------
    app.state.cam = {"jpg": b"", "ts": 0.0}
    app.state.vframe = {"jpg": b"", "ts": 0.0}
    MAX_JPG = 4_000_000

    async def read_jpeg(req: Request) -> bytes:
        data = await req.body()
        if not data or len(data) > MAX_JPG or data[:2] != b"\xff\xd8":
            raise HTTPException(400, "expected a JPEG (<= 4 MB)")
        return data

    @app.post("/api/vision/update")
    def vision_update(body: dict, _: dict = Depends(dealer)):
        table.on_vision(body)
        return {"ok": True}

    @app.post("/api/vision/frame")
    async def vision_frame_put(req: Request, _: dict = Depends(dealer)):
        app.state.vframe = {"jpg": await read_jpeg(req), "ts": time.time()}
        return {"ok": True}

    @app.get("/api/vision/frame.jpg")
    def vision_frame_get(_: dict = Depends(dealer)):
        from fastapi.responses import Response
        if not app.state.vframe["jpg"]:
            raise HTTPException(404, "no frame yet")
        return Response(app.state.vframe["jpg"], media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    @app.post("/api/cam/frame")
    async def cam_frame_put(req: Request, _: dict = Depends(dealer)):
        app.state.cam = {"jpg": await read_jpeg(req), "ts": time.time()}
        return {"ok": True}

    @app.get("/api/cam/latest.jpg")
    def cam_frame_get(_: dict = Depends(dealer)):
        from fastapi.responses import Response
        if not app.state.cam["jpg"]:
            raise HTTPException(404, "no camera frame yet")
        return Response(app.state.cam["jpg"], media_type="image/jpeg",
                        headers={"x-frame-ts": str(app.state.cam["ts"]), "Cache-Control": "no-store"})

    @app.get("/api/vision/areas")
    def areas_get(_: dict = Depends(dealer)):
        from vision import areas as va
        return {"areas": va.load(), "names": va.NAMES}

    @app.post("/api/vision/areas")
    def areas_put(body: dict, _: dict = Depends(dealer)):
        """Save the drawn play areas (fractions of the camera image). The vision service reloads them live."""
        from vision import areas as va
        try:
            return {"areas": va.save(body.get("areas") or {})}
        except ValueError as e:
            raise HTTPException(400, str(e)) from e

    @app.get("/areas")
    def areas_page():
        return FileResponse(STATIC / "areas.html")

    @app.get("/api/dealer/seat_qr/{seat}.png")
    def seat_qr(seat: int, req: Request, _: dict = Depends(dealer)):
        """QR for the seat's join link, built from the host the dealer is using (LAN IP or tunnel)."""
        import io

        import qrcode
        from fastapi.responses import Response
        if seat not in table.seat_codes:
            raise HTTPException(404, "no such seat")
        url = f"{public_base(req)}/play/{seat}?code={table.seat_codes[seat]}"
        buf = io.BytesIO()
        qrcode.make(url).save(buf, format="PNG")
        return Response(buf.getvalue(), media_type="image/png", headers={"Cache-Control": "no-store", "x-join-url": url})

    @app.get("/dealer")
    def dealer_page():
        return RedirectResponse("/ui/?dealer=1")

    @app.get("/cam")
    def cam_page():
        return FileResponse(STATIC / "cam.html")

    @app.middleware("http")
    async def no_store(request: Request, call_next):
        resp = await call_next(request)
        if request.url.path.startswith(("/ui", "/dealer", "/areas", "/cam")):
            resp.headers["Cache-Control"] = "no-store"          # always serve the latest UI while we iterate
        return resp

    app.mount("/ui", StaticFiles(directory=str(WEB), html=True), name="ui")

    # ---------- websockets ----------
    @app.websocket("/ws/table")
    async def ws_table(ws: WebSocket):
        await ws.accept()
        last = -1
        try:
            while True:
                if table.version != last:
                    last = table.version
                    await ws.send_json(table.public_state())
                await asyncio.sleep(0.2)
        except (WebSocketDisconnect, RuntimeError):
            return

    @app.websocket("/ws/vision")
    async def ws_vision(ws: WebSocket):
        """Vision service pushes section-3 messages. Requires a dealer token: ?token=..."""
        try:
            c = auth.check(ws.query_params.get("token"))
            if c.get("role") != "dealer":
                raise AuthError("dealer token required")
        except AuthError:
            await ws.close(code=4401)
            return
        await ws.accept()
        try:
            while True:
                table.on_vision(await ws.receive_json())
        except (WebSocketDisconnect, RuntimeError):
            return

    return app


app = create_app(persist=True) if os.environ.get("AUTOPOKER_NO_AUTOSTART") != "1" else None  # type: ignore[assignment]
