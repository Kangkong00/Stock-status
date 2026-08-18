"""실행 환경 설정. 데이터는 전부 이 PC 안에만 저장된다."""
from __future__ import annotations

import os
import sys
from pathlib import Path


def app_root() -> Path:
    """PyInstaller 로 묶였을 때와 소스 실행일 때 모두 올바른 기준 경로를 준다."""
    if getattr(sys, "frozen", False):          # .exe 로 실행 중
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


def resource_root() -> Path:
    """템플릿/스키마처럼 패키지에 동봉된 읽기 전용 리소스의 위치."""
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS) / "app"      # type: ignore[attr-defined]
    return Path(__file__).resolve().parent


DATA_DIR = Path(os.environ.get("STOCK_DATA_DIR", app_root() / "data"))
DB_PATH = Path(os.environ.get("STOCK_DB_PATH", DATA_DIR / "stock.db"))
BACKUP_DIR = DATA_DIR / "backups"
UPLOAD_DIR = DATA_DIR / "uploads"
EXPORT_DIR = DATA_DIR / "exports"
SCHEMA_PATH = resource_root() / "schema.sql"

HOST = os.environ.get("STOCK_HOST", "127.0.0.1")   # 외부 노출 없음
PORT = int(os.environ.get("STOCK_PORT", "8765"))

# 유사도 매칭 임계값
AUTO_ACCEPT_SCORE = 0.94   # 이 이상이면 사람 확인 없이 자동 매칭 + 별칭 학습
SUGGEST_SCORE = 0.45       # 이 이상이면 후보로 제시 (확인 클릭 1번이므로 넉넉하게)
MAX_CANDIDATES = 5

DEFAULT_SETTINGS = {
    "company_name": "",
    "report_daily_enabled": "1",
    "report_daily_time": "08:30",
    "report_weekly_enabled": "1",
    "report_weekly_day": "1",          # 1=월요일 ... 7=일요일
    "report_weekly_time": "08:30",
    "report_to": "",                   # 수신자, 쉼표 구분
    "smtp_host": "",
    "smtp_port": "587",
    "smtp_user": "",
    "smtp_password": "",
    "smtp_security": "starttls",       # starttls | ssl | none
    "smtp_from": "",
    "dormant_days": "90",              # 이 일수 이상 미입출고면 장기미사용
    "backup_keep": "30",
}


def ensure_dirs() -> None:
    for d in (DATA_DIR, BACKUP_DIR, UPLOAD_DIR, EXPORT_DIR):
        d.mkdir(parents=True, exist_ok=True)
