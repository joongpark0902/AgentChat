"""로컬 웹서버: FastAPI + WebSocket. 실행: python -m app.server"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from urllib.parse import quote

from .auth import COOKIE, MAX_FAILS, SESSION_DAYS, Auth
from .config import CONFIG_DIR, ROOT, editable_config, find_codex_exe, load_config, save_editable_config
from .orchestrator import Manager, is_temp_file
from .preview import preview
from .push import Notifier, Push
from .stt import Transcriber, build_prompt

WEB_DIR = ROOT / "web"
AVATAR_DIR = CONFIG_DIR / "avatars"
PRIVATE_DIR = ROOT / "private"   # 비밀번호 해시·세션 키·알림 키/구독(백업 zip·git 에서 제외)
SKIP_DIRS = {"__pycache__", ".git", "node_modules", ".venv", "venv", ".idea", ".vscode"}
UPLOAD_LIMIT = 50 * 1024 * 1024
# 로그인 전에도 열리는 것: 로그인 화면, 홈 화면 설치(manifest·아이콘), 서비스 워커
PUBLIC_PATHS = {"/api/login", "/api/auth/status", "/manifest.webmanifest", "/sw.js"}
PUBLIC_PREFIXES = ("/static/icons/", "/static/login.")


class Hub:
    def __init__(self):
        self.clients: set[WebSocket] = set()
        self.presence: dict[WebSocket, dict] = {}   # 기기별 알림 구독 주소·화면을 보고 있는지
        self.observers: list = []

    async def broadcast(self, payload: dict) -> None:
        data = json.dumps(payload, ensure_ascii=False)
        for ws in list(self.clients):
            try:
                await ws.send_text(data)
            except Exception:
                self.clients.discard(ws)
                self.presence.pop(ws, None)
        for fn in self.observers:
            await fn(payload)

    def visible_endpoints(self) -> set[str]:
        """지금 화면을 켜 두고 보고 있는 기기 → 푸시를 보내지 않는다(화면 안에서 바로 보임)."""
        return {p["endpoint"] for ws, p in self.presence.items()
                if ws in self.clients and p.get("visible") and p.get("endpoint")}


def _safe_name(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|]+', "_", os.path.basename(name or "file")).strip() or "file"
    return name[:120]


def _unique(path: Path) -> Path:
    if not path.exists():
        return path
    stem, suf, n = path.stem, path.suffix, 2
    while (path.parent / f"{stem}_v{n}{suf}").exists():
        n += 1
    return path.parent / f"{stem}_v{n}{suf}"


def _inside(base: Path, rel: str) -> Path:
    p = (base / rel).resolve()
    if base.resolve() not in (p, *p.parents):
        raise HTTPException(400, "작업 폴더 밖의 경로입니다")
    return p


def _thumbnail(src: Path, key: str) -> str:
    """사진을 화면용 320px JPG 로 줄인다. PIL 이 없으면 원본을 그대로 쓴다."""
    try:
        from PIL import Image
    except ImportError:
        return src.name
    dst = _unique(AVATAR_DIR / f"{key}_{time.strftime('%Y%m%d_%H%M%S')}_320.jpg")
    im = Image.open(src).convert("RGB")
    w, h = im.size
    s = min(w, h)  # 가운데 정사각형으로 자른 뒤 축소
    im = im.crop(((w - s) // 2, (h - s) // 2, (w - s) // 2 + s, (h - s) // 2 + s))
    im.thumbnail((320, 320))
    im.save(dst, quality=88)
    return dst.name


def pick_folder_dialog(initial: str | None = None) -> str | None:
    """이 PC 에 폴더 선택 창을 띄운다(로컬 서버라 가능)."""
    import tkinter
    from tkinter import filedialog

    root = tkinter.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        path = filedialog.askdirectory(title="작업 폴더 선택", initialdir=initial or str(Path.home() / "Desktop"))
    finally:
        root.destroy()
    return str(Path(path)) if path else None


def create_app(manager_factory=None, transcriber=None, private_dir: Path | None = None) -> FastAPI:
    hub = Hub()
    state: dict = {}
    stt = transcriber or Transcriber()
    private_dir = Path(private_dir or PRIVATE_DIR)
    auth = Auth(private_dir / "auth.json", remote_port=8790)   # 원격 포트는 설정을 읽은 뒤 맞춘다
    push = Push(private_dir)
    notifier = Notifier(push, lambda: mgr().cfg, hub.visible_endpoints)
    hub.observers.append(notifier.observe)

    def mgr() -> Manager:
        return state["manager"]

    def foreign_origin(conn) -> bool:
        """PC 포트에 다른 웹사이트가 보낸 요청인지. 브라우저는 다른 사이트에서 보낸 요청에 Origin 을 붙이므로,
        그 값이 이 화면 주소(Host)와 다르면 막는다. 폰 포트는 SameSite=Strict 로그인 쿠키가 같은 역할을 한다."""
        origin = conn.headers.get("origin")
        if not origin or not auth.is_local(conn.scope):
            return False
        from urllib.parse import urlsplit
        return urlsplit(origin).netloc.lower() != (conn.headers.get("host") or "").lower()

    def require_local(request: Request) -> None:
        if not auth.is_local(request.scope):
            raise HTTPException(403, "PC 화면에서만 할 수 있습니다")

    def room_or_404(conv_id: str):
        room = mgr().get(conv_id)
        if not room:
            raise HTTPException(404, "대화를 찾을 수 없습니다")
        return room

    async def push_config() -> None:
        await hub.broadcast({"type": "config", "config": mgr().cfg.public()})

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        cfg = load_config()
        auth.remote_port = cfg.settings.remote_port
        push.subject = cfg.settings.push_subject or push.subject
        state["manager"] = manager_factory(cfg, hub.broadcast) if manager_factory else Manager(cfg, hub.broadcast)
        yield
        await state["manager"].stop_all()

    app = FastAPI(lifespan=lifespan)
    AVATAR_DIR.mkdir(parents=True, exist_ok=True)
    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")
    app.mount("/avatars", StaticFiles(directory=AVATAR_DIR), name="avatars")

    @app.middleware("http")
    async def guard_and_no_cache(request, call_next):
        """① 폰(원격 포트)은 로그인해야 들어온다. ② 화면 파일은 항상 새로 받게
        (9/29: 서버를 새 코드로 켜도 브라우저가 예전 화면을 쓰는지 의심됐음)."""
        path = request.url.path
        if request.method not in ("GET", "HEAD", "OPTIONS") and foreign_origin(request):
            return JSONResponse({"detail": "다른 사이트에서 보낸 요청은 받지 않습니다"}, status_code=403)
        if not auth.authorized(request.scope, request.cookies) and path not in PUBLIC_PATHS \
                and not path.startswith(PUBLIC_PREFIXES):
            if path == "/":
                resp = FileResponse(WEB_DIR / "login.html")
            elif path.startswith("/api/"):
                resp = JSONResponse({"detail": "로그인이 필요합니다"}, status_code=401)
            else:
                resp = PlainTextResponse("로그인이 필요합니다", status_code=401)
        else:
            resp = await call_next(request)
        if path == "/" or path.startswith("/static/") or path in ("/sw.js", "/manifest.webmanifest"):
            resp.headers["Cache-Control"] = "no-cache"
        return resp

    @app.get("/")
    async def index():
        return FileResponse(WEB_DIR / "index.html")

    # ------------------------------------------------------------ 홈 화면 설치(PWA)
    @app.get("/manifest.webmanifest")
    async def manifest():
        return FileResponse(WEB_DIR / "manifest.webmanifest", media_type="application/manifest+json")

    @app.get("/sw.js")
    async def service_worker():
        # 서비스 워커는 사이트 맨 위(/)에 있어야 화면 전체를 맡을 수 있다
        return FileResponse(WEB_DIR / "sw.js", media_type="text/javascript")

    # ------------------------------------------------------------ 로그인(폰)
    def _status(request: Request) -> dict:
        return {"password_set": auth.password_set, "local": auth.is_local(request.scope),
                "logged_in": auth.authorized(request.scope, request.cookies), "locked_sec": auth.lock_left(),
                "session_days": SESSION_DAYS}

    @app.get("/api/auth/status")
    async def auth_status(request: Request):
        return _status(request)

    @app.post("/api/login")
    async def login(request: Request, body: dict = Body(...)):
        if auth.is_local(request.scope):
            return {"ok": True}
        if not auth.password_set:
            raise HTTPException(400, "아직 비밀번호가 없습니다. PC 화면의 설정 → 모바일·알림에서 먼저 정해 주세요.")
        if auth.lock_left():
            raise HTTPException(429, f"여러 번 틀려 잠겼습니다. {auth.lock_left()}초 뒤에 다시 해 주세요.")
        if not await asyncio.to_thread(auth.verify, str(body.get("password", ""))):
            if auth.lock_left():
                raise HTTPException(429, f"{MAX_FAILS}번 틀려 {auth.lock_left() // 60}분 동안 잠갔습니다.")
            raise HTTPException(401, "비밀번호가 틀렸습니다")
        resp = JSONResponse({"ok": True})
        resp.set_cookie(COOKIE, auth.issue(), max_age=SESSION_DAYS * 86400, httponly=True, secure=True,
                        samesite="strict", path="/")
        return resp

    @app.post("/api/logout")
    async def logout():
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(COOKIE, path="/", secure=True, httponly=True, samesite="strict")
        return resp

    @app.post("/api/auth/password")
    async def set_password(request: Request, body: dict = Body(...)):
        require_local(request)
        try:
            await asyncio.to_thread(auth.set_password, str(body.get("password", "")))
        except ValueError as e:
            raise HTTPException(400, str(e))
        return _status(request)

    @app.post("/api/auth/logout-all")
    async def logout_all(request: Request):
        auth.logout_all()
        return _status(request)

    # ------------------------------------------------------------ 알림(웹 푸시)
    @app.get("/api/push/status")
    async def push_status():
        return {"public_key": await asyncio.to_thread(push.public_key),
                "devices": [{"label": s.get("label", ""), "added": s.get("added"), "endpoint": s["endpoint"]}
                            for s in push.subs]}

    @app.post("/api/push/subscribe")
    async def push_subscribe(body: dict = Body(...)):
        try:
            push.add(body.get("subscription") or {}, str(body.get("label", "")))
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {"ok": True, "count": len(push.subs)}

    @app.post("/api/push/unsubscribe")
    async def push_unsubscribe(body: dict = Body(...)):
        return {"ok": push.remove(str(body.get("endpoint", ""))), "count": len(push.subs)}

    @app.post("/api/push/test")
    async def push_test(body: dict = Body(default={})):
        ep = body.get("endpoint")
        skip = {s["endpoint"] for s in push.subs if ep and s["endpoint"] != ep}
        if not push.subs or (ep and not push.has(ep)):
            raise HTTPException(400, "이 기기는 아직 알림을 켜지 않았습니다")
        results = await push.send(mgr().cfg.room_name or "작업방", "시험 알림입니다 · 잘 도착했습니다", {}, skip=skip)
        return {"results": results}

    # ------------------------------------------------------------ 설정
    @app.get("/api/config")
    async def get_config():
        return {"editable": editable_config(), "public": mgr().cfg.public()}

    @app.put("/api/config")
    async def put_config(patch: dict = Body(...)):
        try:
            backup = save_editable_config(patch)
        except Exception as e:
            raise HTTPException(400, f"저장 실패: {e}")
        mgr().reload(load_config())
        await push_config()
        return {"ok": True, "backup_dir": backup, "editable": editable_config()}

    @app.post("/api/avatar/{key}")
    async def upload_avatar(key: str, body: dict = Body(...)):
        if key not in mgr().cfg.agents and key != "user":
            raise HTTPException(404, "없는 참여자")
        data = base64.b64decode(body.get("data", "").split(",")[-1])
        ext = os.path.splitext(body.get("name", ""))[1].lower() or ".png"
        src = _unique(AVATAR_DIR / f"{key}_upload_{time.strftime('%Y%m%d_%H%M%S')}{ext}")
        src.write_bytes(data)
        fname = _thumbnail(src, key)
        _set_avatar(key, fname)
        mgr().reload(load_config())
        await push_config()
        return {"ok": True, "avatar": fname}

    def _set_avatar(key: str, fname: str) -> None:
        save_editable_config({"user_avatar": fname} if key == "user" else {"agents": {key: {"avatar": fname}}})

    @app.post("/api/avatar/{key}/generate")
    async def generate_avatar(key: str, body: dict = Body(...)):
        if key not in mgr().cfg.agents:
            raise HTTPException(404, "없는 참여자")
        desc = str(body.get("description", "")).strip()
        if not desc:
            raise HTTPException(400, "인물 설명을 적어 주세요")

        async def job():
            await hub.broadcast({"type": "avatar_status", "key": key, "status": "generating"})
            from tools.gen_avatars import generate
            try:
                ok, info = await asyncio.to_thread(generate, key, desc)
            except Exception as e:
                ok, info = False, str(e)
            if ok:
                fname = _thumbnail(AVATAR_DIR / info, key)
                _set_avatar(key, fname)
                mgr().reload(load_config())
                await push_config()
                await hub.broadcast({"type": "avatar_status", "key": key, "status": "done", "avatar": fname})
            else:
                await hub.broadcast({"type": "avatar_status", "key": key, "status": "failed", "message": info[-300:]})

        asyncio.create_task(job())
        return {"started": True}

    CLAUDE_MODELS = [
        {"id": "claude-fable-5-1", "label": "Claude Fable 5.1", "note": "가장 강력"},
        {"id": "claude-opus-5-5", "label": "Claude Opus 5.5", "note": ""},
        {"id": "claude-sonnet-5", "label": "Claude Sonnet 5", "note": "빠름"},
        {"id": "claude-haiku-4-5-20251001", "label": "Claude Haiku 4.5", "note": "가장 빠름"},
    ]
    CLAUDE_EFFORTS = ["low", "medium", "high", "xhigh", "max"]  # claude --help 의 --effort 선택지

    def codex_models() -> dict:
        """`codex debug models` 의 카탈로그(visibility=list)와 config.toml 기본 모델."""
        if "codex_models" in state:
            return state["codex_models"]
        models, default = [], None
        try:
            import subprocess
            out = subprocess.run([find_codex_exe(), "debug", "models"], capture_output=True, text=True,
                                 encoding="utf-8", errors="replace", timeout=40,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
            for m in json.loads(out).get("models", []):
                if m.get("visibility") == "list":
                    models.append({"id": m["slug"], "label": m.get("display_name") or m["slug"],
                                   "note": m.get("description", ""),
                                   "efforts": [e.get("effort") for e in m.get("supported_reasoning_levels") or []]})
        except Exception:
            pass
        try:
            import tomllib
            with open(Path.home() / ".codex" / "config.toml", "rb") as f:
                c = tomllib.load(f)
            default = {"model": c.get("model"), "effort": c.get("model_reasoning_effort")}
        except Exception:
            default = None
        state["codex_models"] = {"models": models, "default": default}
        return state["codex_models"]

    @app.get("/api/models")
    async def list_models():
        return {"claude": {"models": CLAUDE_MODELS, "efforts": CLAUDE_EFFORTS},
                "codex": await asyncio.to_thread(codex_models)}

    # ------------------------------------------------------------ 음성 인식(PC 안)
    def stt_prompt() -> str:
        cfg = mgr().cfg
        names = [a.name for a in cfg.agents.values()] + [cfg.user.get("name", "")]
        return build_prompt(cfg.settings.stt_vocab or [], names)

    @app.get("/api/stt/status")
    async def stt_status():
        size = mgr().cfg.settings.stt_model
        return {"status": stt.status, "model": size, "downloaded": stt.is_downloaded(size)}

    @app.post("/api/stt/warmup")
    async def stt_warmup():
        """마이크를 누르는 순간 모델을 미리 불러와, 말을 마쳤을 때 기다림을 줄인다."""
        size = mgr().cfg.settings.stt_model
        asyncio.get_running_loop().run_in_executor(None, stt.load, size)
        return {"status": stt.status, "downloaded": stt.is_downloaded(size)}

    @app.post("/api/stt")
    async def stt_transcribe(body: dict = Body(...)):
        data = base64.b64decode(body.get("data", "").split(",")[-1])
        if not data:
            raise HTTPException(400, "녹음이 비어 있습니다")
        if len(data) > 25 * 1024 * 1024:
            raise HTTPException(413, "녹음이 너무 깁니다(25MB 이하)")
        try:
            return await asyncio.to_thread(stt.transcribe, data, body.get("mime", "audio/webm"),
                                           mgr().cfg.settings.stt_model, stt_prompt())
        except Exception as e:
            raise HTTPException(500, f"음성 인식 실패: {type(e).__name__}: {e}")

    # ------------------------------------------------------------ 대화
    @app.get("/api/conversations")
    async def list_conversations():
        return {"conversations": mgr().list(), "recent_folders": mgr().recent_folders()}

    @app.post("/api/conversations")
    async def create_conversation(body: dict = Body(default={})):
        gr = body.get("global_rules")
        try:
            room = mgr().create(workspace=body.get("workspace") or None, title=body.get("title") or None,
                                safe_mode=None if gr is None else not bool(gr),
                                exclude=[str(x) for x in body.get("exclude") or []])
        except ValueError as e:
            raise HTTPException(400, str(e))
        await mgr().broadcast_list()
        return {"id": room.conv_id, "summary": room.summary()}

    @app.post("/api/conv/{conv_id}/archive")
    async def archive_conversation(conv_id: str, body: dict = Body(default={})):
        room = room_or_404(conv_id)
        await room.set_archived(bool(body.get("archived", True)))
        return {"ok": True, "summary": room.summary()}

    @app.post("/api/conv/{conv_id}/exclude")
    async def exclude_agent(conv_id: str, body: dict = Body(...)):
        room = room_or_404(conv_id)
        try:
            await room.set_excluded(str(body.get("agent", "")), bool(body.get("excluded", True)))
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {"ok": True, "summary": room.summary()}

    @app.post("/api/pick-folder")
    async def pick_folder(request: Request, body: dict = Body(default={})):
        require_local(request)  # 폴더 선택 창은 PC 화면에 뜨므로 폰에서는 막는다
        try:
            path = await asyncio.to_thread(pick_folder_dialog, body.get("initial"))
        except Exception as e:
            raise HTTPException(500, f"폴더 선택 창을 열지 못했습니다: {e}")
        return {"path": path}

    # ------------------------------------------------------------ 파일
    @app.get("/api/conv/{conv_id}/files")
    async def list_files(conv_id: str):
        room = room_or_404(conv_id)
        base = room.workspace
        since = room.created - 1
        items = []
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.startswith("."))
            for fn in sorted(filenames):
                if is_temp_file(fn):  # ~$ 엑셀 잠금 파일 등은 산출물이 아님
                    continue
                p = Path(dirpath) / fn
                try:
                    st = p.stat()
                except OSError:
                    continue
                items.append({"path": p.relative_to(base).as_posix(), "size": st.st_size, "mtime": st.st_mtime,
                              "in_conv": st.st_mtime >= since})
                if len(items) >= 2000:
                    return {"workspace": str(base), "files": items, "truncated": True}
        return {"workspace": str(base), "files": items, "truncated": False, "since": since}

    @app.get("/api/conv/{conv_id}/file")
    async def read_file(conv_id: str, path: str):
        p = _inside(room_or_404(conv_id).workspace, path)
        if not p.is_file():
            raise HTTPException(404, "파일이 없습니다")
        raw_url = f"/api/conv/{conv_id}/raw?path={quote(path)}"
        out = await asyncio.to_thread(preview, p, raw_url)
        out.update({"size": p.stat().st_size, "mtime": p.stat().st_mtime, "raw_url": raw_url})
        return out

    @app.post("/api/conv/{conv_id}/open-file")
    async def open_file(conv_id: str, request: Request, body: dict = Body(...)):
        require_local(request)
        p = _inside(room_or_404(conv_id).workspace, body.get("path", ""))
        if not p.is_file():
            raise HTTPException(404, "파일이 없습니다")
        if os.name == "nt":
            os.startfile(str(p))  # 엑셀·워드 등 연결된 기본 앱으로 열기
        return {"ok": True}

    @app.get("/api/conv/{conv_id}/raw")
    async def raw_file(conv_id: str, path: str):
        p = _inside(room_or_404(conv_id).workspace, path)
        if not p.is_file():
            raise HTTPException(404, "파일이 없습니다")
        return FileResponse(p)

    @app.post("/api/conv/{conv_id}/open")
    async def open_folder(conv_id: str, request: Request, body: dict = Body(default={})):
        require_local(request)
        base =room_or_404(conv_id).workspace
        target = _inside(base, body["path"]) if body.get("path") else base
        if os.name == "nt":
            os.startfile(str(target if target.is_dir() else target.parent))
        return {"ok": True}

    @app.post("/api/conv/{conv_id}/upload")
    async def upload(conv_id: str, body: dict = Body(...)):
        base = room_or_404(conv_id).workspace
        data = base64.b64decode(body.get("data", "").split(",")[-1])
        if len(data) > UPLOAD_LIMIT:
            raise HTTPException(413, "50MB 이하 파일만 첨부할 수 있습니다")
        folder = base / "_첨부"
        folder.mkdir(exist_ok=True)
        dst = _unique(folder / _safe_name(body.get("name", "file")))
        dst.write_bytes(data)
        return {"path": dst.relative_to(base).as_posix(), "size": len(data)}

    # ------------------------------------------------------------ 실시간
    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket):
        await ws.accept()
        if foreign_origin(ws):
            await ws.close(code=4403)  # 다른 웹사이트가 127.0.0.1 작업방에 몰래 지시를 보내는 것을 막음
            return
        if not auth.authorized(ws.scope, ws.cookies):
            await ws.close(code=4401)  # 화면이 이 코드를 보면 로그인 화면으로 돌아간다
            return
        hub.clients.add(ws)
        m = mgr()
        await ws.send_text(json.dumps({"type": "init", "config": m.cfg.public(), "conversations": m.list(),
                                       "recent_folders": m.recent_folders(), "limits": m.limits.data,
                                       "slash_commands": m.slash_commands,
                                       "client": {"local": auth.is_local(ws.scope)}},
                                      ensure_ascii=False))
        try:
            while True:
                msg = json.loads(await ws.receive_text())
                kind = msg.get("type")
                room = m.get(msg.get("conv", "")) if msg.get("conv") else None
                if kind == "presence":
                    hub.presence[ws] = {"endpoint": str(msg.get("endpoint") or ""), "visible": bool(msg.get("visible"))}
                elif kind == "open" and room:
                    await ws.send_text(json.dumps(room.snapshot(), ensure_ascii=False))
                elif kind == "say" and room:
                    mode = msg.get("mode") if msg.get("mode") in ("review", "quick") else "review"
                    await room.user_message(str(msg.get("text", "")), mode=mode,
                                            attachments=[str(a) for a in msg.get("attachments") or []])
                elif kind == "stop" and room:
                    asyncio.create_task(room.stop())
                elif kind == "approve" and room:
                    room.resolve_approval(str(msg.get("id")), bool(msg.get("allow")))
        except WebSocketDisconnect:
            pass
        finally:
            hub.clients.discard(ws)
            hub.presence.pop(ws, None)

    app.state.auth, app.state.push, app.state.notifier, app.state.hub = auth, push, notifier, hub
    return app


app = create_app()


def _listen(host: str, port: int):
    import socket

    sock = socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET, socket.SOCK_STREAM)
    if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):  # 윈도우: 다른 프로그램이 같은 포트를 가로채지 못하게
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    sock.bind((host, port))
    sock.listen(128)
    sock.setblocking(False)
    return sock


def main() -> None:
    import uvicorn

    cfg = load_config()
    s = cfg.settings
    socks = [_listen(s.host, s.port)]
    print(f"단톡방: http://{s.host}:{s.port}")
    try:  # 폰 전용 포트: 이 PC 안에서만 열고, Tailscale serve 가 폰 요청을 여기로 넘긴다(항상 로그인 필요)
        socks.append(_listen("127.0.0.1", s.remote_port))
        print(f"모바일(Tailscale serve 용): http://127.0.0.1:{s.remote_port}  — 로그인 필요")
    except OSError as e:
        print(f"모바일 포트 {s.remote_port} 를 열지 못했습니다(모바일 접속만 안 됨): {e}")
    uvicorn.Server(uvicorn.Config(app, log_level="warning")).run(sockets=socks)


if __name__ == "__main__":
    main()
