"""v7 테스트(모바일): 로그인·세션 쿠키·잠금, 원격 포트 차단, PC 전용 기능 막기, PWA 파일, 웹 푸시(암호화·알림 시점)."""
import asyncio
import base64
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from app.auth import COOKIE, LOCK_SEC, MAX_FAILS, SESSION_DAYS, Auth
from app.push import Notifier, Push
from tests import test_flow
from tests.test_flow import FakeRunner, review


def b64d(s: str) -> bytes:
    s = "".join(s.split())
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


class SrvBase(unittest.TestCase):
    """ServerTests 의 임시 설정·로그 폴더만 빌려 쓴다(상속·직접 import 하면 그 테스트들이 두 번 돈다)."""
    def setUp(self):
        self.st = test_flow.ServerTests("test_config_api")
        self.st.setUp()
        self.tmp = self.st.tmp

    def tearDown(self):
        self.st.tearDown()

    def make_client(self, remote=False):
        return self.st.make_client(remote)


class AuthUnitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.auth = Auth(self.tmp / "auth.json", remote_port=8790)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_local_is_decided_by_port_and_loopback(self):
        a = self.auth
        self.assertTrue(a.is_local({"server": ("127.0.0.1", 8780), "client": ("127.0.0.1", 5000)}))
        # Tailscale serve 를 거친 폰 요청도 보낸 곳은 127.0.0.1 로 보인다 → 포트로 가른다
        self.assertFalse(a.is_local({"server": ("127.0.0.1", 8790), "client": ("127.0.0.1", 5000)}))
        self.assertFalse(a.is_local({"server": ("0.0.0.0", 8780), "client": ("100.101.1.2", 5000)}))

    def test_password_hash_session_and_logout_all(self):
        a = self.auth
        with self.assertRaises(ValueError):
            a.set_password("short")
        a.set_password("correct horse")
        raw = (self.tmp / "auth.json").read_text(encoding="utf-8")
        self.assertNotIn("correct horse", raw)  # 원문은 저장하지 않음
        self.assertTrue(a.verify("correct horse"))
        self.assertFalse(a.verify("wrong"))
        tok = a.issue()
        self.assertTrue(a.check(tok))
        self.assertTrue(Auth(self.tmp / "auth.json", 8790).check(tok))   # 서버를 다시 켜도 로그인 유지
        self.assertFalse(a.check(tok[:-1] + ("0" if tok[-1] != "0" else "1")))
        old = str(int(time.time()) - SESSION_DAYS * 86400 - 10)
        import hashlib, hmac
        expired = old + "." + hmac.new(a._secret(), old.encode(), hashlib.sha256).hexdigest()
        self.assertFalse(a.check(expired))  # 30일 지나면 만료
        a.logout_all()
        self.assertFalse(a.check(tok))
        tok2 = a.issue()
        a.set_password("another pass")  # 비밀번호를 바꾸면 기존 로그인도 끊김
        self.assertFalse(a.check(tok2))

    def test_lockout_after_repeated_failures(self):
        a = self.auth
        a.set_password("correct horse")
        for _ in range(MAX_FAILS):
            self.assertFalse(a.verify("nope"))
        self.assertGreater(a.lock_left(), LOCK_SEC - 5)
        self.assertFalse(a.verify("correct horse"))  # 잠긴 동안은 맞아도 안 됨
        a._locked_until = time.time() - 1
        self.assertTrue(a.verify("correct horse"))


