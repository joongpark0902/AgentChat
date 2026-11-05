"""v12: 프로젝트(대화 묶음) — 메모 전달·이동/해제 경계, 폴더 기준 묶기 제안, 원본 보존. 회사명·경로는 지어낸 예시."""
import json
import unittest

from app.orchestrator import Manager
from app.projects import ProjectStore, memo_block, memo_signature, suggest_groups
from tests.test_flow import Base, FakeRunner, ServerTests, review

MEMO_HEAD = "[프로젝트 메모 —"
RELEASE = "[프로젝트 메모 해제]"


class Memo(Base):
    def setUp(self):
        super().setUp()
        self.store = ProjectStore(self.cfg.settings.log_dir)
        self.pa = self.store.create("A사 실사", memo="기준일 2025-12-31, 단위 원")

    async def ask(self, room, text="질문", n=1):
        FakeRunner.scripts["jake"] = ["답"] * n
        await self.run_to_end(room, text, mode="quick")
        return self.called("jake")[-1]

    async def test_sent_once_then_only_on_change(self):
        FakeRunner.scripts = {"jake": [], "clara": [], "quinn": []}
        room = self.room(project=self.pa["id"])
        first = await self.ask(room)
        self.assertTrue(first.startswith(MEMO_HEAD + " A사 실사]"))
        self.assertIn("단위 원", first)
        self.assertNotIn(MEMO_HEAD, await self.ask(room))           # 같은 메모는 다시 안 보냄
        self.store.update(self.pa["id"], {"memo": "기준일 2025-12-31, 단위 천원"})
        self.assertIn("단위 천원", await self.ask(room))              # 바뀌면 다시

    async def test_restarted_session_gets_memo_again(self):
        # 10/8: 이전 세션을 이어받지 못해 새 세션으로 답한 경우 — 안내를 남기고, 새 세션이 모르는 메모는 다음 호출에 다시 보냄
        FakeRunner.scripts = {"jake": [], "clara": [], "quinn": []}
        room = self.room(project=self.pa["id"])
        await self.ask(room)
        FakeRunner.scripts["jake"] = [("RESTARTED", "새 세션 답")]
        await self.run_to_end(room, "둘째", mode="quick")
        self.assertNotIn(MEMO_HEAD, self.called("jake")[-1])
        notes = [m["text"] for m in room.messages if m["sender"] == "system" and "새 세션으로 다시 시작" in m["text"]]
        self.assertEqual(len(notes), 1)
        self.assertTrue((await self.ask(room)).startswith(MEMO_HEAD + " A사 실사]"))
        self.assertNotIn(MEMO_HEAD, await self.ask(room))

    async def test_move_to_other_project_with_same_text_resends(self):
        FakeRunner.scripts = {"jake": [], "clara": [], "quinn": []}
        pb = self.store.create("B사 실사", memo=self.pa["memo"])     # 문구까지 같은 다른 프로젝트
        room = self.room(project=self.pa["id"])
        await self.ask(room)
        await room.set_project(pb["id"])
        self.assertTrue((await self.ask(room)).startswith(MEMO_HEAD + " B사 실사]"))

    async def test_unassign_delete_or_empty_memo_releases(self):
        FakeRunner.scripts = {"jake": [], "clara": [], "quinn": []}
        room = self.room(project=self.pa["id"])
        await self.ask(room)
        await room.set_project(None)
        self.assertTrue((await self.ask(room)).startswith(RELEASE))   # 해제
        self.assertNotIn(RELEASE, await self.ask(room))               # 해제도 한 번만
        await room.set_project(self.pa["id"])
        await self.ask(room)
        self.store.update(self.pa["id"], {"memo": ""})
        self.assertTrue((await self.ask(room)).startswith(RELEASE))   # 빈 메모로 바꿈

    async def test_reviewers_get_memo_and_state_survives_restart(self):
        FakeRunner.scripts = {"jake": ["v1"], "clara": [review("APPROVE")], "quinn": [review("APPROVE")]}
        room = self.room(project=self.pa["id"])
        await self.run_to_end(room, "표 작성")
        self.assertTrue(self.called("clara")[0].startswith(MEMO_HEAD))
        self.assertTrue(self.called("quinn")[0].startswith(MEMO_HEAD))
        r2 = self.room(conv_id=room.conv_id)                         # 재시작
        self.assertEqual(r2.project, self.pa["id"])
        FakeRunner.calls = []
        self.assertNotIn(MEMO_HEAD, await self.ask(r2))               # 세션이 이어지면 다시 안 보냄

    async def test_new_session_resends_and_raw_command_untouched(self):
        FakeRunner.scripts = {"jake": [], "clara": [], "quinn": []}
        room = self.room(project=self.pa["id"])
        await self.ask(room)
        room.runners["jake"].started = False                          # 세션이 새로 시작된 경우
        self.assertTrue((await self.ask(room)).startswith(MEMO_HEAD))
        room.runners["jake"].started = False
        FakeRunner.scripts["jake"] = ["압축됨"]
        await self.run_to_end(room, "/compact")
        self.assertEqual(self.called("jake")[-1], "/compact")         # /명령 앞에는 붙이지 않음

    async def test_excluded_reviewer_never_gets_memo(self):
        FakeRunner.scripts = {"jake": ["v1"], "clara": [review("APPROVE")], "quinn": []}
        room = self.room(project=self.pa["id"], exclude=["quinn"])
        await self.run_to_end(room, "표 작성")
        self.assertEqual(self.called("quinn"), [])

    def test_signature_includes_project_id(self):
        pb = {"id": "pB", "name": "B", "memo": self.pa["memo"]}
        self.assertNotEqual(memo_signature(self.pa), memo_signature(pb))
        self.assertEqual(memo_block(None, ""), "")
        self.assertTrue(memo_block(None, "x").startswith(RELEASE))


