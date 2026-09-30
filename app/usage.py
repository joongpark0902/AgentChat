"""토큰·구독 한도 사용량.

근거(2026-09-28 실측):
- claude stream-json: `rate_limit_event.rate_limit_info.unifiedWindows.{five_hour,seven_day}.{utilization(0~1), resetsAt}`,
  `result.modelUsage.<모델>.{inputTokens,outputTokens,cacheReadInputTokens,cacheCreationInputTokens,costUSD}`, `result.total_cost_usd`
  (total_cost_usd 는 API 정가 환산값 — 구독이라 실제 청구액 아님)
- codex exec --json: `turn.completed.usage.{input_tokens,cached_input_tokens,output_tokens,reasoning_output_tokens}`
  한도는 세션 파일 ~/.codex/sessions/YYYY/MM/DD/rollout-*-<thread_id>.jsonl 의
  `event_msg/token_count.payload.rate_limits.{primary(300분),secondary(10080분)}.{used_percent,resets_at}`,
  모델은 `turn_context.payload.model`
"""
from __future__ import annotations

import json
import time
from pathlib import Path

CODEX_SESSIONS = Path.home() / ".codex" / "sessions"


def claude_limits(rate_limit_info: dict) -> dict | None:
    wins = (rate_limit_info or {}).get("unifiedWindows") or {}
    out = {}
    for key in ("five_hour", "seven_day"):
        w = wins.get(key)
        if isinstance(w, dict) and w.get("utilization") is not None:
            out[key] = {"pct": round(float(w["utilization"]) * 100, 1), "resets_at": w.get("resetsAt")}
    return out or None


def claude_usage(result_event: dict) -> dict:
    mu = result_event.get("modelUsage") or {}
    tot = {"input_tokens": 0, "cache_read_tokens": 0, "cache_write_tokens": 0, "output_tokens": 0}
    for m in mu.values():
        tot["input_tokens"] += int(m.get("inputTokens") or 0)
        tot["cache_read_tokens"] += int(m.get("cacheReadInputTokens") or 0)
        tot["cache_write_tokens"] += int(m.get("cacheCreationInputTokens") or 0)
        tot["output_tokens"] += int(m.get("outputTokens") or 0)
    if not mu:  # modelUsage 가 없으면 usage 로 대신
        u = result_event.get("usage") or {}
        tot = {"input_tokens": int(u.get("input_tokens") or 0), "cache_read_tokens": int(u.get("cache_read_input_tokens") or 0),
               "cache_write_tokens": int(u.get("cache_creation_input_tokens") or 0), "output_tokens": int(u.get("output_tokens") or 0)}
    cost = result_event.get("total_cost_usd")
    per_model = {m: sum(int(v.get(k) or 0) for k in ("inputTokens", "outputTokens", "cacheReadInputTokens", "cacheCreationInputTokens"))
                 for m, v in mu.items()}
    return {"engine": "claude", "models": list(mu.keys()), **tot, "per_model": per_model,
            "total_tokens": sum(tot.values()), "cost_usd": round(float(cost), 4) if cost is not None else None}


def codex_usage(turn_usage: dict, model: str | None) -> dict:
    u = turn_usage or {}
    tot = {"input_tokens": int(u.get("input_tokens") or 0), "cache_read_tokens": int(u.get("cached_input_tokens") or 0),
           "cache_write_tokens": int(u.get("cache_write_input_tokens") or 0), "output_tokens": int(u.get("output_tokens") or 0),
           "reasoning_tokens": int(u.get("reasoning_output_tokens") or 0)}
    # codex 의 input_tokens 는 캐시 포함 총량(실측: input 16462 / cached 11008) → 합계는 input+output 기준
    return {"engine": "codex", "models": [model] if model else [], **tot,
            "total_tokens": tot["input_tokens"] + tot["output_tokens"], "cost_usd": None}


def find_codex_rollout(thread_id: str, sessions: Path = CODEX_SESSIONS) -> Path | None:
    if not thread_id or not sessions.exists():
        return None
    # 최근 날짜 폴더부터 찾는다(전체 재귀 검색은 파일이 많을 때 느림)
    days = sorted((p for p in sessions.glob("*/*/*") if p.is_dir()), reverse=True)
    for d in days[:14]:
        hit = next(iter(d.glob(f"rollout-*{thread_id}.jsonl")), None)
        if hit:
            return hit
    return None


def read_codex_rollout(path: Path) -> tuple[dict | None, str | None]:
    """세션 파일에서 마지막 한도 정보와 모델을 읽는다."""
    limits, model = None, None
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None, None
    for line in reversed(lines):
        if limits is None and '"rate_limits"' in line:
            try:
                rl = (json.loads(line).get("payload") or {}).get("rate_limits") or {}
            except ValueError:
                continue
            out = {}
            for src, key in (("primary", "five_hour"), ("secondary", "seven_day")):
                w = rl.get(src)
                if isinstance(w, dict) and w.get("used_percent") is not None:
                    out[key] = {"pct": round(float(w["used_percent"]), 1), "resets_at": w.get("resets_at"),
                                "window_minutes": w.get("window_minutes")}
            limits = out or None
        if model is None and '"turn_context"' in line:
            try:
                model = (json.loads(line).get("payload") or {}).get("model")
            except ValueError:
                pass
        if limits is not None and model is not None:
            break
    return limits, model


