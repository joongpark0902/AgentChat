"""라운드별로 각 에이전트에게 보낼 메시지를 만든다. 역할 지침은 시스템 프롬프트(또는 codex 첫 메시지)에 따로 들어간다."""
from __future__ import annotations

from .config import AgentConfig
from .verdict import Review


def _numbered(items: list[str]) -> str:
    return "\n".join(f"{i}. {t}" for i, t in enumerate(items, 1))


# 10/1: 사용자 요청 — 한 번에 긴 작업 대신 같은 시간을 3라운드로 나눠 셋이 자주 주고받으며 산출물을 낸다
STAGED_PLAN_WORKER = (
    "[라운드 1 — 방향 잡기] 이 과제는 감독과 주고받으며 3라운드로 진행합니다(1 방향 → 2 초안 → 3 마무리).\n"
    "이번 라운드에서는 본 작업(산출물 작성·계산)을 하지 말고, 몇 분 안에 끝낼 분량으로 아래만 짧게 올려 주세요.\n"
    "① 지시를 어떻게 이해했는지 ② 산출물 구성(파일명, 시트·목차) ③ 쓸 원자료와 위치 ④ 작업 순서 ⑤ {user}이 정해야 할 것\n"
    "원자료는 구성을 파악할 만큼만 열어 보세요.")

STAGED_DRAFT_WORKER = ("[라운드 2 — 초안] 감독들이 계획을 검토했습니다. 지적마다 반영 여부를 한 줄씩 답하고, "
                       "계획대로 본 작업을 해 산출물 초안을 만든 뒤 보고해 주세요. 라운드 3에서 감독 지적을 받아 마무리합니다.")

STAGED_PLAN_REVIEW = (
    "[라운드 1 — 계획 검토] {worker}가 본 작업 전에 계획만 올렸습니다(산출물은 아직 없음). "
    "당신의 체크리스트 관점에서 미리 짚을 점만 짧게 지적하세요: 지시 누락·오해석, 산출물 구성, 원자료 선택, 빠진 검증. "
    "재계산·파일 전수 검토는 라운드 2(초안)에서 합니다. 필요하면 원자료 위치·구성만 확인하세요. "
    "계획대로 가도 되면 APPROVE 입니다(승인해도 라운드 2 초안으로 넘어갑니다).")


def worker_prompt(round_no: int, requirements: list[str], new_requirements: list[str],
                  feedback: list[tuple[AgentConfig, Review]], user_name: str, stage: str | None = None) -> str:
    if stage == "plan":
        parts = [f"[{user_name}의 지시]\n{_numbered(requirements)}", STAGED_PLAN_WORKER.format(user=user_name)]
    elif round_no == 1 and not feedback:
        parts = [f"[{user_name}의 지시]\n{_numbered(requirements)}", "작업하고 결과를 보고해 주세요."]
    else:
        head = STAGED_DRAFT_WORKER if stage == "draft" else \
            f"[라운드 {round_no}] 감독 검토 결과입니다. 지적마다 수정 여부를 답하고 필요한 수정을 해 주세요."
        parts = [head]
        for agent, review in feedback:
            parts.append(f"■ {agent.name}({agent.title}) — {review.verdict}\n{review.to_text()}")
        if any(r.user_blockers for _, r in feedback):
            parts.append(f"('사용자 결정' 표시가 붙은 지적은 {user_name}이 정할 사안입니다. 임의로 고치지 말고 그대로 두세요. "
                         f"과제가 끝날 때 {user_name}께 따로 넘어갑니다.)")
    if new_requirements and stage != "plan" and not (round_no == 1 and not feedback):
        parts.append(f"[{user_name}이 새로 추가한 지시 — 최우선 반영]\n{_numbered(new_requirements)}")
    return "\n\n".join(parts)