class Suggest(unittest.TestCase):
    def test_grouping_rules(self):
        convs = [
            {"id": "1", "workspace": r"C:\w\FDD\예시사"},
            {"id": "2", "workspace": r"C:\w\FDD\예시사"},
            {"id": "3", "workspace": r"C:\w\FDD\예시사\요청자료"},         # 하위 폴더
            {"id": "4", "workspace": r"C:\other\FDD\예시사"},               # 끝 두 단계가 같음
            {"id": "5", "workspace": r"C:\Audit\FY2025"},
            {"id": "6", "workspace": r"C:\Tax\FY2025"},                     # 끝 한 단계만 같음 → 따로
            {"id": "7", "workspace": r"C:\app\workspace\20260101_000000"},
            {"id": "8", "workspace": r"C:\app\workspace\20260102_000000"},  # 앱이 만든 새 폴더 → 제외
            {"id": "9", "workspace": r"C:\w"},                              # 2단계 위 폴더 → 끌어모으지 않음
        ]
        g = suggest_groups(convs, skip_roots=[r"C:\app\workspace"])
        self.assertEqual(len(g), 1)
        self.assertEqual(sorted(c["id"] for c in g[0]["convs"]), ["1", "2", "3", "4"])
        self.assertEqual(g[0]["folder"], r"C:\w\FDD\예시사")
        self.assertEqual(g[0]["name"], "예시사")


