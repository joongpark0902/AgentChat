"""파서·명령 조립·설정 저장 단위 테스트 (CLI 호출 없음). 실행: python -m unittest discover -s tests -t . -v"""
import dataclasses
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

import yaml

import app.config as appconfig
from app.config import editable_config, load_config, save_editable_config
from app.runners import CallTimer, ClaudeRunner, CodexRunner, describe_tool
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

    def test_claude_cut_off_first_answer_resumes(self):
        # 10/1: 첫 답 도중 시간 초과로 끊기면 result 가 없어 started 가 False 로 남고,
        # 다음 호출이 같은 --session-id 로 새로 만들려다 계속 실패했음
        r = ClaudeRunner(self.cfg.agents["jake"], self.cfg.settings)
        cmd = r.build_command(r"C:\ws", None)
        sid = cmd[cmd.index("--session-id") + 1]
        r.on_line(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "작업 중"}]}}), FakeCtx())
        self.assertTrue(r.started)
        cmd2 = r.build_command(r"C:\ws", None)
        self.assertEqual(cmd2[cmd2.index("--resume") + 1], sid)

    def test_claude_existing_session_file_resumes(self):
        # 예전 버전에서 이미 막힌 대화: 상태엔 started=False 지만 CLI 세션 기록이 있으면 이어받기
        home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, home, True)
        old = os.environ.get("CLAUDE_CONFIG_DIR")
        os.environ["CLAUDE_CONFIG_DIR"] = str(home)
        self.addCleanup(lambda: os.environ.pop("CLAUDE_CONFIG_DIR") if old is None else os.environ.__setitem__("CLAUDE_CONFIG_DIR", old))
        r = ClaudeRunner(self.cfg.agents["jake"], self.cfg.settings)
        r.restore({"session_id": "11111111-2222-3333-4444-555555555555", "started": False})
        self.assertIn("--session-id", r.build_command(r"C:\ws", None))  # 기록이 없으면 그대로 새로 시작
        (home / "projects" / "C--ws").mkdir(parents=True)
        (home / "projects" / "C--ws" / "11111111-2222-3333-4444-555555555555.jsonl").write_text("{}\n", encoding="utf-8")
        cmd = r.build_command(r"C:\ws", None)
        self.assertEqual(cmd[cmd.index("--resume") + 1], "11111111-2222-3333-4444-555555555555")

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

    def test_claude_reviewer_auto_mode_option(self):
        # 10/5: 감독도 permission_mode: auto 를 주면 실무자처럼 분류기가 판단(삭제·이동·push 는 승인 카드). 기본값은 지금까지와 같음
        base = dataclasses.replace(self.cfg.agents["clara"], permission_mode=None)
        cmd = ClaudeRunner(base, self.cfg.settings).build_command(r"C:\ws", None)
        self.assertNotIn("--permission-mode", cmd)
        self.assertNotIn("--settings", cmd)
        auto = dataclasses.replace(base, permission_mode="auto")
        cmd = ClaudeRunner(auto, self.cfg.settings).build_command(r"C:\ws", None)
        self.assertEqual(cmd[cmd.index("--permission-mode") + 1], "auto")
        ask = json.loads(cmd[cmd.index("--settings") + 1])["permissions"]["ask"]
        for rule in ("PowerShell(Remove-Item*)", "PowerShell(Move-Item*)", "Bash(rm *)", "PowerShell(git push*)"):
            self.assertIn(rule, ask)
        self.assertEqual(cmd[cmd.index("--tools") + 1], "Read,Glob,Grep,Bash,PowerShell,Write")  # Edit 는 여전히 없음
        self.assertNotIn("Edit(./**)", cmd)
        self.assertNotIn("Write(./**)", cmd)
        self.assertNotIn("acceptEdits", cmd)

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


FAKE_CLI = Path(__file__).resolve().parent / "fake_cli_pipe.py"


def pid_alive(pid: int) -> bool:
    if os.name == "nt":
        import ctypes
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        k32.GetExitCodeProcess(h, ctypes.byref(code))
        k32.CloseHandle(h)
        return code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


class PipeTests(unittest.TestCase):
    """10/2: 지시문을 다 쓴 뒤에야 출력을 읽어서, 시작 출력이 큰 새 세션(Clara)에 긴 지시문을 보내면 서로 막혀 멈췄음."""
    PROMPT = "검토해 주세요. " * 12000  # 약 10만 자(UTF-8 약 25만 바이트) — 통로 버퍼보다 훨씬 큼

    def make(self, mode: str, timeout: int):
        cfg = load_config()
        settings = dataclasses.replace(cfg.settings, call_timeout_sec=timeout)
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        pid_file = tmp / "pid.txt"

        class R(ClaudeRunner):
            def build_command(self, workspace, schema_path):
                return [sys.executable, str(FAKE_CLI), mode, str(pid_file)]
        return R(cfg.agents["jake"], settings), str(tmp), pid_file

    def wait_pid(self, pid_file: Path) -> int:
        for _ in range(100):
            if pid_file.exists() and pid_file.read_text():
                return int(pid_file.read_text())
            time.sleep(0.05)
        self.fail("가짜 CLI 가 시작되지 않음")

    def test_long_prompt_with_big_startup_output(self):
        r, ws, _ = self.make("bigfirst", 20)
        t0 = time.monotonic()
        res = r._run_blocking(self.PROMPT, ws, None, lambda *a: None, None)
        took = time.monotonic() - t0
        self.assertEqual(res.error, "")
        self.assertTrue(res.ok)
        self.assertIn("받은 글자", res.text)
        self.assertLess(took, 15)

    def test_timeout_while_input_blocked(self):
        r, ws, pid_file = self.make("noread", 2)
        t0 = time.monotonic()
        res = r._run_blocking(self.PROMPT * 4, ws, None, lambda *a: None, None)
        took = time.monotonic() - t0
        self.assertIn("제한 시간", res.error)
        self.assertLess(took, 12)
        self.assertFalse(pid_alive(self.wait_pid(pid_file)))
        writer = getattr(r, "_writer", None)
        self.assertTrue(writer is None or not writer.is_alive())

    def test_cancel_while_input_blocked(self):
        r, ws, pid_file = self.make("noread", 60)
        out = {}
        th = threading.Thread(target=lambda: out.setdefault("res", r._run_blocking(self.PROMPT * 4, ws, None,
                                                                                    lambda *a: None, None)))
        t0 = time.monotonic()
        th.start()
        pid = self.wait_pid(pid_file)
        time.sleep(0.5)
        r.cancel()
        th.join(15)
        self.assertFalse(th.is_alive())
        self.assertLess(time.monotonic() - t0, 15)
        self.assertFalse(pid_alive(pid))
        writer = getattr(r, "_writer", None)
        self.assertTrue(writer is None or not writer.is_alive())


