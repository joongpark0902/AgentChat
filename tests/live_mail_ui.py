"""✉ 메일 창을 실제 브라우저로 띄워 확인한다(자동 테스트 아님 — 손으로 돌리는 확인용).

실행: python tests/live_mail_ui.py [--real] [--out 폴더]
- 쓰고 있는 서버·설정·로그는 건드리지 않는다: 임시 폴더의 예시 설정으로 127.0.0.1:8799 에 서버를 하나 더 띄우고,
  담당자는 가짜 응답기(CLI 호출 없음)로 돌린 뒤 끝나면 내린다.
- 기본: 지어낸 가짜 편지함으로 화면을 그려 사진을 남긴다(사진에 실제 메일이 들어가지 않게).
- --real: 실제 Outlook 에 붙어 화면의 건수·ID 를 메일을 한 통씩 직접 센 값과 대사한다(사진은 찍지 않고 숫자만 낸다).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import tests  # noqa: E402,F401  (설정 폴더를 임시 예시 설정으로 돌려 둔다)
from app import mail  # noqa: E402
from app.orchestrator import Manager  # noqa: E402
from tests import test_outlook as TO  # noqa: E402
from tests.test_flow import FakeRunner, review  # noqa: E402
from tools import outlook_tool as T  # noqa: E402

PORT, PHONE_PORT = 8799, 8798       # 쓰고 있는 서버(8780·8790)와 겹치지 않는 포트
BASE = f"http://127.0.0.1:{PORT}"
PHONE = f"http://127.0.0.1:{PHONE_PORT}"   # 폰 전용 포트 흉내(로그인 필요, PC 전용 기능 막힘)


def start_server(tmp: Path):
    import uvicorn
    import yaml
    import app.config as appconfig
    from app.server import _listen, create_app

    cfg_file = appconfig.CONFIG_DIR / "settings.yaml"   # tests 가 만든 임시 예시 설정(개인 설정 아님)
    st = yaml.safe_load(cfg_file.read_text(encoding="utf-8")) or {}
    st["remote_port"] = PHONE_PORT
    cfg_file.write_text(yaml.safe_dump(st, allow_unicode=True, sort_keys=False), encoding="utf-8")

    def factory(cfg, bc):
        cfg.settings.log_dir = tmp / "logs"
        cfg.settings.workspace_root = tmp / "ws"
        cfg.settings.flow = "classic"
        return Manager(cfg, bc, runner_factory=FakeRunner)

    server = uvicorn.Server(uvicorn.Config(create_app(factory, private_dir=tmp / "private"),
                                           log_level="warning", timeout_graceful_shutdown=3))
    socks = [_listen("127.0.0.1", PORT), _listen("127.0.0.1", PHONE_PORT)]
    th = threading.Thread(target=lambda: server.run(sockets=socks), daemon=True)
    th.start()
    for _ in range(100):
        if server.started:
            return server, th
        time.sleep(0.1)
    raise RuntimeError("임시 서버가 뜨지 않았습니다")


def api(path: str, data: dict | None = None) -> dict:
    req = urllib.request.Request(BASE + path, data=json.dumps(data).encode() if data is not None else None,
                                 headers={"Content-Type": "application/json"} if data is not None else {})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read().decode("utf-8"))


def fake_mailbox():
    """test_outlook 의 지어낸 편지함 + 목록이 30건을 넘도록 40통 추가, 1통은 본문을 못 읽게."""
    case = TO.ScopeAndList("test_folders_totals")
    case.setUp()
    now = datetime.now().replace(second=0, microsecond=0)
    for i, m in enumerate((case.m_ndr, case.m_client, case.m_sub, case.m_next, case.m_day1, case.m_day0, case.m_old)):
        m.ReceivedTime = now - timedelta(hours=3 + i * 5)
    for i in range(40):
        it = TO.FakeItem(f"월간 보고 {i + 1:02d}호", sender="박마바", addr="park@maba.example",
                         when=now - timedelta(days=8 + i), body="정기 보고입니다")
        case.inbox.Items.append(it)
        it.Parent = case.inbox
        case.ns.by_id[it.EntryID] = it
    case.ns.broken = {case.m_old.EntryID}
    return case


def shoot(page, out: Path, name: str) -> None:
    page.screenshot(path=str(out / name))
    print("  사진:", name)


def run_fake(out: Path) -> bool:
    from playwright.sync_api import sync_playwright

    case = fake_mailbox()
    T.connect = lambda: (case.app, case.ns)
    FakeRunner.scripts = {"jake": ["메일 2통을 읽었습니다."], "clara": [review("APPROVE")], "quinn": [review("APPROVE")]}
    FakeRunner.calls = []
    conv = api("/api/conversations", {})["id"]
    ok, errors, failed = True, [], []

    def check(label, cond):
        nonlocal ok
        ok &= bool(cond)
        print(f"  [{'OK' if cond else '실패'}] {label}")

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 860})
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" and "Failed to load resource" not in m.text else None)
        page.on("response", lambda r: failed.append(f"{r.status} {urllib.parse.unquote(r.url.replace(BASE, ''))}") if r.status >= 400 else None)
        page.goto(f"{BASE}/#conv={conv}")
        page.wait_for_selector("#btnMail:not(.hidden)", timeout=10000)
        check("PC 화면에 ✉ 버튼이 보임", page.is_visible("#btnMail"))
        shoot(page, out, "1_입력창_메일버튼.png")

        page.click("#btnMail")
        page.wait_for_selector(".mail-row")
        strong = page.inner_text(".mail-line.strong")
        check(f"처음 목록은 최근 7일: '{strong}'", strong.startswith("전체 7건 중 7건 표시"))
        info = page.inner_text(".mail-info")
        check("조회 범위·뺀 폴더·외부 글 알림 표시", all(s in info for s in ("조회 범위: 받은 메일 전체", "뺀 폴더:", "보낸 편지함 1건", "외부인이 쓴 글")))
        shoot(page, out, "2_메일창_최근7일.png")

        page.select_option(".mail-bar select", "0")
        page.wait_for_function("document.querySelector('.mail-line.strong')?.textContent.includes('47건')")
        strong = page.inner_text(".mail-line.strong")
        check(f"전체 기간: '{strong}'", "전체 47건 중 30건 표시" in strong and "좁혀서" in strong)
        check("목록 30줄", page.locator(".mail-list .mail-row").count() == 30)
        shoot(page, out, "3_메일창_전체기간_30건제한.png")

        page.fill(".mail-bar input[type=text]", "trial")
        page.check(".mail-opt input")
        page.wait_for_selector(".mail-sep")
        info = page.inner_text(".mail-info")
        check("본문을 못 읽은 메일은 경고 + 미확인 목록", "검색이 불완전합니다 — 1통" in info and "확인된 메일 0건, 미확인 1통" in info
              and page.locator(".mail-list .mail-row").count() == 1 and "미확인" in page.inner_text(".mail-list"))
        shoot(page, out, "4_메일창_본문검색_미확인경고.png")

        page.uncheck(".mail-opt input")
        page.fill(".mail-bar input[type=text]", "가나상사")
        page.press(".mail-bar input[type=text]", "Enter")
        page.wait_for_function("document.querySelector('.mail-line.strong')?.textContent.includes('2건')")
        rows = page.locator(".mail-list .mail-row")
        rows.nth(0).click()
        rows.nth(1).click()
        check("누른 메일 2통이 '붙임'으로 표시", page.locator(".mail-row.picked").count() == 2 and "2통" in page.inner_text(".sheet-foot .note"))
        rows.nth(1).click()
        check("다시 누르면 빠짐", page.locator(".mail-row.picked").count() == 1)
        rows.nth(1).click()
        shoot(page, out, "5_메일창_검색_2통고름.png")

        rows.nth(1).locator(".m-view").click()        # '본문' → 목록 자리에 그 한 통을 펼친다
        page.wait_for_selector(".mv-body")
        seen = page.inner_text(".mail-view")
        check("본문 보기: 제목·보낸이·첨부 이름·본문·글자 수가 보임", all(x in seen for x in (
            "가나상사 실사 자료", "김가나 <kim@gana.example>", "자료.xlsx", "본문 삽입 그림", "조회서 첨부", "본문 6자 전부 표시", "외부인이 쓴 글")))
        check("본문 보기에서도 붙임 상태가 보이고 목록은 가려짐", "지시에서 빼기" in page.inner_text(".mv-top") and not page.is_visible(".mail-list"))
        shoot(page, out, "9_본문보기.png")
        page.locator(".mv-top .btn").first.click()
        check("← 목록으로 돌아오면 고른 2통이 그대로", page.is_visible(".mail-list") and page.locator(".mail-row.picked").count() == 2
              and not page.is_visible(".mail-view"))
        page.click(".sheet-foot .btn")
        check("창을 닫으면 입력창 위에 메일 칩 2개", page.locator("#chips .chip.mail").count() == 2)
        check("글 없이도 보내기 버튼이 켜짐", page.is_enabled("#btnSend"))

        page.fill("#input", "이 메일들 읽고 요약해 주세요")
        page.press("#input", "Enter")
        page.wait_for_selector("#holdBar:not(.hidden)")
        hold = page.inner_text("#holdBar")
        check("보내기 대기 줄에는 내가 쓴 글만(긴 ID 없음)", "이 메일들 읽고 요약해 주세요" in hold and "ID=" not in hold)
        shoot(page, out, "6_보내기대기_칩.png")
        page.keyboard.press("Escape")
        check("Esc 로 되돌리면 글과 메일 칩이 그대로 돌아옴",
              page.input_value("#input") == "이 메일들 읽고 요약해 주세요" and page.locator("#chips .chip.mail").count() == 2)
        page.press("#input", "Enter")
        page.press("#input", "Enter")
        page.wait_for_selector(".row.out .attach")
        page.wait_for_function("document.querySelectorAll('.row.in').length >= 1", timeout=15000)
        bubble = page.inner_text(".row.out .bubble")
        check("말풍선은 글 + 메일 제목 칩(긴 ID·머리말은 숨김)", "이 메일들 읽고 요약해 주세요" in bubble and "ID=" not in bubble
              and "첨부 메일 —" not in bubble and page.locator(".row.out .attach").count() == 2)
        check("보낸 뒤 칩이 비워짐", page.locator("#chips .chip").count() == 0)
        time.sleep(1.0)
        shoot(page, out, "7_보낸뒤_말풍선.png")

        # '새 메일 요약' 버튼: 아직 요약하지 않은 최근 24시간 메일(가짜 편지함에서 5통)을 지금 요약 — 매일 아침 예약과 같은 일
        FakeRunner.scripts["jake"] = ["조치 필요 1통 · 참고 4통\n" + "\n".join(
            f"[{k}] 김가나 — 제목 {k}\n핵심 내용 한 줄" + ("\n→ 조치: 회신(10/8)" if k == 2 else "") for k in range(1, 6))]
        page.on("dialog", lambda dlg: dlg.accept())
        page.click("#btnMail")
        page.wait_for_selector(".mail-row")
        page.click(".mail-bar .btn.gray")
        page.wait_for_function("document.body.innerText.includes('메일 요약 결과: 대상 5통 중 5통 요약.')", timeout=20000)
        check("'새 메일 요약' → 새 대화가 열리고 대상 5통이 칩으로 보임", page.locator(".row.out .attach").count() == 5
              and "ID=" not in page.inner_text(".row.out .bubble"))
        check("요약 대화 제목과 결과 안내", f"메일 요약 {datetime.now().month}/{datetime.now().day}" in page.inner_text("body")
              and "[2] 김가나 — 제목 2" in page.inner_text("#messages"))
        digest_prompt = FakeRunner.calls[-1][1]
        check("요약 지시문에 '읽고 요약만'과 메일 5통의 ID 가 들어감", "읽고 요약만 합니다" in digest_prompt
              and len(re.findall(r"ID=[0-9A-F]+", digest_prompt)) == 5)
        time.sleep(0.5)
        shoot(page, out, "10_새메일요약_대화.png")

        # 폰 접속: 로그인해도 ✉ 버튼이 없고 목록 요청은 막힌다. 다만 지시에 붙인 메일의 제목은 대화 내용이라 폰에서도 보인다.
        api("/api/auth/password", {"password": "phone-pass-1"})
        phone = browser.new_page(viewport={"width": 390, "height": 844})
        phone.goto(PHONE + "/")
        phone.fill("#pw", "phone-pass-1")
        phone.click("#go")
        phone.wait_for_selector("#input", timeout=10000)
        phone.goto(f"{PHONE}/#conv={conv}")
        phone.reload()
        phone.wait_for_selector(".row.out .bubble", timeout=10000)
        check("폰 화면에는 ✉ 버튼이 없음", not phone.is_visible("#btnMail") and phone.is_visible("#btnAttach"))
        status = phone.evaluate("fetch('/api/mail?days=0').then(r => r.status)")
        check(f"폰에서 메일 목록 요청은 거절(HTTP {status})", status == 403)
        status = phone.evaluate(f"fetch('/api/mail/body?id={case.m_sub.EntryID}').then(r => r.status)")
        check(f"폰에서 본문 요청도 거절(HTTP {status})", status == 403)
        seen = phone.locator(".row.out .attach").count()
        check(f"(알림) 지시에 붙인 메일 제목 칩 {seen}개는 대화 내용이라 폰에서도 보임", seen == 2)
        shoot(phone, out, "8_폰화면_버튼없음.png")
        browser.close()

    sent = FakeRunner.calls[0][1] if FakeRunner.calls else ""
    want = [case.m_client.EntryID, case.m_sub.EntryID]
    check("실무자에게 간 지시문에 머리말과 두 메일의 ID 가 들어감",
          "[첨부 메일 — Outlook 에서 고른 메일입니다." in sent and re.findall(r"ID=([0-9A-F]+)", sent) == want)
    check("화면 스크립트 오류 없음" + (f": {errors[:3]}" if errors else ""), not errors)
    check("메일 관련 요청 실패 없음", not any("/api/mail" in f for f in failed))
    if failed:
        print("  (참고) 실패한 다른 요청:", sorted(set(failed)))
    untouched = all(not it.calls and not it.saved for it in case.ns.by_id.values())
    check("가짜 편지함에 저장·보내기·지우기·옮기기 호출 없음", untouched)
    case.doCleanups()
    return ok


def run_real(tmp: Path, digest) -> bool:
    """실제 Outlook: 화면이 받는 값(/api/mail)을 메일을 한 통씩 직접 센 기준과 대사한다. 메일 내용은 출력하지 않는다."""
    import pythoncom
    import win32com.client
    from playwright.sync_api import sync_playwright

    pythoncom.CoInitialize()
    ns = win32com.client.GetActiveObject("Outlook.Application").GetNamespace("MAPI")

    def snap():
        d = {}

        def w(f):
            if f.DefaultItemType == 0:
                d[f.EntryID] = (f.Items.Count, f.UnReadItemCount)
            for s in f.Folders:
                w(s)
        w(ns.DefaultStore.GetRootFolder())
        return hashlib.sha256(json.dumps(sorted(d.items())).encode()).hexdigest()[:16]

    before = snap()
    skip = set()

    def sub(f):
        skip.add(f.EntryID)
        for s in f.Folders:
            sub(s)
    for i in (3, 4, 5, 16, 23):
        sub(ns.GetDefaultFolder(i))
    base = {}

    def walk(f):
        if f.DefaultItemType == 0 and f.EntryID not in skip:
            for it in f.Items:
                def g(a):
                    try:
                        return getattr(it, a)
                    except Exception:
                        return None
                rt = g("ReceivedTime")
                base[it.EntryID] = (datetime(rt.year, rt.month, rt.day, rt.hour, rt.minute, rt.second) if rt else None,
                                    " ".join(str(g(a) or "") for a in ("Subject", "SenderName", "SenderEmailAddress", "To")).lower(),
                                    (g("Body") or "").lower())
        for s in f.Folders:
            walk(s)
    walk(ns.DefaultStore.GetRootFolder())
    print(f"  독립 기준(메일을 한 통씩 직접 순회): 수신 범위 {len(base):,}건")
    midnight = datetime.combine(datetime.now().date(), datetime.min.time())
    ok = True
    cases = [(7, "", 0), (30, "", 0), (0, "", 0), (0, "조회서", 0), (30, "자료", 0), (0, "100%", 1), (0, "_", 1), (90, "감사 보고서", 1)]
    for days, q, body in cases:
        d = api(f"/api/mail?days={days}&body={body}&q={urllib.parse.quote(q)}")
        words = q.lower().split()
        since = midnight - timedelta(days=days) if days else None
        hit = [(t, k) for k, (t, meta, text) in base.items()
               if (since is None or (t is not None and t >= since))
               and all(w in meta or (body and w in text) for w in words)]
        timed = sorted((x for x in hit if x[0] is not None), reverse=True)
        want30 = [k for _, k in timed[:30]]
        got = [r["id"] for r in d["rows"]]
        same = (d["ok"] and d["total"] == len(hit) and len(got) == len(set(got)) == min(30, len(hit))
                and (got == want30 or set(got) == set(want30)) and d["unverified_total"] == 0 and not d["mismatch"])
        order = [r["time"] for r in d["rows"]] == sorted((r["time"] for r in d["rows"]), reverse=True)
        ok &= same and order
        print(f"  [{'OK' if same and order else '실패'}] 기간 {days or '전체'} · 검색 {q!r}{' +본문' if body else ''}: "
              f"기준 {len(hit):,}건 / 화면값 {d['total']:,}건, 표시 {len(got)}건, 표시 ID 일치 {got == want30}"
              f"{'' if got == want30 else '(같은 시각 메일의 순서만 다름)' if set(got) == set(want30) else ''}, 최신순 {order}, 미확인 {d['unverified_total']}")
    # 본문 보기: 최근 7일 메일을 최대 15통 열어, 화면이 받는 본문을 Outlook 에서 직접 읽은 본문과 대조(내용은 출력하지 않음)
    opened = same_body = unread_kept = 0
    for r in api("/api/mail?days=7")["rows"][:15]:
        it = ns.GetItemFromID(r["id"])
        was = it.UnRead
        raw = (it.Body or "").replace("\r\n", "\n").replace("\r", "\n")
        want = re.sub(r"\n{3,}", "\n\n", raw).strip().replace("<<<메일 원문", "<<메일 원문")
        d = api(f"/api/mail/body?id={r['id']}")
        opened += 1
        same_body += bool(d["ok"] and d["body"] == want[:20000] and d["body_total"] == len(want)
                          and len(d["attachments"]) == it.Attachments.Count and d["subject"] == (it.Subject or ""))
        unread_kept += ns.GetItemFromID(r["id"]).UnRead == was
    body_ok = opened > 0 and same_body == opened == unread_kept
    ok &= body_ok
    print(f"  [{'OK' if body_ok else '실패'}] 본문 보기 {opened}통: 본문·글자 수·첨부 개수·제목 일치 {same_body}통, 안읽음 표시 그대로 {unread_kept}통")
    # 매일 아침 요약의 대상 뽑기(건수 제한 없음)를 기준과 대사. 이 스레드에서 직접 불러, 들고 있는 Outlook 참조가 끊기지 않는지도 본다
    now = datetime.now().replace(microsecond=0)
    for label, since in (("24시간", now - timedelta(hours=24)), ("7일", now - timedelta(days=7)), ("30일", now - timedelta(days=30)),
                         ("90일", now - timedelta(days=90))):
        d = mail.collect_since(since)
        want = {k for k, (t, _, _) in base.items() if t is not None and t >= since}
        got = [r["id"] for r in d["rows"]]
        times = [r["time"] for r in d["rows"]]
        same = d["ok"] and set(got) == want and len(got) == len(set(got)) and times == sorted(times, reverse=True) and not d["mismatch"]
        ok &= same
        print(f"  [{'OK' if same else '실패'}] 요약 대상 조회 최근 {label}: 기준 {len(want):,}건 / 조회 {len(got):,}건, ID 집합 일치 {set(got) == want}, 중복 {len(got) - len(set(got))}")
    want = sorted((k for k, (t, _, _) in base.items() if t is not None and t >= now - timedelta(hours=24)), key=lambda k: (base[k][0], k), reverse=True)
    FakeRunner.calls = []
    FakeRunner.scripts = {"jake": ["\n".join(f"[{k}] 보낸이 — 제목\n핵심 내용입니다" for k in range(i + 1, min(i + 30, len(want)) + 1))
                                   for i in range(0, min(len(want), 120), 30)]}
    d = api("/api/mail/digest", {})
    for _ in range(240):                      # 요약이 끝날 때까지(실행 잠금이 풀릴 때까지) 기다린 뒤 상태 파일을 한 번만 읽는다
        time.sleep(0.25)
        if not digest.lock.locked():
            break
    state_file = tmp / "logs" / "mail_digest.json"
    seen = json.loads(state_file.read_text(encoding="utf-8")).get("seen", {}) if state_file.exists() else {}
    sent = [i for _, p, _ in FakeRunner.calls for i in re.findall(r"ID=([0-9A-F]+)$", p, re.M)]
    dg = d.get("ok") and d.get("total") == len(want) and sent == want[:120] and set(seen) == set(want[:120])
    ok &= bool(dg)
    print(f"  [{'OK' if dg else '실패'}] '새 메일 요약' 1회(실제 편지함·가짜 응답기, 실제 Claude 호출 없음): 기준 {len(want)}통 / 대상 {d.get('total')}통, "
          f"지시문의 ID·순서 일치 {sent == want[:120]}, 요약 기록 {len(seen)}통")
    seq = [api("/api/mail?days=7")["total"] for _ in range(10)]
    res, errs = [], []

    def work():
        try:
            res.append(api("/api/mail?days=7")["total"])
        except Exception as e:  # noqa: BLE001
            errs.append(type(e).__name__)
    ts = [threading.Thread(target=work) for _ in range(8)]
    [t.start() for t in ts]
    [t.join(120) for t in ts]
    stable = len(set(seq + res)) == 1 and not errs and len(res) == 8
    ok &= stable
    print(f"  [{'OK' if stable else '실패'}] 실제 Outlook 연속 10회·동시 8회 조회: 결과 {sorted(set(seq + res))}, 오류 {errs}")

    conv = api("/api/conversations", {})["id"]
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 860})
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(f"{BASE}/#conv={conv}")
        page.wait_for_selector("#btnMail:not(.hidden)", timeout=10000)
        page.click("#btnMail")
        page.wait_for_selector(".mail-line.strong", timeout=60000)
        strong = page.inner_text(".mail-line.strong")
        n_rows = page.locator(".mail-list .mail-row").count()
        d = api("/api/mail?days=7")
        dom = strong.startswith(f"전체 {d['total']:,}건 중 {len(d['rows']):,}건 표시") and n_rows == len(d["rows"]) and not errors
        ok &= dom
        print(f"  [{'OK' if dom else '실패'}] 실제 Outlook 으로 그린 화면: '{strong.split(' —')[0]}', 목록 {n_rows}줄, 스크립트 오류 {len(errors)}건")
        browser.close()
    after = snap()
    ok &= before == after
    print(f"  [{'OK' if before == after else '실패'}] 편지함 폴더별 건수·안읽음 스냅샷: 전 {before} / 후 {after}")
    del ns
    pythoncom.CoUninitialize()
    return ok


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--real", action="store_true", help="실제 Outlook 과 숫자 대사(사진 없음)")
    ap.add_argument("--out", default=str(ROOT / "_화면확인_메일버튼"))
    a = ap.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    tmp = Path(tempfile.mkdtemp(prefix="agentchat_mail_ui_"))
    real_connect = T.connect
    server, th = start_server(tmp)
    print(f"임시 서버: {BASE} (설정·로그·작업 폴더: {tmp}) · 메일 기능 사용 가능: {mail.available()}")
    try:
        if a.real:
            ok = run_real(tmp, server.config.app.state.digest())
        else:
            out = Path(a.out)
            out.mkdir(parents=True, exist_ok=True)
            ok = run_fake(out)
            print("사진 폴더:", out)
    finally:
        T.connect = real_connect
        server.should_exit = True
        th.join(20)
        made = sorted(p.name for p in (tmp / "logs").glob("*")) if (tmp / "logs").exists() else []
        print(f"임시 서버 내림: {not th.is_alive()} · 임시 로그 폴더에 생긴 파일 {len(made)}개(쓰던 logs 폴더와 별개)")
        shutil.rmtree(tmp, ignore_errors=True)
    print("결과:", "모두 통과" if ok else "실패 있음")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
