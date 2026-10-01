"""대화 흐름과 대화 목록 관리.

검토 모드: 사용자 지시 → 실무자 → 감독들(순서대로) → (지적 시) 실무자 수정 → 반복
- 모든 감독이 APPROVE 하면 완료, max_rounds 안에 합의가 안 되면 마지막 지적 원문을 모아 사용자에게 넘김
- 진행 중 사용자 메시지는 대기열에 넣었다가 다음 에이전트 호출 직전에 끼워 넣음
빠른 질문: 실무자만 답함(검토 없음) / @이름: 그 사람만 답함
승인: 허용 목록 밖의 명령은 채팅에 승인 카드를 띄우고 사용자가 누를 때까지 기다림
세션: 에이전트별 CLI 세션 ID 를 state.json 에 저장해 서버를 재시작해도 맥락이 이어짐
"""
from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Awaitable, Callable

from . import inbox, prompts
from .projects import ProjectStore, memo_block, memo_signature, suggest_groups
from .config import AppConfig
from .logstore import LogStore
from .runners import BaseRunner, CancelledRun, make_runner
from .usage import LimitStore
from .verdict import REVIEW_SCHEMA, parse_review

Broadcast = Callable[[dict], Awaitable[None]]
RunnerFactory = Callable[..., BaseRunner]

MODES = ("review", "quick")

UNAVAILABLE_COMMANDS = {
    "rc": "`/rc`(원격 제어)는 터미널의 대화형 Claude Code 에서만 됩니다. 이 앱은 CLI 를 -p(비대화) 모드로 불러서 쓸 수 없습니다(실측).",
    "remote-control": "`/remote-control`은 터미널의 대화형 Claude Code 에서만 됩니다(실측).",
    "resume": "`/resume`은 이 앱에 필요 없습니다. 대화마다 에이전트 세션이 저장돼 있어, 왼쪽 목록에서 대화를 열고 이어서 말하면 그대로 이어집니다.",
    "continue": "`/continue`는 이 앱에 필요 없습니다. 왼쪽 목록에서 대화를 열고 이어서 말하면 세션이 이어집니다.",
    "exit": "이 앱에서는 `/exit` 대신 대화창을 닫거나 대화 나가기(보관)를 쓰시면 됩니다.",
    "login": "`/login`은 터미널에서 `claude auth login`으로 해 주세요.",
}
RECENT_CONTEXT_MESSAGES = 8
COMPACT_TIP_TOKENS = 1_000_000   # 한 답변이 이만큼 넘으면 /compact 안내(대화당 한 번)
SNAPSHOT_SKIP_DIRS = {"__pycache__", ".git", "node_modules", ".venv", "venv", "_qa"}
SNAPSHOT_LIMIT = 5000


def is_temp_file(name: str) -> bool:
    """엑셀·워드 잠금 파일(~$…), LibreOffice 잠금(.~lock…)은 산출물이 아님."""
    return name.startswith("~$") or name.startswith(".~lock")


def workspace_snapshot(base) -> dict:
    """작업 폴더 파일 → (수정 시각, 크기). 라운드 전후를 비교해 바뀐 파일을 찾는 데 쓴다."""
    import os
    out = {}
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if d not in SNAPSHOT_SKIP_DIRS and not d.startswith(".")]
        for fn in filenames:
            if is_temp_file(fn):
                continue
            p = os.path.join(dirpath, fn)
            try:
                st = os.stat(p)
            except OSError:
                continue
            out[os.path.relpath(p, base).replace("\\", "/")] = (st.st_mtime, st.st_size)
            if len(out) >= SNAPSHOT_LIMIT:
                return out
    return out


def changed_between(before: dict, after: dict, limit: int = 60) -> list[str]:
    changed = [p for p, v in after.items() if before.get(p) != v]
    changed.sort(key=lambda p: after[p][0], reverse=True)
    return changed[:limit]


def _claude_keys(cfg: AppConfig) -> list[str]:
    return [k for k, a in cfg.agents.items() if a.engine == "claude"]


def _rules_off(cfg: AppConfig, state: dict, safe_mode: bool | None = None,
               global_rules: dict | None = None) -> list[str]:
    """전역 규칙을 끈 Claude 담당자. 저장된 대화 > 새 대화 요청(담당자별 > 일괄) > 설정 기본값 순."""
    keys = _claude_keys(cfg)
    if "rules_off" in state:
        return [k for k in state["rules_off"] if k in keys]
    if "safe_mode" in state:  # 10/1 이전 대화: 전원 일괄
        return list(keys) if state["safe_mode"] else []
    if isinstance(global_rules, dict):
        return [k for k in keys if not global_rules.get(k, cfg.settings.rules_on(k))]
    if safe_mode is not None:
        return list(keys) if safe_mode else []
    return [k for k in keys if not cfg.settings.rules_on(k)]


def _title_from(messages: list[dict]) -> str:
    first = next((m for m in messages if m.get("sender") == "user"), None)
    return (first["text"] or (first.get("attachments") or [""])[0])[:40] if first else ""


