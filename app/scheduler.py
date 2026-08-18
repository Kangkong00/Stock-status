"""
일일/주간 리포트 자동 발송 스케줄러.

프로그램이 켜져 있는 동안 1분마다 시각을 확인한다.
notify_log 의 UNIQUE 제약으로 같은 기간에 두 번 보내지 않는다.
PC 를 꺼 두어 발송 시각을 놓쳤다면, 다음 실행 시 그날 안에 한 번 따라잡는다.
"""
from __future__ import annotations

import threading
import traceback
from datetime import date, datetime, timedelta

from . import db, mailer


class ReportScheduler:
    def __init__(self, interval: int = 60):
        self.interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_check: datetime | None = None
        self.last_result: str = ""

    # ------------------------------------------------------------ 제어
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="report-scheduler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        # 기동 직후엔 잠시 쉰다 (초기 설정 중 발송 방지)
        if self._stop.wait(20):
            return
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                self.last_result = traceback.format_exc(limit=3)
            self._stop.wait(self.interval)

    # ------------------------------------------------------------ 판정
    def tick(self, now: datetime | None = None) -> list[str]:
        now = now or datetime.now()
        self.last_check = now
        done: list[str] = []

        if not mailer.is_configured():
            return done

        s = db.get_settings()
        if s.get("report_daily_enabled") == "1" and self._due(now, s.get("report_daily_time", "08:30")):
            if self._send("daily", now.date().isoformat()):
                done.append("daily")

        if s.get("report_weekly_enabled") == "1":
            weekday_ok = str(now.isoweekday()) == str(s.get("report_weekly_day", "1"))
            if weekday_ok and self._due(now, s.get("report_weekly_time", "08:30")):
                iso = now.isocalendar()
                if self._send("weekly", f"{iso[0]}-W{iso[1]:02d}"):
                    done.append("weekly")
        return done

    @staticmethod
    def _due(now: datetime, hhmm: str) -> bool:
        """설정 시각이 지났는지. PC 를 꺼 뒀다 켰어도 그날 안이면 따라잡는다."""
        try:
            h, m = (int(x) for x in str(hhmm).split(":")[:2])
        except (ValueError, TypeError):
            h, m = 8, 30
        return (now.hour, now.minute) >= (h, m)

    def _send(self, kind: str, period_key: str) -> bool:
        already = db.query_one(
            "SELECT id FROM notify_log WHERE kind=? AND period_key=? AND ok=1",
            (kind, period_key))
        if already:
            return False
        try:
            res = mailer.send_report(kind)
            detail = res.get("subject") or res.get("reason", "")
            ok = 1 if res.get("sent") else 1     # 보낼 게 없어 건너뛴 것도 '처리 완료'로 기록
            self.last_result = f"{kind} {period_key}: {detail}"
        except Exception as exc:
            ok, detail = 0, str(exc)[:400]
            self.last_result = f"{kind} {period_key} 실패: {detail}"
        with db.tx() as c:
            c.execute(
                "INSERT INTO notify_log(kind, period_key, ok, detail) VALUES (?,?,?,?) "
                "ON CONFLICT(kind, period_key) DO UPDATE SET ok=excluded.ok, "
                "detail=excluded.detail, sent_at=datetime('now','localtime')",
                (kind, period_key, ok, detail))
        return bool(ok)

    # ------------------------------------------------------------ 상태 조회
    def status(self) -> dict:
        s = db.get_settings()
        rows = db.query("SELECT * FROM notify_log ORDER BY id DESC LIMIT 5")
        return {
            "running": bool(self._thread and self._thread.is_alive()),
            "configured": mailer.is_configured(),
            "daily": {"enabled": s.get("report_daily_enabled") == "1",
                      "time": s.get("report_daily_time")},
            "weekly": {"enabled": s.get("report_weekly_enabled") == "1",
                       "day": s.get("report_weekly_day"), "time": s.get("report_weekly_time")},
            "last_check": self.last_check.strftime("%Y-%m-%d %H:%M") if self.last_check else None,
            "last_result": self.last_result,
            "recent": [dict(r) for r in rows],
        }


scheduler = ReportScheduler()
