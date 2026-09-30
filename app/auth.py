"""모바일 접속 로그인: 비밀번호 해시·세션 쿠키·잠금.

PC 에서 직접 여는 주소(127.0.0.1:8780)는 지금처럼 로그인 없이 쓴다.
폰은 Tailscale(serve) → 이 PC 의 원격 전용 포트(127.0.0.1:8790)로만 들어오고, 그 포트는 항상 로그인해야 한다.
포트로 구분하는 이유: 프록시를 거치면 요청 주소가 모두 127.0.0.1 로 보여 주소만으로는 폰과 PC 를 가를 수 없다.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path

COOKIE = "agentchat_session"
SESSION_DAYS = 30
MAX_FAILS = 5            # 연속으로 이만큼 틀리면
LOCK_SEC = 300           # 이 시간 동안 로그인 잠금
PBKDF2_ITER = 600_000
MIN_PASSWORD = 8
LOOPBACK = {"127.0.0.1", "::1", "localhost"}


class Auth:
    def __init__(self, path: Path, remote_port: int):
        self.path = Path(path)
        self.remote_port = remote_port
        self._fails: list[float] = []
        self._locked_until = 0.0
        self.data = self._load()

    # ------------------------------------------------------------ 저장
    def _load(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)

    # ------------------------------------------------------------ 누구의 요청인가
    def is_local(self, scope: dict) -> bool:
        """PC 에서 직접 연 요청인지: 원격 전용 포트가 아니고, 보낸 곳이 이 PC 자신이어야 한다."""
        server = scope.get("server") or (None, None)
        client = scope.get("client") or (None, None)
        return server[1] != self.remote_port and client[0] in LOOPBACK

    def authorized(self, scope: dict, cookies: dict) -> bool:
        return self.is_local(scope) or self.check(cookies.get(COOKIE, ""))

    # ------------------------------------------------------------ 비밀번호
    @property
    def password_set(self) -> bool:
        return bool(self.data.get("password"))

    def set_password(self, password: str) -> None:
        if len(password or "") < MIN_PASSWORD:
            raise ValueError(f"비밀번호는 {MIN_PASSWORD}자 이상이어야 합니다")
        salt = secrets.token_bytes(16)
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITER)
        self.data["password"] = {"salt": salt.hex(), "hash": digest.hex(), "iter": PBKDF2_ITER}
        self.data["secret"] = secrets.token_hex(32)  # 비밀번호를 바꾸면 기존 로그인은 모두 끊는다
        self._save()

    def lock_left(self) -> int:
        return max(0, int(self._locked_until - time.time() + 0.999))

    def verify(self, password: str) -> bool:
        """맞으면 True. 틀린 횟수가 쌓이면 잠근다(잠긴 동안은 맞아도 False)."""
        if self.lock_left() or not self.password_set:
            return False
        p = self.data["password"]
        digest = hashlib.pbkdf2_hmac("sha256", (password or "").encode("utf-8"), bytes.fromhex(p["salt"]), int(p["iter"]))
        if hmac.compare_digest(digest.hex(), p["hash"]):
            self._fails.clear()
            return True
        now = time.time()
        self._fails = [t for t in self._fails if now - t < LOCK_SEC] + [now]
        if len(self._fails) >= MAX_FAILS:
            self._locked_until = now + LOCK_SEC
            self._fails.clear()
        return False

    # ------------------------------------------------------------ 세션 쿠키
    def _secret(self) -> bytes:
        if not self.data.get("secret"):
            self.data["secret"] = secrets.token_hex(32)
            self._save()
        return bytes.fromhex(self.data["secret"])

    def issue(self) -> str:
        issued = str(int(time.time()))
        sig = hmac.new(self._secret(), issued.encode(), hashlib.sha256).hexdigest()
        return f"{issued}.{sig}"

    def check(self, token: str) -> bool:
        if not token or "." not in token or not self.password_set:
            return False
        issued, sig = token.split(".", 1)
        if not issued.isdigit() or time.time() - int(issued) > SESSION_DAYS * 86400:
            return False
        good = hmac.new(self._secret(), issued.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(good, sig)

    def logout_all(self) -> None:
        self.data["secret"] = secrets.token_hex(32)
        self._save()
