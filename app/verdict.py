"""감독 응답(구조화 출력) 스키마와 파싱."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

# Claude --json-schema / Codex --output-schema 공용.
# Codex(OpenAI strict) 제약: 모든 속성을 required 에 넣고 additionalProperties=false.
REVIEW_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "verdict": {"type": "string", "enum": ["APPROVE", "REVISE"]},
        "summary": {"type": "string", "description": "검토 결과 한두 문장. 문제가 없으면 없다고 말한다."},
        "issues": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "severity": {"type": "string", "enum": ["blocker", "minor"]},
                    "description": {"type": "string"},
                    "needs_user": {"type": "boolean", "description": (
                        "실무자가 고칠 수 없고 사용자가 정해야 하는 사안이면 true. 실무자가 근거를 들어 "
                        "'수정 안 함'으로 답한 지적을 다시 올릴 때도 true(같은 지적으로 라운드를 반복하지 않는다).")},
                },
                "required": ["severity", "description", "needs_user"],
            },
        },
        "requirements": {
            "type": "array",
            "description": "사용자 지시를 항목별로 쪼개 결과물 파일에서 확인한 결과. 담당 범위가 아니면 빈 배열.",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "item": {"type": "string", "description": "지시 항목"},
                    "met": {"type": "boolean"},
                    "evidence": {"type": "string", "description": "직접 연 파일과 위치(슬라이드·시트·셀). 보고문 인용은 근거가 아님."},
                },
                "required": ["item", "met", "evidence"],
            },
        },
        "checks": {"type": "string", "description": "직접 확인한 것(읽은 파일, 실행한 명령과 결과). 없으면 빈 문자열."},
    },
    "required": ["verdict", "summary", "issues", "requirements", "checks"],
}


@dataclass
class Review:
    verdict: str  # APPROVE | REVISE
    summary: str
    issues: list[dict] = field(default_factory=list)
    checks: str = ""
    parse_warning: str = ""
    requirements: list[dict] = field(default_factory=list)

    @property
    def approved(self) -> bool:
        return self.verdict == "APPROVE"

    @property
    def blockers(self) -> list[dict]:
        return [i for i in self.issues if i.get("severity") == "blocker"]

    @property
    def user_blockers(self) -> list[dict]:
        """사용자가 정해야 하는 필수 지적(실무자에게 돌려보내도 해결되지 않음)."""
        return [i for i in self.blockers if i.get("needs_user")]

    @property
    def worker_blockers(self) -> list[dict]:
        return [i for i in self.blockers if not i.get("needs_user")]

    def to_text(self) -> str:
        lines = [self.summary.strip()]
        for r in self.requirements:
            mark = "충족" if r.get("met") else "미충족"
            lines.append(f"- [{mark}] {str(r.get('item', '')).strip()} — {str(r.get('evidence', '')).strip()}")
        for i in self.issues:
            tag = "필수" if i.get("severity") == "blocker" else "제안"
            if i.get("needs_user"):
                tag += "·사용자 결정"
            lines.append(f"- [{tag}] {i.get('description', '').strip()}")
        if self.checks.strip():
            lines.append(f"\n확인한 것: {self.checks.strip()}")
        if self.parse_warning:
            lines.append(f"\n(시스템: {self.parse_warning})")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {"verdict": self.verdict, "summary": self.summary, "issues": self.issues,
                "requirements": self.requirements,
                "checks": self.checks, "parse_warning": self.parse_warning}


def _from_obj(obj: dict) -> Review:
    verdict = str(obj.get("verdict", "")).upper()
    issues = [i for i in (obj.get("issues") or []) if isinstance(i, dict)]
    reqs = [r for r in (obj.get("requirements") or []) if isinstance(r, dict)]
    review = Review(verdict=verdict if verdict in ("APPROVE", "REVISE") else "REVISE",
                    summary=str(obj.get("summary", "")), issues=issues, checks=str(obj.get("checks", "")),
                    requirements=reqs)
    if verdict not in ("APPROVE", "REVISE"):
        review.parse_warning = f"판정값 '{verdict}' 을 알 수 없어 REVISE 로 처리했습니다"
    # 규칙: blocker 가 있으면 APPROVE 여도 REVISE, 판정은 REVISE 인데 blocker 가 없으면 그대로 둠
    if review.verdict == "APPROVE" and review.blockers:
        review.verdict = "REVISE"
        review.parse_warning = "APPROVE 였지만 필수(blocker) 지적이 있어 REVISE 로 처리했습니다"
    return review


def parse_review(structured: dict | None, text: str) -> Review:
    """구조화 출력 → 본문 JSON → 본문 'VERDICT:' 줄 순서로 시도."""
    if isinstance(structured, dict):
        return _from_obj(structured)
    text = (text or "").strip()
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return _from_obj(obj)
    except ValueError:
        pass
    m = re.search(r"\{.*\}", text, re.S)
    if m:
        try:
            obj = json.loads(m.group(0))
            if isinstance(obj, dict) and "verdict" in obj:
                return _from_obj(obj)
        except ValueError:
            pass
    m = re.search(r"VERDICT\s*[:：]\s*(APPROVE|REVISE)", text, re.I)
    if m:
        return Review(verdict=m.group(1).upper(), summary=text, parse_warning="구조화 출력이 없어 본문에서 판정을 읽었습니다")
    return Review(verdict="REVISE", summary=text or "(빈 응답)", parse_warning="판정을 읽지 못해 REVISE 로 처리했습니다")
