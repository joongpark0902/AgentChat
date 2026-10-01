"""대화 흐름·대화 목록·서버 API 테스트: 가짜 러너 사용 (CLI 호출 없음)."""
import asyncio
import base64
import json
import shutil
import tempfile
import unittest
from pathlib import Path

import app.config as appconfig
from app.config import load_config
from app.orchestrator import Manager, Room
from app.runners import BaseRunner, CancelledRun, RunResult


def review(verdict, *issues):
    return {"verdict": verdict, "summary": f"{verdict} 요약", "checks": "",
            "issues": [{"severity": s, "description": d} for s, d in issues]}


class FakeRunner(BaseRunner):
    """scripts[agent_key] 에 응답을 순서대로 넣어두면 차례로 돌려준다. ("APPROVAL", 명령) 이면 승인을 요청한다."""
    scripts: dict = {}
    calls: list = []
    gate: asyncio.Event | None = None
    final_ok = True
    finals: list = []

    async def run(self, prompt, workspace, schema_path=None, activity=None, approve=None, **kw):
        if "[최종 보고] 라운드가 끝났습니다" in prompt and FakeRunner.final_ok:  # 과제 끝 최종 보고: 대본·호출 기록과 별도
            FakeRunner.finals.append((self.agent.key, prompt))
            return RunResult(ok=True, text="사장님, 최종 보고드립니다.", session_id=self.session_id)
        FakeRunner.calls.append((self.agent.key, prompt, schema_path))
        if activity:
            activity("실행 중: python test.py")
        if FakeRunner.gate is not None and self.agent.key == "jake":
            await FakeRunner.gate.wait()
        if self._cancelled:
            raise CancelledRun()
        self.session_id = self.session_id or f"sess-{self.agent.key}"
        self.started = True
        item = FakeRunner.scripts[self.agent.key].pop(0)
        if isinstance(item, tuple) and item[0] == "APPROVAL":
            allowed = await asyncio.to_thread(approve, "PowerShell", {"command": item[1]}, "삭제")
            if self._cancelled:
                raise CancelledRun()
            return RunResult(ok=True, text=f"승인 결과: {allowed}", session_id=self.session_id)
        if isinstance(item, dict):
            return RunResult(ok=True, text=json.dumps(item, ensure_ascii=False), structured=item, session_id=self.session_id)
        if item is None:
            return RunResult(ok=False, text="", error="boom")
        return RunResult(ok=True, text=item, session_id=self.session_id)


