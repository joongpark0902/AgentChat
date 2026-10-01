"""agents.yaml / settings.yaml 로딩."""
from __future__ import annotations

import glob
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"


@dataclass
class AgentConfig:
    key: str
    name: str
    title: str
    engine: str  # claude | codex
    access: str  # write | readonly | test
    prompt: str
    model: str | None = None
    effort: str | None = None
    avatar: str | None = None
    color: str = "#8E8E93"
    checklist: list[str] = field(default_factory=list)
    not_my_job: str = ""

    user_name: str = "사용자"  # load_config 에서 agents.yaml 의 user.name 으로 채움
    memory_dir: str = ""       # load_config 에서 settings 의 memory_dir 로 채움

    def system_prompt(self, workspace: str) -> str:
        checklist = "\n".join(f"  {i}. {c}" for i, c in enumerate(self.checklist, 1))
        text = self.prompt
        for k, v in {"{name}": self.name, "{workspace}": workspace, "{user}": self.user_name,
                     "{memory_dir}": self.memory_dir, "{dump_tool}": str(ROOT / "tools" / "dump_office.py"),
                     "{checklist}": checklist, "{not_my_job}": self.not_my_job}.items():
            text = text.replace(k, v)
        return text.strip()

    def public(self) -> dict:
        return {"key": self.key, "name": self.name, "title": self.title, "engine": self.engine,
                "avatar": f"/avatars/{self.avatar}" if self.avatar else None, "color": self.color,
                "model": self.model, "effort": self.effort}


@dataclass
class Settings:
    host: str
    port: int
    max_rounds: int
    call_timeout_sec: int
    workspace_root: Path
    log_dir: Path
    python_dir: str
    claude_exe: str
    claude_safe_mode: bool
    codex_exe: str
    codex_sandbox: str
    codex_windows_sandbox: str | None
    stt_engine: str = "local"          # local: PC 안 Whisper / browser: Chrome 음성 인식(Google 전송)
    stt_model: str = "small"           # small: 정확 / base: 빠름
    stt_vocab: list = None             # 인식 힌트 용어(initial_prompt)
    memory_dir: str = ""               # 에이전트가 쓰는 메모리 폴더(터미널과 같은 곳)
    codex_approvals_reviewer: str | None = "auto_review"   # codex MCP 호출 자동 검토
    readonly_mcp_allow: list = None    # 감독(읽기 전용)이 묻지 않고 쓰는 MCP 서버
    remote_port: int = 8790            # 폰 전용 포트(127.0.0.1). Tailscale serve 가 여기로 넘겨주고, 항상 로그인 필요
    push_subject: str = ""             # 웹 푸시 VAPID 연락처(비우면 기본값)
    claude_rules: dict = None          # 담당자별 전역 규칙 {key: True/False}. 없는 담당자는 safe_mode 를 따름
    flow: str = "classic"              # classic: 라운드마다 완성본 / staged: 1 방향(계획) → 2 초안 → 3 마무리

    def rules_on(self, key: str) -> bool:
        return bool((self.claude_rules or {}).get(key, not self.claude_safe_mode))


@dataclass
class AppConfig:
    room_name: str
    user: dict
    worker: str
    reviewers: list[str]
    agents: dict[str, AgentConfig]
    settings: Settings

    def public(self) -> dict:
        user = dict(self.user)
        if user.get("avatar"):
            user["avatar"] = f"/avatars/{user['avatar']}"
        return {"room_name": self.room_name, "user": user, "worker": self.worker,
                "stt": {"engine": self.settings.stt_engine, "model": self.settings.stt_model},
                "reviewers": self.reviewers, "max_rounds": self.settings.max_rounds, "flow": self.settings.flow,
                "agents": {k: a.public() for k, a in self.agents.items()}}


# 음성 인식 힌트 기본 용어(2026-09-28 실측: 힌트 없으면 "손익"→"손이", "영업이익"→"영업이 입"으로 잘못 인식)
DEFAULT_STT_VOCAB = ["손익", "영업이익", "매출", "원가", "판관비", "재무제표", "감사조서", "주석", "엑셀", "풋팅"]


