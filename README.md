# KCMVP 인증서 다운로더 (Windows 단독 실행)

**인증서 받기(CAVP)** 와 **엔트로피 인증서 받기** 기능의 GUI 프로그램입니다.
서버·러너·브라우저 설치 없이 Windows PC 에서 exe 하나로 돕니다.

- KCMVP 계정 · 비밀번호, 인증서 종류(CAVP / 엔트로피), 시작 번호 · 끝 번호를 넣고 **[인증서 받기]**
- CAVP: 자가검증 목록 CAVP 탭에서 번호 범위의 행마다 "결과 확인 → 전자서명"
  → `<저장 폴더>\cavp\CAVP_<번호>.pdf`
- 엔트로피: Entropy 탭에서 "세부결과 → 전자 서명"
  → `<저장 폴더>\entropy\<번호>_<환경명>.pdf` (환경명 = 목록의 OS 열, 웹앱과 같은 규칙)
- 작업 중에 범위를 더 넣으면(종류 무관) **예약**되어 지금 작업이 끝나는 대로 이어서 받습니다
- **[중단]** 을 누르면 진행 중인 건을 정리하고 남은 예약은 취소합니다
- 비밀번호는 저장하지 않습니다 (계정 ID · 저장 폴더 · 종류만 `kcmvp_cert_downloader.json` 에 기억)

제작: **Myungjun Kim** · © 2026 Myungjun Kim · GitHub: <https://github.com/samdae5back.kcmvp_cert_downloader> · 라이선스: **GNU AGPL v3** (아래 참고)
창 제목과 오른쪽 아래 푸터, exe 파일 속성(자세히 탭)에 같은 표기가 들어갑니다. 푸터의 GitHub 링크는 누르면 브라우저에서 열립니다.

> CAVP 인증서 이름이 웹앱(`환경_비트_아키텍처_번호.pdf`)과 다른 이유: 그 환경 정보는 웹앱의
> 운영환경 매핑에서 오는 것이라 사이트 목록에는 없습니다. 단독 도구는 번호로만 이름을 짓습니다.

## 빌드 (Windows 에서)

1. [python.org](https://www.python.org/downloads/windows/) 에서 Python 3.10 ~ 3.14 설치
   (기본 옵션 그대로 — tcl/tk 포함, "Add python.exe to PATH" 체크 권장)
2. 이 폴더(`tools/kcmvp_cert_downloader`)를 Windows PC 로 복사
3. `build_windows.bat` 더블클릭 (또는 명령 프롬프트에서 실행)

배치 파일이 하는 일:

| 단계 | 내용 |
|---|---|
| 가상환경 | `.venv` 생성 |
| 의존성 | `playwright==1.64.1`, `pyinstaller` 설치 (Python 3.10~3.14 모두 미리 빌드된 wheel 로 설치됨) |
| 빌드 | `kcmvp_cert_downloader.spec` 으로 `dist\KcmvpCertDownloader\` 생성 (onedir) |
| 브라우저 | 크로미움을 `dist\KcmvpCertDownloader\browsers\` 에 설치 |

결과물은 **`dist\KcmvpCertDownloader\` 폴더 전체**입니다 (약 300 MB, 대부분 크로미움).
zip 으로 묶어 전달하고, 받은 쪽은 아무 폴더에나 풀어 `KcmvpCertDownloader.exe` 를 실행하면 됩니다.
`browsers\` 폴더가 빠지면 실행되지 않습니다.

> 서명되지 않은 exe 라 처음 실행 때 SmartScreen 경고가 뜰 수 있습니다 — "추가 정보 → 실행".

## 사용

```
KCMVP 계정   [        ]   비밀번호 [        ]
인증서 종류  (•) CAVP 인증서  ( ) 엔트로피 인증서
시작 번호    [ 1  ]       끝 번호  [ 10 ]
저장 폴더    [ C:\...\KcmvpCertDownloader\certificates ] [찾아보기] [폴더 열기]
[인증서 받기] [중단]  ☐ 브라우저 창 표시
```

- 시작 번호는 선택한 종류의 폴더에 이미 있는 가장 큰 번호 + 1 이 기본값입니다.
- 작업이 돌기 시작하면 버튼이 **[예약 추가]** 로 바뀝니다. 종류·범위를 바꿔 누르면 예약 목록에 쌓입니다.
  같은 종류에서 이미 예약된 번호는 건너뜁니다.
- CAVP 는 **검증 완료(통과)** 상태인 행만 인증서가 있습니다. 아직인 행은 "'결과 확인' 버튼 없음" 으로
  표시되고 현재 상태를 함께 보여줍니다.
- 한 건당 수 분 걸릴 수 있습니다(사이트가 PDF 를 그려 서명하는 시간). 그동안 세션은 자동으로 연장됩니다.
- 목록에 없는 번호는 결과 표에 한 줄로 모아 표시합니다.
- 같은 번호의 파일이 이미 있으면 **덮어씁니다**.
- 큐가 비면 브라우저를 닫습니다. 다시 **[인증서 받기]** 를 누르면 새로 로그인합니다.
- 문제가 생기면 **브라우저 창 표시** 를 켜고 다시 실행해 화면을 직접 보세요.
  로그는 `logs\kcmvp_cert_downloader.log`, 막힌 화면의 HTML 은 `logs\html\` 에 남습니다.

## 개발용 실행 (빌드 없이)

```
py -3 -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python -m playwright install chromium
.venv\Scripts\python kcmvp_cert_downloader.py
```

환경변수로 바꿀 수 있는 값은 러너와 같습니다: `KCMVP_BASE_URL`, `KCMVP_CERT_SIGN_TIMEOUT`(초, 기본 1200),
`KCMVP_CERT_DIALOG_TIMEOUT`(초, 기본 120), `KCMVP_SESSION_KEEPALIVE_SECONDS`(기본 120),
`KCMVP_CHECK_PAGE_LIMIT`(CAVP 목록을 훑을 최대 페이지, 기본 20).

## 유지보수 메모

Playwright 버전은 서버(1.45.0)와 다르게 최신판을 씁니다. 1.45.0 이 고정하는 greenlet 3.0.3 은 Python 3.13/3.14 용
wheel 이 없어 Windows 에서 `Failed building wheel for greenlet` 로 설치가 실패하기 때문입니다. 두 쪽은 코드를 공유하지
않으므로 버전이 달라도 됩니다.

사이트 셀렉터와 전자서명 응답 캡처(XHR 후킹) · 세션 유지 규칙은 `scripts/kcmvp_sample_runner.py` 의
CAVP 인증서(`download_row_certificate`, `run_download_job`) · 엔트로피 인증서(`run_entropy_cert_job`) 부분을
그대로 옮긴 것입니다. KCMVP 사이트가 바뀌어 러너를 고치면 이 파일의 같은 함수도 맞춰야 합니다.
러너와 달리 `STATE`(runner_state) · 웹서버 · 데이터 디렉터리에는 의존하지 않습니다.

## 라이선스

이 프로그램은 [GNU Affero General Public License v3.0](LICENSE) 으로 배포합니다.

- 자유롭게 사용·복제·수정·재배포할 수 있습니다.
- 수정해서 배포하거나 **네트워크로 서비스하는 경우에도** 같은 라이선스로 소스 코드를 공개해야 합니다.
- 보증은 없습니다. 자세한 조건은 `LICENSE` 파일을 보세요.
