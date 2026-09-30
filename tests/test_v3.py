"""v3 기능 테스트: 사용량 파싱, 산출물 미리보기, 한도 알림 (CLI 호출 없음)."""
import asyncio
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from app import usage
from app.config import load_config
from app.orchestrator import Manager
from app.preview import preview
from app.runners import ClaudeRunner, CodexRunner, RunResult
from tests.test_flow import Base, FakeRunner


class UsageParseTests(unittest.TestCase):
    def test_claude_usage_and_limits(self):
        cfg = load_config()
        r = ClaudeRunner(cfg.agents["jake"], cfg.settings)
        r.build_command(r"C:\ws", None)

        class Ctx:
            msg_tokens = {}
            lims = []
            def activity(self, m=None, tokens=None): pass
            def limits(self, lim): self.lims.append(lim)
            def meta(self, info): pass
            def close_stdin(self): pass
        ctx = Ctx()
        # 실측 형식 그대로
        r.on_line(json.dumps({"type": "rate_limit_event", "rate_limit_info": {"status": "allowed", "unifiedWindows": {
            "five_hour": {"utilization": 0.58, "resetsAt": 1790591400}, "seven_day": {"utilization": 0.24, "resetsAt": 1790899200}}}}), ctx)
        self.assertEqual(ctx.lims[0]["five_hour"]["pct"], 58.0)  # 답변 도중 바로 알림
        res = r.parse_output(json.dumps({"type": "result", "session_id": "s", "result": "OK", "is_error": False,
                                         "total_cost_usd": 0.1498104,
                                         "modelUsage": {"claude-opus-5-5": {"inputTokens": 2, "outputTokens": 4, "cacheReadInputTokens": 16892,
                                                                            "cacheCreationInputTokens": 18293, "costUSD": 0.14}}}))
        self.assertEqual(res.limits, {"five_hour": {"pct": 58.0, "resets_at": 1790591400}, "seven_day": {"pct": 24.0, "resets_at": 1790899200}})
        self.assertEqual(res.usage["total_tokens"], 2 + 4 + 16892 + 18293)
        self.assertEqual(res.usage["models"], ["claude-opus-5-5"])
        self.assertEqual(res.usage["cost_usd"], 0.1498)

    def test_codex_usage_and_rollout(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            day = tmp / "2026" / "09" / "28"
            day.mkdir(parents=True)
            tid = "01a0e70f-2d93-75b3-9c21-fe788aed4d9b"
            lines = [
                {"type": "turn_context", "payload": {"model": "gpt-5.6-luna", "effort": "low"}},
                {"type": "event_msg", "payload": {"type": "token_count", "rate_limits": {
                    "primary": {"used_percent": 3.0, "window_minutes": 300, "resets_at": 1790600282},
                    "secondary": {"used_percent": 1.0, "window_minutes": 10080, "resets_at": 1791012499}}}},
            ]
            (day / f"rollout-2026-09-28T17-08-46-{tid}.jsonl").write_text("\n".join(json.dumps(x) for x in lines), encoding="utf-8")
            path = usage.find_codex_rollout(tid, tmp)
            self.assertIsNotNone(path)
            limits, model = usage.read_codex_rollout(path)
            self.assertEqual(model, "gpt-5.6-luna")
            self.assertEqual(limits["five_hour"]["pct"], 3.0)
            self.assertEqual(limits["seven_day"]["resets_at"], 1791012499)
            self.assertIsNone(usage.find_codex_rollout("없는-id", tmp))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        u = usage.codex_usage({"input_tokens": 16462, "cached_input_tokens": 11008, "output_tokens": 7, "reasoning_output_tokens": 0}, "gpt-x")
        self.assertEqual(u["total_tokens"], 16469)
        self.assertEqual(u["cache_read_tokens"], 11008)
        self.assertIsNone(u["cost_usd"])

    def test_codex_runner_collects_turn_usage(self):
        cfg = load_config()
        cfg.settings.codex_exe = r"C:\fake\codex.exe"
        r = CodexRunner(cfg.agents["quinn"], cfg.settings)
        res = r.parse_output("\n".join([
            json.dumps({"type": "thread.started", "thread_id": "no-such-thread"}),
            json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "끝"}}),
            json.dumps({"type": "turn.completed", "usage": {"input_tokens": 100, "cached_input_tokens": 40, "output_tokens": 5}}),
        ]))
        self.assertEqual(res.usage["total_tokens"], 105)
        self.assertIsNone(res.limits)  # 세션 파일이 없으면 한도는 비움

    def _claude_result(self, cost, models):
        return json.dumps({"type": "result", "session_id": "s", "result": "ok", "is_error": False, "total_cost_usd": cost,
                           "usage": {"input_tokens": 7, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0, "output_tokens": 3},
                           "modelUsage": {m: {"inputTokens": i, "outputTokens": o, "cacheReadInputTokens": 0, "cacheCreationInputTokens": 0}
                                          for m, (i, o) in models.items()}})

    def test_claude_per_answer_from_cumulative(self):
        """실측: modelUsage·total_cost_usd 는 세션 누적 → 차이로 답변 1건 사용량을 구해야 함."""
        cfg = load_config()
        r = ClaudeRunner(cfg.agents["jake"], cfg.settings)
        r._was_started = False
        u1 = r.parse_output(self._claude_result(0.006, {"sonnet": (2, 3)})).usage
        self.assertEqual((u1["total_tokens"], u1["cost_usd"]), (5, 0.006))
        r._was_started = True
        u2 = r.parse_output(self._claude_result(0.0078, {"sonnet": (2, 3), "haiku": (1596, 48)})).usage
        self.assertEqual(u2["total_tokens"], 1596 + 48)  # 누적 1649 − 직전 5
        self.assertEqual(u2["cost_usd"], 0.0018)
        self.assertEqual(u2["models"], ["haiku"])  # 이전 답변에서만 쓴 모델은 빼고
        # 세션 상태 저장·복원 후에도 이어서 계산
        r2 = ClaudeRunner(cfg.agents["jake"], cfg.settings)
        r2.restore(r.state())
        r2._was_started = True
        u3 = r2.parse_output(self._claude_result(0.0098, {"sonnet": (2, 3), "haiku": (3303, 96)})).usage
        self.assertEqual(u3["total_tokens"], (3303 - 1596) + (96 - 48))

    def test_claude_old_session_without_snapshot_uses_call_usage(self):
        cfg = load_config()
        r = ClaudeRunner(cfg.agents["jake"], cfg.settings)
        r._was_started = True  # 예전에 시작된 세션, 기준점 없음
        u = r.parse_output(self._claude_result(5.0, {"opus": (100000, 900)})).usage
        self.assertEqual(u["total_tokens"], 10)  # 호출 단위 usage(7+3) 사용
        self.assertIsNone(u["cost_usd"])          # 누적 금액은 차이를 알 수 없어 비움
        self.assertFalse(u.get("cumulative"))

    def test_codex_last_turn_delta(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            f = tmp / "r.jsonl"
            tc = lambda i, o: json.dumps({"type": "event_msg", "payload": {"type": "token_count", "info": {
                "total_token_usage": {"input_tokens": i, "cached_input_tokens": 0, "output_tokens": o}}}})
            f.write_text("\n".join([json.dumps({"type": "event_msg", "payload": {"type": "task_started"}}), tc(1000, 10), tc(1500, 20),
                                    json.dumps({"type": "event_msg", "payload": {"type": "task_started"}}), tc(1560, 21)]), encoding="utf-8")
            u = usage.codex_last_turn(f)
            self.assertEqual((u["input_tokens"], u["output_tokens"], u["total_tokens"]), (60, 1, 61))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_limit_store_persists(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            s = usage.LimitStore(tmp / "u.json")
            self.assertFalse(s.update("claude", None))
            self.assertTrue(s.update("claude", {"five_hour": {"pct": 10, "resets_at": 1}}))
            self.assertEqual(usage.LimitStore(tmp / "u.json").data["claude"]["five_hour"]["pct"], 10)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class PreviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_xlsx_values_and_hidden_sheet(self):
        from openpyxl import Workbook
        wb = Workbook()
        ws = wb.active
        ws.title = "손익"
        ws.append(["항목", "금액"])
        ws.append(["매출", 1234567])
        ws.append(["이익", "=B2*0.1"])
        h = wb.create_sheet("숨김")
        h.sheet_state = "hidden"
        p = self.tmp / "a.xlsx"
        wb.save(p)
        d = preview(p, "/raw")
        self.assertEqual(d["kind"], "table")
        self.assertEqual([s["name"] for s in d["sheets"]], ["손익"])  # 숨김 시트 제외
        self.assertEqual(d["sheets"][0]["rows"][1], ["매출", "1,234,567"])
        self.assertEqual(d["sheets"][0]["rows"][2], ["이익", "=B2*0.1"])  # 계산값이 없으면 수식을 보여줌
        self.assertEqual(p.stat().st_size, p.stat().st_size)  # 읽기만 함

    def test_csv_cp949(self):
        p = self.tmp / "b.csv"
        p.write_bytes("항목,금액\n매출,100\n".encode("cp949"))
        d = preview(p, "/raw")
        self.assertEqual(d["sheets"][0]["rows"], [["항목", "금액"], ["매출", "100"]])

    def test_docx_pptx_md_pdf(self):
        import docx
        from pptx import Presentation
        doc = docx.Document()
        doc.add_heading("검토 결과", 1)
        doc.add_paragraph("본문입니다")
        t = doc.add_table(rows=1, cols=2)
        t.rows[0].cells[0].text, t.rows[0].cells[1].text = "A", "B"
        doc.save(self.tmp / "c.docx")
        d = preview(self.tmp / "c.docx", "/raw")
        self.assertEqual([b["type"] for b in d["blocks"]], ["h", "p", "table"])
        prs = Presentation()
        s = prs.slides.add_slide(prs.slide_layouts[1])
        s.shapes.title.text = "제목 슬라이드"
        prs.save(self.tmp / "d.pptx")
        self.assertIn("제목 슬라이드", preview(self.tmp / "d.pptx", "/raw")["slides"][0]["texts"])
        (self.tmp / "e.md").write_text("# 제목\n- 항목", encoding="utf-8")
        self.assertEqual(preview(self.tmp / "e.md", "/raw")["kind"], "markdown")
        (self.tmp / "f.pdf").write_bytes(b"%PDF-1.4")
        self.assertEqual(preview(self.tmp / "f.pdf", "/raw?path=f.pdf"), {"kind": "pdf", "url": "/raw?path=f.pdf"})

    def test_broken_xlsx_reports_error(self):
        p = self.tmp / "broken.xlsx"
        p.write_bytes(b"not a zip")
        self.assertEqual(preview(p, "/raw")["kind"], "error")


class UsageFlowTests(Base):
    async def test_usage_attached_and_limits_broadcast(self):
        class UsageRunner(FakeRunner):
            async def run(self, *a, **kw):
                res = await super().run(*a, **kw)
                res.usage = {"engine": self.agent.engine, "total_tokens": 1000, "input_tokens": 900, "output_tokens": 100,
                             "cache_read_tokens": 0, "cache_write_tokens": 0, "cost_usd": 0.5 if self.agent.engine == "claude" else None, "models": []}
                res.limits = {"five_hour": {"pct": 42.0, "resets_at": 1}}
                return res
        FakeRunner.scripts = {"jake": ["답"], "clara": [], "quinn": []}
        m = Manager(self.cfg, self.bc, runner_factory=UsageRunner)
        room = m.create()
        await self.run_to_end(room, "질문", mode="quick")
        self.assertEqual(room.messages[-1]["usage"]["total_tokens"], 1000)
        ev = [e for e in self.events if e["type"] == "usage_limits"]
        self.assertEqual(ev[-1]["limits"]["claude"]["five_hour"]["pct"], 42.0)
        self.assertTrue((self.cfg.settings.log_dir / "usage_limits.json").exists())


class ArchiveTests(Base):
    async def test_archive_hides_keeps_files_and_restores(self):
        FakeRunner.scripts = {"jake": ["답", "또 답"], "clara": [], "quinn": []}
        m = Manager(self.cfg, self.bc, runner_factory=FakeRunner)
        room = m.create()
        (room.workspace / "산출물.txt").write_text("x", encoding="utf-8")
        await self.run_to_end(room, "질문", mode="quick")
        await room.set_archived(True)
        self.assertTrue(m.list()[0]["archived"])
        self.assertTrue((room.workspace / "산출물.txt").exists())          # 파일은 그대로
        self.assertTrue(room.store.jsonl.exists())                          # 기록도 그대로
        # 서버 재시작 후에도 보관 상태 유지
        m2 = Manager(self.cfg, self.bc, runner_factory=FakeRunner)
        self.assertTrue(m2.list()[0]["archived"])
        r2 = m2.get(room.conv_id)
        self.assertTrue(r2.archived)
        # 보관된 대화에 메시지를 보내면 자동으로 다시 꺼냄
        await self.run_to_end(r2, "다시", mode="quick")
        self.assertFalse(r2.archived)
        self.assertFalse(m2.list()[0]["archived"])

    async def test_archive_running_stops_first(self):
        FakeRunner.gate = asyncio.Event()
        FakeRunner.scripts = {"jake": ["v1"], "clara": [], "quinn": []}
        room = self.room()
        await room.user_message("피보나치")
        await asyncio.sleep(0.05)
        self.assertTrue(room.running)
        await room.set_archived(True)
        self.assertFalse(room.running)
        self.assertTrue(room.archived)
        self.assertIn("중단", room.messages[-1]["text"])


class ArchiveApiTests(unittest.TestCase):
    def test_archive_endpoint(self):
        from tests.test_flow import ServerTests
        st = ServerTests("test_config_api")
        st.setUp()
        try:
            with st.make_client() as client:
                cid = client.post("/api/conversations", json={}).json()["id"]
                r = client.post(f"/api/conv/{cid}/archive", json={"archived": True})
                self.assertEqual(r.status_code, 200)
                self.assertTrue(r.json()["summary"]["archived"])
                lst = client.get("/api/conversations").json()["conversations"]
                self.assertTrue(next(c for c in lst if c["id"] == cid)["archived"])
                self.assertFalse(client.post(f"/api/conv/{cid}/archive", json={"archived": False}).json()["summary"]["archived"])
                self.assertEqual(client.post("/api/conv/없음/archive", json={}).status_code, 404)
        finally:
            st.tearDown()


class SttTests(unittest.TestCase):
    def test_prompt_includes_vocab_and_names(self):
        from app.stt import build_prompt
        p = build_prompt(["손익", "영업이익"], ["Jake", "사장님", ""])
        self.assertIn("손익, 영업이익, Jake, 사장님", p)
        self.assertEqual(build_prompt([], []), "")

    def test_stt_endpoint_with_fake_transcriber(self):
        import base64
        from fastapi.testclient import TestClient
        from app.server import create_app
        from tests.test_flow import ServerTests

        class FakeStt:
            status = "ready"
            def __init__(self): self.calls = []
            def is_downloaded(self, size): return True
            def load(self, size): return None
            def transcribe(self, audio, mime, size, prompt):
                self.calls.append((len(audio), mime, size, prompt))
                return {"text": "영업이익 계산해줘", "seconds": 0.1, "audio_seconds": 1.0, "model": size}

        st = ServerTests("test_config_api")
        st.setUp()
        fake = FakeStt()
        try:
            def factory(cfg, bc):
                cfg.settings.log_dir = st.tmp / "logs"
                cfg.settings.workspace_root = st.tmp / "ws"
                return Manager(cfg, bc, runner_factory=FakeRunner)
            with TestClient(create_app(factory, transcriber=fake, private_dir=st.tmp / "private"),
                            client=("127.0.0.1", 50000)) as client:
                r = client.post("/api/stt", json={"data": "data:audio/webm;base64," + base64.b64encode(b"x" * 2000).decode(),
                                                  "mime": "audio/webm;codecs=opus"})
                self.assertEqual(r.status_code, 200)
                self.assertEqual(r.json()["text"], "영업이익 계산해줘")
                size, mime, model, prompt = fake.calls[0]
                self.assertEqual((size, model), (2000, "small"))
                self.assertIn("손익", prompt)      # 기본 용어 힌트
                self.assertIn("Eric", prompt)      # 참여자 이름
                self.assertEqual(client.post("/api/stt", json={"data": ""}).status_code, 400)
                self.assertTrue(client.get("/api/stt/status").json()["downloaded"])
                # 설정 저장: 음성 인식 방식·모델·용어
                client.put("/api/config", json={"settings": {"stt_engine": "browser", "stt_model": "base", "stt_vocab": ["풋팅", " "]}})
                ed = client.get("/api/config").json()["editable"]["settings"]
                self.assertEqual((ed["stt_engine"], ed["stt_model"], ed["stt_vocab"]), ("browser", "base", ["풋팅"]))
                self.assertEqual(client.get("/api/config").json()["public"]["stt"], {"engine": "browser", "model": "base"})
        finally:
            st.tearDown()


if __name__ == "__main__":
    unittest.main()
