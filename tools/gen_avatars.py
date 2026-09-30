"""Codex 내장 image_gen(ChatGPT 구독, API 키 불필요)으로 가상 인물 프로필 사진을 만든다.

사용: python tools/gen_avatars.py jake "30대 남성 개발자"   (key, 인물 설명)
결과: config/avatars/<key>.png  (기존 파일이 있으면 <key>_v2.png ... 로 저장)
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import find_codex_exe  # noqa: E402

AVATAR_DIR = ROOT / "config" / "avatars"


def next_name(key: str) -> str:
    name, n = f"{key}.png", 2
    while (AVATAR_DIR / name).exists():
        name, n = f"{key}_v{n}.png", n + 1
    return name


def generate(key: str, description: str, timeout: int = 900) -> tuple[bool, str]:
    AVATAR_DIR.mkdir(parents=True, exist_ok=True)
    out = next_name(key)
    prompt = (
        "이미지 생성 도구(image_gen)로 프로필 사진 1장을 만들어 주세요.\n"
        f"- 인물: {description}. 실존 인물이 아닌 가상의 인물입니다.\n"
        "- 스타일: 사진처럼 사실적인(photorealistic) 인물 사진. 밝은 사무실 배경을 흐리게, 자연광, 어깨 위 상반신, 정면을 보며 옅은 미소.\n"
        "- 정사각형(1:1), 얼굴이 가운데. 글자·워터마크 없음.\n"
        f"- 완성된 이미지를 현재 작업 폴더에 `{out}` 파일명으로 복사해 주세요. 다른 파일은 만들지 마세요.\n"
        "- 마지막 답변은 저장한 파일명 한 줄만."
    )
    cmd = [find_codex_exe(), "exec", "--json", "--skip-git-repo-check", "-s", "workspace-write",
           "-c", 'windows.sandbox="unelevated"', "-C", str(AVATAR_DIR), "-"]
    p = subprocess.run(cmd, input=prompt, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=timeout, cwd=AVATAR_DIR,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    last = ""
    for line in p.stdout.splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        item = ev.get("item") or {}
        if ev.get("type") == "item.completed" and item.get("type") == "agent_message":
            last = item.get("text", "")
    if (AVATAR_DIR / out).exists():
        return True, out
    return False, last or p.stderr[-500:]


if __name__ == "__main__":
    ok, info = generate(sys.argv[1], sys.argv[2])
    print(("OK " if ok else "FAIL ") + info)
    sys.exit(0 if ok else 1)
