"""GitHub 에 올리기 전 기밀 검사(반드시 통과해야 푸시). 10/1 사고: 테스트 예시에 비상장 고객사 실명이 들어간 채 공개됨.

검사어는 저장소에 넣지 않는다 → private/sensitive_terms.txt (git 제외 폴더, 한 줄에 하나, # 은 주석).
여기에 작업공간의 고객사명·거래처명·개인 경로·이메일을 적어 둔다. 대소문자 무시, 부분 일치.

사용:
  python tools/secret_scan.py                 # 작업 트리(추적 + 새 파일, .gitignore 제외)
  python tools/secret_scan.py --rev main      # 그 브랜치로 올라갈 모든 커밋의 모든 파일 + 커밋 메시지
종료 코드: 0 = 통과, 1 = 발견(목록 출력), 2 = 검사어 파일 없음
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TERMS_FILE = ROOT / "private" / "sensitive_terms.txt"
BINARY = re.compile(r"\.(png|jpe?g|gif|ico|webp|pdf|xlsx?|docx?|pptx?|zip|bundle)$", re.I)


def git(*a: str) -> str:
    return subprocess.run(["git", "-C", str(ROOT), *a], capture_output=True, text=True,
                          encoding="utf-8", errors="replace").stdout


def load_terms() -> list[str]:
    if not TERMS_FILE.exists():
        return []
    return [t.strip() for t in TERMS_FILE.read_text(encoding="utf-8").splitlines()
            if t.strip() and not t.strip().startswith("#")]


def scan_text(label: str, text: str, pat: re.Pattern, out: list) -> None:
    for i, line in enumerate(text.splitlines(), 1):
        m = pat.search(line)
        if m:
            out.append(f"{label}:{i} [{m.group(0)}] {line.strip()[:100]}")


def main() -> int:
    terms = load_terms()
    if not terms:
        print(f"검사어 파일이 없거나 비었습니다: {TERMS_FILE}")
        return 2
    pat = re.compile("|".join(re.escape(t) for t in terms), re.I)
    found: list[str] = []
    if "--rev" in sys.argv:
        rev = sys.argv[sys.argv.index("--rev") + 1]
        commits = git("rev-list", rev).split()
        seen: set[str] = set()
        for c in commits:
            msg = git("log", "-1", "--format=%an <%ae>%n%B", c)
            scan_text(f"커밋메시지 {c[:7]}", msg, pat, found)
            for line in git("ls-tree", "-r", c).splitlines():
                meta, path = line.split("\t", 1)
                blob = meta.split()[2]
                if pat.search(path):
                    found.append(f"파일이름 {c[:7]} {path}")
                if blob in seen or BINARY.search(path):
                    continue
                seen.add(blob)
                scan_text(f"{c[:7]} {path}", git("cat-file", "-p", blob), pat, found)
        print(f"검사: {rev} 의 커밋 {len(commits)}개, 파일 내용 {len(seen)}개, 검사어 {len(terms)}개")
    else:
        files = git("ls-files", "--cached", "--others", "--exclude-standard").splitlines()
        n = 0
        for f in files:
            if pat.search(f):
                found.append(f"파일이름 {f}")
            if BINARY.search(f) or f.startswith("tools/secret_scan"):
                continue
            try:
                text = (ROOT / f).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            n += 1
            scan_text(f, text, pat, found)
        print(f"검사: 작업 트리 파일 {n}개, 검사어 {len(terms)}개")
    if found:
        print(f"발견 {len(found)}건 — 푸시하지 마세요:")
        print("\n".join(found))
        return 1
    print("통과(0건)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
