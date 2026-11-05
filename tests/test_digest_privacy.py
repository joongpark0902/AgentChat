"""메일 요약이 PC 밖(폰 알림)으로 내보내는 글에 개인정보·업체명이 들어가지 않는지 — 정상·실패 경로 모두.
가짜 개인정보·업체명(모두 지어낸 값)을 메일에 넣고, 실제 Notifier 가 푸시 서버로 넘기려는 제목·본문·데이터를 전부 검사한다.
(실무자 지시문에는 보낸이·제목이 들어가는 것이 요약 기능의 본질이라 여기서 막지 않는다 — 그 사실은 마지막 테스트로 기록)"""
from datetime import timedelta

from app import digest as D
from app.orchestrator import Manager
from app.push import Notifier
from tests.test_digest import T0, DigestBase, ans
from tests.test_flow import FakeRunner

PII = ["홍길동", "hong.gildong@yesi-sangsa.example", "예시상사", "010-1234-5678", "900101-1234567", "123-45-67890"]


def secret_row(i, when):
    return {"id": f"{i:048X}", "time": when, "sender": "홍길동 (주)예시상사", "addr": "hong.gildong@yesi-sangsa.example",
            "subject": f"예시상사 계약 건 {i} 연락처 010-1234-5678 주민 900101-1234567 사업자 123-45-67890",
            "has_att": False, "kind": "", "folder": "예시상사 거래처"}


class FakePush:
    def __init__(self):
        self.subs, self.out = ["phone"], []

    async def send(self, title, body, data, skip=()):
        self.out.append((title, body, data))


class PushPrivacy(DigestBase):
    def setUp(self):
        super().setUp()
        self.push = FakePush()
        self.notifier = Notifier(self.push, lambda: self.cfg, lambda: set())

        async def bc(payload):
            self.events.append(payload)
            await self.notifier.observe(payload)
        self.m = Manager(self.cfg, bc, runner_factory=FakeRunner)
        self.note = self.notifier
        self.d = self.new_digest()

    async def sent(self):
        for t in list(self.notifier.tasks):
            await t
        return self.push.out

    def assert_clean(self, out):
        self.assertTrue(out, "알림이 한 건도 없음 — 경로를 시험하지 못함")
        for title, body, data in out:
            blob = f"{title} {body} {data}"
            for word in PII:
                self.assertNotIn(word, blob)

    async def test_normal_run(self):
        self.box += [secret_row(i, T0 - timedelta(minutes=i)) for i in range(1, 4)]
        FakeRunner.scripts["jake"] = [ans(1, 3).replace("김가나", "홍길동 (주)예시상사")]
        res = await self.d.run()
        self.assertEqual(res["done"], 3)
        out = await self.sent()
        self.assertEqual([b for _, b, _ in out], ["메일 요약 3통 · 확인해 주세요"])
        self.assert_clean(out)

    async def test_given_up_list_stays_in_the_chat_only(self):
        self.box.append(secret_row(1, T0 - timedelta(minutes=5)))
        for _ in range(D.ITEM_TRIES):
            FakeRunner.scripts["jake"] = [ans(1, 1, unread={1})]
            res = await self.d.run(manual=True)
        self.assertEqual(res["gave_up"], 1)
        self.assertIn("홍길동", self.sys_notes(res["conv"])[-1])     # PC 안 대화창에는 남는다(사람이 직접 확인하라는 목록)
        self.assert_clean(await self.sent())

    async def test_outlook_failure_with_names_in_the_error(self):
        self.box.append(secret_row(1, T0 - timedelta(hours=1)))
        self.off = "폴더 '예시상사 거래처'(hong.gildong@yesi-sangsa.example)를 읽지 못했습니다"
        res = await self.d.run()
        self.assertFalse(res["ok"])
        self.assert_clean(await self.sent())

    async def test_worker_failure(self):
        self.box += [secret_row(i, T0 - timedelta(minutes=i)) for i in range(1, 3)]
        FakeRunner.scripts["jake"] = [None]
        res = await self.d.run()
        self.assertTrue(res["failed"])
        self.assert_clean(await self.sent())

    async def test_partial_answer(self):
        self.box += [secret_row(i, T0 - timedelta(minutes=i)) for i in range(1, 4)]
        FakeRunner.scripts["jake"] = [ans(1, 3, skip={2}).replace("김가나", "홍길동")]
        res = await self.d.run()
        self.assertEqual((res["done"], res["missing"]), (2, 1))
        out = await self.sent()
        self.assertEqual([b for _, b, _ in out], ["메일 요약 2통 · 일부는 요약하지 못했습니다"])
        self.assert_clean(out)

    async def test_fact_worker_instruction_carries_sender_and_subject(self):
        # 기록용: 요약을 하려면 실무자(AI)에게 보낸이·주소·제목이 간다(본문은 실무자가 메일 도구로 읽음)
        self.box.append(secret_row(1, T0 - timedelta(minutes=1)))
        FakeRunner.scripts["jake"] = [ans(1, 1)]
        await self.d.run()
        (p,) = self.prompts()
        for word in ("홍길동", "hong.gildong@yesi-sangsa.example", "예시상사"):
            self.assertIn(word, p)