class RemoteServerTests(SrvBase):
    def test_remote_requires_login(self):
        with self.make_client(remote=True) as client:
            r = client.get("/")
            self.assertEqual(r.status_code, 200)
            self.assertIn("비밀번호", r.text)          # 앱 대신 로그인 화면
            self.assertNotIn("app.js", r.text)
            self.assertEqual(client.get("/api/conversations").status_code, 401)
            self.assertEqual(client.get("/static/app.js").status_code, 401)
            self.assertEqual(client.post("/api/conversations", json={}).status_code, 401)
            for p in ("/manifest.webmanifest", "/sw.js", "/static/icons/icon-192.png", "/static/icons/apple-touch-icon.png"):
                self.assertEqual(client.get(p).status_code, 200, p)   # 홈 화면 설치는 로그인 전에도
            self.assertIn("standalone", client.get("/manifest.webmanifest").text)
            with client.websocket_connect("wss://testserver:8790/ws") as ws:  # 이 클라이언트의 ws 는 기본 주소를 안 따름
                with self.assertRaises(Exception) as cm:
                    ws.receive_json()
                self.assertEqual(getattr(cm.exception, "code", None), 4401)
            st = client.get("/api/auth/status").json()
            self.assertEqual((st["local"], st["logged_in"], st["password_set"]), (False, False, False))
            self.assertEqual(client.post("/api/login", json={"password": "x" * 10}).status_code, 400)  # 비밀번호 아직 없음
            # 폰에서 비밀번호를 정할 수 없음(로그인 전이라 401)
            self.assertEqual(client.post("/api/auth/password", json={"password": "x" * 10}).status_code, 401)

    def test_local_sets_password_then_phone_logs_in(self):
        with self.make_client() as pc:
            self.assertTrue(pc.get("/api/auth/status").json()["local"])
            self.assertEqual(pc.post("/api/auth/password", json={"password": "short"}).status_code, 400)
            self.assertTrue(pc.post("/api/auth/password", json={"password": "phone-pass-1"}).json()["password_set"])
            cid = pc.post("/api/conversations", json={}).json()["id"]
            with pc.websocket_connect("/ws") as ws:
                self.assertEqual(ws.receive_json()["client"], {"local": True})
        with self.make_client(remote=True) as phone:
            r = phone.post("/api/login", json={"password": "wrong-pass"})
            self.assertEqual(r.status_code, 401)
            r = phone.post("/api/login", json={"password": "phone-pass-1"})
            self.assertEqual(r.status_code, 200)
            sc = r.headers["set-cookie"].lower()
            for flag in ("httponly", "secure", "samesite=strict", f"max-age={SESSION_DAYS * 86400}"):
                self.assertIn(flag, sc)
            self.assertIn(COOKIE, phone.cookies)
            self.assertEqual(phone.get("/api/conversations").status_code, 200)
            self.assertIn("app.js", phone.get("/").text)
            with phone.websocket_connect("wss://testserver:8790/ws") as ws:
                init = ws.receive_json()
                self.assertEqual(init["client"], {"local": False})
            # PC 화면에 창이 뜨는 기능·비밀번호 변경은 폰에서 막음
            self.assertEqual(phone.post("/api/pick-folder", json={}).status_code, 403)
            self.assertEqual(phone.post(f"/api/conv/{cid}/open", json={}).status_code, 403)
            self.assertEqual(phone.post(f"/api/conv/{cid}/open-file", json={"path": "a.txt"}).status_code, 403)
            self.assertEqual(phone.post("/api/auth/password", json={"password": "x" * 10}).status_code, 403)
            # 모든 기기 로그아웃 → 이 폰도 끊김
            self.assertEqual(phone.post("/api/auth/logout-all").status_code, 200)
            self.assertEqual(phone.get("/api/conversations").status_code, 401)

    def test_pc_port_rejects_other_websites(self):
        """다른 웹사이트(Origin 이 다름)가 PC 작업방에 지시를 보내지 못하게."""
        with self.make_client() as pc:
            evil = {"Origin": "https://evil.example"}
            same = {"Origin": "http://127.0.0.1:8780"}
            self.assertEqual(pc.post("/api/conversations", json={}, headers=evil).status_code, 403)
            self.assertEqual(pc.post("/api/conversations", json={}, headers=same).status_code, 200)
            self.assertEqual(pc.post("/api/conversations", json={}).status_code, 200)  # Origin 없는 로컬 도구는 그대로
            self.assertEqual(pc.get("/api/conversations", headers=evil).status_code, 200)  # 읽기 요청은 브라우저가 응답을 못 넘겨줌
            with pc.websocket_connect("ws://127.0.0.1:8780/ws", headers=evil) as ws:
                with self.assertRaises(Exception) as cm:
                    ws.receive_json()
                self.assertEqual(getattr(cm.exception, "code", None), 4403)
            with pc.websocket_connect("ws://127.0.0.1:8780/ws", headers=same) as ws:
                self.assertEqual(ws.receive_json()["type"], "init")

    def test_login_lockout_over_http(self):
        with self.make_client() as pc:
            pc.post("/api/auth/password", json={"password": "phone-pass-1"})
        with self.make_client(remote=True) as phone:
            codes = [phone.post("/api/login", json={"password": "bad"}).status_code for _ in range(MAX_FAILS)]
            self.assertEqual(codes[-1], 429)
            self.assertEqual(phone.post("/api/login", json={"password": "phone-pass-1"}).status_code, 429)


class PushTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_vapid_key_created_once_and_subscriptions(self):
        p = Push(self.tmp)
        key = p.public_key()
        self.assertEqual(len(b64d(key)), 65)          # P-256 비압축 공개키
        self.assertEqual(Push(self.tmp).public_key(), key)  # 다시 켜도 같은 키
        with self.assertRaises(ValueError):
            p.add({"endpoint": "http://x", "keys": {}})
        sub = {"endpoint": "https://push.example/abc", "keys": {"p256dh": "k", "auth": "a"}}
        p.add(sub, "iPhone Safari · 홈 화면 앱")
        p.add(sub, "iPhone")                           # 같은 기기는 한 번만
        self.assertEqual(len(Push(self.tmp).subs), 1)
        self.assertTrue(p.remove(sub["endpoint"]))
        self.assertEqual(Push(self.tmp).subs, [])

    def test_rfc8291_example_vector(self):
        """RFC 8291 5장·부록 A 의 예시 값으로 암호화하면 문서의 결과와 바이트 단위로 같아야 한다."""
        import http_ece
        from cryptography.hazmat.primitives.asymmetric import ec
        as_private = ec.derive_private_key(int.from_bytes(b64d("yfWPiYE-n46HLnH0KqZOF1fJJU3MYrct3AELtAQ-oRw"), "big"), ec.SECP256R1())
        ua_public = b64d("BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcxaOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4")
        expected = b64d("""DGv6ra1nlYgDCS1FRnbzlwAAEABBBP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27ml
            mlMoZIIgDll6e3vCYLocInmYWAmS6TlzAC8wEqKK6PBru3jl7A_yl95bQpu6cVPT
            pK4Mqgkf1CXztLVBSt2Ks3oZwbuwXPXLWyouBWLVWGNWQexSgSxsj_Qulcy4a-fN""")
        body = http_ece.encrypt(b"When I grow up, I want to be a watermelon", salt=b64d("DGv6ra1nlYgDCS1FRnbzlw"),
                                private_key=as_private, dh=ua_public, auth_secret=b64d("BTBZMqHH6r4Tts7J_aSIgg"),
                                version="aes128gcm", rs=4096)
        self.assertEqual(body, expected)

    def test_pywebpush_payload_decrypts_on_phone_side(self):
        """pywebpush 가 만든 본문을, 폰 쪽 개인키로 풀면 원문이 나와야 한다."""
        import http_ece
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from pywebpush import WebPusher
        ua = ec.generate_private_key(ec.SECP256R1())
        pub = ua.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
        auth = b"0123456789abcdef"
        sub = {"endpoint": "https://push.example/x", "keys": {"p256dh": base64.urlsafe_b64encode(pub).decode().rstrip("="),
                                                              "auth": base64.urlsafe_b64encode(auth).decode().rstrip("=")}}
        enc = WebPusher(sub).encode('{"title":"작업방","body":"Jake 작업 완료"}'.encode(), content_encoding="aes128gcm")
        plain = http_ece.decrypt(enc["body"], private_key=ua, auth_secret=auth, version="aes128gcm")
        self.assertEqual(plain.decode(), '{"title":"작업방","body":"Jake 작업 완료"}')

    def test_expired_subscription_is_removed(self):
        p = Push(self.tmp)
        p.add({"endpoint": "https://push.example/gone", "keys": {"p256dh": "k", "auth": "a"}})
        with mock.patch.object(Push, "_send_one", return_value=(False, 410, "Gone")):
            res = asyncio.run(p.send("t", "b"))
        self.assertFalse(res[0]["ok"])
        self.assertEqual(p.subs, [])


