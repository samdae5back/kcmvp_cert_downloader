#!/usr/bin/env python3
r"""
KCMVP 인증서 다운로더 — Windows 단독 실행용 GUI.
made by Myungjun Kim · © 2026 Myungjun Kim. All rights reserved. · https://github.com/samdae5back

crypto_manage 웹앱의 "인증서 받기"(CAVP) · "엔트로피 인증서 받기" 기능
(scripts/kcmvp_sample_runner.py 의 download_row_certificate / run_entropy_cert_job 흐름)을
러너·웹서버·데이터 디렉터리 없이 혼자 돌게 떼어낸 것이다.
사이트 셀렉터, 전자서명 응답 캡처(XHR 후킹), 세션 유지 규칙은 러너와 같다 —
러너 쪽 셀렉터를 고치면 여기도 맞춰야 한다.

동작
  1. 계정·비밀번호, 인증서 종류(CAVP / 엔트로피), 번호 범위를 넣고 [인증서 받기]
     → 크로미움(헤드리스)으로 로그인
  2. CAVP:     자가검증 목록 CAVP 탭을 페이지별로 돌며 번호를 찾아 "결과 확인 → 전자서명"
     엔트로피: Entropy 탭을 돌며 번호를 찾아 "세부결과 → 전자 서명"
  3. 응답 PDF 저장
       CAVP     <저장 폴더>\cavp\CAVP_<번호>.pdf
       엔트로피 <저장 폴더>\entropy\<번호>_<환경명>.pdf   (환경명 = 목록의 OS 열)
     (웹앱의 CAVP 인증서 이름 '환경_비트_아키텍처_번호' 는 웹앱의 운영환경 매핑에서 오는
      정보라 단독 도구에서는 쓸 수 없다 → 번호로만 이름을 짓는다)
  4. 작업 중에 범위를 더 넣으면(종류 무관) 예약 큐에 쌓여 지금 작업이 끝나는 대로 이어서 받는다
  5. 큐가 비면 브라우저를 닫는다. 다시 [인증서 받기]를 누르면 새로 로그인한다.

비밀번호는 메모리에만 두고 어디에도 저장하지 않는다(설정 파일에는 계정 ID·저장 폴더·종류만).

개발용 실행:   python kcmvp_cert_downloader.py
Windows 빌드:  build_windows.bat  (README.md 참고)
"""

from __future__ import annotations

import base64
import json
import logging
import os
import queue
import re
import subprocess
import sys
import threading
import time
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

APP_NAME = "KCMVP 인증서 다운로더"
CREDIT = "made by Myungjun Kim"
COPYRIGHT = "© 2026 Myungjun Kim. All rights reserved."
GITHUB_URL = "https://github.com/samdae5back"   # 예: "https://github.com/<아이디>" 또는 저장소 주소 — 비워 두면 링크를 표시하지 않는다
APP_VERSION = "1.1.0"
# 창 제목·오른쪽 아래 푸터에 표시한다. exe 파일 속성(자세히 탭)의 회사·저작권은 version_info.txt.
FROZEN = bool(getattr(sys, "frozen", False))
APP_DIR = (Path(sys.executable) if FROZEN else Path(__file__)).resolve().parent
LOG_DIR = APP_DIR / "logs"
HTML_DUMP_DIR = LOG_DIR / "html"
CONFIG_FILE = APP_DIR / "kcmvp_cert_downloader.json"
DEFAULT_SAVE_DIR = APP_DIR / "certificates"

# PyInstaller 로 묶인 exe 는 옆의 browsers\ 폴더에 든 크로미움을 쓴다(build_windows.bat 가
# 거기에 설치한다). playwright 를 import 하기 *전에* 정해야 반영된다.
if FROZEN and not os.environ.get("PLAYWRIGHT_BROWSERS_PATH"):
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(APP_DIR / "browsers")

