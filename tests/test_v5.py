"""v5 테스트: 감독 병렬 검토, 감독 실패 시 계속, / 명령 그대로 전달, 최근 대화 전달, 실시간 진행, MCP·메모리 설정 (CLI 호출 없음)."""
import asyncio
import json
import unittest

from app.config import _as_text, load_config
from app.orchestrator import Manager
from app.runners import ClaudeRunner, CodexRunner, RunResult
from tests.test_flow import Base, FakeRunner, review


class ParallelReviewTests(Base):
    async def test_reviewers_run_at_the_same_time(self):
        started, release = [], asyncio.Event()

        class SlowReviewer(FakeRunner):
            async def run(self, prompt, workspace, schema_path=None, activity=None, approve=None, **kw):
                if self.agent.key in ("clara", "quinn"):
                    started.append(self.agent.key)
                    await release.wait()
                return await super().run(prompt, workspace, schema_path, activity, approve, **kw)

        FakeRunner.scripts = {"jake": ["v1"], "clara": [review("APPROVE")], "quinn": [review("APPROVE")]}
        room = self.room()
        room.runner_factory = SlowReviewer
        room.runners = {k: SlowReviewer(a, self.cfg.settings) for k, a in self.cfg.agents.items()}
        await room.user_message("피보나치")
        for _ in range(100):
            await asyncio.sleep(0.01)
            if len(started) == 2:
                break
        self.assertEqual(sorted(started), ["clara", "quinn"])  # 둘 다 먼저 끝나기를 기다리지 않고 시작됨
        self.assertEqual(sorted(room.active), ["clara", "quinn"])
        release.set()
        await room.task
        self.assertTrue(room.messages[-1].get("done"))

    async def test_one_reviewer_fails_task_continues(self):
        FakeRunner.scripts = {"jake": ["v1"], "clara": [None], "quinn": [review("APPROVE")]}
        room = self.room()
        await self.run_to_end(room, "피보나치")
        texts = [m["text"] for m in room.messages if m["sender"] == "system"]
        self.assertTrue(any("Clara 검토 실패" in t and "나머지 감독" in t for t in texts))
        self.assertTrue(room.messages[-1].get("done"))
        self.assertIn("Clara 검토 실패로 제외", room.messages[-2]["text"])

    async def test_all_reviewers_fail_stops(self):
        FakeRunner.scripts = {"jake": ["v1"], "clara": [None], "quinn": [None]}
        room = self.room()
        await self.run_to_end(room, "피보나치")
        self.assertIn("모든 감독의 검토가 실패", room.messages[-1]["text"])


class SlashTests(Base):
    async def test_slash_goes_raw_to_worker_without_review(self):
        FakeRunner.scripts = {"jake": ["## Context Usage ..."], "clara": [], "quinn": []}
        room = self.room()
        await self.run_to_end(room, "/context", mode="review")
        self.assertEqual(FakeRunner.calls[0][1], "/context")          # 감싸지 않고 그대로
        self.assertEqual([k for k, _, _ in FakeRunner.calls], ["jake"])  # 검토 없음

    async def test_mention_slash_to_clara(self):
        FakeRunner.scripts = {"jake": [], "clara": ["결과"], "quinn": []}
        room = self.room()
        await self.run_to_end(room, "@Clara /compact")
        self.assertEqual(FakeRunner.calls[0][:2], ("clara", "/compact"))

    async def test_unavailable_and_codex_and_unknown(self):
        FakeRunner.scripts = {"jake": [], "clara": [], "quinn": []}
        room = self.room(slash_commands=lambda: ["context", "compact", "footing"])
        await room.user_message("/rc")
        self.assertIn("-p(비대화)", room.messages[-1]["text"])
        await room.user_message("/resume")
        self.assertIn("세션이 저장돼", room.messages[-1]["text"])
        await room.user_message("@Eric /context")
        self.assertIn("Codex", room.messages[-1]["text"])
        await room.user_message("/없는명령")
        self.assertIn("목록에 없습니다", room.messages[-1]["text"])
        self.assertEqual(FakeRunner.calls, [])  # 어느 것도 CLI 를 부르지 않음

    async def test_slash_while_running_is_refused(self):
        FakeRunner.gate = asyncio.Event()
        FakeRunner.scripts = {"jake": ["v1"], "clara": [review("APPROVE")], "quinn": [review("APPROVE")]}
        room = self.room()
        await room.user_message("피보나치")
        await asyncio.sleep(0.05)
        await room.user_message("/compact")
        self.assertIn("진행 중에는", room.messages[-1]["text"])
        self.assertEqual(room.pending, [])
        FakeRunner.gate.set()
        await room.task


class ContextTests(Base):
    async def test_quick_and_mention_include_recent_conversation(self):
        FakeRunner.scripts = {"jake": ["표2 만들었습니다", "네"], "clara": ["다시 봤습니다"], "quinn": []}
        room = self.room()
        await self.run_to_end(room, "ROAS 표2 만들어줘", mode="quick")
        await self.run_to_end(room, "@Clara 다시해")
        clara_prompt = FakeRunner.calls[1][1]
        self.assertIn("[최근 대화", clara_prompt)
        self.assertIn("ROAS 표2 만들어줘", clara_prompt)
        self.assertIn("표2 만들었습니다", clara_prompt)
        self.assertNotIn("@Clara 다시해\n", clara_prompt.split("[사장님의 메시지")[0])  # 지금 메시지는 맥락에 중복 안 함