class NotifierTests(SrvBase):
    def test_push_sent_at_right_moments_without_client_text(self):
        """완료·승인 대기 때 알림, 화면을 보고 있는 기기는 제외, 문구에 대화 제목·내용 없음."""
        FakeRunner.scripts = {"jake": ["예시사 매출 대사 완료"], "clara": [review("APPROVE")], "quinn": [review("APPROVE")]}
        sent = []

        async def fake_send(self_push, title, body, data=None, skip=None):
            sent.append({"title": title, "body": body, "data": data, "skip": set(skip or ())})
            return []

        with mock.patch.object(Push, "send", fake_send), self.make_client() as client:
            app = client.app
            app.state.push.subs = [{"endpoint": "https://push.example/phone", "keys": {}},
                                   {"endpoint": "https://push.example/tablet", "keys": {}}]
            conv = client.post("/api/conversations", json={"title": "예시사 FDD"}).json()["id"]
            with client.websocket_connect("/ws") as ws:
                ws.receive_json()
                ws.send_json({"type": "presence", "endpoint": "https://push.example/tablet", "visible": True})
                ws.send_json({"type": "open", "conv": conv})
                ws.receive_json()
                ws.send_json({"type": "say", "conv": conv, "text": "매출 대사해줘"})
                for _ in range(300):
                    d = ws.receive_json()
                    if d["type"] == "message" and d["message"].get("done"):
                        break
                for _ in range(50):  # 완료 후 state(running=False) 가 올 때까지
                    d = ws.receive_json()
                    if d["type"] == "state" and not d["running"]:
                        break
            time.sleep(0.2)
        self.assertEqual(len(sent), 1, sent)
        self.assertEqual(sent[0]["body"], "Jake 작업 완료 · 확인해 주세요")
        self.assertEqual(sent[0]["data"], {"conv": conv})
        self.assertEqual(sent[0]["skip"], {"https://push.example/tablet"})   # 보고 있는 기기 제외
        self.assertNotIn("예시사", sent[0]["title"] + sent[0]["body"])

    def test_texts_for_each_ending(self):
        from app.config import load_config
        cfg = load_config()
        n = Notifier(Push(Path(tempfile.mkdtemp())), lambda: cfg, lambda: set())
        self.assertIn("판단이 필요합니다", n.text_for({"sender": "system", "escalation": True}))
        self.assertEqual(n.text_for({"sender": "system", "text": "오류로 과제를 멈췄습니다"}), "작업이 멈췄습니다 · 확인해 주세요")
        self.assertEqual(n.text_for({"sender": "clara", "text": "답"}), "Clara 답변 도착")

    def test_approval_and_user_stop(self):
        from app.config import load_config
        cfg = load_config()
        p = Push(Path(tempfile.mkdtemp()))
        p.subs = [{"endpoint": "https://push.example/phone", "keys": {}}]
        n = Notifier(p, lambda: cfg, lambda: set())

        async def run():
            with mock.patch.object(Push, "send", mock.AsyncMock(return_value=[])):
                await n.observe({"type": "message", "conv": "c1", "message": {"sender": "jake", "kind": "approval", "text": "rm -rf"}})
                await n.observe({"type": "state", "conv": "c1", "running": True})
                await n.observe({"type": "message", "conv": "c1", "message": {"sender": "system", "text": "사용자 요청으로 중단했습니다."}})
                await n.observe({"type": "state", "conv": "c1", "running": False})
                await asyncio.sleep(0)
        asyncio.run(run())
        self.assertEqual([s["body"] for s in n.sent], ["Jake가 실행 허락을 기다립니다"])  # 직접 멈춘 건 알리지 않음


if __name__ == "__main__":
    unittest.main()
