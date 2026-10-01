"""v11: 지난 대화 전체 검색 + 결정 대기함."""
import asyncio
import json
import unittest

from app.inbox import pending_decision, search_messages
from app.logstore import LogStore
from app.orchestrator import Manager
from tests.test_flow import Base, FakeRunner, ServerTests, review

U = "사장님"


def msg(sender, text, i, **kw):
    return {"id": f"m{i}", "sender": sender, "text": text, "ts": 1000.0 + i, **kw}


QUESTION = "작업했습니다.\n\n**확인 요청**\n1. v3를 쓸까요, v2에 넣을까요?\n2. 숨김 열은 둘까요?"


class Decision(unittest.TestCase):
    def test_question_heading_is_pending(self):
        d = pending_decision([msg("user", "표 만들어", 1), msg("jake", QUESTION, 2)], U)
        self.assertEqual(d["reason"], "question")
        self.assertEqual(d["msg_id"], "m2")
        self.assertIn("v3를 쓸까요", d["summary"])

    def test_other_headings(self):
        for head in ["⑤ 사장님이 정해야 할 것", "## 사장님께서 정하실 것", "**사장님 결정 필요**",
                     "④ 정해 주실 것", "③ 아직 결정하지 않은 것 두 가지", "사장님께 여쭐 것"]:
            self.assertIsNotNone(pending_decision([msg("jake", f"보고\n\n{head}\n- A안/B안", 1)], U), head)

    def test_user_answered_is_not_pending(self):
        self.assertIsNone(pending_decision([msg("jake", QUESTION, 1), msg("user", "1. 가", 2)], U))

    def test_no_heading_is_not_pending(self):
        self.assertIsNone(pending_decision([msg("jake", "완료했습니다. 확인 결과 이상 없습니다.", 1)], U))
        long_line = "이번에 테스트 결과 확인 요청 사항은 모두 처리했고 추가로 할 일은 없으며 파일은 그대로 두었습니다 감사합니다"
        self.assertIsNone(pending_decision([msg("jake", long_line, 1)], U))  # 본문 문장 속 단어는 제목이 아님

    def test_mid_round_team_report_is_not_pending(self):
        plan = msg("jake", "계획\n\n⑤ 사장님이 정해야 할 것\n1. 범위", 1, round=1, stage="plan")
        self.assertIsNone(pending_decision([plan], U))
        final = msg("jake", "사장님, 끝났습니다.\n\n확인 요청\n1. 푸시할까요?", 2, final=True, done=True)
        self.assertIsNotNone(pending_decision([plan, final], U))

    def test_escalation_and_system_tip(self):
        esc = msg("system", "3라운드 안에 두 감독의 승인이 모두 나지 않아 사장님께 넘깁니다.\n- 합계 불일치", 1, escalation=True)
        self.assertEqual(pending_decision([esc], U)["reason"], "escalation")
        tip = msg("system", "이번 답변에 3.66M 토큰이 쓰였습니다.", 3)
        self.assertIsNotNone(pending_decision([msg("jake", QUESTION, 2), tip], U))  # 안내 문구는 건너뜀

    def test_running_is_not_pending(self):
        self.assertIsNone(pending_decision([msg("jake", QUESTION, 1)], U, running=True))


class Search(unittest.TestCase):
    def test_hits_snippet_case_and_order(self):
        msgs = [msg("user", "DART 공시 확인", 1), msg("jake", "x" * 200 + " dart 원문 " + "y" * 200, 2),
                msg("jake", "관계없음", 3), msg("user", "dart 취소", 4, retracted=True)]
        total, hits = search_messages(msgs, "Dart")
        self.assertEqual(total, 2)                      # 취소한 지시는 제외
        self.assertEqual(hits[0]["msg_id"], "m2")        # 최근 것부터
        self.assertTrue(hits[0]["snippet"].startswith("…") and hits[0]["snippet"].endswith("…"))
        self.assertLess(len(hits[0]["snippet"]), 200)


