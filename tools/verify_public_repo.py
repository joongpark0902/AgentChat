r"""공개 저장소 검증: GitHub 에서 새로 내려받은 사본으로, 전체 이력의 모든 파일 버전(지워진 것 포함)을 검사한다.
- 글 파일: 검사어(private/sensitive_terms.txt)·개인정보 패턴(이메일·전화·주민·사업자번호·법인 표기·PC 사용자 경로)
- 그림·그 밖의 바이너리: 고유 내용(blob)마다 크기·종류·메타데이터(EXIF·PNG 글 조각·JPEG 주석)를 적고, 그림은 눈 확인용으로 꺼내 둔다
- 올리면 안 되는 경로(개인 설정·로그·첨부·실제 인물 사진 등)가 이력 어디에라도 있었는지
- 커밋 작성자·커미터 이메일과 커밋 메시지
사용: python tools/verify_public_repo.py <출력 폴더>   (출력 폴더 안에 clone\ 과 blobs\ 가 생긴다. 출력 폴더는 git 제외 위치로)
결과: 출력 폴더\report.json + 화면 요약. 확인하지 못한 바이너리는 '미확인'으로 따로 센다(0건 집계에 넣지 않음)."""
from __future__ import annotations

import io
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
URL = "https://github.com/joongpark0902/AgentChat.git"
TERMS = [t.strip() for t in (ROOT / "private" / "sensitive_terms.txt").read_text(encoding="utf-8").splitlines()
         if t.strip() and not t.strip().startswith("#")]
PII = {
    "이메일": re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"),
    "전화": re.compile(r"\b01[016789]-?\d{3,4}-?\d{4}\b|\b0\d{1,2}-\d{3,4}-\d{4}\b"),
    "주민·외국인번호": re.compile(r"\b\d{6}-[1-8]\d{6}\b"),
    "사업자번호": re.compile(r"\b\d{3}-\d{2}-\d{5}\b"),
    "법인 표기": re.compile(r"주식회사|\(주\)|㈜"),
    "PC 사용자 경로": re.compile(r"[Cc]:[\\/]+Users[\\/]+(?!Public|Default|<|\{|%|\w*\.\.\.)\w+", re.I),
}
SAFE_MAIL = re.compile(r"@([\w-]+\.)*(example(\.\w+)*|test|invalid|localhost|users\.noreply\.github\.com|anthropic\.com)$"
                       r"|@v?\d+(\.\d+)*$", re.I)
FORBIDDEN = re.compile(r"(^|/)(agents\.yaml|settings\.yaml|\.claude\.json|auth\.json)$|(^|/)(private|logs|_첨부|_메일|backups|workspace)/"
                       r"|avatars/local_|\.(jsonl|state\.json)$", re.I)
TEXT_EXT = re.compile(r"\.(py|js|ts|css|html?|md|txt|ya?ml|json|bat|cmd|ps1|toml|ini|cfg|svg|gitignore|gitkeep)$|^[^.]+$", re.I)


def git(cwd, *a, binary=False):
    p = subprocess.run(["git", "-C", str(cwd), *a], capture_output=True)
    return p.stdout if binary else p.stdout.decode("utf-8", "replace")


def _utf8(data: bytes) -> bool:
    try:
        data.decode("utf-8")
        return True
    except UnicodeDecodeError:
        return False


def image_meta(data: bytes) -> dict:
    from PIL import Image
    im = Image.open(io.BytesIO(data))
    exif = {k: str(v)[:60] for k, v in (im.getexif() or {}).items()}
    info = {k: str(v)[:80] for k, v in im.info.items() if k not in ("dpi", "jfif", "jfif_version", "jfif_unit", "jfif_density",
                                                                    "progressive", "progression", "icc_profile", "gamma")}
    return {"format": im.format, "size": im.size, "exif": exif, "info": info}