from playwright.sync_api import (  # noqa: E402  (환경변수 설정 뒤에 import 해야 한다)
    Error as PlaywrightError,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
except ImportError:  # tkinter 없는 리눅스 서버에서 워커만 import 해 테스트할 때
    tk = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# 설정 (러너와 같은 환경변수 이름 — 보통은 손댈 일 없다)
# ---------------------------------------------------------------------------
def env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        return default


BASE_URL = os.getenv("KCMVP_BASE_URL", "https://www.kcmvp.or.kr:8084/")
LIST_URL = BASE_URL.rstrip("/") + "/self/verification/list.do"
# 인증서는 브라우저가 PDF를 그린 뒤 이 API로 올려 서명받는다 — 응답 JSON의 result가 base64 PDF.
# CAVP 인증서와 엔트로피 인증서가 같은 API를 쓴다.
SIGN_API_PATH = "/self/verification/api/getElectroSign.do"
# 사이트의 세션 유지 tick(delaySessionExpire()가 치는 것과 같은 요청). 세션은 600초.
SESSION_RESET_PATH = "/user/api/sessionReset.do"
SESSION_KEEPALIVE_SECONDS = env_int("KCMVP_SESSION_KEEPALIVE_SECONDS", 120)
CERT_SIGN_TIMEOUT = env_int("KCMVP_CERT_SIGN_TIMEOUT", 20 * 60)
CERT_DIALOG_TIMEOUT = env_int("KCMVP_CERT_DIALOG_TIMEOUT", 120)
# CAVP 그리드(서버 페이징)를 몇 페이지까지 훑을지 — 페이지 링크가 없으면 그 전에 멈춘다.
CHECK_PAGE_LIMIT = env_int("KCMVP_CHECK_PAGE_LIMIT", 20)
# 한 번에 예약할 수 있는 번호 개수(오타로 1~100000 같은 범위를 넣는 걸 막는다)
MAX_RANGE = 500

KIND_LABELS = {"cavp": "CAVP 인증서", "entropy": "엔트로피 인증서"}
KIND_SUBDIRS = {"cavp": "cavp", "entropy": "entropy"}


@dataclass(frozen=True)
class Selectors:
    """KCMVP 사이트 마크업 기준 — scripts/kcmvp_sample_runner.py 의 Selectors 와 같다."""
    login_id: str = "form[name='loginForm'] input[name='id']"
    login_password: str = "form[name='loginForm'] input[name='password']"
    login_button: str = "#btn_login"
    self_verification_top: str = "div[name='selfVeri']"
    self_verification_left: str = "li.left-selfVerification[data-link='/self/verification/list.do']"

    # CAVP 탭 — 서버 페이징 켄도 그리드. 검증 완료(06) 행에만 '결과 확인' 버튼이 있다.
    cavp_table: str = "#cavpTable"
    cavp_result_button: str = "button[data-func='cavpResultRegist']"
    cavp_result_dialog: str = "#cavpResultDialog"
    cavp_sign_button: str = "div[data-func='electSignCavp']"     # 결과 모달 푸터의 '전자서명'
    cavp_result_close: str = "div[data-func='cancelCavpResultDiag']"

    # 자가검증 목록의 두 번째 탭 'Entropy' — 같은 화면에서 그리드만 바뀐다(클라이언트 페이징).
    tab_items: str = "ul.tabMenu li.nav-item"
    entropy_div: str = "#entropyDiv"
    entropy_table: str = "#entropyTable"
    entropy_row_button: str = "button[data-func='entropyViewFun']"   # 행의 '세부결과'
    entropy_view_dialog: str = "#entropyViewDialog"
    entropy_detail_tab: str = "#entropyDetailTab"                    # 잡음원(txt) 선택 select
    entropy_sign_button: str = "div[data-func='electSignV2']"        # 모달 푸터의 '전자 서명'
    entropy_close_button: str = "div[data-func='cancelEntropyViewDiag']"


SELECTORS = Selectors()

CAVP_GRID_XPATH = (
    "xpath=//table[@id='cavpTable']/ancestor::div"
    "[contains(concat(' ', normalize-space(@class), ' '), ' k-grid ')][1]")
ENTROPY_GRID_XPATH = (
    "xpath=//table[@id='entropyTable']/ancestor::div"
    "[contains(concat(' ', normalize-space(@class), ' '), ' k-grid ')][1]")


class StopRequested(RuntimeError):
    """사용자가 [중단]을 눌렀다."""


@dataclass
class Control:
    """워커가 사이트 조작 함수들에 넘기는 문맥 — 로그인 정보, 중단 신호, 상태 표시."""
    user_id: str
    password: str
    stop_event: threading.Event
    status: Callable[[str], None]

    @property
    def stopping(self) -> bool:
        return self.stop_event.is_set()

    def check_stop(self) -> None:
        if self.stop_event.is_set():
            raise StopRequested("중단 요청으로 인증서 생성을 취소했습니다")


# ---------------------------------------------------------------------------
# 페이지 조작 도우미 (러너에서 그대로 옮김 — 캡처·STATE 의존만 뺐다)
# ---------------------------------------------------------------------------
def safe_name(value: str) -> str:
    return re.sub(r"[^0-9A-Za-z가-힣_-]+", "_", value).strip("_") or "page"


def wait_for_page_settle(page: Page, timeout: int = 10_000) -> None:
    try:
        page.wait_for_load_state("networkidle", timeout=timeout)
    except PlaywrightTimeoutError:
        pass


def is_visible(page: Page, selector: str) -> bool:
    try:
        locator = page.locator(selector)
        return locator.count() > 0 and locator.first.is_visible()
    except PlaywrightError:
        return False


def has_selector(page: Page, selector: str) -> bool:
    try:
        return page.locator(selector).count() > 0
    except PlaywrightError:
        return False


def dump_page_html(page: Page, number: str, label: str) -> None:
    """인증서 흐름이 막혔을 때 화면 HTML을 남긴다 — 셀렉터 보정을 위한 진단용."""
    try:
        HTML_DUMP_DIR.mkdir(parents=True, exist_ok=True)
        path = HTML_DUMP_DIR / f"cert_{safe_name(label)}_{safe_name(number)}.html"
        path.write_text(page.content(), encoding="utf-8")
        logging.warning("인증서 단계 실패(%s): 화면 HTML 저장 -> %s", label, path)
    except Exception:  # noqa: BLE001 - 진단 저장 실패가 본 흐름을 막으면 안 된다
        logging.debug("화면 HTML 저장 실패: No.%s", number)


def page_eval_json(page: Page, script: str):
    """페이지에서 JSON *문자열*을 받아 파이썬 값으로 돌려준다.

    KCMVP 사이트는 전역 ``Map``을 자기 것으로 덮어써서, 객체·배열을 그대로 return 하면
    Playwright 직렬화가 **말없이 None**을 돌려준다(원시값·문자열은 정상). 그래서
    페이지 쪽에서 JSON.stringify 한 문자열만 주고받는다. (러너 주석과 같은 함정)
    """
    try:
        raw = page.evaluate(script)
    except PlaywrightError:
        return None
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return None


def close_extra_pages(page: Page, known: set) -> None:
    """전자서명 성공 시 열리는 blob PDF 탭을 정리한다(내용은 이미 응답으로 받았다)."""
    for candidate in page.context.pages:
        if candidate is page or candidate in known:
            continue
        try:
            if not candidate.is_closed():
                candidate.close()
        except PlaywrightError:
            pass


# ---------------------------------------------------------------------------
# 로그인 · 자가검증 목록
# ---------------------------------------------------------------------------
def login(page: Page, ctl: Control) -> Page:
    logging.info("로그인: KCMVP 여는 중 (%s)", BASE_URL)
    page.goto(BASE_URL, wait_until="domcontentloaded")
    page.locator(SELECTORS.login_id).fill(ctl.user_id)
    page.locator(SELECTORS.login_password).fill(ctl.password)

    context = page.context
    try:
        page.locator(SELECTORS.login_button).click()
    except PlaywrightError:
        if not page.is_closed():
            raise

    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        live_pages = [candidate for candidate in context.pages if not candidate.is_closed()]
        for candidate in reversed(live_pages):
            wait_for_page_settle(candidate, timeout=1_000)
            if is_visible(candidate, SELECTORS.login_id):
                # 로그인 화면에 남은 채 안내 팝업(sweetalert)이 떴으면 그 내용이 곧 실패 사유다.
                notice = login_popup_text(candidate)
                if notice:
                    raise RuntimeError(f"로그인 실패: {notice}")
                continue
            logging.info("로그인 성공")
            return candidate
        ctl.check_stop()
        time.sleep(0.5)

    raise RuntimeError("30초 안에 로그인이 끝나지 않았습니다. 계정·비밀번호 또는 사이트 상태를 확인하세요.")


def login_popup_text(page: Page) -> str:
    try:
        popup = page.locator(".swal2-popup").first
        if popup.count() == 0 or not popup.is_visible():
            return ""
        return " ".join((popup.inner_text() or "").split())[:200]
    except PlaywrightError:
        return ""


def go_to_sample_page(page: Page) -> None:
    logging.info("자가검증 목록 여는 중")
    top = page.locator(SELECTORS.self_verification_top).first
    top.wait_for(state="visible", timeout=30_000)
    top.click()
    wait_for_page_settle(page)

    left_item = page.locator(SELECTORS.self_verification_left).first
    left_item.wait_for(state="visible", timeout=30_000)
    left_item.scroll_into_view_if_needed()
    try:
        left_item.click(timeout=5_000)
    except PlaywrightError:
        page.evaluate("selector => document.querySelector(selector)?.click()",
                      SELECTORS.self_verification_left)
    wait_for_page_settle(page)

    if not has_selector(page, "body[data-left='selfVerification']"):
        page.goto(LIST_URL, wait_until="domcontentloaded")
        wait_for_page_settle(page)


def has_list_grid(page: Page, timeout: int = 3_000) -> bool:
    """자가검증 목록(CAVP 또는 Entropy 그리드)이 화면에 보이는가.

    두 표는 탭에 따라 하나만 보인다. 쉼표 셀렉터의 ``.first`` 는 DOM 순서상 앞인
    (숨겨진) CAVP 표를 집어 Entropy 탭이 열린 상태를 '목록 없음'으로 오판하므로
    각각 따로 본다.
    """
    deadline = time.monotonic() + timeout / 1000
    while True:
        if is_visible(page, SELECTORS.cavp_table) or is_visible(page, SELECTORS.entropy_table):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.25)


def ensure_list_page(page: Page, ctl: Control) -> Page:
    """목록이 사라졌으면(세션 만료 등) 다시 열고, 로그인 화면이면 다시 로그인한다."""
    if has_list_grid(page):
        return page
    logging.warning("목록이 사라짐: 자가검증 목록을 다시 엽니다")
    ctl.status("목록이 사라져 다시 여는 중")
    page.goto(LIST_URL, wait_until="domcontentloaded")
    wait_for_page_settle(page)
    if has_list_grid(page, timeout=10_000):
        return page
    if is_visible(page, SELECTORS.login_id):
        logging.warning("세션이 끊겨 다시 로그인합니다")
        page = login(page, ctl)
    go_to_sample_page(page)
    if not has_list_grid(page, timeout=10_000):
        raise RuntimeError("자가검증 목록을 다시 열지 못했습니다")
    return page


def _click_tab(page: Page, want_cavp: bool) -> bool:
    try:
        tabs = page.locator(SELECTORS.tab_items)
        for index in range(tabs.count()):
            tab = tabs.nth(index)
            is_cavp = (tab.inner_text() or "").strip().upper() == "CAVP"
            if is_cavp == want_cavp:
                tab.click()
                return True
    except PlaywrightError:
        logging.exception("탭을 누르지 못했습니다")
    return False


def open_cavp_tab(page: Page) -> bool:
    if is_visible(page, SELECTORS.cavp_table):
        return True
    if not _click_tab(page, want_cavp=True):
        logging.warning("자가검증 목록에 CAVP 탭이 없습니다")
        return False
    try:
        page.locator(SELECTORS.cavp_table).first.wait_for(state="visible", timeout=10_000)
        return True
    except PlaywrightError:
        logging.exception("CAVP 탭을 열지 못했습니다")
        return False


def open_entropy_tab(page: Page) -> bool:
    if is_visible(page, SELECTORS.entropy_div):
        return True
    if not _click_tab(page, want_cavp=False):
        logging.warning("자가검증 목록에 Entropy 탭이 없습니다")
        return False
    try:
        page.locator(SELECTORS.entropy_div).first.wait_for(state="visible", timeout=10_000)
        page.locator(SELECTORS.entropy_table).first.wait_for(state="visible", timeout=10_000)
        return True
    except PlaywrightError:
        logging.exception("Entropy 탭을 열지 못했습니다")
        return False


# ---------------------------------------------------------------------------
# 세션 유지 · 전자서명 응답 캡처 (CAVP · 엔트로피 공용)
# ---------------------------------------------------------------------------
def keep_session_alive(page: Page) -> None:
    """PDF 렌더링 동안은 서버 요청이 0건이라 세션이 조용히 만료된다 → 주기적으로 친다.

    페이지 JS 스레드는 렌더링으로 바쁠 수 있으므로 같은 쿠키를 쓰되 JS 스레드를
    거치지 않는 page.request 로 보낸다.
    """
    try:
        page.request.post(BASE_URL.rstrip("/") + SESSION_RESET_PATH, timeout=30_000)
    except PlaywrightError:
        logging.debug("세션 유지 요청 실패")