class LimitStore:
    """엔진별 최신 한도(5시간·주간)를 들고 있고 logs/usage_limits.json 에 저장한다."""

    def __init__(self, path: Path):
        self.path = path
        try:
            self.data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.data = {}

    def update(self, engine: str, limits: dict | None) -> bool:
        if not limits:
            return False
        self.data[engine] = {**limits, "updated": time.time()}
        try:
            self.path.write_text(json.dumps(self.data, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass
        return True


# ---------------------------------------------------------------- 답변 1건 사용량 = 누적 차이
# 2026-09-28 실측: claude result 의 modelUsage·total_cost_usd, codex turn.completed.usage 는 모두 "세션 누적"이다.
# (claude 최상위 usage 만 호출 단위) → 직전 누적값을 저장해 두고 차이로 이번 답변 사용량을 구한다.
TOKEN_KEYS = ("input_tokens", "cache_read_tokens", "cache_write_tokens", "output_tokens", "reasoning_tokens", "total_tokens")


def delta(cur: dict, prev: dict | None) -> dict:
    out = dict(cur)
    if not prev:
        return out
    for k in TOKEN_KEYS:
        if k in cur:
            out[k] = max(0, int(cur.get(k) or 0) - int(prev.get(k) or 0))
    if cur.get("cost_usd") is not None and prev.get("cost_usd") is not None:
        out["cost_usd"] = round(max(0.0, cur["cost_usd"] - prev["cost_usd"]), 4)
    if isinstance(cur.get("per_model"), dict):  # 이번 답변에서 사용량이 늘어난 모델만
        pm = prev.get("per_model") or {}
        out["models"] = [m for m, n in cur["per_model"].items() if n > int(pm.get(m) or 0)]
    return out


def claude_usage_top(result_event: dict) -> dict:
    """세션 누적 기준점이 없을 때(예전 세션을 처음 이어갈 때) 쓰는 호출 단위 usage. 금액은 알 수 없음."""
    u = result_event.get("usage") or {}
    tot = {"input_tokens": int(u.get("input_tokens") or 0), "cache_read_tokens": int(u.get("cache_read_input_tokens") or 0),
           "cache_write_tokens": int(u.get("cache_creation_input_tokens") or 0), "output_tokens": int(u.get("output_tokens") or 0)}
    return {"engine": "claude", "models": [], **tot, "total_tokens": sum(tot.values()), "cost_usd": None}


def codex_last_turn(path: Path) -> dict | None:
    """세션 파일에서 마지막 작업(task_started 이후)의 토큰 = 마지막 누적 − 작업 시작 직전 누적."""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    before, last = None, None
    for line in lines:
        if '"task_started"' in line:
            before = last
        elif '"token_count"' in line:
            try:
                info = (json.loads(line).get("payload") or {}).get("info") or {}
            except ValueError:
                continue
            if info.get("total_token_usage"):
                last = info["total_token_usage"]
    if not last:
        return None
    cur = codex_usage(last, None)
    return delta(cur, codex_usage(before, None) if before else None)


def codex_context(path: Path, nbytes: int = 256 * 1024) -> tuple[int | None, int | None]:
    """세션 컨텍스트 크기(마지막 요청의 입력+출력 토큰)와 모델 창 크기.
    2026-09-30 실측: token_count.payload.info = {total_token_usage, last_token_usage, model_context_window}."""
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - nbytes))
            lines = f.read().decode("utf-8", errors="replace").splitlines()
    except OSError:
        return None, None
    for line in reversed(lines):
        if '"last_token_usage"' not in line:
            continue
        try:
            info = (json.loads(line).get("payload") or {}).get("info") or {}
        except ValueError:
            continue
        last = info.get("last_token_usage") or {}
        used = int(last.get("input_tokens") or 0) + int(last.get("output_tokens") or 0)
        return used or None, int(info.get("model_context_window") or 0) or None
    return None, None


def read_codex_tail(path: Path, nbytes: int = 256 * 1024) -> tuple[dict | None, dict | None]:
    """실행 중 실시간 표시용: 세션 파일 끝부분만 읽어 마지막 누적 토큰(total_token_usage)과 한도를 얻는다."""
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - nbytes))
            lines = f.read().decode("utf-8", errors="replace").splitlines()
    except OSError:
        return None, None
    for line in reversed(lines):
        if '"token_count"' not in line:
            continue
        try:
            payload = json.loads(line).get("payload") or {}
        except ValueError:
            continue
        total = (payload.get("info") or {}).get("total_token_usage")
        rl = payload.get("rate_limits") or {}
        limits = {}
        for src, key in (("primary", "five_hour"), ("secondary", "seven_day")):
            w = rl.get(src)
            if isinstance(w, dict) and w.get("used_percent") is not None:
                limits[key] = {"pct": round(float(w["used_percent"]), 1), "resets_at": w.get("resets_at")}
        return total, limits or None
    return None, None
