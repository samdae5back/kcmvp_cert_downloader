# -*- mode: python ; coding: utf-8 -*-
# PyInstaller 스펙 — build_windows.bat 가 쓴다.
#   dist\EntropyCertDownloader\EntropyCertDownloader.exe  (onedir: 실행이 빠르고 디버그가 쉽다)
# 크로미움은 번들에 넣지 않고 exe 옆 browsers\ 폴더에 따로 설치한다(배치 파일 참고).
# Playwright 드라이버(node.exe + package)는 playwright 가 제공하는 PyInstaller 훅이 모으지만,
# 훅 등록이 빠지는 환경을 대비해 여기서도 명시한다.
# PyInstaller 6.x 문법 (5.x 의 block_cipher / a.zipped_data 는 6 에서 없어졌다).
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

a = Analysis(
    ["entropy_cert_downloader.py"],
    pathex=[],
    binaries=[],
    datas=collect_data_files("playwright"),
    hiddenimports=collect_submodules("playwright"),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="EntropyCertDownloader",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,          # GUI 앱 — 콘솔 창 없음 (로그는 logs\entcert_downloader.log)
    disable_windowed_traceback=False,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="EntropyCertDownloader",
)