class Base(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = load_config()
        self.cfg.settings.log_dir = self.tmp / "logs"
        self.cfg.settings.workspace_root = self.tmp / "ws"
        self.cfg.settings.flow = "classic"  # 실제 설정이 3단계여도 기존 흐름 테스트는 기존 방식으로
        self.events = []
        FakeRunner.calls = []
        FakeRunner.finals = []
        FakeRunner.gate = None

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    async def bc(self, payload):
        self.events.append(payload)

    def room(self, **kw):
        return Room(self.cfg, self.bc, runner_factory=FakeRunner, **kw)

    async def run_to_end(self, room, text, **kw):
        await room.user_message(text, **kw)
        while room.running:  # 대기 지시로 이어진 과제까지
            await room.task
        if room.task:
            await room.task

    def senders(self, room):
        return [m["sender"] for m in room.messages]

    def called(self, key):
        return [p for k, p, _ in FakeRunner.calls if k == key]


class FlowTests(Base):
    async def test_both_approve_round1(self):
        FakeRunner.scripts = {"jake": ["fib 작성함"], "clara": [review("APPROVE")], "quinn": [review("APPROVE")]}
        room = self.room()
        await self.run_to_end(room, "피보나치 함수 작성")
        # 라운드가 끝나면 진행 기록(시스템) 뒤에 실무자가 사장님께 최종 보고
        self.assertEqual(self.senders(room), ["user", "jake", "clara", "quinn", "system", "jake"])
        self.assertTrue(room.messages[-1].get("done"))
        self.assertTrue(room.messages[-1].get("final"))
        self.assertIn("모두 APPROVE", FakeRunner.finals[0][1])
        self.assertIn("팀 내부 보고", FakeRunner.calls[0][1])   # 라운드 중 실무자는 감독에게 보고
        self.assertIn("팀 내부 의견", FakeRunner.calls[1][1])   # 감독은 실무자에게
        self.assertIn("피보나치 함수 작성", FakeRunner.calls[0][1])
        self.assertIn("fib 작성함", FakeRunner.calls[1][1])
        self.assertIsNone(FakeRunner.calls[0][2])      # 실무자는 스키마 없음
        self.assertIsNotNone(FakeRunner.calls[1][2])   # 감독은 판정 스키마
        self.assertTrue(any(e["type"] == "typing" and e["on"] and e["activity"] for e in self.events))
        self.assertTrue(all(e.get("conv") == room.conv_id for e in self.events))
        self.assertEqual(room.title, "피보나치 함수 작성")
        states = [e for e in self.events if e["type"] == "state"]
        self.assertTrue(states[0]["running"])
        self.assertFalse(states[-1]["running"])  # 끝났다는 알림이 '진행 중'으로 나가면 안 됨

    async def test_quick_mode_final_state_not_running(self):
        FakeRunner.scripts = {"jake": ["답"], "clara": [], "quinn": []}
        room = self.room()
        await self.run_to_end(room, "질문", mode="quick")
        self.assertFalse([e for e in self.events if e["type"] == "state"][-1]["running"])
        self.assertFalse(room.summary()["running"])

    async def test_revise_then_approve(self):
        FakeRunner.scripts = {
            "jake": ["v1", "v2 수정함"],
            "clara": [review("REVISE", ("blocker", "음수 입력 요건 누락")), review("APPROVE")],
            "quinn": [review("APPROVE"), review("APPROVE")],
        }
        room = self.room()
        await self.run_to_end(room, "피보나치")
        self.assertEqual(self.senders(room), ["user", "jake", "clara", "quinn", "jake", "clara", "quinn", "system", "jake"])
        self.assertIn("음수 입력 요건 누락", self.called("jake")[1])
        self.assertIn("라운드 2", self.called("jake")[1])

    async def test_escalate_after_max_rounds(self):
        n = self.cfg.settings.max_rounds
        FakeRunner.scripts = {"jake": [f"v{i}" for i in range(n)], "clara": [review("APPROVE")] * n,
                              "quinn": [review("REVISE", ("blocker", "재귀라 fib(50) 안 끝남"))] * n}
        room = self.room()
        await self.run_to_end(room, "피보나치")
        self.assertTrue(room.messages[-1].get("escalation"))   # 최종 보고에 '판단 필요' 표시(알림 문구)
        log = room.messages[-2]                                  # 감독 지적 원문은 진행 기록에 그대로
        self.assertIn("재귀라 fib(50) 안 끝남", log["text"])
        self.assertIn("사장님께 넘깁니다", log["text"])
        self.assertIn("재귀라 fib(50) 안 끝남", FakeRunner.finals[0][1])

    async def test_quick_mode_only_worker(self):
        FakeRunner.scripts = {"jake": ["바로 답변"], "clara": [], "quinn": []}
        room = self.room()
        await self.run_to_end(room, "이 함수 뭐야?", mode="quick")
        self.assertEqual(self.senders(room), ["user", "jake"])
        self.assertIn("검토 절차 없이", FakeRunner.calls[0][1])
        self.assertIsNone(FakeRunner.calls[0][2])

    async def test_mention_calls_only_that_agent(self):
        FakeRunner.scripts = {"jake": [], "clara": [], "quinn": ["테스트 돌려봤습니다"]}
        room = self.room()
        await self.run_to_end(room, "@Eric 지금 테스트 다시 돌려줘")
        self.assertEqual(self.senders(room), ["user", "quinn"])
        self.assertEqual([k for k, _, _ in FakeRunner.calls], ["quinn"])
        self.assertIn("지금 테스트 다시 돌려줘", FakeRunner.calls[0][1])
        self.assertNotIn("@Eric", FakeRunner.calls[0][1])
        self.assertIsNone(FakeRunner.calls[0][2])

    async def test_unknown_mention_falls_back_to_review(self):
        FakeRunner.scripts = {"jake": ["v1"], "clara": [review("APPROVE")], "quinn": [review("APPROVE")]}
        room = self.room()
        await self.run_to_end(room, "@nobody 안녕")
        self.assertEqual(self.senders(room)[-2:], ["system", "jake"])

    async def test_attachments_in_prompt(self):
        FakeRunner.scripts = {"jake": ["읽었습니다"], "clara": [], "quinn": []}
        room = self.room()
        await self.run_to_end(room, "이 파일 요약", mode="quick", attachments=["_첨부/보고서.txt"])
        self.assertIn("_첨부/보고서.txt", FakeRunner.calls[0][1])
        self.assertEqual(room.messages[0]["attachments"], ["_첨부/보고서.txt"])

    async def test_approval_allow(self):
        FakeRunner.scripts = {"jake": [("APPROVAL", "Remove-Item old.txt")], "clara": [], "quinn": []}
        room = self.room()
        await room.user_message("정리해줘", mode="quick")
        for _ in range(100):
            await asyncio.sleep(0.01)
            card = next((m for m in room.messages if m.get("kind") == "approval"), None)
            if card:
                break
        self.assertEqual(card["approval"]["status"], "pending")
        self.assertIn("Remove-Item old.txt", card["approval"]["detail"])
        self.assertTrue(room.summary()["approvals"] == 1)
        self.assertTrue(room.resolve_approval(card["approval"]["id"], True))
        await room.task
        self.assertEqual(card["approval"]["status"], "allowed")
        self.assertEqual(room.messages[-1]["text"], "승인 결과: True")
        self.assertTrue(any(e["type"] == "message_update" for e in self.events))
        # 재시작 후에도 상태가 로그에서 복원됨
        room2 = self.room(conv_id=room.conv_id)
        self.assertEqual(next(m for m in room2.messages if m.get("kind") == "approval")["approval"]["status"], "allowed")

    async def test_stop_denies_pending_approval(self):
        FakeRunner.scripts = {"jake": [("APPROVAL", "Remove-Item x")], "clara": [], "quinn": []}
        room = self.room()
        await room.user_message("지워줘", mode="quick")
        for _ in range(100):
            await asyncio.sleep(0.01)
            if room.approvals:
                break
        await room.stop()
        self.assertFalse(room.running)
        self.assertEqual(next(m for m in room.messages if m.get("kind") == "approval")["approval"]["status"], "denied")

    async def test_interjection_delivered_to_next_agent(self):
        FakeRunner.gate = asyncio.Event()
        FakeRunner.scripts = {"jake": ["v1", "v2 반영"],
                              "clara": [review("REVISE", ("blocker", "타입힌트 미반영")), review("APPROVE")],
                              "quinn": [review("APPROVE"), review("APPROVE")]}
        room = self.room()
        await room.user_message("피보나치")
        await asyncio.sleep(0.05)
        await room.user_message("타입힌트도 붙여줘")
        self.assertEqual(room.pending, ["타입힌트도 붙여줘"])
        FakeRunner.gate.set()
        await room.task
        clara_prompt = self.called("clara")[0]
        self.assertIn("타입힌트도 붙여줘", clara_prompt)
        self.assertIn("아직 전달되지 않았습니다", clara_prompt)
        self.assertIn("타입힌트도 붙여줘", self.called("jake")[1])

    async def test_approve_but_worker_missed_interjection_runs_another_round(self):
        FakeRunner.gate = asyncio.Event()
        FakeRunner.scripts = {"jake": ["v1", "v2 README 추가"], "clara": [review("APPROVE")] * 2,
                              "quinn": [review("APPROVE")] * 2}
        room = self.room()
        await room.user_message("피보나치")
        await asyncio.sleep(0.05)
        await room.user_message("README 도")
        FakeRunner.gate.set()
        await room.task
        self.assertEqual(len(self.called("jake")), 2)
        self.assertIn("README 도", self.called("jake")[1])
        self.assertIn("라운드 2", room.messages[-2]["text"])

    async def test_last_round_unseen_instruction_carried_to_new_task(self):
        self.cfg.settings.max_rounds = 1
        FakeRunner.gate = asyncio.Event()
        FakeRunner.scripts = {"jake": ["v1", "README 작성"], "clara": [review("APPROVE")] * 2,
                              "quinn": [review("APPROVE")] * 2}
        room = self.room()
        await room.user_message("피보나치")
        await asyncio.sleep(0.05)
        await room.user_message("README 도")
        FakeRunner.gate.set()
        while room.running:
            await room.task
        self.assertEqual(len(self.called("jake")), 2)
        self.assertIn("README 도", self.called("jake")[1])

    async def test_worker_failure_stops(self):
        FakeRunner.scripts = {"jake": [None], "clara": [], "quinn": []}
        room = self.room()
        await self.run_to_end(room, "피보나치")
        self.assertIn("호출 실패", room.messages[-1]["text"])

    async def test_stop(self):
        FakeRunner.gate = asyncio.Event()
        FakeRunner.scripts = {"jake": ["v1"], "clara": [], "quinn": []}
        room = self.room()
        await room.user_message("피보나치")
        await asyncio.sleep(0.05)
        await room.stop()
        self.assertFalse(room.running)
        self.assertIn("중단", room.messages[-1]["text"])
        self.assertEqual(room.typing, {})


class ManagerTests(Base):
    async def test_create_list_restore(self):
        FakeRunner.scripts = {"jake": ["v1"], "clara": [review("APPROVE")], "quinn": [review("APPROVE")]}
        m = Manager(self.cfg, self.bc, runner_factory=FakeRunner)
        proj = self.tmp / "내프로젝트"
        proj.mkdir()
        room = m.create(workspace=str(proj), safe_mode=True)
        self.assertEqual(room.workspace, proj)
        await self.run_to_end(room, "피보나치")
        lst = m.list()
        self.assertEqual(lst[0]["id"], room.conv_id)
        self.assertEqual(lst[0]["title"], "피보나치")
        self.assertTrue(lst[0]["safe_mode"])
        self.assertEqual(m.recent_folders(), [str(proj)])
        self.assertTrue(any(e["type"] == "conversations" for e in self.events))
        # 서버 재시작 흉내: 새 Manager 로 같은 대화를 열면 세션·폴더·전역규칙 설정이 복원됨
        m2 = Manager(self.cfg, self.bc, runner_factory=FakeRunner)
        r2 = m2.get(room.conv_id)
        self.assertEqual(r2.workspace, proj)
        self.assertEqual(r2.runners["quinn"].session_id, "sess-quinn")
        self.assertTrue(r2.runners["jake"].safe_mode)
        self.assertEqual(len(r2.messages), len(room.messages))
        self.assertEqual(m2.list()[0]["title"], "피보나치")
        self.assertIsNone(m2.get("없는대화"))

    async def test_old_conversation_without_title_uses_first_message(self):
        logs = self.cfg.settings.log_dir
        logs.mkdir(parents=True)
        (logs / "20260101_000000.state.json").write_text(json.dumps({"conv_id": "20260101_000000"}), encoding="utf-8")
        (logs / "20260101_000000.jsonl").write_text(json.dumps(
            {"id": "a", "sender": "user", "text": "예전 지시입니다", "ts": 1.0}, ensure_ascii=False) + "\n", encoding="utf-8")
        m = Manager(self.cfg, self.bc, runner_factory=FakeRunner)
        self.assertEqual(m.list()[0]["title"], "예전 지시입니다")
        self.assertEqual(m.get("20260101_000000").title, "예전 지시입니다")

    async def test_create_rejects_missing_folder(self):
        m = Manager(self.cfg, self.bc, runner_factory=FakeRunner)
        with self.assertRaises(ValueError):
            m.create(workspace=str(self.tmp / "없는폴더"))


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cfgdir = self.tmp / "config"
        self.cfgdir.mkdir()
        for n in ("agents.yaml", "settings.yaml"):
            shutil.copy(appconfig.CONFIG_DIR / n, self.cfgdir / n)
        self._orig = appconfig.CONFIG_DIR
        appconfig.CONFIG_DIR = self.cfgdir  # 실제 설정 파일을 건드리지 않도록
        FakeRunner.calls, FakeRunner.gate = [], None

    def tearDown(self):
        appconfig.CONFIG_DIR = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def make_client(self, remote: bool = False):
        """remote=False: PC 에서 직접 연 화면(127.0.0.1:8780). True: 폰(Tailscale → 원격 포트 8790, 로그인 필요)."""
        from fastapi.testclient import TestClient
        from app.server import create_app

        def factory(cfg, bc):
            cfg.settings.log_dir = self.tmp / "logs"
            cfg.settings.workspace_root = self.tmp / "ws"
            cfg.settings.flow = "classic"
            m = Manager(cfg, bc, runner_factory=FakeRunner)
            orig_reload = m.reload

            def reload(new_cfg):  # 설정 저장 후 다시 읽어도 임시 폴더를 쓰게
                new_cfg.settings.log_dir = self.tmp / "logs"
                new_cfg.settings.workspace_root = self.tmp / "ws"
                new_cfg.settings.flow = "classic"
                orig_reload(new_cfg)
            m.reload = reload
            return m

        app = create_app(factory, private_dir=self.tmp / "private")
        if remote:
            return TestClient(app, base_url="https://testserver:8790", client=("127.0.0.1", 50001))
        return TestClient(app, base_url="http://127.0.0.1:8780", client=("127.0.0.1", 50000))

    def test_end_to_end(self):
        FakeRunner.scripts = {"jake": ["fib 완료"], "clara": [review("APPROVE")], "quinn": [review("APPROVE")]}
        with self.make_client() as client:
            self.assertEqual(client.get("/").status_code, 200)
            self.assertEqual(client.get("/static/app.js").status_code, 200)
            conv = client.post("/api/conversations", json={}).json()["id"]
            with client.websocket_connect("/ws") as ws:
                init = ws.receive_json()
                self.assertEqual(init["type"], "init")
                self.assertEqual(init["config"]["agents"]["quinn"]["name"], "Eric")
                self.assertTrue(init["config"]["agents"]["jake"]["avatar"].startswith("/avatars/"))
                ws.send_json({"type": "open", "conv": conv})
                self.assertEqual(ws.receive_json()["type"], "snapshot")
                ws.send_json({"type": "say", "conv": conv, "text": "피보나치"})
                senders = []
                for _ in range(200):
                    ev = ws.receive_json()
                    if ev["type"] == "message":
                        senders.append(ev["message"]["sender"])
                        if ev["message"]["sender"] == "system":
                            break
                self.assertEqual(senders, ["user", "jake", "clara", "quinn", "system"])
            # 파일 API
            up = client.post(f"/api/conv/{conv}/upload", json={"name": "메모.txt", "data": base64.b64encode("안녕".encode()).decode()}).json()
            self.assertEqual(up["path"], "_첨부/메모.txt")
            up2 = client.post(f"/api/conv/{conv}/upload", json={"name": "메모.txt", "data": base64.b64encode(b"x").decode()}).json()
            self.assertEqual(up2["path"], "_첨부/메모_v2.txt")  # 덮어쓰지 않음
            files = client.get(f"/api/conv/{conv}/files").json()["files"]
            self.assertIn("_첨부/메모.txt", [f["path"] for f in files])
            self.assertEqual(client.get(f"/api/conv/{conv}/file", params={"path": "_첨부/메모.txt"}).json()["content"], "안녕")
            self.assertEqual(client.get(f"/api/conv/{conv}/file", params={"path": "../../x"}).status_code, 400)
            self.assertEqual(client.get("/api/conv/없음/files").status_code, 404)
            lst = client.get("/api/conversations").json()["conversations"]
            self.assertEqual(lst[0]["title"], "피보나치")

    def test_config_api(self):
        with self.make_client() as client:
            d = client.get("/api/config").json()
            self.assertEqual(d["editable"]["user_name"], "사장님")
            with client.websocket_connect("/ws") as ws:
                ws.receive_json()
                r = client.put("/api/config", json={"agents": {"jake": {"title": "수석 실무자"}}})
                self.assertEqual(r.status_code, 200)
                ev = ws.receive_json()
                self.assertEqual(ev["type"], "config")
                self.assertEqual(ev["config"]["agents"]["jake"]["title"], "수석 실무자")
            self.assertEqual(client.put("/api/config", json={"agents": {"nobody": {}}}).status_code, 400)
            self.assertTrue(list((self.cfgdir / "backups").glob("backup_*")))

    def test_avatar_upload(self):
        import io
        import app.server as srv
        from PIL import Image
        avatars = self.tmp / "avatars"
        avatars.mkdir()
        orig = srv.AVATAR_DIR
        srv.AVATAR_DIR = avatars  # 실제 사진 폴더를 건드리지 않도록
        try:
            buf = io.BytesIO()
            Image.new("RGB", (800, 600), "red").save(buf, "PNG")
            with self.make_client() as client:
                r = client.post("/api/avatar/clara", json={"name": "me.png", "data": base64.b64encode(buf.getvalue()).decode()})
                self.assertEqual(r.status_code, 200)
                fname = r.json()["avatar"]
                self.assertTrue(fname.startswith("clara_") and fname.endswith("_320.jpg"))
                with Image.open(avatars / fname) as im:
                    self.assertEqual(im.size, (320, 320))  # 가운데 정사각형으로 잘라 축소
                self.assertEqual(client.get("/api/config").json()["editable"]["agents"]["clara"]["avatar"], fname)
                self.assertEqual(client.post("/api/avatar/nobody", json={"name": "x.png", "data": ""}).status_code, 404)
        finally:
            srv.AVATAR_DIR = orig


if __name__ == "__main__":
    unittest.main()
