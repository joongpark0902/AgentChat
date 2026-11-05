r"""README 화면 사진을 다시 찍는다(지어낸 예시 자료·배포용 기본 담당자만 — 개인 설정·실제 대화·실제 사진은 쓰지 않는다).

실행: python tools/readme_shots.py [--out docs]
- tests 패키지가 설정 폴더를 임시 예시 설정(agents.example.yaml·settings.example.yaml)으로 돌려 둔다.
- 기록·개인 키는 임시 폴더. 작업 폴더 경로는 사진에 찍히므로 사용자 이름이 없는 C:\AgentChat\workspace 아래에 만든다.
- 메일 창은 tests/test_outlook 의 지어낸 편지함. 담당자는 가짜 응답기(CLI 호출 없음).
- 127.0.0.1:8797 에 임시 서버를 띄우고 끝나면 내린다(쓰고 있는 서버·설정·기록은 건드리지 않음).
"""
from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import tests  # noqa: E402,F401  (설정 폴더 → 임시 예시 설정)
from tests import live_mail_ui as L  # noqa: E402
from tests.test_flow import FakeRunner  # noqa: E402
from tools import outlook_tool as T  # noqa: E402

DEMO = Path(r"C:\AgentChat\workspace")
L.PORT, L.PHONE_PORT = 8797, 8796
L.BASE, L.PHONE = f"http://127.0.0.1:{L.PORT}", f"http://127.0.0.1:{L.PHONE_PORT}"
CTX = {"jake": (388_000, 1_000_000), "clara": (96_000, 1_000_000), "quinn": (186_000, 258_400)}
TASK = "첨부한 샘플 매출 자료로 월별·채널별 집계표를 엑셀로 만들어 주세요. 합계 행과 검증 셀도 넣어 주세요."
SCROLLER = "(() => { let e = document.querySelector('#messages'); while (e && e.scrollHeight <= e.clientHeight + 2) e = e.parentElement; return e; })()"


class ShotRunner(FakeRunner):
    """가짜 응답기 + 컨텍스트 창에 보일 세션 크기·답변 토큰."""
    async def run(self, prompt, workspace, schema_path=None, activity=None, approve=None, **kw):
        res = await super().run(prompt, workspace, schema_path, None, approve, **kw)   # '실행 중' 표시가 답 뒤에 남지 않게
        used, window = CTX.get(self.agent.key, (50_000, 1_000_000))
        self.context = {"used": used, "window": window, "updated": time.time()}
        res.usage = {"engine": "claude" if self.agent.key != "quinn" else "codex", "total_tokens": 437_000}
        return res


def rev(verdict, summary, issues=(), reqs=()):
    return {"verdict": verdict, "summary": summary, "checks": "원자료와 결과물 파일을 직접 열어 확인했습니다.",
            "issues": [{"severity": s, "description": d} for s, d in issues],
            "requirements": [{"item": i, "met": True, "evidence": e} for i, e in reqs]}


def make_files(ws: Path) -> None:
    from openpyxl import Workbook
    ws.mkdir(parents=True, exist_ok=True)
    rows = [("1월", 1200, 300), ("2월", 1350, 280), ("3월", 1500, 410), ("4월", 1420, 390), ("5월", 1610, 450), ("6월", 1700, 470)]
    (ws / "샘플_매출.csv").write_text("월,온라인,오프라인\n" + "\n".join(f"{m},{a},{b}" for m, a, b in rows), encoding="utf-8")
    # 사진 속 이야기와 같게: v1 은 합계 수식이 B2:B6(6월 빠짐) → 8,910천원, v2 는 B2:B7 → 11,080천원. 검증 셀 E8 은 같은 범위로 TRUE
    for v, end in (("v1", 6), ("v2", 7)):
        wb = Workbook()
        sh = wb.active
        sh.title = "월별 집계"
        sh.append(["월", "온라인(천원)", "오프라인(천원)", "합계(천원)", "검증"])
        for i, (m, a, b) in enumerate(rows, 2):
            sh.append([m, a, b, f"=B{i}+C{i}"])
        sh.append(["합계", f"=SUM(B2:B{end})", f"=SUM(C2:C{end})", f"=SUM(D2:D{end})", f"=D8=B8+C8"])
        wb.save(ws / f"매출집계_{v}.xlsx")
    recalc([ws / "매출집계_v1.xlsx", ws / "매출집계_v2.xlsx"])