def main(out: Path):
    out.mkdir(parents=True, exist_ok=True)
    clone = out / "clone"
    if not clone.exists():
        subprocess.run(["git", "clone", "--quiet", "--mirror", URL, str(clone)], check=True)
    head = git(clone, "rev-parse", "main").strip()
    commits = git(clone, "rev-list", "--all").split()
    meta = [l.split("\x1f") for l in git(clone, "log", "--all", "--format=%h\x1f%ae\x1f%ce\x1f%B\x1e").split("\x1e") if l.strip()]
    rep = {"head": head, "commits": len(commits), "emails": sorted({m[i].strip() for m in meta for i in (1, 2)}),
           "message_hits": [], "text_hits": [], "forbidden_paths": [], "binaries": [], "unchecked": []}
    for m in meta:
        for t in TERMS:
            if t.lower() in m[3].lower():
                rep["message_hits"].append({"commit": m[0].strip(), "term": t[:2] + "…"})
    # 전체 이력의 (경로, blob) — 같은 내용은 한 번만 검사
    seen, paths_by_blob = set(), {}
    for c in commits:
        for line in git(clone, "-c", "core.quotepath=false", "ls-tree", "-r", c).splitlines():
            mode_type_sha, path = line.split("\t", 1)
            sha = mode_type_sha.split()[2]
            paths_by_blob.setdefault(sha, set()).add(path)
            if FORBIDDEN.search(path):
                rep["forbidden_paths"].append({"commit": c[:7], "path": path})
    blobs_dir = out / "blobs"
    blobs_dir.mkdir(exist_ok=True)
    for sha, paths in paths_by_blob.items():
        path = sorted(paths)[0]
        data = git(clone, "cat-file", "-p", sha, binary=True)
        is_text = b"\x00" not in data[:8000] and _utf8(data)   # 확장자가 아니라 내용으로 판단(매니페스트 등도 글로 검사)
        if is_text:
            text = data.decode("utf-8", "replace")
            low = text.lower()
            for t in TERMS:
                if t.lower() in low:
                    rep["text_hits"].append({"path": path, "blob": sha[:7], "kind": "검사어", "value": t[:2] + "…"})
            for kind, p in PII.items():
                for mm in p.finditer(text):
                    v = mm.group(0)
                    if kind == "이메일" and SAFE_MAIL.search(v):
                        continue
                    line = text[:mm.start()].count("\n") + 1
                    rep["text_hits"].append({"path": path, "blob": sha[:7], "line": line, "kind": kind, "value": v[:6] + "…"})
            continue
        entry = {"paths": sorted(paths), "blob": sha[:7], "bytes": len(data)}
        low = data.lower()
        entry["term_bytes"] = [t[:2] + "…" for t in TERMS if t.isascii() and len(t) >= 4 and t.lower().encode() in low]
        try:
            entry.update(image_meta(data))
            ext = path.rsplit(".", 1)[-1]
            (blobs_dir / f"{sha[:7]}.{ext}").write_bytes(data)
            entry["extracted"] = f"blobs/{sha[:7]}.{ext}"
        except Exception as e:  # 그림이 아닌 바이너리 → 확인 못 함으로 따로
            entry["error"] = type(e).__name__
            rep["unchecked"].append(entry)
            continue
        rep["binaries"].append(entry)
    (out / "report.json").write_text(json.dumps(rep, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"HEAD {head[:7]} / 커밋 {len(commits)}개 / 고유 파일 내용 {len(paths_by_blob)}개 (그림 {len(rep['binaries'])}, 미확인 {len(rep['unchecked'])})")
    print("작성자·커미터 이메일:", rep["emails"])
    print("커밋 메시지 검사어:", len(rep["message_hits"]), "/ 금지 경로:", len(rep["forbidden_paths"]),
          "/ 글 파일 걸림:", len(rep["text_hits"]))
    for h in rep["text_hits"]:
        print("  글", h)
    for b in rep["binaries"]:
        print(f"  그림 {b['blob']} {b['paths']} {b['format']} {b['size']} EXIF {b['exif'] or '없음'} 기타 {b['info'] or '없음'} 바이트검사 {b['term_bytes'] or '없음'}")
    for u in rep["unchecked"]:
        print("  미확인", u)


if __name__ == "__main__":
    main(Path(sys.argv[1]))