class ManagerInbox(Base):
    def write_log(self, cid, msgs, state=None):
        logs = self.cfg.settings.log_dir
        logs.mkdir(parents=True, exist_ok=True)
        st = LogStore(logs, cid)
        for m in msgs:
            st.append(m, m["sender"])
        st.save_state({"conv_id": cid, "title": cid, **(state or {})})

    async def test_stale_approval_in_log_not_counted_before_or_after_restart(self):
        card = msg("jake", "Jake가 실행 허락을 요청합니다: PowerShell", 3, kind="approval",
                   approval={"id": "a1", "tool": "PowerShell", "detail": "Remove-Item x", "status": "pending"})
        self.write_log("c1", [msg("user", "정리해", 1), msg("jake", QUESTION, 2), card])
        m = Manager(self.cfg, self.bc, runner_factory=FakeRunner)
        items = m.inbox()
        self.assertEqual([i["reason"] for i in items], ["question"])   # 로그에 남은 pending 카드는 세지 않음
        m.get("c1")                                                     # 재시작 후 대화를 연 상태
        self.assertEqual([i["reason"] for i in m.inbox()], ["question"])

    async def test_archived_excluded_and_search_across(self):
        self.write_log("c1", [msg("jake", QUESTION, 1)])
        self.write_log("c2", [msg("jake", QUESTION.replace("v3", "w3"), 1)], state={"archived": True})
        m = Manager(self.cfg, self.bc, runner_factory=FakeRunner)
        self.assertEqual([i["id"] for i in m.inbox()], ["c1"])
        res = {r["id"]: r for r in m.search("숨김 열")}
        self.assertEqual(set(res), {"c1", "c2"})                        # 검색은 보관 포함
        self.assertTrue(res["c2"]["archived"])
        self.assertEqual(m.search("w"), [])                            # 1글자는 검색 안 함

    async def test_search_returns_all_matching_conversations(self):
        for i in range(31):  # 경계: 30개를 넘어도 모두 나와야 함
            self.write_log(f"c{i:02d}", [msg("jake", f"감사조서 {i}", 1)])
        self.write_log("zz", [msg("jake", "관계없음", 1)])
        m = Manager(self.cfg, self.bc, runner_factory=FakeRunner)
        res = m.search("감사조서")
        self.assertEqual(len(res), 31)
        self.assertEqual(sum(r["total"] for r in res), 31)

    async def test_live_approval_counted_then_cleared(self):
        FakeRunner.scripts = {"jake": [("APPROVAL", "Remove-Item old.txt")], "clara": [], "quinn": []}
        m = Manager(self.cfg, self.bc, runner_factory=FakeRunner)
        room = m.create()
        await room.user_message("정리해줘", mode="quick")
        for _ in range(100):
            await asyncio.sleep(0.01)
            if room.approvals:
                break
        items = m.inbox()
        self.assertEqual([i["reason"] for i in items], ["approval"])   # 진행 중이라 질문 판정은 안 함
        self.assertIn("Remove-Item old.txt", items[0]["summary"])
        room.resolve_approval(next(iter(room.approvals)), True)
        await room.task
        self.assertEqual(m.inbox(), [])

    async def test_final_report_with_question_after_task(self):
        FakeRunner.scripts = {"jake": ["v1"], "clara": [review("APPROVE")], "quinn": [review("APPROVE")]}
        m = Manager(self.cfg, self.bc, runner_factory=FakeRunner)
        room = m.create()
        await self.run_to_end(room, "표 작성")
        self.assertEqual(m.inbox(), [])          # 고정 최종 보고에는 확인 요청이 없음
        room.messages[-1]["text"] += "\n\n확인 요청\n1. 올릴까요?"
        self.assertEqual(m.inbox()[0]["reason"], "question")


class RepeatRecommend(Base):
    def test_topic_words_and_similar(self):
        from app.inbox import similar_tasks, topic_words
        self.assertIn("대사", topic_words("원장과 정산서를 대사해줘"))
        self.assertNotIn("해줘", topic_words("정리해줘 작성해줘"))
        convs = [{"id": "a", "title": "채널별 정산서 원장 대사", "updated": 2.0, "text": "채널별 정산서 원장 대사"},
                 {"id": "b", "title": "인터뷰 녹취 요약", "updated": 3.0, "text": "녹취 요약 HTML"},
                 {"id": "c", "title": "정산서 확인", "updated": 4.0, "text": "정산서 확인"}]
        res = similar_tasks("홈쇼핑 정산서를 원장과 대사해줘", convs)
        self.assertEqual([r["id"] for r in res], ["a"])              # 2개 이상 겹친 것만(정산서·원장·대사)
        self.assertEqual(similar_tasks("", convs), [])

    async def test_final_report_lists_similar_past_task(self):
        old = self.room()
        FakeRunner.scripts = {"jake": ["v1"], "clara": [review("APPROVE")], "quinn": [review("APPROVE")]}
        await self.run_to_end(old, "홈쇼핑 정산서 원장 대사")
        FakeRunner.finals = []
        FakeRunner.scripts = {"jake": ["v1"], "clara": [review("APPROVE")], "quinn": [review("APPROVE")]}
        new = self.room()
        await self.run_to_end(new, "카드사 정산서 원장 대사해줘")
        prompt = FakeRunner.finals[0][1]
        self.assertIn("반복 작업 추천", prompt)
        self.assertIn("홈쇼핑 정산서 원장 대사", prompt)
        self.assertIn("승인하기 전에는 메모·스킬을 만들지 마세요", prompt)

    async def test_first_time_task_has_no_recommendation(self):
        FakeRunner.scripts = {"jake": ["v1"], "clara": [review("APPROVE")], "quinn": [review("APPROVE")]}
        await self.run_to_end(self.room(), "피보나치 함수")
        self.assertNotIn("반복 작업 추천", FakeRunner.finals[0][1])


class Api(ServerTests):
    def test_search_and_inbox_api(self):
        FakeRunner.scripts = {"jake": ["피보나치 작성\n\n확인 요청\n1. 캐시 쓸까요?"], "clara": [], "quinn": []}
        with self.make_client() as client:
            conv = client.post("/api/conversations", json={}).json()["id"]
            with client.websocket_connect("/ws") as ws:
                ws.receive_json()
                ws.send_json({"type": "open", "conv": conv})
                ws.receive_json()
                ws.send_json({"type": "say", "conv": conv, "text": "피보나치", "mode": "quick"})
                for _ in range(100):
                    ev = ws.receive_json()
                    if ev["type"] == "message" and ev["message"]["sender"] == "jake":
                        break
            res = client.get("/api/search", params={"q": "캐시"}).json()["results"]
            self.assertEqual(res[0]["id"], conv)
            for _ in range(50):
                items = client.get("/api/inbox").json()["items"]
                if items:
                    break
            self.assertEqual(items[0]["reason"], "question")
            self.assertEqual(client.get("/api/search", params={"q": "x"}).json()["results"], [])


if __name__ == "__main__":
    unittest.main()
