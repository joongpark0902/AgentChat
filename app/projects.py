"""프로젝트(대화 묶음): 이름·기본 작업 폴더·공통 메모. logs/projects.json 에 저장(git 에 올리지 않는 폴더).

대화가 어느 프로젝트에 속하는지는 각 대화의 state.json 의 "project" 값이 원본이다(대화 하나 = 프로젝트 하나).
10/1 사용자 요청: 같은 고객사 대화들을 폴더처럼 묶고, 메모를 담당자 셋에게 공통 지시로 전달한다.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
import uuid
from pathlib import Path, PureWindowsPath


class ProjectStore:
    def __init__(self, log_dir: Path):
        self.path = Path(log_dir) / "projects.json"

    def all(self) -> list[dict]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return [p for p in data.get("projects", []) if isinstance(p, dict) and p.get("id")]

    def get(self, pid: str | None) -> dict | None:
        return next((p for p in self.all() if p["id"] == pid), None) if pid else None

    def _save(self, projects: list[dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")  # 쓰다 끊겨도 원본이 깨지지 않게
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"projects": projects}, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)

    def create(self, name: str, folder: str = "", memo: str = "") -> dict:
        name = (name or "").strip()
        if not name:
            raise ValueError("프로젝트 이름을 적어 주세요")
        p = {"id": "p" + uuid.uuid4().hex[:8], "name": name, "folder": (folder or "").strip(),
             "memo": (memo or "").strip(), "created": time.time()}
        self._save(self.all() + [p])
        return p

    def update(self, pid: str, fields: dict) -> dict:
        projects = self.all()
        p = next((x for x in projects if x["id"] == pid), None)
        if not p:
            raise KeyError(pid)
        for k in ("name", "folder", "memo"):
            if k in fields:
                p[k] = str(fields[k] or "").strip()
        if not p["name"]:
            raise ValueError("프로젝트 이름을 적어 주세요")
        self._save(projects)
        return p

    def delete(self, pid: str) -> bool:
        projects = self.all()
        left = [p for p in projects if p["id"] != pid]
        if len(left) == len(projects):
            return False
        self._save(left)
        return True


def memo_signature(project: dict | None) -> str:
    """담당자에게 마지막으로 보낸 메모를 가리키는 값. 프로젝트 id 를 넣어, 같은 문구의 다른 프로젝트로 옮겨도 다시 보낸다.
    프로젝트가 없거나 메모가 비면 '' (= 적용 중인 메모 없음)."""
    if not project or not (project.get("memo") or "").strip():
        return ""
    digest = hashlib.sha1(project["memo"].strip().encode("utf-8")).hexdigest()[:12]
    return f"{project['id']}:{digest}"


def memo_block(project: dict | None, prev_sig: str) -> str:
    """이번 호출 앞에 붙일 메모 안내. 바뀐 게 없으면 ''."""
    sig = memo_signature(project)
    if sig == prev_sig:
        return ""
    if sig:
        return (f"[프로젝트 메모 — {project['name']}] 이 대화가 속한 프로젝트의 공통 지시입니다. "
                f"이전에 받은 프로젝트 메모가 있으면 이것으로 바꿔 적용하세요.\n{project['memo'].strip()}\n\n")
    return "[프로젝트 메모 해제] 이전에 받은 프로젝트 메모는 이제 이 대화에 적용되지 않습니다.\n\n"


# ---------------------------------------------------------------- 폴더 기준 묶기 제안
def _parts(path: str) -> tuple[str, ...]:
    return tuple(x.lower() for x in PureWindowsPath(path).parts)


def _related(a: tuple, b: tuple) -> bool:
    """바로 위·아래 폴더이거나, 끝의 두 단계(예: '…\\fdd\\예시사')가 같으면 같은 일로 본다.
    끝 한 단계만 같은 것('FY2025', '새 폴더')은 서로 다른 일일 수 있어 묶지 않는다.
    상위 폴더를 1단계로 제한하는 이유: 작업공간 맨 위 폴더(예: …\\claude)에서 연 대화가 그 아래 모든 대화를 끌어모으지 않게."""
    if not a or not b:
        return False
    n = min(len(a), len(b))
    if a[:n] == b[:n] and abs(len(a) - len(b)) <= 1:
        return True
    return len(a) >= 2 and len(b) >= 2 and a[-2:] == b[-2:]


def suggest_groups(convs: list[dict], skip_roots: list[str] = (), min_size: int = 2) -> list[dict]:
    """convs: [{id, title, workspace}] (프로젝트에 아직 안 든 대화). 작업 폴더가 관련된 대화끼리 묶은 초안.
    skip_roots 아래 폴더(앱이 대화마다 자동으로 만든 새 폴더)는 서로 무관하므로 뺀다."""
    skips = [_parts(r) for r in skip_roots if r]
    items = []
    for c in convs:
        p = _parts(c.get("workspace") or "")
        if not p or any(p[:len(s)] == s for s in skips):
            continue
        items.append((c, p))
    groups: list[list] = []
    for c, p in items:  # 관련된 묶음이 여럿이면 하나로 합친다
        hit = [g for g in groups if any(_related(p, q) for _, q in g)]
        merged = [(c, p)]
        for g in hit:
            merged += g
            groups.remove(g)
        groups.append(merged)
    out = []
    for g in groups:
        if len(g) < min_size:
            continue
        counts: dict[str, int] = {}
        for c, _ in g:
            counts[c["workspace"]] = counts.get(c["workspace"], 0) + 1
        folder = max(counts, key=lambda w: (counts[w], -len(w)))  # 가장 많이 쓴 폴더(같으면 짧은 것)
        out.append({"name": PureWindowsPath(folder).name or folder, "folder": folder,
                    "convs": [{"id": c["id"], "title": c.get("title", ""), "workspace": c["workspace"]} for c, _ in g]})
    out.sort(key=lambda x: len(x["convs"]), reverse=True)
    return out
