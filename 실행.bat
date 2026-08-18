@echo off
setlocal
title 재고관리
cd /d "%~dp0"
set "HERE=%~dp0"
set "PYVER=?"

echo.
echo   ==========================================
echo             재  고  관  리
echo   ==========================================
echo.

rem --- 압축을 풀지 않고 ZIP 안에서 바로 실행했는지 -------------------
echo "%HERE%" | find /i "AppData\Local\Temp" >nul
if not errorlevel 1 goto ZIPWARN
if not exist "run.py" goto NOFILES

rem --- 파이썬 찾기 : py 런처 우선, 없으면 python ---------------------
set "PY="
py -3 -c "import sys" >nul 2>&1
if not errorlevel 1 set "PY=py -3"
if defined PY goto GOTPY

python -c "import sys" >nul 2>&1
if not errorlevel 1 set "PY=python"
if defined PY goto GOTPY
goto NOPYTHON

:GOTPY
for /f "delims=" %%v in ('%PY% -c "import sys;print(sys.version.split()[0])" 2^>nul') do set "PYVER=%%v"
echo   파이썬 %PYVER% 확인
echo.

if exist ".venv\Scripts\python.exe" goto RUN

echo   최초 실행입니다. 필요한 것을 설치합니다.
echo   1~3분 걸립니다. 이 창을 닫지 마세요.
echo.
%PY% -m venv ".venv"
if not exist ".venv\Scripts\python.exe" goto VENVFAIL

".venv\Scripts\python.exe" -m pip install --upgrade pip --quiet
".venv\Scripts\python.exe" -m pip install -r requirements.txt --quiet
if errorlevel 1 goto PIPFAIL
echo   설치가 끝났습니다.
echo.

:RUN
echo   프로그램을 시작합니다. 잠시 후 브라우저가 열립니다.
echo   끝내려면 이 창에서 Ctrl+C 를 누르세요.
echo.
".venv\Scripts\python.exe" run.py
echo.
echo   프로그램이 종료되었습니다.
pause
exit /b 0

rem ===================================================================
:ZIPWARN
echo   [문제] 압축을 풀지 않고 실행하셨습니다.
echo.
echo     내려받은 ZIP 파일에 마우스 오른쪽 클릭 - "압축 풀기" 를 하신 뒤,
echo     풀린 폴더 안의 실행.bat 을 다시 눌러 주세요.
echo.
pause
exit /b 1

:NOFILES
echo   [문제] 프로그램 파일을 찾을 수 없습니다. run.py 가 없습니다.
echo.
echo     실행.bat 이 압축 푼 폴더 안에 다른 파일들과 같이 있어야 합니다.
echo     지금 위치: %HERE%
echo.
pause
exit /b 1

:NOPYTHON
echo   [문제] 파이썬을 찾을 수 없습니다.
echo.
echo     https://www.python.org/downloads/ 에서 내려받아 설치해 주세요.
echo.
echo     설치 첫 화면 맨 아래 "Add Python to PATH" 를 반드시 체크하세요.
echo     이미 설치하셨다면 이 체크를 빠뜨리신 것입니다.
echo     설치 파일을 다시 실행해 Modify 로 추가하거나,
echo     지우고 체크한 상태로 다시 설치하시면 됩니다.
echo.
echo     설치 후 이 창을 닫고 실행.bat 을 다시 눌러 주세요.
echo.
pause
exit /b 1

:VENVFAIL
echo.
echo   [문제] 실행 환경을 만들지 못했습니다.
echo.
echo     Microsoft Store 에서 설치한 파이썬이면 이 문제가 생깁니다.
echo     https://www.python.org/downloads/ 의 정식 설치본을 쓰세요.
echo.
pause
exit /b 1

:PIPFAIL
echo.
echo   [문제] 필요한 구성요소를 내려받지 못했습니다.
echo.
echo     인터넷 연결을 확인해 주세요. 설치할 때만 인터넷이 필요합니다.
echo     회사 네트워크면 방화벽에 막혔을 수 있습니다.
echo.
pause
exit /b 1
