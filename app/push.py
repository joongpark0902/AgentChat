"""작업 완료 알림(웹 푸시). 폰(홈 화면에 설치한 앱)이 구독하면, 라운드가 끝나거나 승인·판단이 필요할 때 알린다.

알림은 Apple·Google 푸시 서버를 거친다(내용은 표준 암호화 RFC 8291 로 가려짐). 그래도 문구에는
대화 제목·고객사명·작업 내용을 넣지 않고 "Jake 작업 완료" 수준으로만 보낸다.
"""
from __future__ import annotations

import asyncio
import base64
import copy
import json
import os
import time
from pathlib import Path

DEFAULT_SUBJECT = "mailto:agentchat@example.com"  # VAPID 연락처(필수 항목). 실제 메일은 가지 않음


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


class Push:
    def __init__(self, folder: Path, subject: str = DEFAULT_SUBJECT):
        self.folder = Path(folder)
        self.key_file = self.folder / "vapid_private.pem"
        self.subs_file = self.folder / "push_subscriptions.json"
        self.subject = subject or DEFAULT_SUBJECT
        self._vapid = None
        try:
            self.subs: list[dict] = json.loads(self.subs_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.subs = []

    # ------------------------------------------------------------ 키
    def vapid(self):
        """서버 서명 키. 처음 쓸 때 만들어 private 폴더에 둔다."""
        if self._vapid is None:
            from py_vapid import Vapid
            if self.key_file.exists():
                self._vapid = Vapid.from_file(str(self.key_file))
            else:
                self.folder.mkdir(parents=True, exist_ok=True)
                v = Vapid()
                v.generate_keys()
                v.save_key(str(self.key_file))
                self._vapid = v
        return self._vapid

    def public_key(self) -> str:
        from cryptography.hazmat.primitives import serialization
        raw = self.vapid().public_key.public_bytes(serialization.Encoding.X962,
                                                   serialization.PublicFormat.UncompressedPoint)
        return _b64url(raw)

    # ------------------------------------------------------------ 구독
    def _save(self) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        tmp = self.subs_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.subs, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.subs_file)

    def add(self, sub: dict, label: str = "") -> None:
        keys = sub.get("keys") or {}
        if not str(sub.get("endpoint", "")).startswith("https://") or not keys.get("p256dh") or not keys.get("auth"):
            raise ValueError("알림 구독 정보가 올바르지 않습니다")
        self.subs = [s for s in self.subs if s["endpoint"] != sub["endpoint"]]
        self.subs.append({"endpoint": sub["endpoint"], "keys": {"p256dh": keys["p256dh"], "auth": keys["auth"]},
                          "label": label[:80], "added": time.time()})
        self._save()

    def remove(self, endpoint: str) -> bool:
        n = len(self.subs)
        self.subs = [s for s in self.subs if s["endpoint"] != endpoint]
        if len(self.subs) != n:
            self._save()
        return len(self.subs) != n

    def has(self, endpoint: str) -> bool:
        return any(s["endpoint"] == endpoint for s in self.subs)

    # ------------------------------------------------------------ 보내기
    def _send_one(self, sub: dict, payload: str) -> tuple[bool, int | None, str]:
        from pywebpush import WebPushException, webpush
        try:
            webpush({"endpoint": sub["endpoint"], "keys": sub["keys"]}, data=payload,
                    vapid_private_key=self.vapid(), vapid_claims=copy.deepcopy({"sub": self.subject}),
                    ttl=3600, timeout=15)
            return True, 201, ""
        except WebPushException as e:
            code = getattr(getattr(e, "response", None), "status_code", None)
            body = ""
            try:
                body = e.response.text[:200]
            except Exception:
                pass
            return False, code, f"{e} {body}".strip()
        except Exception as e:  # 네트워크 끊김 등
            return False, None, f"{type(e).__name__}: {e}"

    async def send(self, title: str, body: str, data: dict | None = None, skip: set[str] | None = None) -> list[dict]:
        payload = json.dumps({"title": title, "body": body, "data": data or {}}, ensure_ascii=False)
        targets = [s for s in self.subs if s["endpoint"] not in (skip or set())]
        results = []
        for sub in targets:
            ok, code, err = await asyncio.to_thread(self._send_one, sub, payload)
            if code in (404, 410):  # 앱을 지웠거나 구독이 만료됨 → 목록에서 뺀다
                self.remove(sub["endpoint"])
            results.append({"endpoint": sub["endpoint"][:60], "ok": ok, "status": code, "error": err})
        return results


class Notifier:
    """방송(broadcast)되는 이벤트를 보고 알림을 보낼 때를 고른다.
    - 승인 카드가 뜨면 곧바로
    - 과제·답변이 끝나 진행 중 → 멈춤으로 바뀌면 마지막 메시지 성격(완료/판단 필요/멈춤/답변)으로 한 번
    """

    def __init__(self, push: Push, cfg_getter, visible_endpoints):
        self.push = push
        self.cfg = cfg_getter
        self.visible = visible_endpoints   # () -> 지금 화면을 보고 있는 기기의 구독 주소 모음(보내지 않음)
        self.running: dict[str, bool] = {}
        self.last: dict[str, dict] = {}
        self.sent: list[dict] = []          # 테스트·진단용 최근 알림
        self.tasks: set[asyncio.Task] = set()

    def _agent_name(self, key: str) -> str:
        a = self.cfg().agents.get(key)
        return a.name if a else key

    def text_for(self, msg: dict | None) -> str:
        cfg = self.cfg()
        user = cfg.user.get("name", "사용자")
        if not msg:
            return "작업이 끝났습니다 · 확인해 주세요"
        if msg.get("done"):
            return f"{self._agent_name(cfg.worker)} 작업 완료 · 확인해 주세요"
        if msg.get("escalation"):
            return f"검토가 끝나지 않았습니다 · {user} 판단이 필요합니다"
        if msg.get("sender") == "system":
            return "작업이 멈췄습니다 · 확인해 주세요"
        return f"{self._agent_name(msg.get('sender', ''))} 답변 도착"

    async def observe(self, payload: dict) -> None:
        conv, kind = payload.get("conv"), payload.get("type")
        if not conv:
            return
        if kind == "message":
            msg = payload.get("message") or {}
            self.last[conv] = msg
            if msg.get("kind") == "approval":
                await self._send(conv, f"{self._agent_name(msg.get('sender', ''))}가 실행 허락을 기다립니다")
        elif kind == "state":
            was, now = self.running.get(conv, False), bool(payload.get("running"))
            self.running[conv] = now
            if was and not now:
                last = self.last.get(conv)
                if last and last.get("sender") == "system" and str(last.get("text", "")).startswith("사용자 요청으로 중단"):
                    return  # 직접 멈춘 것은 알리지 않음
                await self._send(conv, self.text_for(last))

    async def _send(self, conv: str, body: str) -> None:
        """보내기는 따로 돌린다(푸시 서버 응답을 기다리느라 채팅 방송이 늦어지지 않게)."""
        if not self.push.subs:
            return
        title = self.cfg().room_name or "작업방"
        self.sent.append({"conv": conv, "title": title, "body": body, "ts": time.time()})
        del self.sent[:-20]
        task = asyncio.create_task(self._deliver(title, body, conv, self.visible()))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def _deliver(self, title: str, body: str, conv: str, skip: set[str]) -> None:
        try:
            await self.push.send(title, body, {"conv": conv}, skip=skip)
        except Exception:
            pass  # 알림 실패가 작업 흐름을 막지 않게