class ManagerProjects(Base):
    def write_state(self, cid, **extra):
        logs = self.cfg.settings.log_dir
        logs.mkdir(parents=True, exist_ok=True)
        s = {"conv_id": cid, "title": cid, "workspace": str(self.tmp), "excluded": ["quinn"],
             "sessions": {"jake": {"session_id": "s1", "started": True}}, **extra}
        (logs / f"{cid}.state.json").write_text(json.dumps(s, ensure_ascii=False), encoding="utf-8")
        return s

    async def test_move_closed_conv_changes_only_project(self):
        before = self.write_state("c1")
        m = Manager(self.cfg, self.bc, runner_factory=FakeRunner)
        p = m.projects.create("A사")
        await m.set_conv_project("c1", p["id"])
        after = json.loads((self.cfg.settings.log_dir / "c1.state.json").read_text(encoding="utf-8"))
        self.assertEqual(after.pop("project"), p["id"])
        self.assertEqual(after, before)                              # 다른 값은 그대로
        self.assertEqual(m.projects_list()[0]["count"], 1)

    async def test_delete_project_keeps_conversations(self):
        self.write_state("c1")
        m = Manager(self.cfg, self.bc, runner_factory=FakeRunner)
        p = m.projects.create("A사")
        await m.set_conv_project("c1", p["id"])
        self.assertEqual(await m.delete_project(p["id"]), 1)
        self.assertIsNone(m.list()[0]["project"])
        self.assertTrue((self.cfg.settings.log_dir / "c1.state.json").exists())
        self.assertEqual(m.projects.all(), [])

    async def test_apply_suggestion_backs_up_and_assigns(self):
        ws = self.tmp / "FDD" / "예시사"
        ws.mkdir(parents=True)
        self.write_state("c1", workspace=str(ws))
        self.write_state("c2", workspace=str(ws))
        m = Manager(self.cfg, self.bc, runner_factory=FakeRunner)
        g = m.suggest_projects()
        self.assertEqual(len(g), 1)
        created = await m.apply_suggestion([{"name": g[0]["name"], "folder": g[0]["folder"],
                                             "conv_ids": [c["id"] for c in g[0]["convs"]]}])
        self.assertEqual({c["project"] for c in m.list()}, {created[0]["id"]})
        self.assertEqual(len(list((self.cfg.settings.log_dir / "backups").glob("backup_*_프로젝트적용_*.state.json"))), 2)
        self.assertEqual(m.suggest_projects(), [])                   # 이미 든 대화는 다시 제안하지 않음

    async def test_new_conv_uses_project_folder_and_filters(self):
        ws = self.tmp / "예시사"
        ws.mkdir()
        m = Manager(self.cfg, self.bc, runner_factory=FakeRunner)
        p = m.projects.create("예시사", folder=str(ws))
        room = m.create(project=p["id"])
        self.assertEqual(room.workspace, ws)
        self.assertEqual(room.project, p["id"])
        other = m.create()
        FakeRunner.scripts = {"jake": ["매출 확인 요청\n\n확인 요청\n1. 원 단위?", "매출 확인 요청\n\n확인 요청\n1. 원?"],
                              "clara": [], "quinn": []}
        await self.run_to_end(room, "매출", mode="quick")
        await self.run_to_end(other, "매출", mode="quick")
        self.assertEqual([r["id"] for r in m.search("매출", project=p["id"])], [room.conv_id])
        self.assertEqual(len(m.search("매출")), 2)
        self.assertEqual([i["id"] for i in m.inbox(project=p["id"])], [room.conv_id])
        with self.assertRaises(ValueError):
            m.create(project="없음")


