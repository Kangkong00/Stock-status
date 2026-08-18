@echo off
chcp 65001 > nul
cd /d "%~dp0"
title 재고관리

where python > nul 2>&1
if errorlevel 1 (
  echo.
  echo   [!] 파이썬이 설치되어 있지 않습니다.
  echo       https://www.python.org/downloads/ 에서 설치하면서
  echo       "Add Python to PATH" 를 반드시 체크해 주세요.
  echo.
  pause
  exit /b 1
)

if not exist ".venv" (
  echo   최초 실행입니다. 필요한 것들을 설치합니다. 잠시만 기다려 주세요...
  python -m venv .venv
  .venv\Scripts\python -m pip install --upgrade pip -q
  .venv\Scripts\pip install -r requirements.txt -q
  echo   설치가 끝났습니다.
  echo.
)

.venv\Scripts\python run.py
pause