def session_lost(page: Page) -> bool:
    try:
        if "verification" in (page.url or ""):
            return False
        return is_visible(page, SELECTORS.login_id)
    except PlaywrightError:
        return False


def dismiss_session_dialog(page: Page) -> None:
    """'1분후 세션이 만료됩니다. 연장하시겠습니까?' 가 뜨면 '네'를 눌러 연장한다."""
    try:
        popup = page.locator(".swal2-popup").first
        if popup.count() == 0 or not popup.is_visible():
            return
        if "세션" not in (popup.inner_text() or ""):
            return
        confirm = page.locator(".swal2-confirm").first
        if confirm.count() > 0 and confirm.is_visible():
            confirm.click(timeout=3_000)
            logging.info("세션 만료 안내: '연장'을 눌렀습니다")
    except PlaywrightError:
        pass


def install_sign_capture_hook(page: Page) -> None:
    """전자서명 API 응답(base64 PDF)을 페이지 안에서 붙잡아 둔다.

    Playwright 의 response 이벤트로 본문을 읽으면 큰 base64 PDF 가 인스펙터 캐시에서
    밀려난다 → 사이트 JS 와 같은 자리(XHR)에서 responseText 를 잡는다.
    """
    page.evaluate(
        """(apiPath) => {
               window.__certCapture = null;
               window.__certBlobUrl = null;
               if (window.__certHooked) return;
               window.__certHooked = true;
               const origOpen = XMLHttpRequest.prototype.open;
               XMLHttpRequest.prototype.open = function (method, url) {
                   try {
                       if (String(url).indexOf(apiPath) !== -1) {
                           this.addEventListener('load', function () {
                               try {
                                   const body = JSON.parse(this.responseText);
                                   window.__certCapture = {
                                       status: this.status,
                                       result: body && body.result ? body.result : null,
                                       error: '',
                                   };
                               } catch (e) {
                                   window.__certCapture = {
                                       status: this.status, result: null,
                                       error: 'JSON 파싱 실패: ' + e.message,
                                   };
                               }
                           });
                       }
                   } catch (e) { /* 후킹 실패해도 원래 요청은 그대로 진행 */ }
                   return origOpen.apply(this, arguments);
               };
               // 완성된 PDF blob URL 도 기억해 둔다(보조 경로).
               const origCreate = URL.createObjectURL;
               URL.createObjectURL = function (obj) {
                   const url = origCreate.apply(this, arguments);
                   try {
                       if (obj && obj.type === 'application/pdf') window.__certBlobUrl = url;
                   } catch (e) { /* 무시 */ }
                   return url;
               };
           }""",
        SIGN_API_PATH,
    )


def read_blob_pdf(page: Page) -> str | None:
    """XHR 후킹이 놓쳤을 때의 보조 경로 — 기억해 둔 blob URL 을 읽어 base64 로 돌려준다."""
    try:
        return page.evaluate(
            """async () => {
                   if (!window.__certBlobUrl) return null;
                   const res = await fetch(window.__certBlobUrl);
                   const blob = await res.blob();
                   return await new Promise((resolve) => {
                       const reader = new FileReader();
                       reader.onload = () => resolve(String(reader.result).split(',')[1] || '');
                       reader.onerror = () => resolve('');
                       reader.readAsDataURL(blob);
                   });
               }""")
    except PlaywrightError:
        return None


def wait_for_signed_pdf(page: Page, number: str, ctl: Control) -> bytes:
    """서명 버튼을 누른 뒤 PDF 가 돌아올 때까지 기다렸다 바이트로 돌려준다."""
    deadline = time.monotonic() + CERT_SIGN_TIMEOUT
    next_keepalive = time.monotonic() + SESSION_KEEPALIVE_SECONDS
    started = time.monotonic()
    while True:
        # 응답 유무만 싼 값으로 확인한다(수 MB 짜리 base64 를 매초 실어 나르지 않도록)
        status = page_eval_json(
            page,
            """() => window.__certCapture
                   ? JSON.stringify({status: window.__certCapture.status,
                                     size: (window.__certCapture.result || '').length,
                                     error: window.__certCapture.error})
                   : ''""")
        if status:
            if not status.get("size") and not status.get("error") \
                    and int(status.get("status") or 0) < 400:
                from_blob = read_blob_pdf(page)
                if from_blob:
                    status = {"status": 200, "size": len(from_blob), "error": "",
                              "encoded": from_blob}
            break
        from_blob = read_blob_pdf(page)
        if from_blob:
            logging.info("blob 창에서 인증서를 받았습니다: No.%s", number)
            status = {"status": 200, "size": len(from_blob), "error": "", "encoded": from_blob}
            break
        ctl.check_stop()
        if session_lost(page):
            dump_page_html(page, number, "session_lost")
            raise RuntimeError("인증서 생성 중 KCMVP 세션이 끊겼습니다")
        if time.monotonic() > deadline:
            dump_page_html(page, number, "sign_timeout")
            raise RuntimeError(f"인증서 생성이 {CERT_SIGN_TIMEOUT // 60}분 안에 끝나지 않았습니다")
        if time.monotonic() >= next_keepalive:
            keep_session_alive(page)
            dismiss_session_dialog(page)
            next_keepalive = time.monotonic() + SESSION_KEEPALIVE_SECONDS
        elapsed = int(time.monotonic() - started)
        if elapsed and elapsed % 30 == 0:
            ctl.status(f"인증서 생성 중 (No.{number}) — {elapsed // 60}분 {elapsed % 60}초 경과")
        page.wait_for_timeout(1_000)

    if status.get("error"):
        raise RuntimeError(f"전자서명 응답을 해석하지 못했습니다: {status['error']}")
    if int(status.get("status") or 0) >= 400:
        raise RuntimeError(f"전자서명 실패 (HTTP {status['status']})")
    if not status.get("size"):
        raise RuntimeError("사이트가 인증서 생성에 실패했습니다 (result 없음)")
    encoded = status.get("encoded") or page.evaluate("() => window.__certCapture.result")
    try:
        pdf = base64.b64decode(encoded)
    except (ValueError, TypeError) as exc:
        raise RuntimeError(f"인증서 데이터를 디코딩하지 못했습니다: {exc}") from exc
    if not pdf.startswith(b"%PDF"):
        raise RuntimeError("받은 데이터가 PDF 가 아닙니다")
    return pdf


# ---------------------------------------------------------------------------
# CAVP 탭 · 그리드(서버 페이징) · 결과 모달
# ---------------------------------------------------------------------------
# 실제 동작(러너 주석, 2026-08-07 팝업 DOM 확인):
#   1) 검증 완료(06) 행의 '결과 확인'(data-func=cavpResultRegist) → 켄도 창 #cavpResultDialog 가
#      열리고 시험 결과 그리드(#cavpResultTable, 수백 건)가 AJAX 로 채워진다.
#   2) 팝업 하단 '전자서명'(data-func=electSignCavp) → 브라우저가 그리드 전체를 PDF 로 그린 뒤
#      getElectroSign.do 로 보내 서버 전자서명을 받는다 → 응답 result(base64 PDF).
#   3) 인증서를 하나 받고 나면 사이트가 이력을 저장하며(saveValid.do) 그리드를 다시 그린다 —
#      그 사이 표가 비어 있어서 행 찾기는 잠깐 기다려야 한다(find_cavp_row).


def cavp_cert_stem(number: str) -> str:
    return f"CAVP_{safe_name(str(number))}"


def cavp_grid_info(page: Page) -> dict:
    grid = page.locator(CAVP_GRID_XPATH)
    if grid.count() == 0:
        return {"currentPage": None, "pages": []}
    selected = grid.locator(".k-pager-numbers .k-state-selected").first
    links = grid.locator(".k-pager-numbers a[data-page]")
    return {
        "currentPage": selected.inner_text().strip() if selected.count() > 0 else None,
        "pages": [links.nth(i).get_attribute("data-page") for i in range(links.count())],
    }


def cavp_current_page(page: Page) -> str | None:
    try:
        return cavp_grid_info(page).get("currentPage")
    except PlaywrightError:
        return None