class ReviewRisks(Base):
    """프로젝트 기능 검토(10/1) 때 스스로 짚은 위험 지점."""

    def write_state(self, cid, ws):
        logs = self.cfg.settings.log_dir
        logs.mkdir(parents=True, exist_ok=True)
        (logs / f"{cid}.state.json").write_text(json.dumps({"conv_id": cid, "title": cid, "workspace": str(ws)},
                                                           ensure_ascii=False), encoding="utf-8")

    def read_state(self, cid):
        return json.loads((self.cfg.settings.log_dir / f"{cid}.state.json").read_text(encoding="utf-8"))

    async def test_apply_while_room_open_is_not_reverted(self):
        ws = self.tmp / "FDD" / "예시사"
        ws.mkdir(parents=True)
        self.write_state("c1", ws)
        self.write_state("c2", ws)
        m = Manager(self.cfg, self.bc, runner_factory=FakeRunner)
        room = m.get("c1")                                            # 한 대화는 열려 있는 상태
        g = m.suggest_projects()[0]
        pid = (await m.apply_suggestion([{"name": g["name"], "folder": g["folder"],
                                          "conv_ids": [c["id"] for c in g["convs"]]}]))[0]["id"]
        self.assertEqual(room.project, pid)
        room._save_state()                                            # 열린 대화가 나중에 상태를 저장해도
        self.assertEqual(self.read_state("c1")["project"], pid)       # 되돌아가지 않음
        self.assertEqual(self.read_state("c2")["project"], pid)
        self.assertEqual(m.get("c2").project, pid)                    # 닫혀 있던 대화를 나중에 열어도 그대로

    async def test_memo_with_braces_is_sent_verbatim(self):
        FakeRunner.scripts = {"jake": ["답"], "clara": [], "quinn": []}
        store = ProjectStore(self.cfg.settings.log_dir)
        p = store.create("A사", memo="표기 {단위} 원, 코드 {0} %s \\n 그대로")
        room = self.room(project=p["id"])
        await self.run_to_end(room, "질문", mode="quick")
        self.assertIn("표기 {단위} 원, 코드 {0} %s \\n 그대로", self.called("jake")[0])

    async def test_missing_project_folder_falls_back_to_new_folder(self):
        m = Manager(self.cfg, self.bc, runner_factory=FakeRunner)
        p = m.projects.create("A사", folder=str(self.tmp / "지워진폴더"))
        room = m.create(project=p["id"])
        self.assertEqual(room.project, p["id"])
        self.assertTrue(room.workspace.is_dir())
        self.assertEqual(room.workspace.parent, self.cfg.settings.workspace_root)  # 앱이 새 폴더를 만듦

    async def test_delete_sends_release_once_to_each_agent(self):
        FakeRunner.scripts = {"jake": ["v1", "v2"], "clara": [review("APPROVE")] * 2, "quinn": [review("APPROVE")] * 2}
        m = Manager(self.cfg, self.bc, runner_factory=FakeRunner)
        p = m.projects.create("A사", memo="단위 원")
        room = m.create(project=p["id"])
        await self.run_to_end(room, "표 작성")
        await m.delete_project(p["id"])
        await self.run_to_end(room, "표 수정")
        for k in ("jake", "clara", "quinn"):
            prompts = self.called(k)
            self.assertTrue(prompts[0].startswith("[프로젝트 메모 —"), k)
            self.assertTrue(prompts[1].startswith("[프로젝트 메모 해제]"), k)
            self.assertEqual(sum(x.count("[프로젝트 메모 해제]") for x in prompts), 1, k)


class Api(ServerTests):
    def test_project_api(self):
        with self.make_client() as client:
            conv = client.post("/api/conversations", json={}).json()["id"]
            pid = client.post("/api/projects", json={"name": "A사", "memo": "단위 원", "conv_ids": [conv]}).json()["project"]["id"]
            lst = client.get("/api/projects").json()["projects"]
            self.assertEqual((lst[0]["name"], lst[0]["count"]), ("A사", 1))
            self.assertEqual(client.put(f"/api/projects/{pid}", json={"memo": "단위 천원"}).json()["project"]["memo"], "단위 천원")
            self.assertEqual(client.put(f"/api/projects/{pid}", json={"name": " "}).status_code, 400)
            self.assertEqual(client.post(f"/api/conv/{conv}/project", json={"project": "없음"}).status_code, 400)
            self.assertEqual(client.post(f"/api/conv/{conv}/project", json={"project": None}).status_code, 200)
            self.assertEqual(client.get("/api/projects/suggest").status_code, 200)
            self.assertEqual(client.delete(f"/api/projects/{pid}").json()["moved"], 0)
            self.assertEqual(client.delete(f"/api/projects/{pid}").status_code, 404)


if __name__ == "__main__":
    unittest.main()