class LiveProgressTests(Base):
    async def test_live_tokens_limits_and_meta_reach_room_and_manager(self):
        class LiveRunner(FakeRunner):
            async def run(self, prompt, workspace, schema_path=None, activity=None, approve=None, on_limits=None, on_meta=None):
                activity("💬 원장을 읽는 중", 1200)
                on_limits({"five_hour": {"pct": 61.0, "resets_at": 1}})
                on_meta({"slash_commands": ["context", "footing"]})
                await asyncio.sleep(0.02)
                return await super().run(prompt, workspace, schema_path, activity, approve)
        FakeRunner.scripts = {"jake": ["답"], "clara": [], "quinn": []}
        m = Manager(self.cfg, self.bc, runner_factory=LiveRunner)
        room = m.create()
        await self.run_to_end(room, "질문", mode="quick")
        await asyncio.sleep(0.05)
        typing = [e for e in self.events if e["type"] == "typing" and e.get("tokens")]
        self.assertEqual(typing[0]["tokens"], 1200)
        self.assertTrue(any(e["type"] == "usage_limits" and e["limits"]["claude"]["five_hour"]["pct"] == 61.0 for e in self.events))
        self.assertEqual(m.slash_commands, ["context", "footing"])
        self.assertTrue(any(e["type"] == "slash_commands" for e in self.events))
        self.assertEqual(Manager(self.cfg, self.bc, runner_factory=FakeRunner).slash_commands, ["context", "footing"])  # 저장됨


class RunnerV5Tests(unittest.TestCase):
    def setUp(self):
        self.cfg = load_config()

    def test_claude_live_tokens_notes_and_init_meta(self):
        from tests.test_units import FakeCtx
        r = ClaudeRunner(self.cfg.agents["jake"], self.cfg.settings)
        ctx = FakeCtx()
        r.on_line(json.dumps({"type": "system", "subtype": "init", "slash_commands": ["context", "footing"]}), ctx)
        self.assertEqual(ctx.metas, [{"slash_commands": ["context", "footing"]}])
        u = {"input_tokens": 10, "cache_read_input_tokens": 1000, "cache_creation_input_tokens": 5, "output_tokens": 50}
        r.on_line(json.dumps({"type": "assistant", "message": {"id": "m1", "usage": u, "content": [{"type": "text", "text": "원장을 먼저 보겠습니다.\n둘째 줄"}]}}), ctx)
        r.on_line(json.dumps({"type": "assistant", "message": {"id": "m1", "usage": u, "content": [{"type": "tool_use", "name": "Read", "input": {"file_path": "C:/x/원장.xlsx"}}]}}), ctx)
        r.on_line(json.dumps({"type": "assistant", "message": {"id": "m2", "usage": u, "content": [{"type": "text", "text": "끝"}]}}), ctx)
        self.assertEqual(ctx.acts, ["💬 원장을 먼저 보겠습니다.", "읽는 중: 원장.xlsx", "💬 끝"])
        self.assertEqual(ctx.tokens, [1065, 1065, 2130])  # 같은 메시지 id 는 한 번만 셈

    def test_reviewer_mcp_allowed_and_codex_auto_review(self):
        r = ClaudeRunner(self.cfg.agents["clara"], self.cfg.settings)
        cmd = r.build_command(r"C:\ws", None)
        for s in ("mcp__claude_ai_DART_Audit_MCP", "mcp__moleg", "mcp__verifier"):
            self.assertIn(s, cmd)
        self.cfg.settings.codex_exe = r"C:\fake\codex.exe"
        c = CodexRunner(self.cfg.agents["quinn"], self.cfg.settings)
        self.assertIn('approvals_reviewer="auto_review"', c.build_command(r"C:\ws", None))
        c.session_id, c.started = "t1", True
        self.assertIn('approvals_reviewer="auto_review"', c.build_command(r"C:\ws", None))  # 이어갈 때도

    def test_codex_intermediate_notes(self):
        from tests.test_units import FakeCtx
        self.cfg.settings.codex_exe = r"C:\fake\codex.exe"
        c = CodexRunner(self.cfg.agents["quinn"], self.cfg.settings)
        ctx = FakeCtx()
        c.on_line(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "수식부터 다시 계산하겠습니다."}}), ctx)
        c.on_line(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": '{"verdict":"APPROVE"}'}}), ctx)
        c.on_line(json.dumps({"type": "item.started", "item": {"type": "web_search", "query": "기준금리"}}), ctx)
        self.assertEqual(ctx.acts, ["💬 수식부터 다시 계산하겠습니다.", "웹 검색 중: 기준금리"])

    def test_memory_dir_in_prompts_and_checklist_text(self):
        sp = self.cfg.agents["jake"].system_prompt(r"C:\ws")
        self.assertIn(self.cfg.settings.memory_dir, sp)
        self.assertNotIn("{memory_dir}", sp)
        self.assertEqual(_as_text({"명백한 성능 문제(예": "지수 시간"}), "명백한 성능 문제(예: 지수 시간")
        eric = self.cfg.agents["quinn"]
        self.assertTrue(all(isinstance(c, str) and "[object Object]" not in c for c in eric.checklist))
        self.assertTrue(any("외부 자료" in c for c in eric.checklist))


if __name__ == "__main__":
    unittest.main()