def read_cavp_view_rows(page: Page) -> list[dict]:
    """지금 보이는 CAVP 그리드 페이지의 행 — {number, resultReady, status}.

    status 는 '상태' 열들(알고리즘 뒤 ~ 날짜 앞)의 글자 — 결과 확인 버튼이 없을 때 사유 표시용.
    """
    return page_eval_json(
        page,
        """() => JSON.stringify(
               Array.from(document.querySelectorAll("#cavpTable tbody tr")).map((tr) => {
                   const td = tr.querySelector("td[data-field='number']");
                   const cells = Array.from(tr.querySelectorAll('td'))
                       .map((c) => c.textContent.trim().replace(/\\s+/g, ' '));
                   const middle = cells.length >= 4 ? cells.slice(2, -1) : cells.slice(2);
                   return {
                       number: td ? td.textContent.trim() : (cells[0] || ''),
                       resultReady: !!tr.querySelector("button[data-func='cavpResultRegist']"),
                       status: middle.filter((c) => c).join(' ').slice(0, 80),
                   };
               }).filter((r) => r.number))""") or []


def first_cavp_number(page: Page) -> str | None:
    rows = read_cavp_view_rows(page)
    return rows[0]["number"] if rows else None


def go_to_cavp_page(page: Page, page_number: int) -> bool:
    """CAVP 그리드를 ``page_number`` 페이지로 옮긴다(서버 페이징 — 페이저 링크 클릭).

    False 는 '그 페이지 링크가 없다'(마지막 페이지를 넘었다) 또는 전환 실패.
    """
    before_first = first_cavp_number(page)
    try:
        grid = page.locator(CAVP_GRID_XPATH)
        if grid.count() == 0:
            return False
        if cavp_current_page(page) == str(page_number):
            return True
        link = grid.locator(f".k-pager-numbers a[data-page='{page_number}']").first
        if link.count() == 0:
            link = grid.locator(f"a.k-link[data-page='{page_number}']").first
        if link.count() == 0:
            return False
        link.click()
    except PlaywrightError:
        return False

    time.sleep(0.4)
    wait_for_page_settle(page)
    try:
        page.locator(f"{SELECTORS.cavp_table} tbody tr").first.wait_for(
            state="attached", timeout=10_000)
    except PlaywrightError:
        return False
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if cavp_current_page(page) == str(page_number):
            if before_first is None or first_cavp_number(page) != before_first \
                    or time.monotonic() > deadline - 5:
                return True
        time.sleep(0.5)
    return cavp_current_page(page) == str(page_number)


