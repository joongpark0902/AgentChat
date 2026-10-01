"""v10: 전역 규칙 담당자별 적용 + 3단계 진행(1 방향 → 2 초안 → 3 마무리)."""
import json
import shutil
import tempfile
import unittest
from pathlib import Path

import app.config as appconfig
from app.config import editable_config, load_config, save_editable_config
from app.orchestrator import Manager
from tests.test_flow import Base, FakeRunner, review


class Staged(Base):
    def setUp(self):
        super().setUp()
        self.cfg.settings.flow = "staged"
        self.cfg.settings.max_rounds = 3

    async def test_plan_then_draft_approved_ends_at_round2(self):
        FakeRunner.scripts = {"jake": ["계획: 시트 3개", "초안 작성함"],
                              "clara": [review("APPROVE"), review("APPROVE")],
                              "quinn": [review("APPROVE"), review("APPROVE")]}
        room = self.room()
        await self.run_to_end(room, "손익 요약표")
        # 계획을 둘 다 승인해도 끝나지 않고 초안으로 넘어간다
        self.assertEqual(self.senders(room), ["user", "jake", "clara", "quinn", "jake", "clara", "quinn", "system", "jake"])
        jake = self.called("jake")
        self.assertIn("방향 잡기", jake[0])
        self.assertIn("본 작업(산출물 작성·계산)을 하지 말고", jake[0])
        self.assertIn("라운드 2 — 초안", jake[1])
        self.assertIn("계획 검토", self.called("clara")[0])
        self.assertIn("라운드 2(초안)", self.called("clara")[1])
        self.assertEqual(room.messages[1].get("stage"), "plan")
        self.assertEqual(room.messages[4].get("stage"), "draft")
        self.assertTrue(room.messages[-1].get("done"))
        self.assertIn("라운드 2", room.messages[-2]["text"])

    async def test_round3_then_escalate_to_user(self):
        FakeRunner.scripts = {"jake": ["계획", "초안", "마무리"],
                              "clara": [review("APPROVE")] * 3,
                              "quinn": [review("REVISE", ("blocker", "합계 불일치"))] * 3}
        room = self.room()
        await self.run_to_end(room, "손익 요약표")
        self.assertEqual(len(self.called("jake")), 3)
        self.assertIn("[라운드 3]", self.called("jake")[2])
        self.assertTrue(room.messages[-1].get("escalation"))
        self.assertIn("3라운드 안에", room.messages[-2]["text"])
        self.assertIn("합계 불일치", room.messages[-2]["text"])

    async def test_plan_revise_feedback_reaches_draft(self):
        FakeRunner.scripts = {"jake": ["계획", "초안"],
                              "clara": [review("REVISE", ("blocker", "연도 헤더 누락")), review("APPROVE")],
                              "quinn": [review("APPROVE"), review("APPROVE")]}
        room = self.room()
        await self.run_to_end(room, "손익 요약표")
        self.assertIn("연도 헤더 누락", self.called("jake")[1])
        self.assertTrue(room.messages[-1].get("done"))

    async def test_no_reviewers_falls_back_to_classic(self):
        FakeRunner.scripts = {"jake": ["바로 완성"], "clara": [], "quinn": []}
        room = self.room(exclude=["clara", "quinn"])
        await self.run_to_end(room, "손익 요약표")
        self.assertNotIn("방향 잡기", self.called("jake")[0])
        self.assertTrue(room.messages[-1].get("done"))

    async def test_max_rounds_1_still_gets_draft(self):
        self.cfg.settings.max_rounds = 1
        FakeRunner.scripts = {"jake": ["계획", "초안"], "clara": [review("APPROVE")] * 2,
                              "quinn": [review("APPROVE")] * 2}
        room = self.room()
        await self.run_to_end(room, "손익 요약표")
        self.assertEqual(len(self.called("jake")), 2)
        self.assertTrue(room.messages[-1].get("done"))


