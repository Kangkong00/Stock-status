"""
집계와 리포트.

"내가 안 봐도 시스템이 먼저 찾아온다"를 담당하는 부분.
대시보드 화면과 이메일 리포트가 같은 함수를 쓴다.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from . import db, models


def _days_ago(n: int) -> str:
    return (date.today() - timedelta(days=n)).isoformat()


# ---------------------------------------------------------------- 요약
def summary() -> dict:
    row = db.query_one(
        """SELECT COUNT(*)                                              AS item_count,
                  SUM(CASE WHEN stock_status='LOW' THEN 1 ELSE 0 END)   AS low_count,
                  SUM(CASE WHEN stock_status='OUT' THEN 1 ELSE 0 END)   AS out_count,
                  COALESCE(SUM(stock_value), 0)                         AS total_value,
                  COALESCE(SUM(on_hand), 0)                             AS total_qty
             FROM v_stock WHERE active = 1"""
    )
    d = dict(row) if row else {}
    d["pending_count"] = db.scalar("SELECT COUNT(*) FROM pending_names WHERE status='open'", (), 0)
    d["alert_count"] = (d.get("low_count") or 0) + (d.get("out_count") or 0)

    today = models.today()
    d["today_in"] = db.scalar(
        "SELECT COALESCE(SUM(qty),0) FROM transactions WHERE txn_date=? AND txn_type='IN' AND voided=0",
        (today,), 0)
    d["today_out"] = db.scalar(
        "SELECT COALESCE(SUM(qty),0) FROM transactions WHERE txn_date=? AND txn_type='OUT' AND voided=0",
        (today,), 0)
    d["today_txn"] = db.scalar(
        "SELECT COUNT(*) FROM transactions WHERE txn_date=? AND voided=0", (today,), 0)

    last = db.query_one("SELECT MAX(txn_date) AS d FROM transactions WHERE voided=0")
    d["last_txn_date"] = last["d"] if last else None
    return d


# ---------------------------------------------------------------- 발주 필요
def reorder_list(limit: int = 200) -> list[dict]:
    """
    재고기준에 도달했거나 품절된 품목.
    '얼마나 부족한지'와 '얼마를 발주해야 하는지'까지 계산해서 준다.
    """
    rows = db.query(
        """SELECT * FROM v_stock
            WHERE active = 1 AND stock_status IN ('LOW','OUT')
            ORDER BY CASE stock_status WHEN 'OUT' THEN 0 ELSE 1 END,
                     (reorder_point - on_hand) DESC
            LIMIT ?""",
        (limit,),
    )
    out = []
    for r in rows:
        d = dict(r)
        shortage = max(0.0, (r["reorder_point"] or 0) - (r["on_hand"] or 0))
        d["shortage"] = shortage
        # 권장발주량이 지정돼 있으면 그걸, 없으면 부족분을 채우는 양
        d["suggest_qty"] = r["reorder_qty"] if r["reorder_qty"] else shortage
        d["suggest_amount"] = d["suggest_qty"] * (r["unit_cost"] or 0)
        d["avg_daily_out"] = _avg_daily_out(r["item_id"])
        d["days_left"] = round((r["on_hand"] / d["avg_daily_out"]), 1) if d["avg_daily_out"] > 0 else None
        out.append(d)
    return out


def _avg_daily_out(item_id: int, window: int = 90) -> float:
    """최근 window 일 평균 일일 출고량. 소진 예상일 계산용."""
    total = db.scalar(
        "SELECT COALESCE(SUM(qty),0) FROM transactions "
        "WHERE item_id=? AND txn_type='OUT' AND voided=0 AND txn_date >= ?",
        (item_id, _days_ago(window)), 0)
    return round(total / window, 4) if total else 0.0


# ---------------------------------------------------------------- 장기 미사용
def dormant_items(days: int | None = None, limit: int = 100) -> list[dict]:
    """오래 움직이지 않은 재고. 자금이 묶여 있는 곳."""
    days = days or int(db.get_setting("dormant_days", "90") or 90)
    cutoff = _days_ago(days)
    rows = db.query(
        """SELECT * FROM v_stock
            WHERE active = 1 AND on_hand > 0
              AND (last_move_date IS NULL OR last_move_date < ?)
            ORDER BY stock_value DESC LIMIT ?""",
        (cutoff, limit),
    )
    out = []
    for r in rows:
        d = dict(r)
        if r["last_move_date"]:
            try:
                d["idle_days"] = (date.today() - date.fromisoformat(r["last_move_date"])).days
            except ValueError:
                d["idle_days"] = None
        else:
            d["idle_days"] = None
        out.append(d)
    return out


# ---------------------------------------------------------------- 기간 집계
def movement(date_from: str, date_to: str, limit: int = 500) -> list[dict]:
    """기간 내 품목별 입출고 집계."""
    return [dict(r) for r in db.query(
        """SELECT i.id AS item_id, i.code, i.name, i.spec, i.unit, i.unit_cost,
                  SUM(CASE WHEN t.txn_type='IN'  THEN t.qty ELSE 0 END) AS in_qty,
                  SUM(CASE WHEN t.txn_type='OUT' THEN t.qty ELSE 0 END) AS out_qty,
                  SUM(CASE WHEN t.txn_type='ADJ' THEN t.signed_qty ELSE 0 END) AS adj_qty,
                  COUNT(*) AS txn_count
             FROM transactions t JOIN items i ON i.id = t.item_id
            WHERE t.voided = 0 AND t.txn_date BETWEEN ? AND ?
            GROUP BY i.id ORDER BY out_qty DESC, in_qty DESC LIMIT ?""",
        (date_from, date_to, limit))]


def opening_closing(item_id: int, date_from: str, date_to: str) -> dict:
    """기초재고 / 기간 입출고 / 기말재고. 수불부의 기본 단위."""
    opening = db.scalar(
        "SELECT COALESCE(SUM(signed_qty),0) FROM transactions "
        "WHERE item_id=? AND voided=0 AND txn_date < ?", (item_id, date_from), 0)
    row = db.query_one(
        """SELECT COALESCE(SUM(CASE WHEN txn_type='IN'  THEN qty END),0) AS in_qty,
                  COALESCE(SUM(CASE WHEN txn_type='OUT' THEN qty END),0) AS out_qty,
                  COALESCE(SUM(CASE WHEN txn_type='ADJ' THEN signed_qty END),0) AS adj_qty
             FROM transactions
            WHERE item_id=? AND voided=0 AND txn_date BETWEEN ? AND ?""",
        (item_id, date_from, date_to))
    d = dict(row) if row else {"in_qty": 0, "out_qty": 0, "adj_qty": 0}
    d["opening"] = opening
    d["closing"] = opening + d["in_qty"] - d["out_qty"] + d["adj_qty"]
    return d


def daily_trend(days: int = 30) -> list[dict]:
    """최근 N일 일별 입출고 건수/수량. 대시보드 그래프용."""
    start = _days_ago(days - 1)
    rows = db.query(
        """SELECT txn_date,
                  SUM(CASE WHEN txn_type='IN'  THEN qty ELSE 0 END) AS in_qty,
                  SUM(CASE WHEN txn_type='OUT' THEN qty ELSE 0 END) AS out_qty,
                  COUNT(*) AS cnt
             FROM transactions WHERE voided=0 AND txn_date >= ?
            GROUP BY txn_date ORDER BY txn_date""", (start,))
    by_date = {r["txn_date"]: dict(r) for r in rows}
    out = []
    for i in range(days):
        d = (date.today() - timedelta(days=days - 1 - i)).isoformat()
        out.append(by_date.get(d, {"txn_date": d, "in_qty": 0, "out_qty": 0, "cnt": 0}))
    return out


def category_breakdown() -> list[dict]:
    return [dict(r) for r in db.query(
        """SELECT COALESCE(NULLIF(category,''),'(미분류)') AS category,
                  COUNT(*) AS item_count, COALESCE(SUM(on_hand),0) AS qty,
                  COALESCE(SUM(stock_value),0) AS value,
                  SUM(CASE WHEN stock_status IN ('LOW','OUT') THEN 1 ELSE 0 END) AS alert_count
             FROM v_stock WHERE active=1
            GROUP BY category ORDER BY value DESC""")]


def top_movers(days: int = 30, limit: int = 10) -> list[dict]:
    return [dict(r) for r in db.query(
        """SELECT i.code, i.name, i.spec, i.unit,
                  SUM(t.qty) AS out_qty, COUNT(*) AS cnt
             FROM transactions t JOIN items i ON i.id = t.item_id
            WHERE t.voided=0 AND t.txn_type='OUT' AND t.txn_date >= ?
            GROUP BY i.id ORDER BY out_qty DESC LIMIT ?""",
        (_days_ago(days), limit))]


# ---------------------------------------------------------------- 리포트 묶음
def build_report(kind: str = "daily") -> dict:
    """일일/주간 리포트에 들어갈 내용을 한 번에 모은다."""
    days = 1 if kind == "daily" else 7
    d_from = _days_ago(days - 1) if days > 1 else models.today()
    d_to = models.today()

    return {
        "kind": kind,
        "title": "일일 재고 리포트" if kind == "daily" else "주간 재고 리포트",
        "period": d_to if kind == "daily" else f"{d_from} ~ {d_to}",
        "date_from": d_from,
        "date_to": d_to,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "company": db.get_setting("company_name", ""),
        "summary": summary(),
        "reorder": reorder_list(limit=50),
        "pending": models.list_pending()[:20],
        "movement": movement(d_from, d_to, limit=30),
        "dormant": dormant_items(limit=15) if kind == "weekly" else [],
        "top_movers": top_movers(days=7 if kind == "daily" else 30, limit=10) if kind == "weekly" else [],
        "categories": category_breakdown() if kind == "weekly" else [],
    }


def has_anything_to_report(report: dict) -> bool:
    """조용한 날엔 메일을 보내지 않기 위한 판단."""
    s = report["summary"]
    return bool(report["reorder"] or report["pending"] or report["movement"]
                or s.get("alert_count"))
