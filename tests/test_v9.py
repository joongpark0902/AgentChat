"""v9: 보낸 지시 수정 — 전달 대기 중이면 대기열에서 빼고, 첫 답이 나오기 전이면 중단 후 되돌린다."""
import asyncio

from tests.test_flow import Base, FakeRunner, review


class EditMessage(Base):
    async def wait_called(self, n):
        for _ in range(200):
            if len(FakeRunner.calls) >= n:
                return
            await asyncio.sleep(0.01)
        self.fail("러너가 호출되지 않음")

    async def finish(self, room):
        while room.running:
            await room.task

    async def test_first_instruction_restarts_session(self):
        FakeRunner.scripts = {"jake": ["v1", "고친 결과"], "clara": [review("APPROVE")], "quinn": [review("APPROVE")]}
        FakeRunner.gate = asyncio.Event()
        room = self.room()
        await room.user_message("표 작성 (오타)")
        first = room.messages[0]
        self.assertEqual(room.editable_ids(), [first["id"]])
        await self.wait_called(1)
        self.assertTrue(await room.edit_message(first["id"]))
        self.assertFalse(room.running)
        self.assertTrue(first.get("retracted"))
        self.assertEqual(room.editable_ids(), [])
        self.assertIsNone(room.runners["jake"].session_id)   # 첫 지시였으므로 세션을 새로 시작
        stop = room.messages[-1]
        self.assertTrue(stop["text"].startswith("사용자 요청으로 중단"))  # 알림(푸시) 생략 조건과 같은 시작 문구
        self.assertIn("지시 수정", stop["text"])
        FakeRunner.gate = None
        await self.run_to_end(room, "표 작성")
        prompt = self.called("jake")[-1]
        self.assertIn("표 작성", prompt)
        self.assertNotIn("[정정]", prompt)                    # 새 세션이라 정정 안내가 필요 없음
        self.assertNotIn("오타", prompt)
        self.assertTrue(room.messages[-1].get("done"))

    async def test_later_instruction_gets_correction_note(self):
        FakeRunner.scripts = {"jake": ["첫 답", "x", "고친 결과"], "clara": [], "quinn": []}
        room = self.room()
        await self.run_to_end(room, "첫 질문", mode="quick")
        FakeRunner.gate = asyncio.Event()
        await room.user_message("둘째 질문 (오타)", mode="quick")
        mid = room.messages[-1]["id"]
        await self.wait_called(2)
        self.assertTrue(await room.edit_message(mid))
        self.assertEqual(room.runners["jake"].session_id, "sess-jake")  # 이어 온 세션은 유지
        FakeRunner.gate = None
        await self.run_to_end(room, "둘째 질문", mode="quick")
        self.assertIn("[정정] 직전 지시는 취소합니다", self.called("jake")[-1])
        self.assertEqual(room.messages[-1]["sender"], "jake")   # 고친 지시에 실제로 답이 옴
        await self.run_to_end(room, "셋째 질문", mode="quick")
        self.assertNotIn("[정정]", self.called("jake")[-1])     # 정정 안내는 한 번만

    async def test_not_editable_after_first_answer(self):
        FakeRunner.scripts = {"jake": ["v1"], "clara": [review("APPROVE")], "quinn": [review("APPROVE")]}
        room = self.room()
        await self.run_to_end(room, "표 작성")
        self.assertEqual(room.editable_ids(), [])
        self.assertFalse(await room.edit_message(room.messages[0]["id"]))
        self.assertFalse(room.messages[0].get("retracted"))

    async def test_pending_instruction_is_removed_cleanly(self):
        FakeRunner.scripts = {"jake": ["v1"], "clara": [review("APPROVE")], "quinn": [review("APPROVE")]}
        FakeRunner.gate = asyncio.Event()
        room = self.room()
        await room.user_message("표 작성")
        await self.wait_called(1)
        await room.user_message("추가: 색도 넣어줘 (취소할 것)")
        queued = next(m for m in room.messages if "취소할 것" in m["text"])
        self.assertIn(queued["id"], room.editable_ids())
        self.assertTrue(await room.edit_message(queued["id"]))
        self.assertTrue(room.running)                        # 진행 중인 작업은 멈추지 않음
        self.assertEqual(room.pending, [])
        FakeRunner.gate.set()
        await self.finish(room)
        self.assertTrue(all("취소할 것" not in p for _, p, _ in FakeRunner.calls))
        self.assertEqual(len(self.called("jake")), 1)

    async def test_pending_kept_when_running_instruction_is_edited(self):
        FakeRunner.scripts = {"jake": ["x", "결과"], "clara": [review("APPROVE")], "quinn": [review("APPROVE")]}
        FakeRunner.gate = asyncio.Event()
        room = self.room()
        await room.user_message("표 작성 (오타)")
        await self.wait_called(1)
        await room.user_message("추가: 합계도 넣어줘")
        self.assertTrue(await room.edit_message(room.messages[0]["id"]))
        self.assertEqual(len(room.pending), 1)               # 전달 대기 지시는 잃지 않음
        FakeRunner.gate = None
        await self.run_to_end(room, "표 작성")
        prompt = self.called("jake")[-1]
        self.assertIn("합계도 넣어줘", prompt)
        self.assertIn("표 작성", prompt)