def find_cavp_row(page: Page, number: str, timeout: float = 20.0):
    """현재 그리드 페이지에서 번호 칸이 ``number`` 인 행 — 그리드가 다시 그려지는 동안 기다린다."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            rows = page.locator(f"{SELECTORS.cavp_table} tbody tr")
            for index in range(rows.count()):
                row = rows.nth(index)
                if row.locator("td[data-field='number']").first.inner_text().strip() == number:
                    return row
        except PlaywrightError:
            pass
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.5)


def close_cert_dialog(page: Page) -> None:
    """CAVP 결과 팝업을 닫아 다음 행을 처리할 수 있게 만든다."""
    try:  # 서명 중 덮이는 대기 레이어는 성공 경로에서만 정리된다
        page.evaluate(
            "() => document.querySelectorAll('.wait-layer').forEach((el) => { el.style.display = 'none'; })")
    except PlaywrightError:
        pass
    for selector in (f"{SELECTORS.cavp_result_dialog} {SELECTORS.cavp_result_close}",
                     SELECTORS.cavp_result_close, ".k-window-titlebar .k-i-close"):
        try:
            locator = page.locator(selector).first
            if locator.count() > 0 and locator.is_visible():
                locator.click(timeout=3_000)
                page.locator(SELECTORS.cavp_result_dialog).first.wait_for(
                    state="hidden", timeout=5_000)
                return
        except PlaywrightError:
            pass
    try:
        page.evaluate(
            "() => { const w = window.jQuery && jQuery('#cavpResultDialog').data('kendoWindow');"
            " if (w) w.close(); }")
    except PlaywrightError:
        logging.debug("CAVP 결과 모달을 닫지 못했습니다")


def download_cavp_certificate(page: Page, number: str, save_dir: Path, ctl: Control) -> Path | None:
    """'결과 확인' → '전자서명' 인증서를 저장한다. 결과 확인 버튼이 없으면(검증 완료 전) None."""
    target_row = find_cavp_row(page, number)
    if target_row is None:
        return None
    button = target_row.locator(SELECTORS.cavp_result_button).first
    if button.count() == 0:
        return None

    known_pages = set(page.context.pages)
    button.scroll_into_view_if_needed()
    button.click()
    try:
        dialog = page.locator(SELECTORS.cavp_result_dialog).first
        dialog.wait_for(state="visible", timeout=CERT_DIALOG_TIMEOUT * 1_000)
        sign = dialog.locator(SELECTORS.cavp_sign_button).first
        if sign.count() == 0:
            sign = page.locator(SELECTORS.cavp_sign_button).first
        try:
            sign.wait_for(state="visible", timeout=CERT_DIALOG_TIMEOUT * 1_000)
        except PlaywrightError:
            dump_page_html(page, number, "cavp_no_sign_button")
            raise RuntimeError("결과 팝업에서 '전자서명' 버튼을 찾지 못했습니다")

        install_sign_capture_hook(page)
        sign.click()
        ctl.status(f"CAVP 인증서 생성 중 (No.{number}) — 수 분 걸릴 수 있음")
        pdf = wait_for_signed_pdf(page, number, ctl)

        dest_dir = save_dir / KIND_SUBDIRS["cavp"]
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / f"{cavp_cert_stem(number)}.pdf"
        dest.write_bytes(pdf)
        logging.info("저장: CAVP No.%s -> %s (%s bytes)", number, dest.name, len(pdf))
        return dest
    finally:
        close_extra_pages(page, known_pages)
        close_cert_dialog(page)


# ---------------------------------------------------------------------------
# Entropy 탭 · 그리드(클라이언트 페이징) · 결과 모달
# ---------------------------------------------------------------------------
def entropy_cert_stem(number: str, environment: str) -> str:
    """파일 이름(확장자 제외) — '샘플번호_환경명'. 환경명은 OS 열 그대로, 경로 금지 문자만 치환."""
    name = re.sub(r'[\\/:*?"<>|\s]+', "_", str(environment).strip()).strip("._") or "env"
    return f"{safe_name(str(number))}_{name}"


def entropy_grid_pages(page: Page) -> int:
    """엔트로피 그리드의 전체 페이지 수(켄도 데이터소스 → 없으면 페이저 링크의 최댓값)."""
    try:
        total = page.evaluate(
            """() => {
                   const grid = window.jQuery && jQuery('#entropyTable').data('kendoGrid');
                   if (!grid || !grid.dataSource) return 0;
                   return grid.dataSource.totalPages ? grid.dataSource.totalPages() : 1;
               }""")
        if int(total or 0) > 0:
            return int(total)
    except (PlaywrightError, TypeError, ValueError):
        pass
    try:
        links = page.locator(ENTROPY_GRID_XPATH).locator("a[data-page]")
        pages = []
        for index in range(links.count()):
            value = links.nth(index).get_attribute("data-page") or ""
            if value.isdigit():
                pages.append(int(value))
        return max(pages) if pages else 0
    except PlaywrightError:
        return 0


def entropy_current_page(page: Page) -> str | None:
    try:
        selected = page.locator(ENTROPY_GRID_XPATH).locator(
            ".k-pager-numbers .k-state-selected").first
        return selected.inner_text().strip() if selected.count() > 0 else None
    except PlaywrightError:
        return None


def go_to_entropy_page(page: Page, page_number: int) -> bool:
    """클라이언트 페이징이라 데이터소스 API 로 옮기고, 위젯을 못 잡을 때만 링크를 누른다."""
    moved = False
    try:
        moved = bool(page.evaluate(
            """(n) => {
                   const grid = window.jQuery && jQuery('#entropyTable').data('kendoGrid');
                   if (!grid || !grid.dataSource) return false;
                   if (grid.dataSource.page() !== n) grid.dataSource.page(n);
                   return grid.dataSource.page() === n;
               }""", page_number))
    except PlaywrightError:
        moved = False
    if not moved:
        if entropy_current_page(page) == str(page_number):
            moved = True
        else:
            try:
                link = page.locator(ENTROPY_GRID_XPATH).locator(
                    f"a[data-page='{page_number}']").first
                if link.count() == 0:
                    return False
                link.click()
            except PlaywrightError:
                return False
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if entropy_current_page(page) == str(page_number):
                    moved = True
                    break
                time.sleep(0.5)
    if not moved:
        return False
    try:
        page.locator(f"{SELECTORS.entropy_table} tbody tr").first.wait_for(
            state="attached", timeout=10_000)
    except PlaywrightError:
        return False
    return True


def read_entropy_view_rows(page: Page) -> list[dict]:
    """지금 보이는 엔트로피 그리드 페이지의 행 — {number, os, bit, seq}."""
    return page_eval_json(
        page,
        """() => JSON.stringify(
               Array.from(document.querySelectorAll("#entropyTable tbody tr")).map((tr) => {
                   const cell = (field) => {
                       const td = tr.querySelector("td[data-field='" + field + "']");
                       return td ? td.textContent.trim() : '';
                   };
                   const btn = tr.querySelector("button[data-func='entropyViewFun']");
                   return {
                       number: cell('number'),
                       os: cell('os') || (btn ? (btn.getAttribute('data-os') || '') : ''),
                       bit: cell('bit') || (btn ? (btn.getAttribute('data-bit') || '') : ''),
                       seq: btn ? (btn.getAttribute('data-seq') || '') : '',
                   };
               }).filter((r) => r.number))""") or []


def close_entropy_dialog(page: Page) -> None:
    """결과 모달을 닫고, 서명이 실패했을 때 남는 화면 상태(대기 레이어·A4 클래스)도 되돌린다."""
    try:
        page.evaluate(
            """() => {
                   document.querySelectorAll('.wait-layer').forEach((el) => {
                       el.style.display = 'none';
                   });
                   const dialog = document.getElementById('entropyViewDialog');
                   if (dialog) dialog.classList.remove('entropyA4View');
                   const cover = document.getElementById('pdfCoverToEntrophy');
                   if (cover) cover.style.display = 'none';
               }""")
    except PlaywrightError:
        pass
    for selector in (f"{SELECTORS.entropy_view_dialog} {SELECTORS.entropy_close_button}",
                     SELECTORS.entropy_close_button):
        try:
            locator = page.locator(selector).first
            if locator.count() > 0 and locator.is_visible():
                locator.click(timeout=3_000)
                page.locator(SELECTORS.entropy_view_dialog).first.wait_for(
                    state="hidden", timeout=5_000)
                return
        except PlaywrightError:
            pass
    try:
        page.evaluate(
            "() => { const w = window.jQuery && jQuery('#entropyViewDialog').data('kendoWindow');"
            " if (w) w.close(); }")
    except PlaywrightError:
        logging.debug("엔트로피 결과 모달을 닫지 못했습니다")


def download_entropy_certificate(page: Page, row: dict, save_dir: Path, ctl: Control) -> Path:
    """한 엔트로피 행의 '세부결과' → '전자 서명' 인증서를 저장한다."""
    number, environment = row.get("number", ""), row.get("os", "")
    button = page.locator(
        f"{SELECTORS.entropy_table} {SELECTORS.entropy_row_button}"
        f"[data-seq='{row.get('seq', '')}']").first
    if button.count() == 0:
        raise RuntimeError("'세부결과' 버튼을 찾지 못했습니다")

    known_pages = set(page.context.pages)
    button.scroll_into_view_if_needed()
    button.click()
    try:
        # 1) 결과 모달이 뜨고 잡음원 목록(select)이 채워질 때까지 — 채워지기 전에
        #    서명하면 내용이 빈 인증서가 나온다. wait_for_function 은 이 사이트의
        #    전역 Map 덮어쓰기 때문에 못 쓴다 → evaluate 폴링.
        dialog = page.locator(SELECTORS.entropy_view_dialog).first
        dialog.wait_for(state="visible", timeout=CERT_DIALOG_TIMEOUT * 1_000)
        detail_deadline = time.monotonic() + CERT_DIALOG_TIMEOUT
        while True:
            try:
                filled = page.evaluate(
                    "() => document.querySelectorAll('#entropyDetailTab option').length")
            except PlaywrightError:
                filled = 0
            if int(filled or 0) > 0:
                break
            ctl.check_stop()
            if time.monotonic() > detail_deadline:
                dump_page_html(page, number, "entropy_no_detail")
                raise RuntimeError("결과 창에 잡음원 목록이 채워지지 않았습니다")
            page.wait_for_timeout(500)

        sign = dialog.locator(SELECTORS.entropy_sign_button).first
        if sign.count() == 0:
            sign = page.locator(SELECTORS.entropy_sign_button).first
        try:
            sign.wait_for(state="visible", timeout=CERT_DIALOG_TIMEOUT * 1_000)
        except PlaywrightError:
            dump_page_html(page, number, "entropy_no_sign_button")
            raise RuntimeError("결과 창에서 '전자 서명' 버튼을 찾지 못했습니다")

        # 2) CAVP 인증서와 같은 API·같은 캡처 경로
        install_sign_capture_hook(page)
        sign.click()
        ctl.status(f"엔트로피 인증서 생성 중 (No.{number} · {environment}) — 수 분 걸릴 수 있음")
        pdf = wait_for_signed_pdf(page, number, ctl)

        dest_dir = save_dir / KIND_SUBDIRS["entropy"]
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / f"{entropy_cert_stem(number, environment)}.pdf"
        dest.write_bytes(pdf)
        logging.info("저장: 엔트로피 No.%s (%s) -> %s (%s bytes)",
                     number, environment, dest.name, len(pdf))
        return dest
    finally:
        close_extra_pages(page, known_pages)
        close_entropy_dialog(page)


# ---------------------------------------------------------------------------
# 워커 스레드 — 브라우저 하나로 예약 큐의 작업(종류·범위)을 차례로 처리한다
# ---------------------------------------------------------------------------
@dataclass
class Job:
    kind: str          # "cavp" | "entropy"
    start: int
    end: int
    numbers: list[str]

    @property
    def label(self) -> str:
        span = f"No.{self.start}" if self.start == self.end else f"No.{self.start}~{self.end}"
        return f"{KIND_LABELS[self.kind]} {span} ({len(self.numbers)}건)"


class Downloader(threading.Thread):
    """GUI 와는 ``events`` 큐로만 대화한다(tk 위젯은 절대 건드리지 않는다).

    이벤트는 (종류, 발신 워커, *내용) 튜플이다. GUI 는 지금 워커가 아닌 발신자의
    이벤트를 버린다 — 마지막 작업이 끝나 브라우저를 닫는 사이에 새 워커가 시작되면
    옛 워커의 뒷정리 이벤트가 새 워커의 표시를 덮어쓰기 때문이다.
      ("log", None, text)  ("status", worker, text)  ("progress", worker, done, total)
      ("queue", worker, [labels])  ("job_start", worker, label)
      ("result", worker, {kind, number, environment, file|error})
      ("job_done", worker, label, saved, total)  ("finished", worker, ok, message)
    """

    def __init__(self, ctl: Control, save_dir: Path, headless: bool, events: queue.Queue):
        super().__init__(daemon=True, name="kcmvp-cert-downloader")
        self.ctl = ctl
        self.save_dir = save_dir
        self.headless = headless
        self.events = events
        self._lock = threading.Lock()
        self._jobs: list[Job] = []
        self._queued: set[tuple[str, str]] = set()   # (종류, 번호) — 아직 처리 안 된 것(중복 예약 방지)
        self._accepting = True                        # 큐가 비어 브라우저를 닫기 시작하면 False
        self.total = 0
        self.done = 0
        self.saved = 0
        self.failed = 0

    # -- GUI 쪽에서 부르는 것 --------------------------------------------------
    def add(self, kind: str, start: int, end: int) -> tuple[str, Job | None]:
        """범위를 예약한다 → ("added", job) / ("duplicate", None) / ("closed", None)."""
        if kind not in KIND_LABELS:
            raise ValueError(kind)
        with self._lock:
            if not self._accepting:
                return "closed", None
            numbers = [str(n) for n in range(start, end + 1) if (kind, str(n)) not in self._queued]
            if not numbers:
                return "duplicate", None
            job = Job(kind, start, end, numbers)
            self._queued.update((kind, n) for n in numbers)
            self._jobs.append(job)
            self.total += len(numbers)
            pending = [j.label for j in self._jobs]
            done, total = self.done, self.total
        self._emit("queue", pending)
        self._emit("progress", done, total)
        return "added", job

    def stop(self) -> None:
        self.ctl.stop_event.set()

    # -- 내부 -----------------------------------------------------------------
    def _emit(self, kind: str, *payload) -> None:
        self.events.put((kind, self) + tuple(payload))

    def _next_job(self) -> Job | None:
        with self._lock:
            if not self._jobs:
                self._accepting = False
                return None
            job = self._jobs.pop(0)
            pending = [j.label for j in self._jobs]
        self._emit("queue", pending)
        return job

    def _mark_done(self, kind: str, number: str, saved: bool | None) -> None:
        with self._lock:
            self._queued.discard((kind, number))
            self.done += 1
            if saved is True:
                self.saved += 1
            elif saved is False:
                self.failed += 1
            done, total = self.done, self.total
        self._emit("progress", done, total)

    def _drop_queue(self) -> int:
        with self._lock:
            self._accepting = False
            dropped = sum(len(j.numbers) for j in self._jobs)
            self._jobs.clear()
            self._queued.clear()
            self.total -= dropped
            done, total = self.done, self.total
        self._emit("queue", [])
        self._emit("progress", done, total)
        return dropped

    def run(self) -> None:
        ok, message = True, ""
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=self.headless)
                context = browser.new_context(ignore_https_errors=True,
                                              viewport={"width": 1440, "height": 1000})
                page = context.new_page()
                try:
                    self.ctl.status("KCMVP 로그인 중")
                    page = login(page, self.ctl)
                    self.ctl.status("자가검증 목록 여는 중")
                    go_to_sample_page(page)
                    while not self.ctl.stopping:
                        job = self._next_job()
                        if job is None:
                            break
                        page = self._run_job(page, job)
                finally:
                    try:
                        browser.close()
                    except PlaywrightError:
                        pass
        except StopRequested:
            message = "중단했습니다"
        except Exception as exc:  # noqa: BLE001 - 어떤 실패든 GUI 에 보여야 한다
            logging.exception("다운로드 작업이 실패했습니다")
            ok, message = False, str(exc).splitlines()[0][:300] if str(exc) else type(exc).__name__
        finally:
            dropped = self._drop_queue()
            if self.ctl.stopping and not message:
                message = "중단했습니다"
            summary = f"{self.saved}건 저장"
            if self.failed:
                summary += f", {self.failed}건 실패"
            if dropped:
                summary += f", 예약 {dropped}건 취소"
            if ok and not message:
                message = f"완료: {summary} → {self.save_dir}"
            else:
                message = f"{message} ({summary})"
            self._emit("finished", ok, message)

    def _run_job(self, page: Page, job: Job) -> Page:
        self._emit("job_start", job.label)
        logging.info("작업 시작: %s → %s", job.label, self.save_dir / KIND_SUBDIRS[job.kind])
        page = ensure_list_page(page, self.ctl)
        if job.kind == "cavp":
            page, remaining, saved = self._run_cavp_job(page, job)
        else:
            page, remaining, saved = self._run_entropy_job(page, job)

        if remaining:
            missing = sorted(remaining, key=int)
            for number in missing:
                self._mark_done(job.kind, number, None)
            if self.ctl.stopping:
                reason = "중단 요청으로 건너뜀"
            elif job.kind == "cavp":
                reason = f"CAVP 목록 1~{CHECK_PAGE_LIMIT}페이지에서 찾지 못한 번호입니다 ({len(missing)}건)"
            else:
                reason = f"엔트로피 목록에 없는 번호입니다 ({len(missing)}건)"
            self._emit("result", {
                "kind": job.kind,
                "number": ", ".join(missing[:20]) + (" 외" if len(missing) > 20 else ""),
                "environment": "-", "error": reason,
            })
            if not self.ctl.stopping:
                logging.warning("%s: 목록에 없는 번호 %s건: %s",
                                KIND_LABELS[job.kind], len(missing), ", ".join(missing[:20]))

        self._emit("job_done", job.label, saved, len(job.numbers))
        logging.info("작업 끝: %s — %s/%s 저장", job.label, saved, len(job.numbers))
        return page

    def _record(self, job: Job, result: dict) -> None:
        self._mark_done(job.kind, result["number"], "file" in result)
        self._emit("result", result)

    def _run_cavp_job(self, page: Page, job: Job) -> tuple[Page, set[str], int]:
        """러너의 run_download_job(kind="cert") 과 같은 흐름 — CAVP 페이지를 차례로 훑는다."""
        remaining = set(job.numbers)
        saved = 0
        if not open_cavp_tab(page):
            raise RuntimeError("자가검증 목록에서 CAVP 탭을 열지 못했습니다")

        for page_number in range(1, CHECK_PAGE_LIMIT + 1):
            if not remaining or self.ctl.stopping:
                break
            if not go_to_cavp_page(page, page_number):
                if page_number == 1:
                    continue
                break  # 페이지 링크가 없다 = 마지막 페이지를 넘었다
            rows = {r["number"]: r for r in read_cavp_view_rows(page)}
            for number in [n for n in rows if n in remaining]:
                if self.ctl.stopping:
                    break
                remaining.discard(number)
                self.ctl.status(f"CAVP 인증서 다운로드 중 (No.{number})")
                result = {"kind": "cavp", "number": number, "environment": "-"}
                try:
                    dest = download_cavp_certificate(page, number, self.save_dir, self.ctl)
                    if dest is None:
                        status = rows[number].get("status") or ""
                        result["error"] = ("'결과 확인' 버튼 없음 (CAVP 검증 완료 전)"
                                           + (f" — 상태: {status}" if status else ""))
                    else:
                        result["file"] = str(dest)
                        saved += 1
                except Exception as exc:  # noqa: BLE001 - 한 건의 실패가 작업을 죽이면 안 된다
                    if isinstance(exc, StopRequested):
                        logging.info("No.%s: %s", number, exc)
                    else:
                        logging.exception("실패: CAVP No.%s", number)
                    result["error"] = str(exc).splitlines()[0][:200]
                self._record(job, result)

                # 한 건 뒤에는 화면이 흔들린다(이력 저장으로 그리드 재렌더·새 탭·세션) →
                # 목록·탭·페이지를 복구한다. 한 번 더 시도하고 안 되면 이 작업은 접는다.
                if remaining and not self.ctl.stopping:
                    page = ensure_list_page(page, self.ctl)
                    if not (open_cavp_tab(page) and go_to_cavp_page(page, page_number)):
                        logging.warning("CAVP %s 페이지로 돌아가지 못해 한 번 더 시도합니다", page_number)
                        page = ensure_list_page(page, self.ctl)
                        if not (open_cavp_tab(page) and go_to_cavp_page(page, page_number)):
                            logging.warning("CAVP %s 페이지 복구 실패 — 작업을 접습니다", page_number)
                            return page, remaining, saved
        return page, remaining, saved

    def _run_entropy_job(self, page: Page, job: Job) -> tuple[Page, set[str], int]:
        """러너의 run_entropy_cert_job 과 같은 흐름 — 클라이언트 페이징 그리드를 돈다."""
        remaining = set(job.numbers)
        saved = 0
        if not open_entropy_tab(page):
            raise RuntimeError("자가검증 목록에서 Entropy 탭을 열지 못했습니다")

        page_count = entropy_grid_pages(page) or 1
        for page_number in range(1, page_count + 1):
            if not remaining or self.ctl.stopping:
                break
            if not go_to_entropy_page(page, page_number):
                continue
            for row in [r for r in read_entropy_view_rows(page) if r["number"] in remaining]:
                if self.ctl.stopping:
                    break
                number = row["number"]
                remaining.discard(number)
                self.ctl.status(f"엔트로피 인증서 다운로드 중 (No.{number} · {row['os']})")
                result = {"kind": "entropy", "number": number, "environment": row["os"]}
                try:
                    dest = download_entropy_certificate(page, row, self.save_dir, self.ctl)
                    result["file"] = str(dest)
                    saved += 1
                except Exception as exc:  # noqa: BLE001 - 한 건의 실패가 작업을 죽이면 안 된다
                    if isinstance(exc, StopRequested):
                        logging.info("No.%s: %s", number, exc)
                    else:
                        logging.exception("실패: 엔트로피 No.%s", number)
                    result["error"] = str(exc).splitlines()[0][:200]
                self._record(job, result)

                if remaining and not self.ctl.stopping:
                    if not is_visible(page, SELECTORS.entropy_table):
                        page = ensure_list_page(page, self.ctl)
                    if not open_entropy_tab(page) or not go_to_entropy_page(page, page_number):
                        logging.warning("엔트로피 목록 %s 페이지로 돌아가지 못했습니다", page_number)
                        return page, remaining, saved
        return page, remaining, saved


# ---------------------------------------------------------------------------
# 로그 · 설정
# ---------------------------------------------------------------------------
class QueueLogHandler(logging.Handler):
    """로그 레코드를 GUI 이벤트 큐로 넘긴다."""

    def __init__(self, events: queue.Queue):
        super().__init__()
        self.events = events

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.events.put(("log", None, self.format(record)))
        except Exception:  # noqa: BLE001
            pass


def setup_logging(events: queue.Queue) -> Path | None:
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S")
    handler = QueueLogHandler(events)
    handler.setFormatter(fmt)
    root.addHandler(handler)
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        log_file = LOG_DIR / "kcmvp_cert_downloader.log"
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(threadName)s %(message)s"))
        root.addHandler(file_handler)
        return log_file
    except OSError:
        return None


def load_config() -> dict:
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_config(data: dict) -> None:
    try:
        CONFIG_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        logging.debug("설정 파일을 쓰지 못했습니다: %s", CONFIG_FILE)


def next_number_hint(save_dir: Path, kind: str) -> int:
    """종류별 하위 폴더에 이미 있는 파일 중 가장 큰 번호 + 1 (없으면 1) — 시작 번호 기본값.

    CAVP 는 'CAVP_<번호>.pdf', 엔트로피는 '<번호>_<환경명>.pdf'.
    """
    numbers = []
    try:
        for path in (save_dir / KIND_SUBDIRS[kind]).glob("*.pdf"):
            stem = path.stem
            if kind == "cavp":
                head = stem[len("CAVP_"):] if stem.startswith("CAVP_") else ""
            else:
                head = stem.partition("_")[0]
            if head.isdigit():
                numbers.append(int(head))
    except OSError:
        pass
    return max(numbers) + 1 if numbers else 1


def open_folder(path: Path) -> None:
    try:
        path.mkdir(parents=True, exist_ok=True)
        if sys.platform == "win32":
            os.startfile(str(path))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except OSError as exc:
        logging.warning("폴더를 열지 못했습니다: %s", exc)


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------
class App:
    POLL_MS = 150

    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.title(f"{APP_NAME} v{APP_VERSION} — {CREDIT}")
        self.root.minsize(800, 640)
        self.events: queue.Queue = queue.Queue()
        self.worker: Downloader | None = None
        self.running = False
        self.closing = False
        self.config = load_config()

        self.user_var = tk.StringVar(value=self.config.get("user_id", ""))
        self.password_var = tk.StringVar()
        self.save_dir_var = tk.StringVar(value=self.config.get("save_dir") or str(DEFAULT_SAVE_DIR))
        kind = self.config.get("kind")
        self.kind_var = tk.StringVar(value=kind if kind in KIND_LABELS else "cavp")
        self.start_var = tk.StringVar()
        self.end_var = tk.StringVar()
        self.show_browser_var = tk.BooleanVar(value=bool(self.config.get("show_browser", False)))
        self.status_var = tk.StringVar(value="대기 중")
        self.progress_var = tk.StringVar(value="")
        self.queue_var = tk.StringVar(value="예약 없음")

        self._build()
        self._refresh_hint()
        log_file = setup_logging(self.events)
        logging.info("%s v%s 시작 (로그: %s)", APP_NAME, APP_VERSION, log_file or "파일 없음")
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(self.POLL_MS, self._poll)

    # -- 화면 구성 ---------------------------------------------------------------
    def _build(self) -> None:
        pad = {"padx": 6, "pady": 3}
        outer = ttk.Frame(self.root, padding=10)
        outer.pack(fill="both", expand=True)

        form = ttk.LabelFrame(outer, text="요청", padding=8)
        form.pack(fill="x")
        form.columnconfigure(1, weight=1)
        form.columnconfigure(3, weight=1)

        ttk.Label(form, text="KCMVP 계정").grid(row=0, column=0, sticky="w", **pad)
        self.user_entry = ttk.Entry(form, textvariable=self.user_var)
        self.user_entry.grid(row=0, column=1, sticky="ew", **pad)
        ttk.Label(form, text="비밀번호").grid(row=0, column=2, sticky="w", **pad)
        self.password_entry = ttk.Entry(form, textvariable=self.password_var, show="*")
        self.password_entry.grid(row=0, column=3, sticky="ew", **pad)

        ttk.Label(form, text="인증서 종류").grid(row=1, column=0, sticky="w", **pad)
        kinds = ttk.Frame(form)
        kinds.grid(row=1, column=1, columnspan=3, sticky="w", **pad)
        self.kind_radios = []
        for value, label in KIND_LABELS.items():
            radio = ttk.Radiobutton(kinds, text=label, value=value, variable=self.kind_var,
                                    command=self._refresh_hint)
            radio.pack(side="left", padx=(0, 14))
            self.kind_radios.append(radio)

        ttk.Label(form, text="시작 번호").grid(row=2, column=0, sticky="w", **pad)
        self.start_entry = ttk.Entry(form, textvariable=self.start_var, width=12)
        self.start_entry.grid(row=2, column=1, sticky="w", **pad)
        ttk.Label(form, text="끝 번호").grid(row=2, column=2, sticky="w", **pad)
        self.end_entry = ttk.Entry(form, textvariable=self.end_var, width=12)
        self.end_entry.grid(row=2, column=3, sticky="w", **pad)

        ttk.Label(form, text="저장 폴더").grid(row=3, column=0, sticky="w", **pad)
        self.save_dir_entry = ttk.Entry(form, textvariable=self.save_dir_var)
        self.save_dir_entry.grid(row=3, column=1, columnspan=3, sticky="ew", **pad)
        folder_buttons = ttk.Frame(form)
        folder_buttons.grid(row=3, column=4, sticky="w")
        self.browse_button = ttk.Button(folder_buttons, text="찾아보기", command=self.on_browse)
        self.browse_button.pack(side="left", padx=(0, 4))
        ttk.Button(folder_buttons, text="폴더 열기",
                   command=lambda: open_folder(Path(self.save_dir_var.get()))).pack(side="left")

        actions = ttk.Frame(form)
        actions.grid(row=4, column=0, columnspan=5, sticky="ew", **pad)
        self.start_button = ttk.Button(actions, text="인증서 받기", command=self.on_start)
        self.start_button.pack(side="left")
        self.stop_button = ttk.Button(actions, text="중단", command=self.on_stop, state="disabled")
        self.stop_button.pack(side="left", padx=(6, 0))
        self.show_browser_check = ttk.Checkbutton(
            actions, text="브라우저 창 표시(문제 확인용)", variable=self.show_browser_var)
        self.show_browser_check.pack(side="left", padx=(16, 0))
        ttk.Label(actions, textvariable=self.queue_var).pack(side="right")

        ttk.Label(outer, text=(f"CAVP 는 cavp\\CAVP_번호.pdf, 엔트로피는 entropy\\번호_환경명.pdf 로 저장합니다. "
                               f"한 번에 최대 {MAX_RANGE}개 번호까지. 작업 중에 범위를 더 넣으면(종류 무관) "
                               "예약되어 끝나는 대로 이어서 받습니다. 같은 번호의 파일은 덮어씁니다."),
                  foreground="#555", wraplength=760, justify="left").pack(anchor="w", pady=(4, 0))

        status = ttk.Frame(outer, padding=(0, 8, 0, 0))
        status.pack(fill="x")
        ttk.Label(status, textvariable=self.status_var).pack(anchor="w")
        bar_row = ttk.Frame(status)
        bar_row.pack(fill="x", pady=(2, 0))
        self.progress = ttk.Progressbar(bar_row, mode="determinate", maximum=1, value=0)
        self.progress.pack(side="left", fill="x", expand=True)
        ttk.Label(bar_row, textvariable=self.progress_var, width=16, anchor="e").pack(side="left")

        results = ttk.LabelFrame(outer, text="결과", padding=6)
        results.pack(fill="both", expand=True, pady=(8, 0))
        columns = ("kind", "number", "environment", "outcome")
        self.tree = ttk.Treeview(results, columns=columns, show="headings", height=9)
        self.tree.heading("kind", text="종류")
        self.tree.heading("number", text="번호")
        self.tree.heading("environment", text="환경명")
        self.tree.heading("outcome", text="결과")
        self.tree.column("kind", width=90, anchor="w", stretch=False)
        self.tree.column("number", width=110, anchor="w", stretch=False)
        self.tree.column("environment", width=200, anchor="w", stretch=False)
        self.tree.column("outcome", width=360, anchor="w")
        self.tree.tag_configure("ok", foreground="#1a7f37")
        self.tree.tag_configure("err", foreground="#b42318")
        tree_scroll = ttk.Scrollbar(results, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=tree_scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        tree_scroll.pack(side="left", fill="y")

        logs = ttk.LabelFrame(outer, text="로그", padding=6)
        logs.pack(fill="both", expand=True, pady=(8, 0))
        self.log_text = tk.Text(logs, height=8, wrap="word", state="disabled")
        log_scroll = ttk.Scrollbar(logs, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=log_scroll.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        log_scroll.pack(side="left", fill="y")

        footer = ttk.Frame(outer)
        footer.pack(fill="x", pady=(4, 0))
        ttk.Label(footer, text=f"{CREDIT} · {COPYRIGHT}", foreground="#888").pack(side="right")
        if GITHUB_URL:  # 왼쪽 아래: 누르면 기본 브라우저로 여는 링크
            link = ttk.Label(footer, text=f"GitHub: {GITHUB_URL}", foreground="#1a5fb4", cursor="hand2")
            link.pack(side="left")
            link.bind("<Button-1>", lambda _e: webbrowser.open(GITHUB_URL))

        if self.user_var.get():
            self.password_entry.focus_set()
        else:
            self.user_entry.focus_set()
        self.root.bind("<Return>", lambda _e: self.on_start())

    # -- 입력 ------------------------------------------------------------------------
    def _refresh_hint(self) -> None:
        """종류·저장 폴더가 바뀌면 시작·끝 번호를 '마지막으로 받은 번호 + 1' 로 맞춘다."""
        hint = next_number_hint(Path(self.save_dir_var.get() or str(DEFAULT_SAVE_DIR)),
                                self.kind_var.get())
        self.start_var.set(str(hint))
        self.end_var.set(str(hint))

    def _read_range(self) -> tuple[int, int] | None:
        start_raw, end_raw = self.start_var.get().strip(), self.end_var.get().strip()
        if not start_raw.isdigit() or not end_raw.isdigit():
            messagebox.showerror(APP_NAME, "시작·끝 번호를 숫자로 입력하세요.")
            return None
        start, end = int(start_raw), int(end_raw)
        if start > end:
            start, end = end, start
        start = max(start, 1)
        if end - start + 1 > MAX_RANGE:
            messagebox.showerror(
                APP_NAME, f"한 번에 최대 {MAX_RANGE}개까지 요청할 수 있습니다 "
                          f"(요청 {end - start + 1}개). 범위를 나눠 넣어 주세요.")
            return None
        return start, end

    def on_browse(self) -> None:
        chosen = filedialog.askdirectory(initialdir=self.save_dir_var.get() or str(APP_DIR),
                                         title="인증서 저장 폴더")
        if chosen:
            self.save_dir_var.set(chosen)
            self._refresh_hint()

    def on_start(self) -> None:
        if self.closing:
            return
        rng = self._read_range()
        if rng is None:
            return
        start, end = rng
        kind = self.kind_var.get()

        if self.running and self.worker is not None:
            outcome, job = self.worker.add(kind, start, end)
            if outcome == "added" and job is not None:
                logging.info("예약 추가: %s", job.label)
                self._advance_range(end)
                return
            if outcome == "duplicate":
                messagebox.showinfo(APP_NAME, "이미 예약되어 있거나 처리 중인 번호입니다.")
                return
            # "closed": 마지막 작업이 막 끝나 브라우저를 닫는 중 → 새 작업으로 시작한다.

        user_id, password = self.user_var.get().strip(), self.password_var.get()
        if not user_id or not password:
            messagebox.showerror(APP_NAME, "KCMVP 계정과 비밀번호를 입력하세요.")
            return
        save_dir = Path(self.save_dir_var.get().strip() or str(DEFAULT_SAVE_DIR))
        try:
            save_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            messagebox.showerror(APP_NAME, f"저장 폴더를 만들 수 없습니다:\n{save_dir}\n{exc}")
            return

        self.config.update({"user_id": user_id, "save_dir": str(save_dir), "kind": kind,
                            "show_browser": bool(self.show_browser_var.get())})
        save_config(self.config)

        ctl = Control(user_id=user_id, password=password, stop_event=threading.Event(),
                      status=lambda text: None)
        worker = Downloader(ctl, save_dir, headless=not self.show_browser_var.get(),
                            events=self.events)
        ctl.status = lambda text, w=worker: self.events.put(("status", w, text))
        self.worker = worker
        _outcome, job = self.worker.add(kind, start, end)
        self._clear_results()
        self._set_running(True)
        logging.info("시작: %s → %s", job.label if job else f"No.{start}~{end}", save_dir)
        self.worker.start()
        self._advance_range(end)

    def on_stop(self) -> None:
        if self.worker is not None and self.running:
            self.worker.stop()
            self.stop_button.configure(state="disabled")
            self.status_var.set("중단 요청 — 진행 중인 건을 정리하는 중입니다 (잠시 걸릴 수 있음)")
            logging.info("중단 요청")

    def on_close(self) -> None:
        if self.running and self.worker is not None and self.worker.is_alive():
            if not messagebox.askyesno(APP_NAME, "작업이 진행 중입니다. 중단하고 종료할까요?"):
                return
            self.closing = True
            self.worker.stop()
            self.status_var.set("종료 중 — 브라우저를 닫는 중입니다")
            self._close_deadline = time.monotonic() + 15
            self.root.after(300, self._close_when_done)
            return
        self._shutdown()

    def _close_when_done(self) -> None:
        if (self.worker is None or not self.worker.is_alive()
                or time.monotonic() > self._close_deadline):
            self._shutdown()
        else:
            self.root.after(300, self._close_when_done)

    def _shutdown(self) -> None:
        if getattr(self, "_shut", False):
            return
        self._shut = True
        self.config.update({"user_id": self.user_var.get().strip(),
                            "save_dir": self.save_dir_var.get().strip(),
                            "kind": self.kind_var.get(),
                            "show_browser": bool(self.show_browser_var.get())})
        save_config(self.config)
        self.root.destroy()

    # -- 상태 반영 ------------------------------------------------------------------
    def _advance_range(self, end: int) -> None:
        self.start_var.set(str(end + 1))
        self.end_var.set(str(end + 1))

    def _set_running(self, running: bool) -> None:
        self.running = running
        state = "disabled" if running else "normal"
        for widget in (self.user_entry, self.password_entry, self.save_dir_entry,
                       self.browse_button, self.show_browser_check):
            widget.configure(state=state)
        self.start_button.configure(text="예약 추가" if running else "인증서 받기")
        self.stop_button.configure(state="normal" if running else "disabled")
        if not running:
            self.queue_var.set("예약 없음")

    def _clear_results(self) -> None:
        for item in self.tree.get_children():
            self.tree.delete(item)
        self.progress.configure(maximum=1, value=0)
        self.progress_var.set("")

    def _append_log(self, text: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", text + "\n")
        lines = int(self.log_text.index("end-1c").split(".")[0])
        if lines > 2000:
            self.log_text.delete("1.0", f"{lines - 2000}.0")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _poll(self) -> None:
        try:
            while True:
                event = self.events.get_nowait()
                self._handle(event)
        except queue.Empty:
            pass
        if not self.closing or (self.worker is not None and self.worker.is_alive()):
            self.root.after(self.POLL_MS, self._poll)

    def _handle(self, event: tuple) -> None:
        kind, source, payload = event[0], event[1], event[2:]
        if source is not None and source is not self.worker:
            return  # 이미 교체된 옛 워커의 뒷정리 이벤트
        if kind == "log":
            self._append_log(payload[0])
        elif kind == "status":
            self.status_var.set(payload[0])
        elif kind == "progress":
            done, total = payload
            self.progress.configure(maximum=max(total, 1), value=done)
            self.progress_var.set(f"{done}/{total} 처리" if total else "")
        elif kind == "queue":
            labels = payload[0]
            self.queue_var.set("예약: " + " · ".join(labels) if labels else "예약 없음")
        elif kind == "job_start":
            self.status_var.set(f"{payload[0]} 처리 시작")
        elif kind == "result":
            result = payload[0]
            label = KIND_LABELS.get(result.get("kind", ""), result.get("kind", ""))
            if "file" in result:
                self.tree.insert("", "end", tags=("ok",), values=(
                    label, result["number"], result.get("environment", ""),
                    f"저장: {Path(result['file']).name}"))
            else:
                self.tree.insert("", "end", tags=("err",), values=(
                    label, result["number"], result.get("environment", ""),
                    f"실패: {result.get('error', '')}"))
            children = self.tree.get_children()
            if children:
                self.tree.see(children[-1])
        elif kind == "job_done":
            label, saved, total = payload
            self.status_var.set(f"{label} 끝 — {saved}/{total} 저장")
        elif kind == "finished":
            ok, message = payload
            self.status_var.set(message)
            self._set_running(False)
            self.worker = None   # 종료 중이면 _close_when_done 이 이걸 보고 창을 닫는다
            self._refresh_hint()
            logging.info(message)
            if not ok and not self.closing:
                messagebox.showerror(APP_NAME, message)

    def run(self) -> None:
        self.root.mainloop()


def main() -> None:
    if tk is None:
        sys.exit("tkinter 가 없습니다. python.org 배포판의 Python(기본으로 tcl/tk 포함)을 쓰세요.")
    if sys.platform == "win32":
        try:  # 고해상도 화면에서 글자가 흐려지지 않게
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:  # noqa: BLE001
            pass
    App().run()


if __name__ == "__main__":
    main()