class RulesPerAgent(Base):
    async def test_per_agent_switch(self):
        room = self.room(global_rules={"jake": True, "clara": False})
        self.assertFalse(room.runners["jake"].safe_mode)
        self.assertTrue(room.runners["clara"].safe_mode)
        self.assertEqual(room.summary()["rules_off"], ["clara"])
        self.assertFalse(room.summary()["safe_mode"])
        # 재시작 후에도 대화별로 고정
        r2 = self.room(conv_id=room.conv_id)
        self.assertEqual(r2.rules_off, ["clara"])
        self.assertTrue(r2.runners["clara"].safe_mode)
        self.assertFalse(r2.runners["jake"].safe_mode)

    async def test_old_state_safe_mode_means_all_off(self):
        logs = self.cfg.settings.log_dir
        logs.mkdir(parents=True)
        (logs / "20260101_000000.state.json").write_text(
            json.dumps({"conv_id": "20260101_000000", "safe_mode": True}), encoding="utf-8")
        m = Manager(self.cfg, self.bc, runner_factory=FakeRunner)
        self.assertEqual(sorted(m.list()[0]["rules_off"]), ["clara", "jake"])
        room = m.get("20260101_000000")
        self.assertTrue(room.runners["jake"].safe_mode and room.runners["clara"].safe_mode)
        self.assertTrue(room.summary()["safe_mode"])

    async def test_default_from_settings(self):
        self.cfg.settings.claude_rules = {"clara": False}
        self.cfg.settings.claude_safe_mode = False
        room = self.room()
        self.assertEqual(room.rules_off, ["clara"])


class ConfigRules(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        for n in ("agents.yaml", "settings.yaml"):
            shutil.copy(appconfig.CONFIG_DIR / n, self.tmp / n)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_save_per_agent_and_flow(self):
        save_editable_config({"settings": {"global_rules": True,
                                           "global_rules_agents": {"jake": True, "clara": False, "nobody": True},
                                           "flow": "staged"}}, self.tmp)
        ed = editable_config(self.tmp)["settings"]
        self.assertEqual(ed["global_rules_agents"], {"jake": True, "clara": False})  # Codex(Eric)는 목록에 없음
        self.assertEqual(ed["flow"], "staged")
        cfg = load_config(self.tmp)
        self.assertTrue(cfg.settings.rules_on("jake"))
        self.assertFalse(cfg.settings.rules_on("clara"))
        self.assertEqual(cfg.settings.flow, "staged")
        self.assertEqual(cfg.public()["flow"], "staged")

    def test_legacy_bool_clears_per_agent(self):
        save_editable_config({"settings": {"global_rules_agents": {"jake": True, "clara": False}}}, self.tmp)
        save_editable_config({"settings": {"global_rules": False}}, self.tmp)
        cfg = load_config(self.tmp)
        self.assertFalse(cfg.settings.rules_on("jake"))
        self.assertFalse(cfg.settings.rules_on("clara"))


class FinalReport(Base):
    async def test_final_report_failure_falls_back_to_system(self):
        FakeRunner.final_ok = False  # 최종 보고 호출도 대본에서 꺼냄 → None = 실패
        try:
            FakeRunner.scripts = {"jake": ["v1", None], "clara": [review("APPROVE")], "quinn": [review("APPROVE")]}
            room = self.room()
            await self.run_to_end(room, "손익 요약표")
        finally:
            FakeRunner.final_ok = True
        last = room.messages[-1]
        self.assertEqual(last["sender"], "system")
        self.assertTrue(last.get("done"))  # 알림은 그대로 '완료'
        self.assertIn("최종 보고 실패", last["text"])

    async def test_no_reviewers_no_extra_report(self):
        FakeRunner.scripts = {"jake": ["완성"], "clara": [], "quinn": []}
        room = self.room(exclude=["clara", "quinn"])
        await self.run_to_end(room, "손익 요약표")
        self.assertEqual(FakeRunner.finals, [])
        self.assertNotIn("팀 내부 보고", self.called("jake")[0])  # 감독이 없으면 바로 사장님께 보고


if __name__ == "__main__":
    unittest.main()
