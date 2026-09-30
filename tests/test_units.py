"""파서·명령 조립·설정 저장 단위 테스트 (CLI 호출 없음). 실행: python -m unittest discover -s tests -t . -v"""
import json
import shutil
import tempfile
import unittest
from pathlib import Path

import yaml

import app.config as appconfig
from app.config import editable_config, load_config, save_editable_config
from app.runners import ClaudeRunner, CodexRunner, describe_tool
from app.verdict import parse_review


class VerdictTests(unittest.TestCase):
    def test_structured_approve(self):
        r = parse_review({"verdict": "APPROVE", "summary": "문제 없음", "issues": [], "checks": ""}, "")
        self.assertTrue(r.approved)
        self.assertEqual(r.parse_warning, "")

    def test_approve_with_blocker_becomes_revise(self):
        r = parse_review({"verdict": "APPROVE", "summary": "s",
                          "issues": [{"severity": "blocker", "description": "x"}], "checks": ""}, "")
        self.assertFalse(r.approved)
        self.assertIn("blocker", r.parse_warning)

    def test_approve_with_minor_stays_approve(self):
        r = parse_review({"verdict": "APPROVE", "summary": "s",
                          "issues": [{"severity": "minor", "description": "x"}], "checks": ""}, "")
        self.assertTrue(r.approved)

    def test_json_in_text(self):
        r = parse_review(None, '{"verdict":"REVISE","summary":"s","issues":[],"checks":""}')
        self.assertEqual(r.verdict, "REVISE")

    def test_verdict_line_fallback(self):
        r = parse_review(None, "검토했습니다.\nVERDICT: APPROVE")
        self.assertTrue(r.approved)
        self.assertTrue(r.parse_warning)

    def test_garbage_is_revise(self):
        r = parse_review(None, "모르겠음")
        self.assertEqual(r.verdict, "REVISE")
        self.assertTrue(r.parse_warning)

    def test_unknown_verdict_is_revise(self):
        r = parse_review({"verdict": "MAYBE", "summary": "", "issues": [], "checks": ""}, "")
        self.assertEqual(r.verdict, "REVISE")


class FakeCtx:
    def __init__(self, allow=True):
        self.sent, self.acts, self.asked, self.closed = [], [], [], False
        self.allow = allow
        self.msg_tokens, self.tokens, self.lims, self.metas = {}, [], [], []

    def send(self, obj):
        self.sent.append(obj)

    def activity(self, msg=None, tokens=None):
        if msg is not None:
            self.acts.append(msg)
        if tokens is not None:
            self.tokens.append(tokens)

    def limits(self, lim):
        self.lims.append(lim)

    def meta(self, info):
        self.metas.append(info)

    def approve(self, tool, inp, desc):
        self.asked.append((tool, inp, desc))
        return self.allow

    def close_stdin(self):
        self.closed = True


class RunnerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cfg = load_config()
        cls.schema = str(Path(__file__).resolve().parent / "_schema.json")
        Path(cls.schema).write_text(json.dumps({"type": "object"}), encoding="utf-8")

    @classmethod
    def tearDownClass(cls):
        Path(cls.schema).unlink(missing_ok=True)

    def test_claude_stream_first_then_resume(self):
        r = ClaudeRunner(self.cfg.agents["jake"], self.cfg.settings)
        r.safe_mode = False
        cmd = r.build_command(r"C:\ws", None)
        for flag in ("--input-format", "stream-json", "--verbose", "--permission-prompt-tool", "stdio", "--session-id"):
            self.assertIn(flag, cmd)
        self.assertNotIn("--permission-prompts", cmd)
        self.assertNotIn("--safe-mode", cmd)
        self.assertIn("claude-opus-5-5", cmd)
        self.assertIn("PowerShell(git commit *)", cmd)
        self.assertIn("Bash(python *)", cmd)
        # 폴더 밖 쓰기·삭제가 자동 허용되지 않게: acceptEdits 금지, 편집 규칙은 작업 폴더 범위로
        self.assertNotIn("acceptEdits", cmd)
        self.assertEqual(cmd[cmd.index("--permission-mode") + 1], "auto")
        ask = json.loads(cmd[cmd.index("--settings") + 1])["permissions"]["ask"]
        for rule in ("PowerShell(Remove-Item*)", "PowerShell(Move-Item*)", "Bash(rm *)", "PowerShell(git push*)"):
            self.assertIn(rule, ask)
        self.assertIn("Write(./**)", cmd)
        self.assertNotIn("Write", cmd)
        self.assertNotIn("Edit", cmd)
        sid = cmd[cmd.index("--session-id") + 1]
        stdin = json.loads(r.build_stdin("안녕", r"C:\ws"))
        self.assertEqual(stdin["message"]["content"], "안녕")
        r.parse_output(json.dumps({"type": "result", "session_id": sid, "result": "ok", "is_error": False}))
        cmd2 = r.build_command(r"C:\ws", None)
        self.assertEqual(cmd2[cmd2.index("--resume") + 1], sid)

    def test_claude_safe_mode_flag(self):
        r = ClaudeRunner(self.cfg.agents["jake"], self.cfg.settings)
        r.safe_mode = True
        self.assertIn("--safe-mode", r.build_command(r"C:\ws", None))

    def test_claude_readonly_reviewer(self):
        r = ClaudeRunner(self.cfg.agents["clara"], self.cfg.settings)
        cmd = r.build_command(r"C:\ws", self.schema)
        # 9/30: 결과물(xlsx·pptx)을 직접 열 수 있게 python 실행 허용. 수정 도구(Edit)는 없음
        self.assertEqual(cmd[cmd.index("--tools") + 1], "Read,Glob,Grep,Bash,PowerShell,Write")
        i = cmd.index("--allowedTools")  # 읽기는 폴더 밖이어도 묻지 않음
        self.assertEqual(cmd[i + 1:i + 4], ["Read", "Glob", "Grep"])
        self.assertIn("PowerShell(python *)", cmd)
        self.assertIn("Write(./_qa/**)", cmd)
        self.assertNotIn("Write(./**)", cmd)
        self.assertNotIn("PowerShell(git commit *)", cmd)
        self.assertNotIn("acceptEdits", cmd)
        self.assertIn("--json-schema", cmd)
        sp = cmd[cmd.index("--append-system-prompt") + 1]
        self.assertIn("Clara", sp)
        self.assertIn("C:\\ws", sp)
        self.assertIn('호칭은 "사장님"', sp)
        for ph in ("{checklist}", "{user}", "{name}"):
            self.assertNotIn(ph, sp)

    def test_claude_control_request_allow_and_deny(self):
        r = ClaudeRunner(self.cfg.agents["jake"], self.cfg.settings)
        req = {"type": "control_request", "request_id": "rq1",
               "request": {"subtype": "can_use_tool", "tool_name": "PowerShell",
                           "input": {"command": "Remove-Item a.txt"}, "description": "삭제"}}
        ctx = FakeCtx(allow=True)
        r.on_line(json.dumps(req), ctx)
        self.assertEqual(ctx.asked[0][0], "PowerShell")
        resp = ctx.sent[0]["response"]
        self.assertEqual(resp["request_id"], "rq1")
        self.assertEqual(resp["response"], {"behavior": "allow", "updatedInput": {"command": "Remove-Item a.txt"}})
        ctx = FakeCtx(allow=False)
        r.on_line(json.dumps(req), ctx)
        self.assertEqual(ctx.sent[0]["response"]["response"]["behavior"], "deny")
        self.assertIn("우회하지", ctx.sent[0]["response"]["response"]["message"])

    def test_claude_tool_use_activity_and_result_closes(self):
        r = ClaudeRunner(self.cfg.agents["jake"], self.cfg.settings)
        ctx = FakeCtx()
        r.on_line(json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Write", "input": {"file_path": r"C:\ws\fib.py"}}]}}), ctx)
        self.assertEqual(ctx.acts, ["작성 중: fib.py"])
        r.on_line(json.dumps({"type": "result", "session_id": "s", "result": "done"}), ctx)
        self.assertTrue(ctx.closed)

    def test_claude_parse_structured_and_missing(self):
        r = ClaudeRunner(self.cfg.agents["clara"], self.cfg.settings)
        out = "\n".join([json.dumps({"type": "system", "subtype": "init"}),
                         json.dumps({"type": "result", "session_id": "x", "result": "{}", "is_error": False,
                                     "structured_output": {"verdict": "APPROVE"}})])
        res = r.parse_output(out)
        self.assertTrue(res.ok)
        self.assertEqual(res.structured, {"verdict": "APPROVE"})
        self.assertFalse(r.parse_output("not json").ok)

    def test_describe_tool(self):
        self.assertEqual(describe_tool("PowerShell", {"command": "python -m unittest"}), "실행 중: python -m unittest")
        self.assertEqual(describe_tool("Grep", {"pattern": "def fib"}), "찾는 중: def fib")

    def test_codex_first_then_resume(self):
        s = self.cfg.settings
        s.codex_exe = r"C:\fake\codex.exe"
        r = CodexRunner(self.cfg.agents["quinn"], s)
        cmd = r.build_command(r"C:\ws", self.schema)
        self.assertEqual(cmd[:3], [r"C:\fake\codex.exe", "exec", "--json"])
        self.assertIn("workspace-write", cmd)
        self.assertIn('windows.sandbox="unelevated"', cmd)
        self.assertEqual(cmd[-1], "-")
        self.assertIn("[역할 지침]", r.build_stdin("검토", r"C:\ws"))
        out = "\n".join([
            json.dumps({"type": "thread.started", "thread_id": "tid-1"}),
            json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": '{"verdict":"REVISE"}'}}),
            json.dumps({"type": "item.completed", "item": {"type": "agent_message",
                                                           "text": '{"verdict":"APPROVE","summary":"ok","issues":[],"checks":""}'}}),
        ])
        res = r.parse_output(out)
        self.assertEqual(res.structured["verdict"], "APPROVE")  # 마지막 메시지 사용
        cmd2 = r.build_command(r"C:\ws", self.schema)
        self.assertEqual(cmd2[1:3], ["exec", "resume"])
        self.assertIn('sandbox_mode="workspace-write"', cmd2)
        self.assertNotIn("-s", cmd2)
        self.assertEqual(cmd2[-2:], ["tid-1", "-"])
        self.assertEqual(r.build_stdin("검토", r"C:\ws"), "검토")

    def test_codex_error_event(self):
        s = self.cfg.settings
        s.codex_exe = r"C:\fake\codex.exe"
        res = CodexRunner(self.cfg.agents["quinn"], s).parse_output(json.dumps({"type": "error", "message": "usage limit"}))
        self.assertFalse(res.ok)
        self.assertIn("usage limit", res.error)


class ConfigSaveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        for n in ("agents.yaml", "settings.yaml"):
            shutil.copy(appconfig.CONFIG_DIR / n, self.tmp / n)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_roundtrip_with_backup(self):
        before = editable_config(self.tmp)
        backup = save_editable_config({
            "user_name": "대표님", "room_name": "테스트방",
            "agents": {"clara": {"name": "Claire", "checklist": ["항목1", " ", "항목2"], "model": ""}},
            "settings": {"max_rounds": 5, "global_rules": False}}, self.tmp)
        after = editable_config(self.tmp)
        self.assertEqual(after["user_name"], "대표님")
        self.assertEqual(after["agents"]["clara"]["name"], "Claire")
        self.assertEqual(after["agents"]["clara"]["checklist"], ["항목1", "항목2"])
        self.assertIsNone(after["agents"]["clara"]["model"])
        self.assertEqual(after["settings"]["max_rounds"], 5)
        self.assertFalse(after["settings"]["global_rules"])
        self.assertEqual(after["agents"]["jake"]["prompt"], before["agents"]["jake"]["prompt"])
        self.assertEqual(len(list(Path(backup).glob("backup_*_설정변경_*.yaml"))), 2)
        self.assertIn("prompt: |", (self.tmp / "agents.yaml").read_text(encoding="utf-8"))  # 여러 줄은 블록으로
        cfg = load_config(self.tmp)
        self.assertTrue(cfg.settings.claude_safe_mode)
        self.assertIn('호칭은 "대표님"', cfg.agents["jake"].system_prompt("C:/ws"))

    def test_unknown_agent_rejected_and_file_untouched(self):
        orig = (self.tmp / "agents.yaml").read_text(encoding="utf-8")
        with self.assertRaises(ValueError):
            save_editable_config({"agents": {"nobody": {"name": "x"}}}, self.tmp)
        self.assertEqual((self.tmp / "agents.yaml").read_text(encoding="utf-8"), orig)


if __name__ == "__main__":
    unittest.main()
