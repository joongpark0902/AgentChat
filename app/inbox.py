"""지난 대화 전체 검색 + 결정 대기함(사용자 판단을 기다리는 대화 모음).

10/1 사용자 요청(Hermes 벤치마킹): 여러 대화에 흩어진 '확인 요청'을 한곳에서 보고, 지난 대화를 내용으로 찾는다.
"""
from __future__ import annotations

import re

SNIPPET = 80          # 검색 결과 앞뒤 문맥 글자 수
MAX_HITS_PER_CONV = 3
DECISION_LINE_MAX = 60  # 이보다 긴 줄은 본문 문장으로 보고 제목으로 치지 않는다


def decision_pattern(user_name: str) -> re.Pattern:
    """실무자 답변 끝의 '확인 요청' / '사장님이 정해야 할 것' 같은 제목 줄."""
    u = re.escape(user_name)
    # 10/1 실제 기록에 나온 제목: '정해 주실 것', '아직 결정하지 않은 것', '사장님께 여쭐 것'
    return re.compile(rf"확인\s*요청|{u}[이가]?\s*정해야\s*할|{u}께서\s*정하실|{u}[이가]?\s*정하실|{u}\s*결정\s*필요"
                      rf"|정해\s*주실|결정하지\s*않은|{u}께\s*여쭤|{u}께\s*여쭐")


def _decision_section(text: str, pat: re.Pattern) -> str | None:
    """제목 줄을 찾으면 그 아래 몇 줄을 요약으로 돌려준다. 없으면 None."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        bare = line.strip().strip("#*_ ").strip()
        if bare and len(bare) <= DECISION_LINE_MAX and pat.search(bare):
            body = [l.strip() for l in lines[i + 1:] if l.strip()][:3]
            return " / ".join(body)[:200] or bare
    return None


def pending_decision(messages: list[dict], user_name: str, running: bool = False) -> dict | None:
    """마지막 상태가 '사용자 판단 대기'인지. 진행 중인 과제(라운드 중간 보고)는 대기로 치지 않는다.

    맨 끝에서부터 보며 승인 카드·일반 시스템 안내(토큰 안내 등)는 건너뛴다.
    - 사용자 메시지가 먼저 나오면 이미 답한 것 → 대기 아님
    - 시스템/담당자 메시지에 escalation 표시 → 대기
    - 담당자 메시지에 확인 요청 제목 줄 → 대기(라운드 중간 보고는 제외, 최종 보고는 포함)
    """
    if running:
        return None
    pat = decision_pattern(user_name)
    for m in reversed(messages):
        if m.get("kind") == "approval":
            continue
        sender = m.get("sender")
        if sender == "user":
            return None
        if sender == "system" and not m.get("escalation"):
            continue
        text = m.get("text") or ""
        if m.get("escalation"):
            first = next((l.strip() for l in text.splitlines() if l.strip()), "")
            return {"msg_id": m.get("id"), "ts": m.get("ts"), "sender": sender, "reason": "escalation",
                    "summary": first[:200]}
        if m.get("round") and not m.get("final"):
            return None  # 라운드 중간의 팀 내부 보고(멈춘 과제 등)
        section = _decision_section(text, pat)
        if section:
            return {"msg_id": m.get("id"), "ts": m.get("ts"), "sender": sender, "reason": "question",
                    "summary": section}
        return None
    return None


def search_messages(messages: list[dict], q: str, limit: int = MAX_HITS_PER_CONV) -> tuple[int, list[dict]]:
    """대소문자 무시 부분 일치. (전체 일치 수, 최근 것부터 limit 개의 {msg_id, ts, sender, snippet})."""
    ql = q.lower()
    total, hits = 0, []
    for m in reversed(messages):
        if m.get("kind") == "approval" or m.get("retracted"):
            continue
        text = m.get("text") or ""
        pos = text.lower().find(ql)
        if pos < 0:
            continue
        total += 1
        if len(hits) < limit:
            a, b = max(0, pos - SNIPPET), min(len(text), pos + len(q) + SNIPPET)
            snippet = ("…" if a else "") + text[a:b].replace("\n", " ") + ("…" if b < len(text) else "")
            hits.append({"msg_id": m.get("id"), "ts": m.get("ts"), "sender": m.get("sender"), "snippet": snippet})
    return total, hits


# ------------------------------------------------------------ 반복 작업 추천 (v11, 10/1 사용자 결정: 비슷한 지난 작업 1건 이상이면 추천)
_PARTICLES = ("해주세요", "해줘", "해봐", "하기", "에서는", "으로는", "에서", "으로", "까지", "부터", "하고", "이랑", "랑", "을", "를", "은", "는", "이", "가",
              "의", "에", "로", "와", "과", "도", "만", "께")
_STOP = {"해줘", "해주세요", "작성", "작업", "파일", "확인", "정리", "보고", "결과", "지시", "이번", "그리고", "다시", "좀",
         "있는", "없는", "하는", "해서", "하고", "어떻게", "어때", "뭐야", "이거", "그거", "우리", "너가", "나한테", "같이"}


def topic_words(text: str) -> set[str]:
    """지시·제목에서 주제 단어를 뽑는다(2글자 이상, 조사 떼기, 흔한 말 제외). 정밀하지 않아도 됨 — 최종 판단은 실무자."""
    out = set()
    for w in re.findall(r"[가-힣A-Za-z0-9]{2,}", text or ""):
        w = w.lower()
        for p in _PARTICLES:
            if w.endswith(p) and len(w) - len(p) >= 2:
                w = w[: -len(p)]
                break
        if w not in _STOP and not w.isdigit():
            out.add(w)
    return out


def similar_tasks(current: str, convs: list[dict], min_shared: int = 2, limit: int = 5) -> list[dict]:
    """convs: [{id, title, updated, text(제목+사용자 지시)}] 중 주제 단어가 min_shared 개 이상 겹치는 대화(많이 겹친 순)."""
    mine = topic_words(current)
    if not mine:
        return []
    out = []
    for c in convs:
        shared = mine & topic_words(c.get("text", ""))
        if len(shared) >= min_shared:
            out.append({"id": c["id"], "title": c.get("title", ""), "updated": c.get("updated"),
                        "shared": sorted(shared)[:6]})
    out.sort(key=lambda x: (len(x["shared"]), x.get("updated") or 0), reverse=True)
    return out[:limit]
