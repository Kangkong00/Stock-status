#!/usr/bin/env python3
"""
윈도우 실행 파일(.exe) 빌드 스크립트.

윈도우 PC 에서 다음을 실행하세요.
    pip install -r requirements.txt pyinstaller
    python build_exe.py

dist/재고관리.exe 하나가 만들어집니다. 더블클릭하면 브라우저가 열립니다.
데이터는 exe 옆의 data 폴더에 저장되므로, 폴더째 옮기면 그대로 이동합니다.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
NAME = "재고관리"


def main() -> int:
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("PyInstaller 가 없습니다.  pip install pyinstaller  후 다시 실행하세요.")
        return 1

    sep = ";" if sys.platform.startswith("win") else ":"
    args = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean", "--onefile",
        "--name", NAME,
        # 템플릿/정적파일/스키마를 실행 파일 안에 동봉한다
        "--add-data", f"{ROOT/'app'/'schema.sql'}{sep}app",
        "--add-data", f"{ROOT/'seed'/'stock.db'}{sep}seed",
        "--add-data", f"{ROOT/'app'/'web'/'templates'}{sep}app/web/templates",
        "--add-data", f"{ROOT/'app'/'web'/'static'}{sep}app/web/static",
        "--hidden-import", "waitress",
        "--hidden-import", "openpyxl.cell._writer",
        "--collect-submodules", "openpyxl",
        # 쓰지 않는 무거운 패키지는 제외해 용량을 줄인다
        "--exclude-module", "tkinter",
        "--exclude-module", "matplotlib",
        "--exclude-module", "numpy",
        "--exclude-module", "PIL",
        "--exclude-module", "pytest",
        str(ROOT / "run.py"),
    ]

    print("빌드를 시작합니다. 몇 분 걸릴 수 있습니다...\n")
    rc = subprocess.call(args, cwd=ROOT)
    if rc != 0:
        print("\n빌드에 실패했습니다.")
        return rc

    exe = ROOT / "dist" / (NAME + (".exe" if sys.platform.startswith("win") else ""))
    print("\n" + "=" * 56)
    print(f"  완료: {exe}")
    if exe.exists():
        print(f"  크기: {exe.stat().st_size / 1024 / 1024:.1f} MB")
    print("=" * 56)
    print("\n  이 파일 하나만 원하는 폴더에 두고 더블클릭하면 실행됩니다.")
    print("  데이터는 실행 파일 옆 data 폴더에 저장됩니다.")
    print("  백신이 처음 실행을 막으면 '허용'을 눌러주세요 (PyInstaller 특성입니다).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
