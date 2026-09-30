"""v6 테스트: 바뀐 파일 목록 전달, 잠금 파일 제외, 대화별 감독 제외(Eric), /compact 안내, 캐시 끄기, Eric 독립 대사 지침."""
import asyncio
import unittest

from app.config import load_config
from app.orchestrator import Manager, changed_between, is_temp_file, workspace_snapshot
from app.runners import RunResult
from tests.test_flow import Base, FakeRunner, review


class ChangedFilesTests(Base):
    async def test_reviewers_get_files_changed_this_round(self):
        class WritingWorker(FakeRunner):
            async def run(self, prompt, workspace, schema_path=None, activity=None, approve=None, **kw):
                if self.agent.key == "jake":
                    from pathlib import Path
                    (Path(workspace) / "표2_v3.xlsx").write_bytes(b"x")
                    (Path(workspace) / "~$표2_v3.xlsx").write_bytes(b"lock")      # 엑셀 잠금 파일은 제외돼야 함
                return await super().run(prompt, workspace, schema_path, activity, approve, **kw)

        FakeRunner.scripts = {"jake": ["표2 만들었습니다"], "clara": [review("APPROVE")], "quinn": [review("APPROVE")]}
        room = self.room()
        (room.workspace / "원장.xlsx").write_bytes(b"old")  # 원래 있던 파일은 목록에 안 나와야 함
        room.runners = {k: WritingWorker(a, self.cfg.settings) for k, a in self.cfg.agents.items()}
        await self.run_to_end(room, "표2 만들어줘")
        clara_prompt = [p for k, p, _ in FakeRunner.calls if k == "clara"][0]
        self.assertIn("이번 라운드에 새로 생기거나 바뀐 파일 — 1개", clara_prompt)
        self.assertIn("- 표2_v3.xlsx", clara_prompt)
        self.assertNotIn("원장.xlsx", clara_prompt.split("[이번 라운드")[1])
        self.assertNotIn("~$", clara_prompt)
        jake_msg = next(m for m in room.messages if m["sender"] == "jake")
        self.assertEqual(jake_msg["changed_files"], ["표2_v3.xlsx"])

    def test_snapshot_helpers(self):
        self.assertTrue(is_temp_file("~$wp_예시사.xlsx"))
        self.assertTrue(is_temp_file(".~lock.a.xlsx#"))
        self.assertFalse(is_temp_file("wp_예시사.xlsx"))
        before = {"a": (1, 1), "b": (1, 1)}
        after = {"a": (1, 1), "b": (2, 5), "c": (3, 1)}
        self.assertEqual(changed_between(before, after), ["c", "b"])  # 최근 수정 순


class ExcludeTests(Base):
    async def test_exclude_eric_at_creation(self):
        FakeRunner.scripts = {"jake": ["v1"], "clara": [review("APPROVE")], "quinn": []}
        m = Manager(self.cfg, self.bc, runner_factory=FakeRunner)
        room = m.create(exclude=["quinn"])
        await self.run_to_end(room, "피보나치")
        self.assertEqual([k for k, _, _ in FakeRunner.calls], ["jake", "clara"])  # Eric 은 호출 안 됨
        self.assertTrue(room.messages[-1].get("done"))
        self.assertEqual(m.list()[0]["excluded"], ["quinn"])
        self.assertEqual(Manager(self.cfg, self.bc, runner_factory=FakeRunner).get(room.conv_id).excluded, ["quinn"])

    async def test_mention_excluded_is_refused_and_toggle(self):
        FakeRunner.scripts = {"jake": [], "clara": [], "quinn": ["답"]}
        room = self.room()
        await room.set_excluded("quinn", True)
        await room.user_message("@Eric 확인해줘")
        self.assertIn("빠져 있습니다", room.messages[-1]["text"])
        self.assertEqual(FakeRunner.calls, [])
        await room.set_excluded("quinn", False)
        await self.run_to_end(room, "@Eric 확인해줘")
        self.assertEqual(FakeRunner.calls[0][0], "quinn")
        with self.assertRaises(ValueError):
            await room.set_excluded("jake", True)  # 실무자는 뺄 수 없음

    async def test_all_reviewers_excluded_finishes_without_review(self):
        FakeRunner.scripts = {"jake": ["v1"], "clara": [], "quinn": []}
        room = self.room(exclude=["clara", "quinn"])
        await self.run_to_end(room, "피보나치")
        self.assertIn("검토 없이 마쳤습니다", room.messages[-1]["text"])
        self.assertEqual(len(FakeRunner.calls), 1)


class CompactTipTests(Base):
    async def test_tip_once_when_answer_is_heavy(self):
        class Heavy(FakeRunner):
            async def run(self, *a, **kw):
                res = await super().run(*a, **kw)
                res.usage = {"engine": "claude", "total_tokens": 1_370_000}
                return res
        FakeRunner.scripts = {"jake": ["답1", "답2"], "clara": [], "quinn": []}
        room = Base.room(self)
        room.runners = {k: Heavy(a, self.cfg.settings) for k, a in self.cfg.agents.items()}
        await self.run_to_end(room, "질문1", mode="quick")
        await self.run_to_end(room, "질문2", mode="quick")
        tips = [m for m in room.messages if m["sender"] == "system" and "/compact" in m["text"]]
        self.assertEqual(len(tips), 1)
        self.assertIn("1.37M", tips[0]["text"])


class ServerV6Tests(unittest.TestCase):
    def test_cache_header_lock_filter_exclude_api(self):
        from tests.test_flow import ServerTests
        st = ServerTests("test_config_api")
        st.setUp()
        try:
            with st.make_client() as client:
                self.assertEqual(client.get("/").headers.get("cache-control"), "no-cache")
                self.assertEqual(client.get("/static/app.js").headers.get("cache-control"), "no-cache")
                cid = client.post("/api/conversations", json={"exclude": ["quinn"]}).json()["id"]
                ws = next(c for c in client.get("/api/conversations").json()["conversations"] if c["id"] == cid)
                self.assertEqual(ws["excluded"], ["quinn"])
                from pathlib import Path
                base = Path(ws["workspace"])
                (base / "a.xlsx").write_bytes(b"x")
                (base / "~$a.xlsx").write_bytes(b"lock")
                names = [f["path"] for f in client.get(f"/api/conv/{cid}/files").json()["files"]]
                self.assertEqual(names, ["a.xlsx"])
                r = client.post(f"/api/conv/{cid}/exclude", json={"agent": "quinn", "excluded": False})
                self.assertEqual(r.json()["summary"]["excluded"], [])
                self.assertEqual(client.post(f"/api/conv/{cid}/exclude", json={"agent": "jake"}).status_code, 400)
        finally:
            st.tearDown()

    def test_eric_independent_reconciliation_rule(self):
        sp = load_config().agents["quinn"].system_prompt("C:/ws")
        self.assertIn("Jake가 만든 스크립트·중간 파일을 다시 돌린 결과로 대사하지 않습니다", sp)
        self.assertIn("원자료", sp)


if __name__ == "__main__":
    unittest.main()
