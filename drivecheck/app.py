"""Authenticated HTTP API and SSE dashboard delivery."""

import asyncio
import fcntl
import json
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from drivecheck import notifications
from drivecheck.config import Config, Settings
from drivecheck.engine import Engine
from drivecheck.hardware import SafetyError
from drivecheck.storage import Store

STATIC = Path(__file__).parent / "static"


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Login(Input):
    token: str = Field(max_length=512)


class RunInput(Input):
    drive_id: str = Field(max_length=200)
    profile: Literal["quick", "extended", "verify"] = "extended"
    confirmation: str = Field(default="", max_length=300)


class NotificationInput(Input):
    provider: Literal["none", "discord", "telegram"] | None = None
    enabled: bool | None = None
    discord_webhook: str | None = Field(default=None, max_length=512)
    telegram_token: str | None = Field(default=None, max_length=256)
    telegram_chat_id: str | None = Field(default=None, max_length=100)
    notify_started: bool | None = None
    clear_discord: bool | None = None
    clear_telegram: bool | None = None


class SettingsInput(Input):
    auto_test: bool | None = None
    notifications: NotificationInput | None = None


def create_app(config: Config | None = None, hardware=None) -> FastAPI:
    config = config or Config.from_env()
    config.prepare()
    sessions: dict[str, float] = {}
    attempts: dict[str, list[float]] = {}

    @asynccontextmanager
    async def lifespan(app):
        lock = (config.data_dir / "station.lock").open("a")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock.close()
            raise RuntimeError(
                "Another DriveCheck process is using this data directory. Use one worker."
            ) from None
        store = Store(config.data_dir / "drivecheck.sqlite3")
        engine = Engine(config, Settings(config), store, hardware)
        app.state.engine = engine
        try:
            await engine.start()
            yield
        finally:
            await engine.stop()
            store.close()
            fcntl.flock(lock, fcntl.LOCK_UN)
            lock.close()

    app = FastAPI(
        title="DriveCheck",
        version="0.1.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.config = config

    @app.exception_handler(RequestValidationError)
    async def bad_input(request, error):
        # Pydantic's default error includes input values (potential credentials).
        return JSONResponse(status_code=422, content={"detail": "Invalid request fields or types"})

    @app.middleware("http")
    async def guard(request: Request, call_next):
        origin = request.headers.get("origin")
        expected = config.public_origin or str(request.base_url).rstrip("/")
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            if origin and origin.rstrip("/") != expected:
                return JSONResponse(
                    status_code=403, content={"detail": "Cross-origin changes are blocked"}
                )
            if (
                request.cookies.get("drivecheck_session")
                and not origin
                and not request.headers.get("authorization")
            ):
                return JSONResponse(
                    status_code=403, content={"detail": "A same-origin request is required"}
                )
        length = request.headers.get("content-length", "0")
        try:
            too_large = int(length) > 16384
        except ValueError:
            too_large = True
        if too_large:
            return JSONResponse(status_code=413, content={"detail": "Request too large"})
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        )
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    def authenticated(request: Request):
        authorization = request.headers.get("authorization", "")
        if authorization:
            if authorization.startswith("Bearer ") and secrets.compare_digest(
                authorization[7:], config.api_key
            ):
                return True
            raise HTTPException(401, "Invalid API bearer token")
        session = request.cookies.get("drivecheck_session", "")
        if sessions.get(session, 0) > time.time():
            return True
        raise HTTPException(401, "Sign in with the station access token")

    def engine() -> Engine:
        return app.state.engine

    @app.get("/api/health")
    async def health():
        return {"status": "ok", "mode": "demo" if config.demo else "hardware", "version": "0.1.0"}

    @app.post("/api/login")
    async def login(body: Login, request: Request):
        address = request.client.host if request.client else "unknown"
        timestamp = time.time()
        failures = [value for value in attempts.get(address, []) if value > timestamp - 60]
        # Bound bookkeeping even if many clients make unauthenticated requests.
        if len(attempts) > 1000:
            attempts.clear()
        attempts[address] = failures
        if len(failures) >= 5:
            raise HTTPException(429, "Too many attempts. Wait one minute and try again.")
        if not secrets.compare_digest(body.token.encode(), config.api_key.encode()):
            failures.append(timestamp)
            raise HTTPException(401, "Incorrect station access token")
        attempts.pop(address, None)
        for key, expiry in list(sessions.items()):
            if expiry <= timestamp:
                sessions.pop(key, None)
        if len(sessions) >= 100:
            sessions.pop(next(iter(sessions)))
        session = secrets.token_urlsafe(32)
        sessions[session] = timestamp + 12 * 3600
        response = JSONResponse({"ok": True})
        response.set_cookie(
            "drivecheck_session",
            session,
            max_age=12 * 3600,
            httponly=True,
            secure=config.secure_cookie,
            samesite="strict",
            path="/api",
        )
        return response

    @app.post("/api/logout", dependencies=[Depends(authenticated)])
    async def logout(request: Request):
        sessions.pop(request.cookies.get("drivecheck_session", ""), None)
        response = JSONResponse({"ok": True})
        response.delete_cookie("drivecheck_session", path="/api")
        return response

    @app.get("/api/state", dependencies=[Depends(authenticated)])
    async def state():
        return engine().state()

    @app.get("/api/events", dependencies=[Depends(authenticated)])
    async def events(request: Request):
        async def stream():
            queue: asyncio.Queue = asyncio.Queue(maxsize=1)
            instance = engine()
            instance.subscribers.add(queue)
            try:
                yield "event: state\ndata: " + json.dumps(instance.state()) + "\n\n"
                while not await request.is_disconnected():
                    try:
                        await asyncio.wait_for(queue.get(), 5)
                        authenticated(request)
                        yield "event: state\ndata: " + json.dumps(instance.state()) + "\n\n"
                    except TimeoutError:
                        authenticated(request)
                        yield ": heartbeat\n\n"
                    except HTTPException:
                        yield "event: expired\ndata: {}\n\n"
                        return
            finally:
                instance.subscribers.discard(queue)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"X-Accel-Buffering": "no", "Cache-Control": "no-store"},
        )

    @app.post("/api/scan", dependencies=[Depends(authenticated)])
    async def scan():
        await engine().scan()
        return engine().state()

    @app.post("/api/runs", dependencies=[Depends(authenticated)])
    async def start_run(body: RunInput):
        try:
            return await engine().enqueue(body.drive_id, body.profile, body.confirmation)
        except (ValueError, SafetyError) as error:
            raise HTTPException(409, str(error)) from None

    @app.post("/api/runs/{run_id}/cancel", dependencies=[Depends(authenticated)])
    async def cancel_run(run_id: str):
        try:
            await engine().cancel(run_id)
            return {"ok": True}
        except ValueError as error:
            raise HTTPException(404, str(error)) from None

    def get_run(run_id):
        run = engine().store.get(run_id)
        if run is None:
            raise HTTPException(404, "Test not found")
        return run

    @app.get("/api/runs/{run_id}/report", dependencies=[Depends(authenticated)])
    async def report(run_id: str):
        run = get_run(run_id)
        return JSONResponse(
            {"schema_version": 1, "simulated": config.demo, **run},
            headers={"Content-Disposition": f'attachment; filename="drivecheck-{run["id"]}.json"'},
        )

    @app.get("/api/runs/{run_id}", dependencies=[Depends(authenticated)])
    async def read_run(run_id: str):
        return get_run(run_id)

    @app.put("/api/settings", dependencies=[Depends(authenticated)])
    async def settings(body: SettingsInput):
        patch = body.model_dump(exclude_none=True)
        try:
            result = engine().settings.update(patch)
        except ValueError as error:
            raise HTTPException(422, str(error)) from None
        engine().publish()
        return result

    @app.post("/api/notifications/test", dependencies=[Depends(authenticated)])
    async def notification_test():
        try:
            await notifications.send(
                engine().settings.value["notifications"],
                ("[Simulation] " if config.demo else "")
                + "DriveCheck test message: notifications are configured.",
            )
            engine().notification_error = None
            engine().publish()
            return {"ok": True}
        except (notifications.NotificationError, ValueError) as error:
            raise HTTPException(502, str(error)) from None

    @app.get("/")
    async def index():
        return FileResponse(STATIC / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app