FAKE_CODEX = Path(__file__).resolve().parent / "fake_cli_codex.py"


class FreshRetryTests(unittest.TestCase):
    """10/8: Codex 로그인 계정이 바뀌어 옛 세션 이어받기가 실패하면 새 세션으로 한 번만 다시 부른다."""

    def make(self, mode: str, engine=CodexRunner, key="quinn"):
        cfg = load_config()
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        self.log = tmp / "calls.txt"
        log = self.log

        class R(engine):
            def build_command(self, workspace, schema_path):
                return [sys.executable, str(FAKE_CODEX), "resume" if self.started else "new", mode, str(log)]
        r = R(cfg.agents[key], dataclasses.replace(cfg.settings, call_timeout_sec=20))
        r.restore({"session_id": "tid-old", "started": True, "usage_snapshot": {"total_tokens": 5}, "context": None})
        return r, str(tmp)

    def calls(self):
        return self.log.read_text(encoding="utf-8").split()

    def run_it(self, r, ws):
        return r._run_with_fresh_retry("검토", ws, None, lambda *a: None, None)

    def test_resume_fails_then_fresh_session(self):
        r, ws = self.make("resume_fails")
        res = self.run_it(r, ws)
        self.assertTrue(res.ok)
        self.assertTrue(res.restarted)
        self.assertEqual(res.text, "역할 지침 받음")   # 새 세션이라 역할 지침을 다시 붙여 보냄
        self.assertEqual(self.calls(), ["resume", "new"])
        self.assertEqual((r.session_id, r.started), ("tid-new", True))

    def test_both_fail_keeps_old_session(self):
        r, ws = self.make("all_fail")
        res = self.run_it(r, ws)
        self.assertFalse(res.ok)
        self.assertFalse(res.restarted)
        self.assertIn("이어받을 수 없습니다", res.error)
        self.assertEqual(self.calls(), ["resume", "new"])   # 한 번만 다시 시도
        self.assertEqual(r.state(), {"session_id": "tid-old", "started": True,
                                     "usage_snapshot": {"total_tokens": 5}, "context": None})

    def test_resume_ok_no_retry(self):
        r, ws = self.make("ok")
        res = self.run_it(r, ws)
        self.assertTrue(res.ok)
        self.assertFalse(res.restarted)
        self.assertEqual(res.text, "역할 지침 없음")
        self.assertEqual(self.calls(), ["resume"])

    def test_first_call_failure_not_retried(self):
        r, ws = self.make("all_fail")
        r.restore({"session_id": None, "started": False})
        self.assertFalse(self.run_it(r, ws).ok)
        self.assertEqual(self.calls(), ["new"])

    def test_claude_not_retried(self):
        self.assertFalse(ClaudeRunner.fresh_retry_on_resume_failure)
        self.assertTrue(CodexRunner.fresh_retry_on_resume_failure)


class CallTimerTests(unittest.TestCase):
    """10/2: 실행 허락을 기다리는 동안은 제한 시간을 세지 않는다."""

    def test_approval_wait_not_counted(self):
        fired = threading.Event()
        t = CallTimer(0.3, fired.set, tick=0.02)
        t.start()
        t.paused(lambda: time.sleep(0.6))()       # 허락 대기 0.6초 > 제한 0.3초
        self.assertFalse(fired.is_set())
        self.assertLess(t.used, 0.3)
        self.assertTrue(fired.wait(1.0))          # 대기가 끝나면 다시 세어 결국 끊김
        t.cancel()

    def test_times_out_without_approval(self):
        fired = threading.Event()
        t = CallTimer(0.1, fired.set, tick=0.02)
        t.start()
        self.assertTrue(fired.wait(1.0))

    def test_cancel_stops(self):
        fired = threading.Event()
        t = CallTimer(0.1, fired.set, tick=0.02)
        t.start()
        t.cancel()
        self.assertFalse(fired.wait(0.3))

    def test_paused_returns_answer(self):
        t = CallTimer(10, lambda: None)
        self.assertTrue(t.paused(lambda tool, inp, desc: tool == "Bash")("Bash", {}, ""))
        self.assertEqual(t._waiting, 0)


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