def recalc(files) -> None:
    """수식 결과값을 파일에 남긴다(따로 띄운 Excel 로 다시 계산·저장 — 사장님이 열어 둔 Excel 과 섞이지 않게 DispatchEx)."""
    try:
        import win32com.client
    except ImportError:
        print("  (pywin32 없음 — 수식 결과값 없이 저장)")
        return
    xl = win32com.client.DispatchEx("Excel.Application")
    xl.Visible, xl.DisplayAlerts = False, False
    try:
        for f in files:
            wb = xl.Workbooks.Open(str(f))
            xl.CalculateFull()
            wb.Save()
            wb.Close(False)
    finally:
        xl.Quit()


SCRIPTS = {
    "jake": [
        "월별 매출 집계표를 만들었습니다.\n\n① **한 일** 첨부하신 `샘플_매출.csv`를 월·채널별로 집계하고 합계 행과 검증 셀을 넣었습니다.\n"
        "② **파일** `매출집계_v1.xlsx` (시트: 월별 집계)\n③ **확인** 합계 8,910천원, 검증 셀 확인",
        "Eric 지적 1건을 고쳤습니다.\n\n- 합계 수식 범위에서 6월 행이 빠진 것 → **수정함** (B2:B6 → B2:B7)\n\n"
        "원본은 그대로 두고 `매출집계_v2.xlsx`로 저장했습니다. 다시 계산한 합계는 11,080천원이고 검증 셀은 TRUE입니다.",
        "거래처 잔액 조회서 회신 3건을 장부와 대사했습니다. 2건은 일치하고 1건은 120천원 차이가 납니다.\n\n"
        "**확인 요청**\n1. 차이 120천원(A사 매출채권)을 회사에 소명 요청할까요, 기말 조정 후보로 둘까요?",
        "견본 계약서 2건(지어낸 예시)의 조항을 비교했습니다. 대금 지급 조건과 계약 기간이 다릅니다.\n\n"
        "**확인 요청**\n1. 비교표를 엑셀로 만들까요, 보고서 한 장으로 요약할까요?",
    ],
    "clara": [rev("APPROVE", "지시하신 항목은 결과물 파일에서 모두 확인했습니다.",
                  reqs=[("월별·채널별 집계표 작성", "매출집계_v1.xlsx › 월별 집계 A1:D7"), ("합계 행 추가", "월별 집계 A8:D8"),
                        ("검증 셀 추가", "월별 집계 E8")]),
              rev("APPROVE", "v2 에서도 지시 항목이 모두 충족됩니다. 원본 v1 은 그대로 있습니다.",
                  reqs=[("합계 범위 수정 반영", "매출집계_v2.xlsx › 월별 집계 B8:D8"), ("원본 보존(새 버전 저장)", "v1·v2 파일 모두 존재")])],
    "quinn": [rev("REVISE", "원자료에서 직접 다시 합산하니 합계가 2,170천원 다릅니다.",
                  issues=[("blocker", "합계 행 수식이 B2:B6 까지만 더해 6월(1,700 / 470)이 빠졌습니다. 원자료 합계 11,080천원, 파일 8,910천원.")]),
              rev("APPROVE", "합계 11,080천원이 원자료 재계산 값과 원 단위까지 일치합니다. 검증 셀 TRUE."),
              "v2 합계 11,080천원 다시 확인했습니다. 원자료와 일치합니다."],
}


