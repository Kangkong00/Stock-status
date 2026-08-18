#!/usr/bin/env python3
"""
재고관리 프로그램 실행기.

이 PC 안에서만 도는 서버를 띄우고 브라우저를 연다.
인터넷 연결이 필요 없고, 외부에서 접속할 수 없다 (127.0.0.1 전용).
"""
from __future__ import annotations

import argparse
import socket
import sys
import threading
import time
import webbrowser

from app import config, db
from app.scheduler import scheduler
from app.web import create_app

BANNER = r"""
  ┌────────────────────────────────────────────────┐
  │            재  고  관  리                      │
  │   입출고만 기록하면 재고는 자동으로 맞춰집니다  │
  └────────────────────────────────────────────────┘
"""


def find_port(host: str, start: int, tries: int = 20) -> int:
    """포트가 이미 쓰이고 있으면 다음 번호를 찾아본다."""
    for offset in range(tries):
        port = start + offset
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind((host, port))
                return port
            except OSError:
                continue
    raise SystemExit(f"사용 가능한 포트를 찾지 못했습니다 ({start}~{start + tries}).")


def open_browser(url: str, delay: float = 1.2) -> None:
    def _go():
        time.sleep(delay)
        try:
            webbrowser.open(url)
        except Exception:
            pass
    threading.Thread(target=_go, daemon=True).start()


def main() -> int:
    ap = argparse.ArgumentParser(description="재고관리 프로그램")
    ap.add_argument("--port", type=int, default=config.PORT)
    ap.add_argument("--host", default=config.HOST)
    ap.add_argument("--no-browser", action="store_true", help="브라우저를 자동으로 열지 않음")
    ap.add_argument("--no-scheduler", action="store_true", help="메일 자동발송을 끔")
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()

    print(BANNER)
    config.ensure_dirs()
    db.init_db()

    port = find_port(args.host, args.port)
    url = f"http://{args.host}:{port}/"

    print(f"  데이터 파일 : {config.DB_PATH}")
    print(f"  접속 주소   : {url}")
    print(f"  종료        : 이 창에서 Ctrl+C")
    print()

    app = create_app()

    if not args.no_scheduler:
        scheduler.start()
        print("  메일 자동발송 스케줄러가 켜졌습니다. (설정 화면에서 시각 변경 가능)")

    if not args.no_browser:
        open_browser(url)

    try:
        if args.debug:
            app.run(host=args.host, port=port, debug=True, use_reloader=False)
        else:
            from waitress import serve
            serve(app, host=args.host, port=port, threads=8, _quiet=True)
    except KeyboardInterrupt:
        print("\n  종료합니다. 데이터는 저장되어 있습니다.")
    finally:
        scheduler.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