def default_memory_dir() -> str:
    """터미널에서 홈 폴더로 Claude Code 를 켰을 때의 메모리 폴더(~/.claude/projects/<홈 경로 인코딩>/memory).
    9/28 실측: 작업 폴더마다 메모리가 따로라, 프로젝트 폴더에서 저장하면 터미널에서 안 보였음."""
    home = str(Path.home())
    enc = "".join(ch if ch.isascii() and ch.isalnum() else "-" for ch in home)
    return str(Path.home() / ".claude" / "projects" / enc / "memory")


DEFAULT_READONLY_MCP = ["claude_ai_DART_Audit_MCP", "moleg", "verifier"]


def find_codex_exe() -> str:
    """Codex 앱 업데이트마다 bin\\<hash> 폴더가 바뀌므로 최신 codex.exe 를 찾는다. 없으면 PATH 의 codex(npm 설치본)."""
    import shutil
    pattern = os.path.expandvars(r"%LOCALAPPDATA%\OpenAI\Codex\bin\*\codex.exe")
    found = sorted(glob.glob(pattern), key=os.path.getmtime, reverse=True)
    if found:
        return found[0]
    on_path = shutil.which("codex")
    if on_path:
        return on_path
    raise FileNotFoundError("Codex CLI 를 찾지 못했습니다(Codex 앱 또는 codex 명령). Codex 를 쓰지 않으려면 "
                            "대화 ⋯ 메뉴에서 그 담당자를 빼거나 설정에서 engine 을 claude 로 바꾸세요.")


CONFIG_FILES = ("agents.yaml", "settings.yaml")


def ensure_config(config_dir: Path) -> None:
    """처음 실행: 개인 설정 파일(git 에 올리지 않음)이 없으면 배포용 예시(*.example.yaml)를 복사해 만든다."""
    import shutil
    for name in CONFIG_FILES:
        dst = config_dir / name
        src = ROOT / "config" / name.replace(".yaml", ".example.yaml")
        if not dst.exists() and src.exists():
            config_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy(src, dst)


def _resolve(p: str) -> Path:
    path = Path(p)
    return path if path.is_absolute() else ROOT / path


def load_config(config_dir: Path | None = None) -> AppConfig:
    config_dir = config_dir or CONFIG_DIR
    ensure_config(config_dir)
    with open(config_dir / "agents.yaml", encoding="utf-8") as f:
        a = yaml.safe_load(f)
    with open(config_dir / "settings.yaml", encoding="utf-8") as f:
        s = yaml.safe_load(f)

    user = a.get("user") or {"name": "사용자"}
    agents = {}
    for key, v in a["agents"].items():
        agents[key] = AgentConfig(
            key=key, name=v["name"], title=v.get("title", ""), engine=v["engine"],
            access=v.get("access", "readonly"), prompt=v["prompt"], model=v.get("model"),
            effort=v.get("effort"), avatar=v.get("avatar"), color=v.get("color", "#8E8E93"),
            checklist=v.get("checklist") or [], not_my_job=v.get("not_my_job", ""),
            user_name=user.get("name", "사용자"))
        if agents[key].engine not in ("claude", "codex"):
            raise ValueError(f"{key}: engine 은 claude 또는 codex 여야 합니다")

    for k in [a["worker"], *a["reviewers"]]:
        if k not in agents:
            raise ValueError(f"agents 에 없는 참여자: {k}")

    codex = s.get("codex", {})
    claude = s.get("claude", {})
    settings = Settings(
        host=s.get("host", "127.0.0.1"), port=int(s.get("port", 8780)),
        max_rounds=int(s.get("max_rounds", 3)), call_timeout_sec=int(s.get("call_timeout_sec", 1200)),
        workspace_root=_resolve(s.get("workspace_root", "workspace")),
        log_dir=_resolve(s.get("log_dir", "logs")),
        # 비워 두면 이 서버를 돌리는 python 의 폴더(에이전트가 `python` 으로 Store 별칭이 아닌 실제 python 을 쓰게)
        python_dir=s.get("python_dir") or str(Path(sys.executable).parent),
        claude_exe=claude.get("exe", "claude"), claude_safe_mode=bool(claude.get("safe_mode", False)),
        codex_exe=codex.get("exe", "auto"), codex_sandbox=codex.get("sandbox", "workspace-write"),
        codex_windows_sandbox=codex.get("windows_sandbox"),
        stt_engine=(s.get("stt") or {}).get("engine", "local"),
        stt_model=(s.get("stt") or {}).get("model", "small"),
        stt_vocab=list((s.get("stt") or {}).get("vocabulary") or DEFAULT_STT_VOCAB),
        memory_dir=s.get("memory_dir") or default_memory_dir(),
        codex_approvals_reviewer=codex.get("approvals_reviewer", "auto_review"),
        readonly_mcp_allow=list(claude.get("readonly_mcp_allow") or DEFAULT_READONLY_MCP),
        remote_port=int(s.get("remote_port", 8790)), push_subject=str(s.get("push_subject") or ""),
        claude_rules={str(k): bool(v) for k, v in (claude.get("global_rules") or {}).items()},
        flow="staged" if s.get("flow") == "staged" else "classic")

    for ag in agents.values():
        ag.memory_dir = settings.memory_dir
    return AppConfig(room_name=a.get("room_name", "작업방"), user=user,
                     worker=a["worker"], reviewers=list(a["reviewers"]), agents=agents, settings=settings)


