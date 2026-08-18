#!/bin/bash
# macOS / Linux 실행 스크립트
cd "$(dirname "$0")"
if [ ! -d ".venv" ]; then
  echo "  최초 실행입니다. 필요한 것들을 설치합니다..."
  python3 -m venv .venv
  .venv/bin/pip install --upgrade pip -q
  .venv/bin/pip install -r requirements.txt -q
  echo "  설치가 끝났습니다."
fi
.venv/bin/python run.py