def main(out: Path) -> None:
    from playwright.sync_api import sync_playwright
    tmp = Path(tempfile.mkdtemp(prefix="agentchat_shots_"))
    DEMO.mkdir(parents=True, exist_ok=True)
    import app.config as appconfig     # 배포용 기본 사진만 임시 설정 폴더로(실제 인물 기반 local_* 는 쓰지 않음)
    av = appconfig.CONFIG_DIR / "avatars"
    av.mkdir(exist_ok=True)
    for name in ("jake_320.jpg", "clara_320.jpg", "quinn_320.jpg"):
        shutil.copy(ROOT / "config" / "avatars" / name, av / name)
    L.FakeRunner = ShotRunner          # 임시 서버가 쓰는 응답기
    FakeRunner.scripts = {k: list(v) for k, v in SCRIPTS.items()}
    FakeRunner.calls = []
    server, th = L.start_server(tmp)
    case = L.fake_mailbox()
    T.connect = lambda: (case.app, case.ns)
    shots = []

    def shot(page, name):
        page.mouse.move(5, 630)
        time.sleep(0.6)
        page.screenshot(path=str(out / name), type="jpeg", quality=88)
        from PIL import Image                     # 브라우저가 붙이는 색 프로필(ICC)까지 빼고 다시 저장 — 메타데이터 없는 사진
        with Image.open(out / name) as im:
            clean = im.convert("RGB")
        clean.save(out / name, "JPEG", quality=88)
        shots.append(name)
        print("  사진:", name)

    try:
        ws = DEMO / "월별매출"
        ws.mkdir(parents=True, exist_ok=True)
        conv = L.api("/api/conversations", {"title": "월별 매출 집계표", "workspace": str(ws)})["id"]
        time.sleep(1.1)
        make_files(ws)   # 대화를 연 뒤에 만들어야 파일 패널의 '산출물'(이 대화에서 생긴 파일)에 세 파일이 모두 잡힌다
        with sync_playwright() as p:
            browser = p.chromium.launch(channel="chrome", headless=True)
            page = browser.new_page(viewport={"width": 1536, "height": 640})
            page.goto(f"{L.BASE}/#conv={conv}")
            page.wait_for_selector("#input", timeout=15000)
            page.evaluate("S.sendDelay = 0")
            page.fill("#input", TASK)
            page.click("#btnSend")
            page.wait_for_function("document.body.innerText.includes('모두 APPROVE')", timeout=60000)
            page.click("#btnFiles")
            page.wait_for_timeout(1200)
            page.click("text=매출집계_v2.xlsx")
            page.wait_for_timeout(1500)
            page.evaluate(f"{SCROLLER}.scrollTop = 0")
            shot(page, "screenshot-1-chat.jpg")
            page.evaluate(f"(() => {{ const s = {SCROLLER}; s.scrollTop = s.scrollHeight * 0.42; }})()")
            shot(page, "screenshot-2-review.jpg")
            page.click(".ctx-chip")
            page.wait_for_selector(".ctx-modal")
            shot(page, "screenshot-3-context.jpg")
            page.keyboard.press("Escape")
            page.evaluate(f"{SCROLLER}.scrollTop = 1e9")
            page.evaluate("S.sendDelay = 10")
            page.fill("#input", "@Eric v2 합계만 한 번 더 확인해줘")
            page.click("#btnSend")
            page.wait_for_timeout(700)
            shot(page, "screenshot-4-hold.jpg")
            page.wait_for_timeout(11000)            # 대기 끝나고 실제로 보내져 답이 오도록
            page.evaluate("S.sendDelay = 0")

            # 결정 대기함 + 프로젝트: 지어낸 실사 건 두 대화
            ids = []
            for title, text in (("거래처 조회서 대사", "@Jake 조회서 회신 3건 장부랑 대사해줘"),
                                ("견본 계약서 비교", "@Jake 견본 계약서 두 건 조항 비교해줘")):
                ws2 = DEMO / title.replace(" ", "_")
                ws2.mkdir(parents=True, exist_ok=True)
                c = L.api("/api/conversations", {"title": title, "workspace": str(ws2)})["id"]
                ids.append(c)
                page.goto(f"{L.BASE}/?v={c}#conv={c}")   # 같은 페이지 안의 해시 이동은 대화가 바뀌지 않아 새로 연다
                page.wait_for_selector("#input")
                page.wait_for_timeout(800)
                page.evaluate("S.sendDelay = 0")
                page.fill("#input", text)
                page.click("#btnSend")
                page.wait_for_function("document.querySelector('#messages').innerText.includes('확인 요청')", timeout=30000)
                page.wait_for_timeout(1500)
            L.api("/api/projects", {"name": "예시사 실사", "memo": "기준일 2025-12-31, 금액 단위 원", "conv_ids": ids + [conv]})
            page.goto(f"{L.BASE}/?v=p#conv={ids[0]}")
            page.wait_for_selector("#input")
            page.wait_for_timeout(1200)
            shot(page, "screenshot-5-project.jpg")
            page.click("#btnInbox")
            page.wait_for_selector(".inbox-item")
            shot(page, "screenshot-6-inbox.jpg")
            page.keyboard.press("Escape")

            page.goto(f"{L.BASE}/?v=m#conv={conv}")
            page.wait_for_selector("#btnMail:not(.hidden)", timeout=15000)
            page.click("#btnMail")
            page.wait_for_selector(".mail-row")
            page.wait_for_timeout(800)
            shot(page, "screenshot-7-mail.jpg")
            browser.close()
    finally:
        server.should_exit = True
        th.join(5)
        shutil.rmtree(tmp, ignore_errors=True)
    print("찍은 사진", len(shots), "장 →", out)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "docs"))
    main(Path(ap.parse_args().out))