# ---------------------------------------------------------------- 화면(설정 창)에서 편집
EDITABLE_AGENT_FIELDS = ("name", "title", "color", "avatar", "model", "effort", "prompt", "checklist", "not_my_job")


def _read_yaml(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


class _LiteralDumper(yaml.SafeDumper):
    pass


def _str_presenter(dumper, data):
    if "\n" in data:  # 여러 줄 프롬프트는 | 블록으로 저장해 사람이 읽기 쉽게
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")
    return dumper.represent_scalar("tag:yaml.org,2002:str", data)


_LiteralDumper.add_representer(str, _str_presenter)

_HEADER = "# 화면의 ⚙설정 창에서 저장하면 이 파일이 다시 써집니다(이전 파일은 config/backups 에 백업).\n"


def _dump_yaml(data: dict) -> str:
    return _HEADER + yaml.dump(data, Dumper=_LiteralDumper, allow_unicode=True, sort_keys=False, width=1000)


def _as_text(item) -> str:
    """9/28 버그: 체크리스트 한 줄에 '예: …'가 있으면 YAML 이 표(dict)로 읽어 화면에 [object Object]로 보였음."""
    if isinstance(item, dict):
        return "; ".join(f"{k}: {v}" for k, v in item.items())
    return str(item)


def editable_config(config_dir: Path | None = None) -> dict:
    config_dir = config_dir or CONFIG_DIR
    ensure_config(config_dir)
    a = _read_yaml(config_dir / "agents.yaml")
    s = _read_yaml(config_dir / "settings.yaml")
    agents = {}
    for key, v in a["agents"].items():
        agents[key] = {f: v.get(f) for f in EDITABLE_AGENT_FIELDS}
        agents[key]["checklist"] = [_as_text(c) for c in (v.get("checklist") or [])]
        agents[key]["engine"] = v.get("engine")
    claude = s.get("claude") or {}
    default_on = not bool(claude.get("safe_mode", False))
    per_agent = claude.get("global_rules") or {}
    # Codex 는 이 스위치와 무관하게 AGENTS.md 를 읽으므로 Claude 담당자만 둔다
    rules_agents = {k: bool(per_agent.get(k, default_on)) for k, v in a["agents"].items() if v.get("engine") == "claude"}
    return {
        "room_name": a.get("room_name", "작업방"),
        "user_name": (a.get("user") or {}).get("name", "사용자"),
        "user_avatar": (a.get("user") or {}).get("avatar"),
        "agents": agents,
        "settings": {"max_rounds": s.get("max_rounds", 3), "call_timeout_sec": s.get("call_timeout_sec", 1200),
                     "global_rules": default_on, "global_rules_agents": rules_agents,
                     "flow": "staged" if s.get("flow") == "staged" else "classic",
                     "stt_engine": (s.get("stt") or {}).get("engine", "local"),
                     "stt_model": (s.get("stt") or {}).get("model", "small"),
                     "stt_vocab": list((s.get("stt") or {}).get("vocabulary") or DEFAULT_STT_VOCAB)},
    }


def save_editable_config(patch: dict, config_dir: Path | None = None) -> str:
    """설정 창 저장: 검증 → 기존 파일 백업 → 저장. 백업 폴더 경로를 돌려준다."""
    config_dir = config_dir or CONFIG_DIR
    import shutil
    import tempfile
    from datetime import datetime

    a = _read_yaml(config_dir / "agents.yaml")
    s = _read_yaml(config_dir / "settings.yaml")
    if "room_name" in patch:
        a["room_name"] = str(patch["room_name"]).strip() or "작업방"
    if "user_name" in patch:
        a.setdefault("user", {})["name"] = str(patch["user_name"]).strip() or "사용자"
    if "user_avatar" in patch:
        a.setdefault("user", {})["avatar"] = patch["user_avatar"] or None
    for key, fields in (patch.get("agents") or {}).items():
        if key not in a["agents"]:
            raise ValueError(f"없는 참여자: {key}")
        for f, v in fields.items():
            if f not in EDITABLE_AGENT_FIELDS:
                continue
            if f == "checklist":
                v = [str(x).strip() for x in (v or []) if str(x).strip()]
            elif isinstance(v, str):
                v = v.strip() if f != "prompt" else v.rstrip() + "\n"
                if f in ("model", "effort", "avatar") and not v:
                    v = None
            a["agents"][key][f] = v
    st = patch.get("settings") or {}
    if "max_rounds" in st:
        s["max_rounds"] = max(1, min(10, int(st["max_rounds"])))
    if "call_timeout_sec" in st:
        s["call_timeout_sec"] = max(60, int(st["call_timeout_sec"]))
    if "stt_engine" in st:
        s.setdefault("stt", {})["engine"] = "browser" if st["stt_engine"] == "browser" else "local"
    if "stt_model" in st:
        s.setdefault("stt", {})["model"] = "base" if st["stt_model"] == "base" else "small"
    if "stt_vocab" in st:
        s.setdefault("stt", {})["vocabulary"] = [str(x).strip() for x in st["stt_vocab"] or [] if str(x).strip()]
    if "global_rules" in st:  # 예전 방식(전원 일괄): 담당자별 값을 지우고 한 값으로
        s.setdefault("claude", {})["safe_mode"] = not bool(st["global_rules"])
        s["claude"].pop("global_rules", None)
    if isinstance(st.get("global_rules_agents"), dict):
        rules = {str(k): bool(v) for k, v in st["global_rules_agents"].items() if k in a["agents"]}
        s.setdefault("claude", {})["global_rules"] = rules
        if rules:  # 목록에 없는 담당자(새로 추가 등)의 기본값: 하나라도 켜져 있으면 켬
            s["claude"]["safe_mode"] = not any(rules.values())
    if "flow" in st:
        s["flow"] = "staged" if st["flow"] == "staged" else "classic"

    agents_text, settings_text = _dump_yaml(a), _dump_yaml(s)
    with tempfile.TemporaryDirectory() as tmp:  # 저장 전에 실제로 읽히는지 검증
        (Path(tmp) / "agents.yaml").write_text(agents_text, encoding="utf-8")
        (Path(tmp) / "settings.yaml").write_text(settings_text, encoding="utf-8")
        load_config(Path(tmp))

    backup_dir = config_dir / "backups"
    backup_dir.mkdir(exist_ok=True)
    for name in ("agents.yaml", "settings.yaml"):
        src = config_dir / name
        mtime = datetime.fromtimestamp(src.stat().st_mtime).strftime("%Y%m%d_%H%M%S")
        dst = backup_dir / f"backup_{mtime}_설정변경_{name}"
        if not dst.exists():
            shutil.copy2(src, dst)
    (config_dir / "agents.yaml").write_text(agents_text, encoding="utf-8")
    (config_dir / "settings.yaml").write_text(settings_text, encoding="utf-8")
    return str(backup_dir)