def review_prompt(round_no: int, requirements: list[str], new_requirements: list[str],
                  worker: AgentConfig, worker_report: str, workspace: str, user_name: str,
                  worker_unseen: list[str] | None = None, changed_files: list[str] | None = None,
                  stage: str | None = None) -> str:
    parts = [
        STAGED_PLAN_REVIEW.format(worker=worker.name) if stage == "plan" else f"[라운드 {round_no} 검토 요청]",
        f"[{user_name}의 지시 전체]\n{_numbered(requirements)}",
    ]
    if new_requirements:
        parts.append(f"(당신에게 새로 알리는 지시: {', '.join(new_requirements)})")
    if worker_unseen:
        parts.append(f"(주의: 다음 지시는 {worker.name}가 이번 작업을 시작한 뒤에 들어와 아직 전달되지 않았습니다. "
                     f"다음 라운드에 전달되므로 이번 판정에서 누락으로 지적하지 마세요: {', '.join(worker_unseen)})")
    parts.append(f"[{worker.name}({worker.title})의 보고]\n{worker_report}")
    if changed_files is not None:
        # 9/29: 폴더 파일이 수백 개라 감독이 라운드마다 전체를 다시 훑어 토큰이 크게 들었음 → 바뀐 파일을 알려 준다
        if changed_files:
            listing = "\n".join(f"- {p}" for p in changed_files)
            parts.append(f"[이번 라운드에 새로 생기거나 바뀐 파일 — {len(changed_files)}개]\n{listing}\n"
                         "검토는 이 파일들과 그 근거가 된 원자료를 중심으로 하고, 폴더 전체를 다시 훑는 것은 꼭 필요할 때만 하세요.")
        else:
            parts.append("(이번 라운드에 작업 폴더에서 바뀐 파일이 없습니다. 보고 내용과 관련 파일만 확인하세요.)")
    if stage == "draft":
        parts.append("라운드 2(초안)입니다. 계획 단계에서 당신이 짚은 점이 반영됐는지와, 새로 만든 산출물을 체크리스트대로 검토하세요.")
    elif round_no >= 2:
        # 9/30: 같은 검증을 라운드마다 처음부터 다시 해 감독 한 번에 최대 22분이 걸렸음
        parts.append("라운드 2 이상입니다. 직전 라운드에 당신이 지적한 것이 해결됐는지와 이번에 바뀐 파일만 확인하세요. "
                     "이미 확인해 통과시킨 항목은 다시 검증하지 않습니다.")
    parts.append(NEEDS_USER_RULE.format(worker=worker.name, user=user_name))
    if stage == "plan":
        parts.append(f"작업 폴더는 {workspace} 입니다. 당신의 체크리스트 범위만, 짧게 JSON 으로 답해 주세요.")
    else:
        parts.append(f"작업 폴더 {workspace} 의 파일을 직접 확인하고, 당신의 체크리스트 범위만 검토해 JSON 으로 답해 주세요.")
    return "\n\n".join(parts)


# 9/30: '숨김 열' 한 건(사용자가 정할 사안)을 감독이 5회 연속 필수 지적해 두 과제가 3라운드까지 헛돌았음
NEEDS_USER_RULE = ("필수 지적마다 needs_user 를 정하세요. {worker}가 고칠 수 없고 {user}이 정해야 하는 사안, 그리고 {worker}가 "
                   "근거를 들어 '수정 안 함'으로 답했는데도 다시 올리는 지적은 needs_user=true 입니다. "
                   "이런 지적은 {worker}에게 돌려보내지 않고 바로 {user}께 넘어갑니다. 그 밖의 지적은 false 입니다.")

NO_EVIDENCE_NUDGE = ("방금 판정은 결과물 파일을 하나도 열지 않고 낸 것이라 받을 수 없습니다. 실무자의 보고문은 근거가 아닙니다. "
                     "이번 라운드에 바뀐 파일을 직접 열어 확인한 뒤 같은 JSON 형식으로 다시 판정해 주세요.")


def user_decision_text(feedback: list[tuple[AgentConfig, Review]], user_name: str, worker_name: str) -> str:
    lines = [f"남은 필수 지적이 모두 {user_name}께서 정하실 사안이라, 라운드를 더 돌리지 않고 넘깁니다.", ""]
    for agent, review in feedback:
        for i in review.user_blockers:
            lines.append(f"- [{agent.name}] {i.get('description', '').strip()}")
    lines += ["", f"어떻게 할지 지시해 주시면 {worker_name}가 이어서 반영합니다. (세션은 유지됩니다)"]
    return "\n".join(lines)


def escalation_text(max_rounds: int, feedback: list[tuple[AgentConfig, Review]], user_name: str) -> str:
    lines = [f"{max_rounds}라운드 안에 두 감독의 승인이 모두 나지 않아 {user_name}께 넘깁니다.",
             "", "남은 쟁점 (마지막 라운드 감독 지적 원문):"]
    for agent, review in feedback:
        if review.approved:
            lines.append(f"\n■ {agent.name}({agent.title}) — APPROVE (쟁점 없음)")
            continue
        lines.append(f"\n■ {agent.name}({agent.title}) — {review.verdict}")
        lines.append(review.summary.strip())
        for i in review.issues:
            tag = "필수" if i.get("severity") == "blocker" else "제안"
            if i.get("needs_user"):
                tag += "·사용자 결정"
            lines.append(f"- [{tag}] {i.get('description', '').strip()}")
    lines += ["", "어떻게 할지 지시해 주시면 이어서 진행합니다. (세션은 유지됩니다)"]
    return "\n".join(lines)
