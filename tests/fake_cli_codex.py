"""Codex 세션 이어받기 실패 재현용 가짜 CLI (10/8: 로그인 계정 전환 대비).
사용: python fake_cli_codex.py <new|resume> <모드> <호출 기록 파일>
- 모드 resume_fails: 이어받기(resume)는 오류로 끝나고, 새 세션(new)은 정상 답
- 모드 all_fail    : 둘 다 오류(한도 초과 흉내)
- 모드 ok          : 둘 다 정상 답
"""
import json
import sys

kind, mode, log = sys.argv[1], sys.argv[2], sys.argv[3]
text = sys.stdin.read()
with open(log, "a", encoding="utf-8") as f:
    f.write(kind + "\n")
if mode == "all_fail" or (mode == "resume_fails" and kind == "resume"):
    print(json.dumps({"type": "error", "message": "이 계정에서는 이 세션을 이어받을 수 없습니다(시험)"}))
    sys.exit(1)
print(json.dumps({"type": "thread.started", "thread_id": "tid-new" if kind == "new" else "tid-old"}))
answer = "역할 지침 받음" if text.startswith("[역할 지침]") else "역할 지침 없음"
print(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": answer}}, ensure_ascii=False))
print(json.dumps({"type": "turn.completed", "usage": {}}))
