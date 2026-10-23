@echo off
chcp 65001 >nul
REM ============================================================================
REM  KCMVP 엔트로피 인증서 다운로더 — Windows 빌드 스크립트
REM
REM  준비: Windows 10/11, python.org 의 Python 3.10 이상 (tcl/tk 포함 기본 설치),
REM        인터넷 연결 (pip 패키지 + 크로미움 다운로드)
REM  결과: dist\EntropyCertDownloader\  폴더 전체가 배포 단위다 (zip 으로 묶어 전달)
REM         ├─ EntropyCertDownloader.exe
REM         ├─ _internal\            (파이썬 런타임 + Playwright 드라이버)
REM         └─ browsers\             (크로미움 — 프로그램이 실행 시 이 폴더를 본다)
REM ============================================================================
setlocal
cd /d "%~dp0"
set APP=EntropyCertDownloader

set PY=
where py >nul 2>nul && set PY=py -3
if "%PY%"=="" ( where python >nul 2>nul && set PY=python )
if "%PY%"=="" (
    echo [오류] Python 을 찾을 수 없습니다. https://www.python.org/downloads/ 에서 설치하세요.
    exit /b 1
)

if not exist .venv (
    echo [1/5] 가상환경 생성
    %PY% -m venv .venv || goto :fail
)
call .venv\Scripts\activate.bat || goto :fail

echo [2/5] 의존성 설치 (playwright, pyinstaller)
python -m pip install --upgrade pip >nul
python -m pip install -r requirements.txt || goto :fail

echo [3/5] 이전 빌드 정리
if exist build rmdir /s /q build
if exist dist\%APP% rmdir /s /q dist\%APP%

echo [4/5] PyInstaller 빌드
python -m PyInstaller --noconfirm --clean entropy_cert_downloader.spec || goto :fail

echo [5/5] 크로미움 설치 -^> dist\%APP%\browsers
set PLAYWRIGHT_BROWSERS_PATH=%CD%\dist\%APP%\browsers
python -m playwright install chromium || goto :fail

echo.
echo 빌드 완료: dist\%APP%\%APP%.exe
echo dist\%APP% 폴더 전체를 압축해 배포하세요 (browsers 폴더가 빠지면 실행되지 않습니다).
exit /b 0

:fail
echo.
echo [오류] 빌드에 실패했습니다. 위 메시지를 확인하세요.
exit /b 1
