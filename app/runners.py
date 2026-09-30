"""공식 CLI(claude -p / codex exec) 호출기. API 키·토큰 추출 없이 로그인된 CLI만 subprocess 로 부른다.

옵션 근거(2026-09-28 실측, claude 2.1.283 / codex-cli 0.155.0):
- claude: -p --input-format stream-json --output-format stream-json --verbose --permission-prompt-tool stdio
          · stdin 에 {"type":"user","message":{...}} 한 줄, 끝나면 stdin 닫기
          · 허용 안 된 도구 → stdout 에 control_request(subtype=can_use_tool, request_id, tool_name, input)
            → stdin 에 control_response {behavior: allow, updatedInput} / {behavior: deny, message}
          · 마지막 {"type":"result"} 에 session_id·result·structured_output(--json-schema)
          · --session-id <uuid> 로 시작, --resume <uuid> 로 이어감
- codex : exec --json → JSONL, thread.started.thread_id 가 세션 ID, exec resume <id> 로 이어감
          (resume 에는 -s 없음 → -c sandbox_mode=...), 프롬프트 '-' 는 stdin(닫아야 진행)
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import threading
import uuid
from dataclasses import dataclass
from typing import Callable

from .config import AgentConfig, Settings, find_codex_exe
from . import usage as usage_mod

# activity(글 한 줄 | None, 지금까지 토큰 | None): 입력 중 표시 갱신. 스레드에서 호출되면 run() 이 이벤트 루프로 넘긴다.
ActivityFn = Callable[..., None]
LimitsFn = Callable[[dict], None]      # 구독 한도가 들어오는 즉시(답변 도중) 알림
MetaFn = Callable[[dict], None]        # CLI 가 알려준 부가 정보(예: 슬래시 명령 목록)
# (도구 이름, 입력, 설명) → 허용 여부. 스레드에서 호출되며 사용자가 채팅에서 누를 때까지 기다린다.
ApproveFn = Callable[[str, dict, str], bool]

# 실무자가 묻지 않고 쓸 수 있는 것: 작업 폴더 안 파일 편집 + python + git(조회·add·commit). 나머지는 auto 분류기가 판단하고,
# 삭제·이동·push 등(ASK_RULES)은 항상 채팅창 승인 카드로 사용자에게 묻는다.
# 2026-09-28 실측: acceptEdits 모드는 작업 폴더 안 Remove-Item 을 묻지 않고 허용했고, 범위 없는 Write/Edit 규칙은 폴더 밖 쓰기도
# 묻지 않고 허용했다 → default 모드 + Edit(./**)·Write(./**)로 바꾸니 셋 다 승인 요청으로 왔다.
_SHELL_ALLOWED = ["python *", "git status*", "git diff*", "git log*", "git show*", "git branch*",
                  "git add *", "git commit *"]
CLAUDE_WRITE_TOOLS = ["Read", "Glob", "Grep", "Edit(./**)", "Write(./**)",
                      *[f"Bash({p})" for p in _SHELL_ALLOWED], *[f"PowerShell({p})" for p in _SHELL_ALLOWED]]
# 2026-09-28 사장님 결정 (b): 실무자는 auto 모드(분류기가 판단) + 삭제·이동·push 등은 ask 규칙으로 항상 승인 카드.
# 실측: auto 모드만 쓰면 Remove-Item·폴더 밖 쓰기도 자동 허용됨 / ask 규칙을 넣으면 Remove-Item 은 승인 요청으로 옴.
WORKER_PERMISSION_MODE = "auto"
_ASK_PS = ["Remove-Item*", "rm *", "del *", "erase *", "rd *", "rmdir *", "ri *",
           "Move-Item*", "mv *", "move *", "mi *", "Rename-Item*", "ren *", "rni *"]
_ASK_SH = ["rm *", "rmdir *", "mv *"]
_ASK_GIT = ["git rm*", "git mv*", "git clean*", "git reset --hard*", "git push*"]
ASK_RULES = ([f"PowerShell({p})" for p in _ASK_PS + _ASK_GIT] + [f"Bash({p})" for p in _ASK_SH + _ASK_GIT])
WORKER_SETTINGS = json.dumps({"permissions": {"ask": ASK_RULES}}, ensure_ascii=False)
DENY_MESSAGE = ("사용자가 채팅창에서 이 명령을 거부했습니다. 같은 목적(특히 삭제·이동·폴더 밖 수정)을 다른 명령이나 도구로 "
                "우회하지 마세요. 꼭 필요하면 이유를 보고에 적고 사용자의 지시를 기다리세요.")
# 2026-09-30: 감독(Clara)이 Read 로는 xlsx·pptx 를 열 수 없어 실무자 보고문만 읽고 승인했음(7회 연속 APPROVE, 회당 20초)
# → python 실행을 허용해 결과물을 직접 열게 한다. 파일 수정 도구(Edit)는 없고, Write 는 검증용 _qa 폴더만 묻지 않는다.
# python 이 아닌 셸 명령·_qa 밖 쓰기는 채팅창 승인 카드로 간다.
CLAUDE_READONLY_TOOLS = "Read,Glob,Grep,Bash,PowerShell,Write"
CLAUDE_READONLY_ALLOWED = ["Read", "Glob", "Grep", "Bash(python *)", "PowerShell(python *)", "Write(./_qa/**)"]


@dataclass
class RunResult:
    ok: bool
    text: str
    structured: dict | None = None
    session_id: str | None = None
    error: str = ""
    raw: str = ""
    usage: dict | None = None    # 이번 답변의 토큰(엔진별 형식은 usage.py)
    limits: dict | None = None   # 구독 한도 사용률 {five_hour:{pct,resets_at}, seven_day:{...}}
    tool_calls: int | None = None  # 이번 답변에서 쓴 도구 횟수(파일 열기·실행·조회). None = 알 수 없음
    context: dict | None = None  # 세션 컨텍스트 크기 {used, window, updated}


class CancelledRun(Exception):
    pass


def child_env(settings: Settings) -> dict:
    env = dict(os.environ)
    if settings.python_dir:
        env["PATH"] = os.pathsep.join([settings.python_dir, os.path.join(settings.python_dir, "Scripts"), env.get("PATH", "")])
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    return env


def kill_tree(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True)
    else:
        proc.kill()


def describe_tool(name: str, inp: dict) -> str:
    """진행 표시용 한 줄 요약."""
    inp = inp or {}
    if name in ("Bash", "PowerShell"):
        return f"실행 중: {inp.get('command', '')}"
    if name in ("Read", "Write", "Edit", "NotebookEdit"):
        verb = {"Read": "읽는 중", "Write": "작성 중", "Edit": "수정 중"}.get(name, "편집 중")
        return f"{verb}: {os.path.basename(str(inp.get('file_path', '')))}"
    if name in ("Glob", "Grep"):
        return f"찾는 중: {inp.get('pattern', '')}"
    if name == "StructuredOutput":
        return "판정 정리 중"
    return f"{name} 사용 중"


class BaseRunner:
    def __init__(self, agent: AgentConfig, settings: Settings):
        self.agent = agent
        self.settings = settings
        self.session_id: str | None = None
        self.started = False  # 세션이 CLI 쪽에 실제로 만들어졌는지
        self.safe_mode = settings.claude_safe_mode  # 대화 단위로 고정(Room 이 덮어씀)
        self.usage_snapshot: dict | None = None  # 직전 답변 시점의 세션 누적 사용량
        self.context: dict | None = None  # 지금 세션이 들고 있는 컨텍스트 크기 {used, window, updated}
        self._tool_calls = 0
        self._was_started = False
        self._proc: subprocess.Popen | None = None
        self._cancelled = False

    # --- 하위 클래스 구현 ---
    def build_command(self, workspace: str, schema_path: str | None) -> list[str]:
        raise NotImplementedError

    def build_stdin(self, prompt: str, workspace: str) -> str:
        return prompt

    def parse_output(self, stdout: str) -> RunResult:
        raise NotImplementedError

    def on_line(self, line: str, ctx: "LineContext") -> None:
        pass

    # --- 공통 ---
    def state(self) -> dict:
        return {"session_id": self.session_id, "started": self.started, "usage_snapshot": self.usage_snapshot,
                "context": self.context}

    def restore(self, state: dict) -> None:
        self.session_id = state.get("session_id")
        self.started = bool(state.get("started"))
        self.usage_snapshot = state.get("usage_snapshot")
        self.context = state.get("context")

    def _set_context(self, used: int | None = None, window: int | None = None) -> None:
        import time as _time
        cur = dict(self.context or {})
        if used:
            cur["used"] = int(used)
        if window:
            cur["window"] = int(window)
        if cur.get("used"):
            cur["updated"] = _time.time()
            self.context = cur

    def _per_answer(self, cumulative: dict | None, fallback: dict | None) -> dict | None:
        """세션 누적값 → 이번 답변 사용량. 기준점이 없고 예전 세션이면 fallback(없으면 누적 표시)."""
        if not cumulative:
            return fallback
        if self.usage_snapshot:
            per = usage_mod.delta(cumulative, self.usage_snapshot)
        elif not self._was_started:
            per = dict(cumulative)  # 새 세션의 첫 답변은 누적 = 이번 답변
        elif fallback:
            per = fallback
        else:
            per = {**cumulative, "cumulative": True}
        self.usage_snapshot = cumulative
        return per

    def cancel(self) -> None:
        self._cancelled = True
        if self._proc:
            kill_tree(self._proc)

    async def run(self, prompt: str, workspace: str, schema_path: str | None = None,
                  activity: ActivityFn | None = None, approve: ApproveFn | None = None,
                  on_limits: LimitsFn | None = None, on_meta: MetaFn | None = None) -> RunResult:
        self._cancelled = False
        loop = asyncio.get_running_loop()

        def ts(fn):
            return (lambda *a: loop.call_soon_threadsafe(fn, *a)) if fn else (lambda *a: None)

        result = await asyncio.to_thread(self._run_blocking, prompt, workspace, schema_path,
                                         ts(activity), approve, ts(on_limits), ts(on_meta))
        if self._cancelled:
            raise CancelledRun()
        return result

    def _run_blocking(self, prompt: str, workspace: str, schema_path: str | None,
                      activity: ActivityFn, approve: ApproveFn | None,
                      on_limits: LimitsFn = lambda d: None, on_meta: MetaFn = lambda d: None) -> RunResult:
        self._was_started = self.started
        self._tool_calls = 0
        try:
            cmd = self.build_command(workspace, schema_path)
        except (OSError, ValueError) as e:  # 예: Codex CLI 가 없는 PC → 과제를 멈추지 않고 그 담당자만 실패로
            return RunResult(ok=False, text="", error=str(e))
        stdin_text = self.build_stdin(prompt, workspace)
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        try:
            proc = subprocess.Popen(cmd, cwd=workspace, env=child_env(self.settings), stdin=subprocess.PIPE,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                    encoding="utf-8", errors="replace", creationflags=flags, bufsize=1)
        except OSError as e:
            return RunResult(ok=False, text="", error=f"CLI 실행 실패: {e}")
        self._proc = proc
        timed_out = threading.Event()

        def on_timeout() -> None:
            timed_out.set()
            kill_tree(proc)

        timer = threading.Timer(self.settings.call_timeout_sec, on_timeout)
        timer.start()
        stderr_chunks: list[str] = []
        t_err = threading.Thread(target=lambda: stderr_chunks.append(proc.stderr.read()), daemon=True)
        t_err.start()
        ctx = LineContext(proc, activity, approve, on_limits, on_meta)
        poller = threading.Thread(target=self.poll_progress, args=(proc, ctx), daemon=True)
        poller.start()
        try:
            proc.stdin.write(stdin_text)
            proc.stdin.flush()
            if not self.keeps_stdin_open:
                proc.stdin.close()  # codex exec 는 stdin 이 열려 있으면 추가 입력을 기다림(실측)
        except OSError:
            pass
        lines = []
        for line in proc.stdout:
            lines.append(line)
            try:
                self.on_line(line, ctx)
            except Exception:  # 진행 표시 실패가 본 작업을 막지 않게
                pass
        ctx.close_stdin()
        proc.wait()
        timer.cancel()
        t_err.join(timeout=5)
        self._proc = None
        stdout = "".join(lines)
        stderr = "".join(stderr_chunks)
        result = self.parse_output(stdout)
        result.raw = stdout
        result.tool_calls = self._tool_calls
        result.context = self.context
        if timed_out.is_set() and not self._cancelled:
            result.ok = False
            result.error = f"제한 시간({self.settings.call_timeout_sec}초) 초과로 중단했습니다"
        elif proc.returncode != 0 and not result.error:
            result.ok = False
            result.error = f"종료 코드 {proc.returncode}: {stderr.strip()[-800:]}"
        return result

    keeps_stdin_open = False

    def poll_progress(self, proc: subprocess.Popen, ctx: "LineContext") -> None:
        """실행 중 주기적으로 진행 상황을 읽는 곳(codex 는 세션 파일). 기본은 아무것도 안 함."""


class LineContext:
    """on_line 이 쓰는 도구 묶음: 진행 표시, 승인 요청, stdin 응답, 실시간 토큰·한도."""

    def __init__(self, proc: subprocess.Popen, activity: ActivityFn, approve: ApproveFn | None,
                 on_limits: LimitsFn = lambda d: None, on_meta: MetaFn = lambda d: None):
        self.proc = proc
        self.activity = activity
        self.approve = approve
        self.limits = on_limits
        self.meta = on_meta
        self.msg_tokens: dict[str, int] = {}  # claude: 응답 메시지 id → 그 메시지 토큰(같은 id 가 여러 번 옴)
        self._lock = threading.Lock()
        self._closed = False

    def send(self, obj: dict) -> None:
        with self._lock:
            if self._closed:
                return
            self.proc.stdin.write(json.dumps(obj, ensure_ascii=False) + "\n")
            self.proc.stdin.flush()

    def close_stdin(self) -> None:
        with self._lock:
            if not self._closed:
                self._closed = True
                try:
                    self.proc.stdin.close()
                except OSError:
                    pass


class ClaudeRunner(BaseRunner):
    keeps_stdin_open = True  # 승인 응답을 stdin 으로 보내야 해서 result 가 올 때까지 열어 둠

    def build_command(self, workspace: str, schema_path: str | None) -> list[str]:
        s, a = self.settings, self.agent
        if not self.session_id:
            self.session_id = str(uuid.uuid4())
        self._rate_info = None
        cmd = [s.claude_exe, "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose"]
        cmd += ["--resume", self.session_id] if self.started else ["--session-id", self.session_id]
        if a.model:
            cmd += ["--model", a.model]
        if a.effort:
            cmd += ["--effort", a.effort]
        if self.safe_mode:
            cmd += ["--safe-mode"]
        cmd += ["--append-system-prompt", a.system_prompt(workspace), "--permission-prompt-tool", "stdio"]
        if schema_path:
            with open(schema_path, encoding="utf-8") as f:
                cmd += ["--json-schema", json.dumps(json.load(f), ensure_ascii=False)]
        if a.access == "readonly":
            # 감독은 읽기 도구만 쓸 수 있고(--tools), 그 읽기는 작업 폴더 밖이어도 묻지 않는다(2026-09-28 사장님 승인).
            # 범위 없는 규칙은 폴더 밖도 허용함(실측: 범위 없는 Write 가 폴더 밖 쓰기를 허용했음).
            # MCP 조회(DART·법령·verifier)도 묻지 않게(9/28 실측: Clara 는 MCP 호출마다 승인 요청이 왔음)
            cmd += ["--tools", CLAUDE_READONLY_TOOLS, "--allowedTools", *CLAUDE_READONLY_ALLOWED,
                    *[f"mcp__{m}" for m in (s.readonly_mcp_allow or [])]]
        else:
            cmd += ["--permission-mode", WORKER_PERMISSION_MODE, "--settings", WORKER_SETTINGS,
                    "--allowedTools", *CLAUDE_WRITE_TOOLS]
        return cmd

    def build_stdin(self, prompt: str, workspace: str) -> str:
        return json.dumps({"type": "user", "message": {"role": "user", "content": prompt}}, ensure_ascii=False) + "\n"

    def on_line(self, line: str, ctx: LineContext) -> None:
        ev = json.loads(line)
        t = ev.get("type")
        if t == "control_request":
            req = ev.get("request") or {}
            rid = ev.get("request_id")
            if req.get("subtype") == "can_use_tool":
                inp = req.get("input") or {}
                allowed = bool(ctx.approve and ctx.approve(req.get("tool_name", ""), inp, req.get("description", "")))
                resp = {"behavior": "allow", "updatedInput": inp} if allowed else \
                    {"behavior": "deny", "message": DENY_MESSAGE}
                ctx.send({"type": "control_response",
                          "response": {"subtype": "success", "request_id": rid, "response": resp}})
            else:
                ctx.send({"type": "control_response", "response": {"subtype": "success", "request_id": rid, "response": {}}})
        elif t == "system" and ev.get("subtype") == "init":
            if ev.get("slash_commands"):
                ctx.meta({"slash_commands": ev.get("slash_commands")})
        elif t == "assistant":
            msg = ev.get("message") or {}
            u = msg.get("usage") or {}
            if msg.get("id") and u:
                ctx.msg_tokens[msg["id"]] = sum(int(u.get(k) or 0) for k in (
                    "input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens", "output_tokens"))
            if u and not ev.get("parent_tool_use_id"):  # 하위 에이전트 말고 본 세션의 마지막 요청 크기 = 지금 컨텍스트
                self._set_context(used=sum(int(u.get(k) or 0) for k in (
                    "input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens", "output_tokens")))
            text = None
            for c in msg.get("content") or []:
                if c.get("type") == "tool_use":
                    if c.get("name") != "StructuredOutput":
                        self._tool_calls += 1
                    text = describe_tool(c.get("name", ""), c.get("input") or {})
                elif c.get("type") == "text" and c.get("text", "").strip():
                    first = c["text"].strip().splitlines()[0]
                    text = "💬 " + first[:140]
            ctx.activity(text, sum(ctx.msg_tokens.values()) or None)
        elif t == "rate_limit_event":
            self._rate_info = ev.get("rate_limit_info")
            lim = usage_mod.claude_limits(self._rate_info)
            if lim:
                ctx.limits(lim)
        elif t == "result":
            if ev.get("session_id"):
                self.started = True
            ctx.close_stdin()  # 대화 한 턴 끝 → CLI 종료

    def parse_output(self, stdout: str) -> RunResult:
        result = None
        for line in stdout.splitlines():
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if ev.get("type") == "result":
                result = ev
        if result is None:
            return RunResult(ok=False, text=stdout.strip()[-800:], error="claude 결과(result 이벤트)를 받지 못했습니다")
        self.started = True
        windows = [int(m.get("contextWindow") or 0) for m in (result.get("modelUsage") or {}).values()
                   if isinstance(m, dict)]
        if any(windows):  # 하위 모델(haiku 등)이 섞여도 본 모델 창이 가장 큼
            self._set_context(window=max(windows))
        is_error = bool(result.get("is_error"))
        return RunResult(ok=not is_error, text=str(result.get("result") or ""),
                         structured=result.get("structured_output"), session_id=self.session_id,
                         error=str(result.get("result")) if is_error else "",
                         usage=self._per_answer(usage_mod.claude_usage(result), usage_mod.claude_usage_top(result)),
                         limits=usage_mod.claude_limits(getattr(self, "_rate_info", None)))


class CodexRunner(BaseRunner):
    def _exe(self) -> str:
        return find_codex_exe() if self.settings.codex_exe == "auto" else self.settings.codex_exe

    def _sandbox(self) -> str:
        return "read-only" if self.agent.access == "readonly" else self.settings.codex_sandbox

    def build_command(self, workspace: str, schema_path: str | None) -> list[str]:
        s, a = self.settings, self.agent
        if self.started and self.session_id:
            cmd = [self._exe(), "exec", "resume", "--json", "--skip-git-repo-check",
                   "-c", f'sandbox_mode="{self._sandbox()}"']
        else:
            cmd = [self._exe(), "exec", "--json", "--skip-git-repo-check", "-s", self._sandbox(), "-C", workspace]
        if s.codex_windows_sandbox:
            cmd += ["-c", f'windows.sandbox="{s.codex_windows_sandbox}"']
        if s.codex_approvals_reviewer:
            # 9/28 실측: exec 는 승인할 사람이 없어 MCP 호출이 "requires approval, but approval policy is never"로 거절됨
            # → approvals_reviewer="auto_review" 면 workspace-write 그대로 MCP·파일 쓰기 모두 됨(--approve-for-me 는 -s 와 같이 못 씀)
            cmd += ["-c", f'approvals_reviewer="{s.codex_approvals_reviewer}"']
        if a.model:
            cmd += ["-m", a.model]
        if a.effort:
            cmd += ["-c", f'model_reasoning_effort="{a.effort}"']
        if schema_path:
            cmd += ["--output-schema", schema_path]
        if self.started and self.session_id:
            cmd += [self.session_id]
        cmd += ["-"]
        return cmd

    def poll_progress(self, proc: subprocess.Popen, ctx: LineContext) -> None:
        """실행 중 4초마다 세션 파일 끝부분에서 누적 토큰·한도를 읽어 실시간으로 보여준다."""
        import time as _time
        path, last_tokens = None, None
        while proc.poll() is None:
            _time.sleep(4)
            if not self.session_id:
                continue
            path = path or usage_mod.find_codex_rollout(self.session_id)
            if not path:
                continue
            total, limits = usage_mod.read_codex_tail(path)
            self._set_context(*usage_mod.codex_context(path))
            if limits:
                ctx.limits(limits)
            if total:
                cur = usage_mod.codex_usage(total, None)
                per = usage_mod.delta(cur, self.usage_snapshot) if self.usage_snapshot else cur
                if per.get("total_tokens") != last_tokens:
                    last_tokens = per.get("total_tokens")
                    ctx.activity(None, last_tokens)

    def build_stdin(self, prompt: str, workspace: str) -> str:
        # codex exec 에는 시스템 프롬프트 옵션이 없어 첫 호출에만 역할 지침을 앞에 붙인다(이후는 세션이 기억)
        if self.started:
            return prompt
        return f"[역할 지침]\n{self.agent.system_prompt(workspace)}\n\n[이번 요청]\n{prompt}"

    def on_line(self, line: str, ctx: LineContext) -> None:
        ev = json.loads(line)
        if ev.get("type") == "thread.started":
            self.session_id = ev.get("thread_id")
            self.started = True
        item = ev.get("item") or {}
        if ev.get("type") == "item.started" and item.get("type") in ("command_execution", "mcp_tool_call", "web_search"):
            self._tool_calls += 1
        if ev.get("type") == "item.completed" and item.get("type") == "agent_message":
            txt = (item.get("text") or "").strip()
            if txt and not txt.startswith("{"):  # 최종 JSON 판정은 제외, 중간 설명만
                ctx.activity("💬 " + txt.splitlines()[0][:140])
        if ev.get("type") == "item.started" and item.get("type") == "web_search":
            ctx.activity(f"웹 검색 중: {item.get('query') or ''}"[:160])
        if ev.get("type") == "item.started" and item.get("type") == "mcp_tool_call":
            ctx.activity(f"{item.get('server')} 조회 중: {item.get('tool')}")
        if ev.get("type") == "item.started" and item.get("type") == "command_execution":
            cmd = item.get("command", "")
            if "-Command" in cmd:
                cmd = cmd.split("-Command", 1)[1].strip().strip("'\"").replace('\\"', '"')
            ctx.activity(f"실행 중: {cmd[:160]}")

    def parse_output(self, stdout: str) -> RunResult:
        last, errors, turn_usage = "", [], {}
        for line in stdout.splitlines():
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            t = ev.get("type")
            item = ev.get("item") or {}
            if t == "thread.started":
                self.session_id = ev.get("thread_id")
                self.started = True
            elif t == "item.completed" and item.get("type") == "agent_message":
                last = item.get("text", "")
            elif t == "turn.completed":
                for k, v in (ev.get("usage") or {}).items():
                    turn_usage[k] = turn_usage.get(k, 0) + (v or 0)
            elif t in ("error", "turn.failed"):
                errors.append(str(ev.get("message") or ev.get("error") or ev))
        structured = None
        try:
            obj = json.loads(last)
            if isinstance(obj, dict):
                structured = obj
        except ValueError:
            pass
        if not last and errors:
            return RunResult(ok=False, text="", session_id=self.session_id, error="; ".join(errors)[-800:])
        limits, model, last_turn = None, self.agent.model, None
        rollout = usage_mod.find_codex_rollout(self.session_id) if self.session_id else None
        if rollout:
            limits, used_model = usage_mod.read_codex_rollout(rollout)
            model = used_model or model
            last_turn = usage_mod.codex_last_turn(rollout)
            self._set_context(*usage_mod.codex_context(rollout))
            if last_turn:
                last_turn["models"] = [model] if model else []
        return RunResult(ok=bool(last), text=last, structured=structured, session_id=self.session_id,
                         error="" if last else "codex 응답 메시지가 없습니다",
                         usage=self._per_answer(usage_mod.codex_usage(turn_usage, model) if turn_usage else None, last_turn),
                         limits=limits)


def make_runner(agent: AgentConfig, settings: Settings) -> BaseRunner:
    return ClaudeRunner(agent, settings) if agent.engine == "claude" else CodexRunner(agent, settings)
