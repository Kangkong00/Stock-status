#!/bin/bash
cd "$(dirname "$0")" || exit 1
if [ ! -f run.py ]; then
  echo "  [문제] run.py 를 찾을 수 없습니다. 압축을 푼 폴더에서 실행해 주세요."
  read -r -p "  엔터를 누르면 닫힙니다." _
  exit 1
fi
PY=python3
command -v $PY >/dev/null 2>&1 || PY=python
command -v $PY >/dev/null 2>&1 || { echo "  [문제] 파이썬이 없습니다. https://www.python.org/downloads/"; exit 1; }
if [ ! -x ".venv/bin/python" ]; then
  echo "  최초 실행입니다. 필요한 것을 설치합니다. 1~3분 걸립니다..."
  $PY -m venv .venv || { echo "  [문제] 실행 환경을 만들지 못했습니다."; exit 1; }
  .venv/bin/python -m pip install --upgrade pip -q
  .venv/bin/pip install -r requirements.txt -q || { echo "  [문제] 구성요소 설치 실패. 인터넷을 확인하세요."; exit 1; }
  echo "  설치가 끝났습니다."
fi
.venv/bin/python run.py
