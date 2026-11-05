"""입력·출력 통로 막힘 재현용 가짜 CLI (10/2: 새 세션 Clara 가 시작부터 멈춘 사고).
사용: python fake_cli_pipe.py <모드> <pid 파일>
- bigfirst: 입력을 읽기 전에 큰 출력(약 512KB, 시작 훅 출력 흉내)을 먼저 내고, 입력 한 줄을 읽은 뒤 result 를 낸다
- noread  : 입력을 전혀 읽지 않고 기다린다(시간 초과·중지 경로 확인용)
"""
import json
import os
import sys
import time

mode, pid_file = sys.argv[1], sys.argv[2]
with open(pid_file, "w") as f:
    f.write(str(os.getpid()))
if mode == "bigfirst":
    for _ in range(8):
        sys.stdout.write(json.dumps({"type": "system", "subtype": "hook_response", "output": "x" * 65536}) + "\n")
    sys.stdout.flush()
    line = sys.stdin.readline()
    sys.stdout.write(json.dumps({"type": "result", "session_id": "s", "result": f"받은 글자 {len(line)}",
                                 "is_error": False}) + "\n")
    sys.stdout.flush()
else:
    time.sleep(600)
