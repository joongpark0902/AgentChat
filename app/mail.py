"""화면의 ✉ 창에 보여줄 메일 목록과 한 통의 본문. tools/outlook_tool.py 의 조회를 감싸 JSON 으로 돌려준다.
읽기 전용 — 목록·검색, 그리고 사용자가 '본문'을 누른 그 한 통의 본문·첨부 이름만 내보낸다.
첨부 내려받기·초안·보내기는 화면에서 다루지 않고 실무자가 도구로 한다."""
from __future__ import annotations

import contextlib
import importlib.util
import os
import threading
from types import SimpleNamespace

from tools import outlook_tool as T

LIMIT = 30          # 화면에 보여줄 건수(전체 건수는 따로 알려 준다)
UNVERIFIED_SHOWN = 20
_lock = threading.Lock()   # Outlook 은 한 번에 한 요청만 — 동시에 온 요청은 차례로 처리한다


def _ole32():
    try:
        import ctypes
        return ctypes.windll.ole32
    except (ImportError, AttributeError):   # Windows 가 아닌 곳(가짜 편지함 테스트)
        return None


@contextlib.contextmanager
def _com():
    """이 스레드에서 COM 을 쓸 수 있게 하고, 여기서 올린 만큼만 되돌린다(서버는 요청을 작업 스레드에서 돌린다).
    pythoncom.CoInitialize/CoUninitialize 짝으로 하면 이미 COM 을 쓰던 스레드에서는 그 스레드의 COM 까지 내려가
    들고 있던 Outlook 참조가 끊긴다(10/6 실측) → Windows 가 돌려주는 결과값을 보고 성공한 초기화만 되돌린다."""
    ole, hr = _ole32(), None
    if ole is not None:
        hr = ole.CoInitializeEx(None, 2)   # 2 = 단일 스레드 방식. 0 새로 올림 / 1 이미 올라 있어 횟수만 늘림 / 음수 실패
    try:
        yield
    finally:
        if hr in (0, 1):
            ole.CoUninitialize()


def available() -> bool:
    """이 PC 에서 메일 창을 쓸 수 있는지(Windows + pywin32). Outlook 이 켜져 있는지는 조회할 때 본다."""
    return os.name == "nt" and importlib.util.find_spec("win32com") is not None


def _row(r: dict) -> dict:
    return {"id": r["id"], "time": r["time"].strftime("%Y-%m-%d %H:%M") if r["time"] else "",
            "sender": r["sender"], "addr": r["addr"], "subject": r["subject"], "unread": r["unread"],
            "has_att": r["has_att"], "kind": T._kind(r["mclass"]), "folder": r["folder"]}


def list_mail(q: str = "", days: int = 7, body: bool = False) -> dict:
    """받은 메일 목록(최신순 LIMIT 건). days=0 이면 전체 기간. 못 읽은 메일은 unverified 로 따로 낸다."""
    words = q.split()
    args = SimpleNamespace(folder="received", since=None, until=None, days=days or None, unread=False)
    with _lock, _com():
        try:
            rows, info = _collect(args, words, bool(body and words))
        except T.ToolError as e:
            return {"ok": False, "error": str(e)}
        except Exception as e:   # Outlook 이 바쁘거나 중간에 꺼진 경우 — 빈 목록으로 넘기지 않고 알린다
            return {"ok": False, "error": f"Outlook 에서 메일을 읽지 못했습니다({type(e).__name__}). Outlook 을 확인한 뒤 다시 조회해 주세요."}
    missed = info["unverified"]
    return {"ok": True, "total": len(rows), "rows": [_row(r) for r in rows[:LIMIT]], "limit": LIMIT,
            "days": days, "words": words, "body": bool(body and words), "body_read": info["body_read"],
            "scope": {"label": info["label"], "folders": info["folders"], "with_mail": info["with_mail"],
                      "scanned": info["scanned"], "excluded": [{"path": p, "count": n} for p, n in info["excluded"]]},
            "mismatch": info["mismatch"], "unverified_total": len(missed),
            "unverified": [_row(r) for r in missed[:UNVERIFIED_SHOWN]], "note": T.NOTE_EXTERNAL}


def collect_since(since) -> dict:
    """since(날짜·시각) 이후 받은 메일 전부 — 건수 제한 없음(화면 목록의 30건 제한과 무관). 매일 요약의 대상을 뽑을 때 쓴다.
    rows 는 최신순, time 은 datetime 그대로."""
    from datetime import datetime
    days = max(0, (datetime.now().date() - since.date()).days)
    args = SimpleNamespace(folder="received", since=None, until=None, days=days, unread=False)
    with _lock, _com():
        try:
            rows, info = _collect(args, (), False)
        except T.ToolError as e:
            return {"ok": False, "error": str(e)}
        except Exception as e:
            return {"ok": False, "error": f"Outlook 에서 메일을 읽지 못했습니다({type(e).__name__})."}
    rows = [r for r in rows if r["time"] is not None and r["time"] >= since]
    return {"ok": True, "since": since, "mismatch": info["mismatch"], "scanned": info["scanned"],
            "rows": [{"id": r["id"], "time": r["time"], "sender": r["sender"], "addr": r["addr"], "subject": r["subject"],
                      "has_att": r["has_att"], "kind": T._kind(r["mclass"]), "folder": r["folder"]} for r in rows]}


BODY_LIMIT = 20000   # 화면에 보여줄 본문 글자 수(넘으면 앞부분만, 전체 글자 수는 따로 알려 준다)


def read_mail(entry_id: str) -> dict:
    """한 통의 본문과 첨부 이름(화면의 '본문' 보기). 읽기만 한다 — 읽음 표시를 바꾸지 않고 첨부는 내려받지 않는다."""
    with _lock, _com():
        try:
            return _read(entry_id)
        except T.ToolError as e:
            return {"ok": False, "error": str(e)}
        except Exception as e:   # 본문을 못 읽은 것을 '본문 없음'으로 보여주지 않는다
            return {"ok": False, "error": f"본문을 읽지 못했습니다({type(e).__name__}). 본문이 없는 것이 아니니 Outlook 에서 직접 확인해 주세요."}


def _read(entry_id: str) -> dict:
    _ol, ns = T.connect()
    it = T._item(ns, entry_id)
    body = T.clean_body(it.Body)   # 못 읽으면 여기서 오류가 난다(빈 글로 넘기지 않음)
    when = T._naive(T._get(it, "ReceivedTime"))
    parent = T._get(it, "Parent")
    return {"ok": True, "id": entry_id, "subject": T._get(it, "Subject", "") or "",
            "sender": T._get(it, "SenderName", "") or "", "addr": T._get(it, "SenderEmailAddress", "") or "",
            "to": T._get(it, "To", "") or "", "cc": T._get(it, "CC", "") or "",
            "time": when.strftime("%Y-%m-%d %H:%M") if when else "",
            "folder": T._rel(parent, len(T._stores(ns)) > 1) if parent is not None else "",
            "unread": bool(T._get(it, "UnRead", False)), "kind": T._kind(T._get(it, "MessageClass", "")),
            "attachments": [{"name": x["name"], "size": x["size"], "inline": x["inline"]} for x in T._attachments(it)],
            "body": body[:BODY_LIMIT], "body_total": len(body), "truncated": len(body) > BODY_LIMIT,
            "note": T.NOTE_EXTERNAL}


def _collect(args, words, in_body):
    """Outlook 개체는 이 함수 안에서만 쓰고 놓는다(CoUninitialize 전에 모두 풀리도록)."""
    _ol, ns = T.connect()
    return T.collect(ns, args, words, in_body)