class Room:
    def __init__(self, cfg: AppConfig, broadcast: Broadcast, runner_factory: RunnerFactory = make_runner,
                 conv_id: str | None = None, workspace: str | None = None, title: str | None = None,
                 safe_mode: bool | None = None, on_change: Callable[[], Awaitable[None]] | None = None,
                 exclude: list[str] | None = None, global_rules: dict | None = None,
                 on_limits: Callable[[str, dict], Awaitable[None]] | None = None,
                 on_meta: Callable[[dict], Awaitable[None]] | None = None,
                 slash_commands: Callable[[], list] | None = None,
                 projects: ProjectStore | None = None, project: str | None = None):
        self.cfg = cfg
        self.projects = projects or ProjectStore(cfg.settings.log_dir)
        self.on_limits = on_limits
        self.on_meta = on_meta
        self.slash_commands = slash_commands or (lambda: [])
        self.active: set[str] = set()          # 지금 답하는 중인 담당자(감독 병렬 검토라 여러 명일 수 있음)
        self.typing_tokens: dict[str, int] = {}
        self._last_limits: dict[str, dict] = {}
        self._sent_context: dict[str, int] = {}
        self._broadcast = broadcast
        self.on_change = on_change
        self.runner_factory = runner_factory
        self.task: asyncio.Task | None = None
        self._busy = False  # 과제 진행 중 여부(과제 마지막 상태 알림 시점에 task.done() 이 아직 False 라 따로 둠)
        self.pending: list[str] = []
        self.pending_ids: list[str] = []       # pending 과 같은 순서의 사용자 메시지 id(전달 전이라 수정·취소 가능)
        self.editable: dict | None = None      # 방금 시작한 지시 {id, agent, fresh}: 첫 답이 나오기 전까지 수정 가능
        self._retracting = False               # 지시 수정 때문에 중단하는 중
        self._correction = False               # 다음 지시 앞에 "직전 지시는 취소" 안내를 붙일지
        self.current_agent: str | None = None
        self.typing: dict[str, str] = {}
        self.round = 0
        self.approvals: dict[str, asyncio.Future] = {}
        self.loop: asyncio.AbstractEventLoop | None = None
        self.schema_path = cfg.settings.log_dir / "review_schema.json"
        cfg.settings.log_dir.mkdir(parents=True, exist_ok=True)
        schema_text = json.dumps(REVIEW_SCHEMA, ensure_ascii=False)
        try:  # 스키마가 바뀌면(앱 업데이트) 예전 파일을 그대로 쓰지 않게 내용이 다르면 다시 쓴다
            stale = self.schema_path.read_text(encoding="utf-8") != schema_text
        except OSError:
            stale = True
        if stale:
            self.schema_path.write_text(schema_text, encoding="utf-8")

        self.runners = {k: runner_factory(a, cfg.settings) for k, a in cfg.agents.items()}
        self.conv_id = conv_id or datetime.now().strftime("%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:4]
        self.store = LogStore(cfg.settings.log_dir, self.conv_id)
        state = self.store.load_state() or {}
        self.title = state.get("title") or title or ""
        self.created = state.get("created") or time.time()
        self.archived = bool(state.get("archived", False))  # 대화 나가기 = 보관(목록에서 숨김, 기록·폴더는 그대로)
        # 대화별로 뺀 담당자(예: 고객자료를 OpenAI 로 보내지 않으려고 Eric 제외)
        self.excluded: list[str] = [k for k in (state.get("excluded") if "excluded" in state else (exclude or []))
                                    if k in cfg.agents and k != cfg.worker]
        self.compact_tip_shown = bool(state.get("compact_tip_shown", False))
        self.archived_at = state.get("archived_at")
        # 전역 규칙을 끈 Claude 담당자 목록(대화마다 고정). safe_mode=True/False 는 예전 방식(전원 일괄)
        self.rules_off: list[str] = _rules_off(cfg, state, safe_mode, global_rules)
        self.project: str | None = state.get("project") if "project" in state else project
        # 담당자별로 마지막에 보낸 프로젝트 메모 표시(memo_signature). 바뀌었을 때만 다시 보내거나 해제를 알린다
        self.memo_sent: dict[str, str] = dict(state.get("memo_sent") or {})
        ws = state.get("workspace") or workspace
        self.workspace = Path(ws) if ws else cfg.settings.workspace_root / self.conv_id
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.messages = self.store.load_messages()
        if not self.title:  # 제목이 없던 예전 대화는 첫 메시지로
            self.title = _title_from(self.messages)
        for m in self.messages:  # 서버 재시작 전 대기 중이던 승인은 만료 처리
            if m.get("kind") == "approval" and m["approval"].get("status") == "pending":
                m["approval"]["status"] = "expired"
        self.requirements: list[str] = state.get("requirements", [])
        self.seen: dict[str, int] = state.get("seen", {})
        for k, st in state.get("sessions", {}).items():
            if k in self.runners:
                self.runners[k].restore(st)
        for k, r in self.runners.items():
            r.safe_mode = k in self.rules_off
        self._save_state()

    # ------------------------------------------------------------ 상태
    @property
    def running(self) -> bool:
        return self._busy

    def _start(self, coro) -> None:
        self._busy = True
        self.task = asyncio.create_task(coro)

    def apply_config(self, cfg: AppConfig) -> None:
        """설정 창 저장 후: 이름·사진·프롬프트 등을 다음 호출부터 반영(세션·대화 단위 전역 규칙 설정은 유지)."""
        self.cfg = cfg
        for k, r in self.runners.items():
            if k in cfg.agents:
                r.agent = cfg.agents[k]
                r.settings = cfg.settings

    def summary(self) -> dict:
        last = next((m for m in reversed(self.messages) if m.get("kind") != "approval"), None)
        who = ""
        if last:
            who = self.cfg.user.get("name", "나") if last["sender"] == "user" else \
                ("" if last["sender"] == "system" else self.cfg.agents.get(last["sender"]).name if last["sender"] in self.cfg.agents else "")
        return {"id": self.conv_id, "title": self.title or "새 대화", "workspace": str(self.workspace),
                "updated": last["ts"] if last else self.created, "created": self.created,
                "last": ((who + ": ") if who else "") + (last["text"][:80] if last else ""),
                "running": self.running, "safe_mode": self.safe_mode, "rules_off": self.rules_off,
                "archived": self.archived, "excluded": self.excluded, "project": self.project,
                "approvals": sum(1 for f in self.approvals.values() if not f.done())}

    @property
    def safe_mode(self) -> bool:
        """예전 표시용: Claude 담당자 전원이 전역 규칙 OFF 인지."""
        keys = _claude_keys(self.cfg)
        return bool(keys) and all(k in self.rules_off for k in keys)

    @property
    def reviewers(self) -> list[str]:
        return [r for r in self.cfg.reviewers if r not in self.excluded]

    async def set_excluded(self, agent_key: str, excluded: bool) -> None:
        if agent_key not in self.cfg.agents or agent_key == self.cfg.worker:
            raise ValueError("실무자는 뺄 수 없습니다" if agent_key == self.cfg.worker else f"없는 담당자: {agent_key}")
        if excluded and agent_key not in self.excluded:
            self.excluded.append(agent_key)
        elif not excluded and agent_key in self.excluded:
            self.excluded.remove(agent_key)
        self._save_state()
        name = self.cfg.agents[agent_key].name
        await self._post("system", f"이 대화에서 {name}를 뺐습니다. 다음 지시부터 {name}에게는 아무 자료도 보내지 않습니다."
                         if excluded else f"이 대화에 {name}를 다시 넣었습니다.")
        if self.on_change:
            await self.on_change()

    async def set_archived(self, archived: bool) -> None:
        """대화 나가기(보관) / 다시 꺼내기. 진행 중이면 먼저 중단한다. 파일은 아무것도 지우지 않는다."""
        if archived and self.running:
            await self.stop()
        self.archived = bool(archived)
        self.archived_at = time.time() if archived else None
        self._save_state()
        if self.on_change:
            await self.on_change()

    def snapshot(self) -> dict:
        return {"type": "snapshot", "conv": self.conv_id, "summary": self.summary(), "messages": self.messages,
                "running": self.running, "round": self.round, "typing": self.typing,
                "typing_tokens": self.typing_tokens, "pending": len(self.pending), "contexts": self.contexts(),
                "editable": self.editable_ids()}

    def editable_ids(self) -> list[str]:
        """지금 수정할 수 있는 사용자 메시지: 전달 대기 중인 것 + 첫 답이 나오기 전의 방금 지시."""
        return list(self.pending_ids) + ([self.editable["id"]] if self.editable else [])

    def _save_state(self) -> None:
        self.store.save_state({"conv_id": self.conv_id, "title": self.title, "created": self.created,
                               "workspace": str(self.workspace), "safe_mode": self.safe_mode,
                               "rules_off": self.rules_off, "archived": self.archived, "archived_at": self.archived_at,
                               "excluded": self.excluded, "compact_tip_shown": self.compact_tip_shown,
                               "sessions": {k: r.state() for k, r in self.runners.items()},
                               "requirements": self.requirements, "seen": self.seen,
                               "project": self.project, "memo_sent": self.memo_sent})

    async def broadcast(self, payload: dict) -> None:
        payload["conv"] = self.conv_id
        await self._broadcast(payload)

    def _name(self, sender: str) -> str:
        if sender == "user":
            return self.cfg.user.get("name", "나")
        if sender == "system":
            return "시스템"
        a = self.cfg.agents.get(sender)
        return f"{a.name} · {a.title}" if a else sender

    async def _post(self, sender: str, text: str, **extra) -> dict:
        msg = {"id": uuid.uuid4().hex[:12], "sender": sender, "text": text, "ts": time.time(), **extra}
        closed = False
        if self.editable and sender in self.cfg.agents and extra.get("kind") != "approval":
            self.editable = None  # 담당자의 첫 답이 나왔다 → 그 지시는 더 이상 수정할 수 없음
            closed = True
        self.messages.append(msg)
        self.store.append(msg, self._name(sender))
        await self.broadcast({"type": "message", "message": msg})
        if closed:
            await self.broadcast({"type": "state", "running": self.running, "round": self.round,
                                  "pending": len(self.pending), "editable": self.editable_ids()})
        if self.on_change:
            await self.on_change()
        return msg

    async def _update(self, msg: dict, **fields) -> None:
        msg.update(fields)
        self.store.append_update(msg["id"], fields)
        await self.broadcast({"type": "message_update", "message": msg})

    async def _state(self) -> None:
        await self.broadcast({"type": "state", "running": self.running, "round": self.round,
                              "pending": len(self.pending), "editable": self.editable_ids()})
        if self.on_change:
            await self.on_change()

    async def _typing(self, agent: str, on: bool, activity: str | None = "", tokens: int | None = None) -> None:
        if on:
            if activity is not None:
                self.typing[agent] = activity
            if tokens is not None:
                self.typing_tokens[agent] = tokens
        else:
            self.typing.pop(agent, None)
            self.typing_tokens.pop(agent, None)
        await self.broadcast({"type": "typing", "agent": agent, "on": on, "activity": self.typing.get(agent, ""),
                              "tokens": self.typing_tokens.get(agent)})

    # ------------------------------------------------------------ 사용자 입력
    def _find_mention(self, text: str) -> tuple[str | None, str]:
        m = re.match(r"^@(\S+)\s*", text)
        if not m:
            return None, text
        tag = m.group(1).lower()
        for k, a in self.cfg.agents.items():
            if tag in (k.lower(), a.name.lower()):
                return k, text[m.end():].strip() or text
        return None, text

    async def user_message(self, text: str, mode: str = "review", attachments: list[str] | None = None) -> None:
        text = text.strip()
        attachments = [a for a in (attachments or []) if a]
        if not text and not attachments:
            return
        self.loop = asyncio.get_running_loop()
        if not self.title:
            self.title = (text or attachments[0])[:40]
            self._save_state()
        if self.archived:
            await self.set_archived(False)
        posted = await self._post("user", text, attachments=attachments, mode=mode)
        body = text
        if attachments:
            body += "\n\n[첨부 파일 — 작업 폴더 기준 경로]\n" + "\n".join(f"- {p}" for p in attachments)

        target, stripped = self._find_mention(text)
        if target and target in self.excluded:
            await self._post("system", f"{self.cfg.agents[target].name}는 이 대화에서 빠져 있습니다. ⋯ 메뉴에서 다시 넣을 수 있습니다.")
            return
        command = (stripped if target else text).strip()
        if command.startswith("/"):
            await self._slash(target or self.cfg.worker, command)
            return
        if self.running:
            self.pending.append(body)
            self.pending_ids.append(posted["id"])
            who = "·".join(self.cfg.agents[k].name for k in sorted(self.active)) or "다음 에이전트"
            await self._post("system", f"{who}의 현재 작업이 끝나면 다음 발언 차례에 이 지시를 전달합니다.")
            await self._state()
            return
        note = ""
        if self._correction:  # 직전 지시를 수정하려고 중단한 뒤의 첫 지시(세션에는 예전 지시가 남아 있음)
            self._correction = False
            note = "[정정] 직전 지시는 취소합니다. 하다 만 작업이 있으면 아래 지시에 맞게 정리하고, 아래 지시만 따르세요.\n\n"
        agent_key = target or self.cfg.worker
        self.editable = {"id": posted["id"], "agent": agent_key, "fresh": not self.runners[agent_key].started}
        if target:
            if attachments:
                stripped += body[len(text):]
            self._start(self._run_single(target, note + stripped))
        elif mode == "quick":
            self._start(self._run_single(self.cfg.worker, note + body))
        else:
            # 수정하려고 중단할 때 남겨 둔 전달 대기 지시가 있으면 함께 시작한다
            held, self.pending, self.pending_ids = list(self.pending), [], []
            self._start(self._run_task(held + [note + body]))
        await self._state()

    async def edit_message(self, msg_id: str) -> bool:
        """사용자가 보낸 지시를 고치려고 되돌린다. 전달 대기 중이면 대기열에서 빼고, 방금 시작한 지시면 진행을 중단한다.
        말풍선은 지우지 않고 '수정됨'으로 남긴다. 화면이 글·첨부를 입력창으로 되돌린다."""
        msg = next((m for m in self.messages if m.get("id") == msg_id and m.get("sender") == "user"), None)
        if not msg:
            return False
        if msg_id in self.pending_ids:
            i = self.pending_ids.index(msg_id)
            self.pending_ids.pop(i)
            self.pending.pop(i)
            await self._update(msg, retracted=True)
            await self._state()
            return True
        ed = self.editable
        if not ed or ed["id"] != msg_id:
            return False
        self.editable = None
        held, held_ids = list(self.pending), list(self.pending_ids)
        self._retracting = True
        try:
            await self.stop()
        finally:
            self._retracting = False
        self.pending, self.pending_ids = held, held_ids  # stop 이 비운 전달 대기 지시는 다음 지시 때 함께 보낸다
        runner = self.runners[ed["agent"]]
        if ed["fresh"]:
            # 이 담당자의 첫 지시였다 → 세션을 버리고 새로 시작하면 원래 지시가 없던 것과 같다
            runner.restore({})
        else:
            # CLI 에 세션을 되감는 옵션이 없다(2026-09-30 claude 2.1.285 --help 확인) → 다음 지시에 정정 안내를 붙인다
            self._correction = True
        await self._update(msg, retracted=True)
        self._save_state()
        await self._state()
        return True

    async def _slash(self, agent_key: str, command: str) -> None:
        """`/명령`은 감싸지 않고 그대로 CLI 에 보낸다(9/28 실측: 메시지를 감싸면 명령이 맨 앞이 아니라 실행되지 않았음)."""
        name = command[1:].split()[0].lower() if len(command) > 1 else ""
        agent = self.cfg.agents[agent_key]
        if name in UNAVAILABLE_COMMANDS:
            await self._post("system", UNAVAILABLE_COMMANDS[name])
            return
        if agent.engine != "claude":
            await self._post("system", f"{agent.name}(Codex)은 `/` 명령을 쓸 수 없습니다. `/` 명령은 Claude 담당자에게 보내 주세요.")
            return
        if self.running:
            await self._post("system", "진행 중에는 `/` 명령을 보낼 수 없습니다. 지금 작업이 끝난 뒤 다시 보내 주세요.")
            return
        known = self.slash_commands()
        if known and name not in {c.lower() for c in known}:
            await self._post("system", f"`/{name}`은 이 앱에서 쓸 수 있는 명령 목록에 없습니다. 입력창에 `/`만 치면 쓸 수 있는 명령이 보입니다.")
            return
        self._start(self._run_single(agent_key, command, raw=True))
        await self._state()

    def _stop_text(self, tail: str = "") -> str:
        if self._retracting:  # 알림 쪽이 "사용자 요청으로 중단"으로 시작하는지 보고 푸시를 생략한다
            return "사용자 요청으로 중단했습니다(지시 수정). 고친 지시를 보내 주세요."
        return "사용자 요청으로 중단했습니다." + tail

    async def stop(self) -> None:
        self.editable = None
        self.pending_ids.clear()
        self.pending.clear()
        for f in self.approvals.values():
            if not f.done():
                f.set_result(False)
        if not self.running or self.task is None:
            self._busy = False
            return
        for r in self.runners.values():
            r.cancel()
        self.task.cancel()
        try:
            await self.task
        except (asyncio.CancelledError, Exception):
            pass
        for r in self.runners.values():  # 중단 표시는 이번 중단까지만(다음 호출이 취소로 오인하지 않게)
            r._cancelled = False

    # ------------------------------------------------------------ 승인
    def _approver(self, agent_key: str):
        loop = asyncio.get_running_loop()

        def approve(tool: str, inp: dict, desc: str) -> bool:  # CLI 출력 읽는 스레드에서 호출됨
            fut = asyncio.run_coroutine_threadsafe(self._ask_approval(agent_key, tool, inp, desc), loop)
            return fut.result()

        return approve

    async def _ask_approval(self, agent_key: str, tool: str, inp: dict, desc: str) -> bool:
        aid = uuid.uuid4().hex[:10]
        fut = asyncio.get_running_loop().create_future()
        self.approvals[aid] = fut
        detail = inp.get("command") or inp.get("file_path") or json.dumps(inp, ensure_ascii=False)[:800]
        name = self.cfg.agents[agent_key].name
        msg = await self._post(agent_key, f"{name}가 실행 허락을 요청합니다: {tool}", kind="approval",
                               approval={"id": aid, "tool": tool, "detail": detail, "description": desc,
                                         "status": "pending"})
        await self._typing(agent_key, True, f"{self.cfg.user.get('name', '사용자')} 승인 대기 중…")
        try:
            allowed = await fut
        finally:
            self.approvals.pop(aid, None)
        await self._update(msg, approval={**msg["approval"], "status": "allowed" if allowed else "denied"})
        await self._typing(agent_key, True, "승인됨 — 계속 진행" if allowed else "거부됨 — 다른 방법 찾는 중")
        if self.on_change:
            await self.on_change()
        return allowed

    def resolve_approval(self, aid: str, allow: bool) -> bool:
        fut = self.approvals.get(aid)
        if fut and not fut.done():
            fut.set_result(bool(allow))
            return True
        return False

    # ------------------------------------------------------------ 흐름
    def _drain_pending(self) -> None:
        if self.pending:
            self.requirements.extend(self.pending)
            self.pending.clear()
            self.pending_ids.clear()

    def _unseen(self, agent_key: str) -> list[str]:
        start = self.seen.get(agent_key, 0)
        self.seen[agent_key] = len(self.requirements)
        return self.requirements[start:]

    def _worker_unseen(self) -> list[str]:
        """실무자가 아직 받지 못한 지시(감독 차례에 대기열에서 꺼낸 것)."""
        return self.requirements[self.seen.get(self.cfg.worker, 0):]

    def _memo_prefix(self, agent_key: str) -> tuple[str, str]:
        """(이번 호출 앞에 붙일 프로젝트 메모 안내, 보낸 뒤 기록할 표시). 새 세션이면 예전에 보낸 메모는 없는 것으로 본다."""
        runner = self.runners[agent_key]
        prev = self.memo_sent.get(agent_key, "") if runner.started else ""
        project = self.projects.get(self.project)
        return memo_block(project, prev), memo_signature(project)

    async def set_project(self, pid: str | None) -> None:
        self.project = pid or None
        self._save_state()
        if self.on_change:
            await self.on_change()

    async def _call(self, agent_key: str, prompt: str, schema: bool, memo: bool = True):
        runner = self.runners[agent_key]
        prefix, sig = self._memo_prefix(agent_key) if memo else ("", None)
        prompt = prefix + prompt
        engine = self.cfg.agents[agent_key].engine
        self.current_agent = agent_key
        self.active.add(agent_key)
        await self._typing(agent_key, True)

        def activity(msg: str | None = None, tokens: int | None = None) -> None:
            asyncio.ensure_future(self._typing(agent_key, True, msg, tokens))
            if (runner.context or {}).get("used") != self._sent_context.get(agent_key):
                asyncio.ensure_future(self._context())

        def limits(lim: dict) -> None:  # 답변 도중에도 한도 게이지 갱신(바뀐 경우만)
            if lim and self._last_limits.get(engine) != lim and self.on_limits:
                self._last_limits[engine] = lim
                asyncio.ensure_future(self.on_limits(engine, lim))

        def meta(info: dict) -> None:
            if self.on_meta:
                asyncio.ensure_future(self.on_meta(info))

        try:
            res = await runner.run(prompt, str(self.workspace),
                                   schema_path=str(self.schema_path) if schema else None,
                                   activity=activity, approve=self._approver(agent_key), on_limits=limits, on_meta=meta)
            if res.limits:
                limits(res.limits)
            if res.ok and sig is not None:
                self.memo_sent[agent_key] = sig
            return res
        finally:
            self.active.discard(agent_key)
            self.current_agent = next(iter(self.active), None)
            await self._typing(agent_key, False)
            await self._context()
            self._save_state()

    def contexts(self) -> dict:
        """담당자별 세션 컨텍스트 크기 {used, window, updated}. 아직 답한 적 없으면 빠진다."""
        return {k: r.context for k, r in self.runners.items() if getattr(r, "context", None)}

    async def _context(self) -> None:
        ctx = self.contexts()
        self._sent_context = {k: v.get("used") for k, v in ctx.items()}
        await self.broadcast({"type": "context", "contexts": ctx})

    async def _maybe_compact_tip(self, agent_key: str, usage: dict | None) -> None:
        tokens = (usage or {}).get("total_tokens") or 0
        if self.compact_tip_shown or tokens < COMPACT_TIP_TOKENS or self.cfg.agents[agent_key].engine != "claude":
            return
        self.compact_tip_shown = True
        self._save_state()
        name = self.cfg.agents[agent_key].name
        await self._post("system", f"이번 답변에 {tokens / 1e6:.2f}M 토큰이 쓰였습니다. 대화가 길어지면 답할 때마다 앞 내용을 다시 읽어 토큰이 늘어납니다.\n"
                         f"· `/compact`를 보내면 {name} 세션을 요약·압축합니다(감독은 `@Clara /compact`).\n"
                         f"· 가벼운 작업은 전역 규칙을 끈 새 대화가 더 가볍습니다.")

    def _recent_context(self, exclude_last: int = 1) -> str:
        """빠른 질문·@멘션용: 최근 대화를 짧게 붙여, 세션에 없는 앞 맥락도 알게 한다(9/28: @Clara 가 앞 맥락을 몰랐음)."""
        msgs = [m for m in self.messages[:-exclude_last] if m.get("kind") != "approval" and m.get("text")]
        lines = []
        for m in msgs[-RECENT_CONTEXT_MESSAGES:]:
            who = self._name(m["sender"])
            text = m["text"].strip().replace("\n", " ")
            lines.append(f"- {who}: {text[:500]}{'…' if len(text) > 500 else ''}")
        return "\n".join(lines)

    async def _finish(self) -> None:
        self.round = 0
        self.current_agent = None
        for k in list(self.typing):
            await self._typing(k, False)
        self._save_state()

    async def _run_single(self, agent_key: str, text: str, raw: bool = False) -> None:
        """빠른 질문 / @멘션 / `/명령`: 한 사람만 답하고 검토는 하지 않는다."""
        user_name = self.cfg.user.get("name", "사용자")
        if raw:
            prompt = text
        else:
            recent = self._recent_context()
            prompt = (f"[최근 대화 — 참고용]\n{recent}\n\n" if recent else "") + \
                f"[{user_name}의 메시지 — 검토 절차 없이 바로 답해 주세요]\n{text}"
        try:
            res = await self._call(agent_key, prompt, schema=False, memo=not raw)  # /명령 앞에는 붙이지 않음
            if res.ok:
                await self._post(agent_key, res.text, usage=res.usage)
                await self._maybe_compact_tip(agent_key, res.usage)
            else:
                await self._post("system", f"{self.cfg.agents[agent_key].name} 호출 실패: {res.error}")
        except (asyncio.CancelledError, CancelledRun):
            await self._post("system", self._stop_text())
        except Exception as e:
            await self._post("system", f"오류: {type(e).__name__}: {e}")
        finally:
            self.editable = None
            await self._finish()
            leftover = list(self.pending)
            self.pending.clear()
            self.pending_ids.clear()
            self._busy = False
            await self._state()
            if leftover:
                self._start(self._run_task(leftover))
                await self._state()

    def _similar_tasks(self) -> list[dict]:
        """이번 과제 지시와 주제가 겹치는 지난 대화(반복 작업 추천용). 다른 대화의 제목 + 사용자 지시 앞부분을 본다."""
        convs = []
        for st in self.cfg.settings.log_dir.glob("*.state.json"):
            cid = st.name[: -len(".state.json")]
            if cid == self.conv_id:
                continue
            try:
                s = json.loads(st.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            msgs = LogStore(self.cfg.settings.log_dir, cid).load_messages()
            users = [m.get("text") or "" for m in msgs if m.get("sender") == "user" and not m.get("retracted")][:5]
            last = msgs[-1]["ts"] if msgs else s.get("created")
            convs.append({"id": cid, "title": s.get("title") or _title_from(msgs), "updated": last,
                          "text": " ".join([s.get("title") or "", *users])})
        return inbox.similar_tasks(" ".join([self.title or "", *self.requirements]), convs)

    async def _final_report(self, outcome: str, detail: str, **flag) -> None:
        """라운드가 모두 끝난 뒤: 진행 기록(시스템)을 남기고, 실무자가 사용자를 불러 최종 보고한다.
        10/1 사용자 요청 — 라운드 중에는 셋이 서로 보고하고, 사용자는 끝났을 때만 부른다. 완료 알림은 이 메시지 기준."""
        await self._post("system", detail)
        user_name = self.cfg.user.get("name", "사용자")
        similar = await asyncio.to_thread(self._similar_tasks)
        res = await self._call(self.cfg.worker, prompts.final_report_prompt(
            outcome, detail, user_name, similar=similar, memory_dir=self.cfg.settings.memory_dir), schema=False)
        if res.ok:
            await self._post(self.cfg.worker, res.text, usage=res.usage, final=True, **flag)
        else:  # 보고가 실패해도 결과는 알 수 있게
            await self._post("system", f"{self.cfg.agents[self.cfg.worker].name} 최종 보고 실패: {res.error}\n"
                                       f"결과: {outcome} (위 진행 기록 참고)", **flag)

    async def _run_task(self, instructions: list[str]) -> None:
        cfg = self.cfg
        user_name = cfg.user.get("name", "사용자")
        worker = cfg.agents[cfg.worker]
        # 새 과제: 이번 과제의 요구사항 목록을 새로 시작(세션 맥락은 유지)
        self.requirements = list(instructions)
        self.seen = {}
        feedback: list = []
        carry: list[str] = []  # 정상 종료 시 실무자에게 아직 전달 못 한 지시 → 새 과제로 이어감
        # 3단계 진행: 라운드 1은 계획만 주고받고, 초안은 라운드 2부터(감독이 없으면 계획 단계가 의미 없어 기존 방식)
        staged = cfg.settings.flow == "staged" and bool(self.reviewers)
        max_rounds = max(2, cfg.settings.max_rounds) if staged else cfg.settings.max_rounds
        try:
            for round_no in range(1, max_rounds + 1):
                self.round = round_no
                stage = ("plan" if round_no == 1 else "draft" if round_no == 2 else None) if staged else None
                await self._state()

                self._drain_pending()
                new_reqs = self._unseen(cfg.worker)
                before = await asyncio.to_thread(workspace_snapshot, self.workspace)
                res = await self._call(cfg.worker, prompts.worker_prompt(
                    round_no, self.requirements, new_reqs, feedback, user_name, stage=stage,
                    reviewers=[cfg.agents[k] for k in self.reviewers]), schema=False)
                if not res.ok:
                    await self._post("system", f"{worker.name} 호출 실패: {res.error}\n과제를 멈춥니다.")
                    return
                changed = changed_between(before, await asyncio.to_thread(workspace_snapshot, self.workspace))
                await self._post(cfg.worker, res.text, round=round_no, usage=res.usage, changed_files=changed,
                                 **({"stage": stage} if stage else {}))
                await self._maybe_compact_tip(cfg.worker, res.usage)
                worker_report = res.text
                if not self.reviewers:  # 이 대화에서 감독을 모두 뺀 경우
                    await self._post("system", "이 대화는 감독이 모두 빠져 있어 검토 없이 마쳤습니다.", done=True)
                    carry = self._worker_unseen()
                    return

                # 감독들은 동시에 검토한다(9/28: 차례로 하면 두 사람 시간이 합쳐져 라운드가 최대 17분)
                feedback, failed = [], []
                self._drain_pending()
                review_prompts = {rk: prompts.review_prompt(
                    round_no, self.requirements, self._unseen(rk), worker, worker_report,
                    str(self.workspace), user_name, worker_unseen=self._worker_unseen(),
                    changed_files=changed, stage=stage) for rk in self.reviewers}

                async def review_one(rk: str):
                    res = await self._call(rk, review_prompts[rk], schema=True)

                    def blind(r) -> bool:  # 바뀐 파일이 있는데 도구를 한 번도 안 쓰고 낸 승인(9/30: 보고문만 읽고 승인)
                        return bool(r.ok and changed and r.tool_calls == 0 and stage != "plan"
                                    and parse_review(r.structured, r.text).approved)

                    if blind(res):
                        res = await self._call(rk, prompts.NO_EVIDENCE_NUDGE, schema=True)
                        if blind(res):
                            res.ok = False
                            res.error = "결과물 파일을 열지 않고 낸 승인이라 무효로 처리했습니다"
                    return rk, res

                jobs = [asyncio.create_task(review_one(rk)) for rk in self.reviewers]
                try:
                    for fut in asyncio.as_completed(jobs):
                        rk, res = await fut
                        if not res.ok:  # 한 명이 멈춰도 과제 전체를 멈추지 않는다
                            failed.append(cfg.agents[rk].name)
                            await self._post("system", f"{cfg.agents[rk].name} 검토 실패: {res.error}\n"
                                                       f"나머지 감독의 판정으로 진행합니다.")
                            continue
                        review = parse_review(res.structured, res.text)
                        feedback.append((cfg.agents[rk], review))
                        await self._post(rk, review.to_text(), round=round_no, verdict=review.verdict,
                                         review=review.to_dict(), usage=res.usage,
                                         **({"stage": stage} if stage else {}))
                finally:
                    for j in jobs:
                        if not j.done():
                            j.cancel()
                if not feedback:
                    await self._post("system", "모든 감독의 검토가 실패해 과제를 멈춥니다. 다시 지시해 주시면 이어서 진행합니다.")
                    return

                # 실무자가 아직 받지 못한 사용자 지시(검토 중 끼어든 것)가 있으면 승인이 나도 한 라운드 더 돈다
                worker_behind = bool(self.pending or self._worker_unseen())
                if all(r.approved for _, r in feedback):
                    if stage == "plan" or (worker_behind and round_no < max_rounds):
                        continue  # 계획 승인은 끝이 아니라 초안으로 넘어가는 신호
                    names = ", ".join(a.name for a, _ in feedback)
                    note = f" ({', '.join(failed)} 검토 실패로 제외)" if failed else ""
                    await self._final_report("모두 승인", f"라운드 {round_no}에서 {names} 모두 APPROVE{note}.\n"
                                                          f"작업 폴더: {self.workspace}", done=True)
                    carry = self._worker_unseen()
                    return
                # 남은 필수 지적이 전부 '사용자 결정'이면 실무자에게 돌려도 해결되지 않는다 → 바로 넘김
                open_reviews = [r for _, r in feedback if not r.approved]
                if not worker_behind and all(r.user_blockers and not r.worker_blockers for r in open_reviews):
                    await self._final_report(f"{user_name} 결정 필요",
                                             prompts.user_decision_text(feedback, user_name, worker.name), escalation=True)
                    carry = self._worker_unseen()
                    return
                if round_no == max_rounds:
                    await self._final_report(f"{max_rounds}라운드 안에 미합의",
                                             prompts.escalation_text(max_rounds, feedback, user_name), escalation=True)
                    carry = self._worker_unseen()
                    return
        except (asyncio.CancelledError, CancelledRun):
            await self._post("system", self._stop_text(" 새 지시를 주시면 이어서 진행합니다."))
        except Exception as e:  # 예기치 못한 오류도 채팅에 보이게
            await self._post("system", f"오류로 과제를 멈췄습니다: {type(e).__name__}: {e}")
        finally:
            self.editable = None
            await self._finish()
            leftover = carry + list(self.pending)
            self.pending.clear()
            self.pending_ids.clear()
            self._busy = False
            await self._state()
            if leftover:
                # 마지막 검토 이후 들어온 지시는 새 과제로 이어서 처리
                self._start(self._run_task(leftover))
                await self._state()


class Manager:
    """대화 목록: logs/*.state.json 을 읽어 목록을 만들고, 열린 대화(Room)를 들고 있는다."""

    def __init__(self, cfg: AppConfig, broadcast: Broadcast, runner_factory: RunnerFactory = make_runner):
        self.cfg = cfg
        self._broadcast = broadcast
        self.runner_factory = runner_factory
        self.rooms: dict[str, Room] = {}
        cfg.settings.log_dir.mkdir(parents=True, exist_ok=True)
        self.recent_file = cfg.settings.log_dir / "recent_folders.json"
        self.limits = LimitStore(cfg.settings.log_dir / "usage_limits.json")
        self.projects = ProjectStore(cfg.settings.log_dir)
        # CLI 가 알려준 슬래시 명령 목록(첫 Claude 호출 때 받아 저장, 자동완성에 씀)
        self.slash_file = cfg.settings.log_dir / "slash_commands.json"
        try:
            self.slash_commands: list[str] = json.loads(self.slash_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.slash_commands = []

    def _room(self, **kw) -> Room:
        return Room(self.cfg, self._broadcast, self.runner_factory, on_change=self.broadcast_list,
                    on_limits=self.update_limits, on_meta=self.update_meta,
                    slash_commands=lambda: self.slash_commands, projects=self.projects, **kw)

    async def update_meta(self, info: dict) -> None:
        cmds = info.get("slash_commands")
        if cmds and cmds != self.slash_commands:
            self.slash_commands = list(cmds)
            try:
                self.slash_file.write_text(json.dumps(self.slash_commands, ensure_ascii=False), encoding="utf-8")
            except OSError:
                pass
            await self._broadcast({"type": "slash_commands", "commands": self.slash_commands})

    async def update_limits(self, engine: str, limits: dict) -> None:
        if self.limits.update(engine, limits):
            await self._broadcast({"type": "usage_limits", "limits": self.limits.data})

    def get(self, conv_id: str) -> Room | None:
        if conv_id in self.rooms:
            return self.rooms[conv_id]
        if not (self.cfg.settings.log_dir / f"{conv_id}.state.json").exists():
            return None
        room = self.rooms[conv_id] = self._room(conv_id=conv_id)
        return room

    def create(self, workspace: str | None = None, title: str | None = None, safe_mode: bool | None = None,
               exclude: list[str] | None = None, global_rules: dict | None = None, project: str | None = None) -> Room:
        proj = self.projects.get(project) if project else None
        if project and not proj:
            raise ValueError("없는 프로젝트입니다")
        if not workspace and proj and proj.get("folder") and Path(proj["folder"]).is_dir():
            workspace = proj["folder"]  # 프로젝트 안에서 연 새 대화는 프로젝트 기본 폴더에서
        if workspace:
            p = Path(workspace)
            if not p.is_dir():
                raise ValueError(f"폴더가 없습니다: {workspace}")
            self._remember_folder(str(p))
        room = self._room(workspace=workspace, title=title, safe_mode=safe_mode, exclude=exclude,
                          global_rules=global_rules, project=proj["id"] if proj else None)
        self.rooms[room.conv_id] = room
        return room

    def list(self) -> list[dict]:
        out = []
        for st in self.cfg.settings.log_dir.glob("*.state.json"):
            cid = st.name[: -len(".state.json")]
            if cid in self.rooms:
                out.append(self.rooms[cid].summary())
                continue
            try:
                s = json.loads(st.read_text(encoding="utf-8"))
            except ValueError:
                continue
            store = LogStore(self.cfg.settings.log_dir, cid)
            msgs = store.load_messages()
            last = next((m for m in reversed(msgs) if m.get("kind") != "approval"), None)
            out.append({"id": cid, "title": s.get("title") or _title_from(msgs) or "새 대화", "workspace": s.get("workspace", ""),
                        "updated": last["ts"] if last else s.get("created", st.stat().st_mtime),
                        "created": s.get("created", st.stat().st_mtime),
                        "last": last["text"][:80] if last else "", "running": False,
                        "safe_mode": s.get("safe_mode", False), "rules_off": _rules_off(self.cfg, s),
                        "archived": bool(s.get("archived", False)), "project": s.get("project"),
                        "approvals": 0})
        out.sort(key=lambda x: x["updated"], reverse=True)
        return out

    # ------------------------------------------------------------ 전체 검색 · 결정 대기함 (v11)
    def _messages_of(self, conv_id: str) -> list[dict]:
        if conv_id in self.rooms:
            return self.rooms[conv_id].messages
        return LogStore(self.cfg.settings.log_dir, conv_id).load_messages()

    def search(self, q: str, project: str | None = None) -> list[dict]:
        """모든 대화(보관 포함)의 메시지 내용에서 찾는다. 원본은 logs/*.jsonl(같은 내용인 .md 는 보지 않음).
        일치한 대화는 개수 제한 없이 모두 돌려준다(대화당 문맥 조각만 최근 3건)."""
        q = (q or "").strip()
        if len(q) < 2:
            return []
        out = []
        for c in self.list():
            if project and c.get("project") != project:
                continue
            total, hits = inbox.search_messages(self._messages_of(c["id"]), q)
            if total:
                out.append({"id": c["id"], "title": c["title"], "archived": c.get("archived", False),
                            "total": total, "hits": hits, "latest": hits[0]["ts"] if hits else c["updated"]})
        out.sort(key=lambda x: x["latest"] or 0, reverse=True)
        return out

    def inbox(self, project: str | None = None) -> list[dict]:
        """사용자 판단을 기다리는 대화. 보관한 대화·진행 중인 과제는 빼고, 승인 카드는 살아 있는 것만 센다.
        (서버를 다시 켜면 예전 승인 카드는 만료된다 — 로그 원본에 pending 으로 남아 있어도 세지 않음)"""
        user_name = self.cfg.user.get("name", "사용자")
        items = []
        for c in self.list():
            if c.get("archived") or (project and c.get("project") != project):
                continue
            room = self.rooms.get(c["id"])
            msgs = self._messages_of(c["id"])
            if room:
                for aid, fut in room.approvals.items():
                    if fut.done():
                        continue
                    card = next((m for m in msgs if m.get("kind") == "approval"
                                 and (m.get("approval") or {}).get("id") == aid), None)
                    if card:
                        items.append({"id": c["id"], "title": c["title"], "reason": "approval", "msg_id": card["id"],
                                      "ts": card.get("ts"), "sender": card.get("sender"),
                                      "summary": (card["approval"].get("detail") or "")[:200]})
            d = inbox.pending_decision(msgs, user_name, running=bool(room and room.running))
            if d:
                items.append({"id": c["id"], "title": c["title"], **d})
        items.sort(key=lambda x: x.get("ts") or 0, reverse=True)
        return items

    # ------------------------------------------------------------ 프로젝트 (v12)
    def projects_list(self, convs: list[dict] | None = None) -> list[dict]:
        counts: dict[str, int] = {}
        for c in convs if convs is not None else self.list():
            if c.get("project"):
                counts[c["project"]] = counts.get(c["project"], 0) + 1
        return [{**p, "count": counts.get(p["id"], 0)} for p in self.projects.all()]

    def _write_state_field(self, conv_id: str, **fields) -> None:
        """닫혀 있는 대화의 state.json 에서 지정한 값만 바꾸고 나머지는 그대로 둔다."""
        path = self.cfg.settings.log_dir / f"{conv_id}.state.json"
        s = json.loads(path.read_text(encoding="utf-8"))
        s.update(fields)
        path.write_text(json.dumps(s, ensure_ascii=False, indent=1), encoding="utf-8")

    async def set_conv_project(self, conv_id: str, pid: str | None, notify: bool = True) -> None:
        if pid and not self.projects.get(pid):
            raise ValueError("없는 프로젝트입니다")
        if conv_id in self.rooms:
            await self.rooms[conv_id].set_project(pid)
        elif (self.cfg.settings.log_dir / f"{conv_id}.state.json").exists():
            self._write_state_field(conv_id, project=pid or None)
        else:
            raise KeyError(conv_id)
        if notify:
            await self.broadcast_list()

    async def delete_project(self, pid: str) -> int:
        """프로젝트만 지운다. 속한 대화는 지우지 않고 '기타'로 옮긴다(다음 호출 때 담당자에게 메모 해제를 알림)."""
        moved = [c["id"] for c in self.list() if c.get("project") == pid]
        for cid in moved:
            await self.set_conv_project(cid, None, notify=False)
        self.projects.delete(pid)
        await self.broadcast_list()
        return len(moved)

    def suggest_projects(self) -> list[dict]:
        """프로젝트에 아직 안 든 대화를 작업 폴더 기준으로 묶은 초안(적용은 사용자가 확인한 뒤)."""
        convs = [c for c in self.list() if not c.get("project") and not c.get("archived")]
        return suggest_groups(convs, skip_roots=[str(self.cfg.settings.workspace_root)])

    async def apply_suggestion(self, groups: list[dict]) -> list[dict]:
        """사용자가 확인한 묶음을 적용. 고치기 전에 해당 대화의 state.json 을 logs/backups 에 복사해 둔다."""
        import shutil
        log_dir = self.cfg.settings.log_dir
        backup = log_dir / "backups"
        backup.mkdir(exist_ok=True)
        created = []
        for g in groups:
            ids = [str(x) for x in g.get("conv_ids") or []]
            if not ids:
                continue
            for cid in ids:
                src = log_dir / f"{cid}.state.json"
                if src.exists():
                    stamp = datetime.fromtimestamp(src.stat().st_mtime).strftime("%Y%m%d_%H%M%S")
                    dst = backup / f"backup_{stamp}_프로젝트적용_{cid}.state.json"
                    if not dst.exists():
                        shutil.copy2(src, dst)
            p = self.projects.create(g.get("name") or "새 프로젝트", g.get("folder") or "", g.get("memo") or "")
            for cid in ids:
                await self.set_conv_project(cid, p["id"], notify=False)
            created.append(p)
        await self.broadcast_list()
        return created

    async def broadcast_list(self) -> None:
        convs = self.list()
        await self._broadcast({"type": "conversations", "conversations": convs, "projects": self.projects_list(convs)})

    def reload(self, cfg: AppConfig) -> None:
        self.cfg = cfg
        for r in self.rooms.values():
            r.apply_config(cfg)

    def recent_folders(self) -> list[str]:
        try:
            return json.loads(self.recent_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []

    def _remember_folder(self, path: str) -> None:
        items = [path] + [p for p in self.recent_folders() if p != path]
        self.recent_file.write_text(json.dumps(items[:10], ensure_ascii=False), encoding="utf-8")

    async def stop_all(self) -> None:
        for r in self.rooms.values():
            await r.stop()
