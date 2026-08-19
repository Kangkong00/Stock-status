"""
도메인 로직.

불변 규칙
  1. 현재고는 어디에도 저장하지 않는다. 항상 v_stock 뷰에서 합산해 읽는다.
  2. 이미 기록된 원장 행은 수정/삭제하지 않는다. 취소는 voided 플래그로 한다.
  3. 입출고 기록에는 주문서 원문 상품명(raw_name)을 반드시 함께 남긴다.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from . import config, db, matching, units

TXN_SIGN = {"IN": 1, "OUT": -1, "ADJ": 1}


class DomainError(Exception):
    """사용자에게 그대로 보여줄 수 있는 오류."""


def today() -> str:
    return date.today().isoformat()


def new_batch_id(prefix: str = "B") -> str:
    return f"{prefix}{datetime.now():%y%m%d%H%M%S}{uuid.uuid4().hex[:4]}"


def _num(value, field: str, *, allow_negative: bool = False) -> float:
    try:
        n = float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        raise DomainError(f"{field}: 숫자가 아닙니다 ({value!r})")
    if not allow_negative and n < 0:
        raise DomainError(f"{field}: 음수는 넣을 수 없습니다 ({value})")
    return n


def _clean_date(value: str | None) -> str:
    if not value:
        return today()
    s = str(value).strip()[:10].replace("/", "-").replace(".", "-")
    try:
        return date.fromisoformat(s).isoformat()
    except ValueError:
        raise DomainError(f"날짜 형식이 올바르지 않습니다: {value!r} (YYYY-MM-DD)")


# =====================================================================
# 품목
# =====================================================================
def create_item(data: dict, conn=None) -> int:
    code = (data.get("code") or "").strip()
    name = (data.get("name") or "").strip()
    if not code:
        raise DomainError("품목코드는 필수입니다.")
    if not name:
        raise DomainError("품목명은 필수입니다.")

    params = (
        code, name,
        (data.get("spec") or "").strip(),
        units.normalize_unit(data.get("unit")) or "PC",
        (data.get("category") or "").strip(),
        (data.get("barcode") or "").strip() or None,
        (data.get("internal_code") or "").strip() or None,
        units.normalize_unit(data.get("pack_unit")),
        _num(data.get("pack_size") or 0, "포장규격"),
        (data.get("location") or "").strip(),
        _num(data.get("reorder_point") or 0, "재고기준"),
        _num(data.get("reorder_qty") or 0, "권장발주량"),
        _num(data.get("unit_cost") or 0, "단가"),
        1 if str(data.get("active", "1")) not in ("0", "False", "false", "") else 0,
        (data.get("memo") or "").strip(),
    )
    sql = """INSERT INTO items
             (code, name, spec, unit, category, barcode, internal_code,
              pack_unit, pack_size, location,
              reorder_point, reorder_qty, unit_cost, active, memo)
             VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"""

    def _do(c) -> int:
        try:
            cur = c.execute(sql, params)
        except Exception as exc:
            if "UNIQUE" in str(exc):
                raise DomainError(f"품목코드 '{code}' 는 이미 등록되어 있습니다.")
            raise
        item_id = int(cur.lastrowid)
        # 자기 이름은 항상 자기 별칭이다 (검색/매칭의 출발점)
        matching.learn_alias(item_id, f"{name} {params[2]}".strip(), source="self", conn=c)
        if code:
            matching.learn_alias(item_id, code, source="self", conn=c)
        ic = (data.get("internal_code") or "").strip()
        if ic:
            matching.learn_alias(item_id, ic, source="self", conn=c)
        return item_id

    if conn is not None:
        return _do(conn)
    with db.tx() as c:
        return _do(c)


_ITEM_FIELDS = (
    "code", "name", "spec", "unit", "category", "barcode", "internal_code",
    "pack_unit", "pack_size", "location",
    "reorder_point", "reorder_qty", "unit_cost", "active", "memo",
)


def update_item(item_id: int, data: dict) -> None:
    sets, params = [], []
    for field in _ITEM_FIELDS:
        if field not in data:
            continue
        value = data[field]
        if field in ("reorder_point", "reorder_qty", "unit_cost", "pack_size"):
            value = _num(value or 0, field)
        elif field == "active":
            value = 1 if str(value) not in ("0", "False", "false", "") else 0
        elif field in ("barcode", "internal_code"):
            value = (str(value).strip() or None)
        elif field in ("unit", "pack_unit"):
            value = units.normalize_unit(value)
        else:
            value = str(value).strip()
        sets.append(f"{field} = ?")
        params.append(value)

    if not sets:
        return
    sets.append("updated_at = datetime('now','localtime')")
    params.append(item_id)
    with db.tx() as c:
        try:
            c.execute(f"UPDATE items SET {', '.join(sets)} WHERE id = ?", params)
        except Exception as exc:
            if "UNIQUE" in str(exc):
                raise DomainError("이미 사용 중인 품목코드입니다.")
            raise


def delete_item(item_id: int) -> None:
    """입출고 이력이 있으면 삭제하지 않고 비활성화한다 (이력 보존)."""
    used = db.scalar("SELECT COUNT(*) FROM transactions WHERE item_id = ?", (item_id,), 0)
    if used:
        with db.tx() as c:
            c.execute("UPDATE items SET active = 0, updated_at = datetime('now','localtime') WHERE id = ?", (item_id,))
        raise DomainError(f"입출고 이력 {used}건이 있어 삭제 대신 '사용중지' 처리했습니다.")
    with db.tx() as c:
        c.execute("DELETE FROM items WHERE id = ?", (item_id,))


def get_item(item_id: int):
    return db.query_one("SELECT * FROM v_stock WHERE item_id = ?", (item_id,))


def find_item_by_code(code: str):
    return db.query_one("SELECT * FROM items WHERE code = ? COLLATE NOCASE", (code,))


def list_stock(*, keyword: str = "", category: str = "", status: str = "",
               include_inactive: bool = False, order: str = "code",
               limit: int | None = None, offset: int = 0) -> list:
    where, params = [], []
    if not include_inactive:
        where.append("active = 1")
    if keyword:
        kw = f"%{keyword.strip()}%"
        where.append("(code LIKE ? OR name LIKE ? OR spec LIKE ? OR category LIKE ? "
                     "OR internal_code LIKE ? OR barcode LIKE ? "
                     "OR item_id IN (SELECT item_id FROM aliases WHERE raw_name LIKE ?))")
        params += [kw, kw, kw, kw, kw, kw, kw]
    if category:
        where.append("category = ?")
        params.append(category)
    if status in ("OK", "LOW", "OUT"):
        where.append("stock_status = ?")
        params.append(status)
    elif status == "ALERT":
        where.append("stock_status IN ('LOW','OUT')")

    orders = {
        "code": "code", "name": "name", "on_hand": "on_hand DESC",
        "shortage": "(reorder_point - on_hand) DESC", "value": "stock_value DESC",
        "last_move": "last_move_date DESC",
    }
    sql = "SELECT * FROM v_stock"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += f" ORDER BY {orders.get(order, 'code')}"
    if limit:
        sql += f" LIMIT {int(limit)} OFFSET {int(offset)}"
    return db.query(sql, params)


def stock_count(*, keyword: str = "", category: str = "", status: str = "",
                include_inactive: bool = False) -> int:
    """조건에 맞는 품목 수. 검색 결과가 잘렸는지 알리기 위해 쓴다."""
    return len(list_stock(keyword=keyword, category=category, status=status,
                          include_inactive=include_inactive))


def categories() -> list[str]:
    rows = db.query("SELECT DISTINCT category FROM items WHERE category <> '' ORDER BY category")
    return [r["category"] for r in rows]


# =====================================================================
# 입출고 원장
# =====================================================================
@dataclass
class TxnResult:
    txn_id: int | None
    item_id: int | None
    status: str            # posted | pending
    pending_id: int | None = None
    candidates: list | None = None
    message: str = ""


def post_txn(*, item_id: int, txn_type: str, qty: float, txn_date: str | None = None,
             raw_name: str = "", partner: str = "", doc_no: str = "", memo: str = "",
             unit_cost: float = 0, batch_id: str = "", source: str = "manual",
             entered_unit: str = "", allow_negative: bool = True, conn=None) -> int:
    """
    원장에 한 건 기록한다. 현재고는 자동으로 따라온다.

    entered_unit 이 품목 기본단위와 다르면 환산해서 저장하고,
    사용자가 실제로 친 수량·단위는 entered_qty/entered_unit 에 그대로 남긴다.
    (예: 가성소다 3포 입력 -> 75KG 저장, "3 BAG" 보존)
    """
    txn_type = txn_type.upper()
    if txn_type not in TXN_SIGN:
        raise DomainError(f"입출고 구분이 올바르지 않습니다: {txn_type}")

    signed_input = _num(qty, "수량", allow_negative=(txn_type == "ADJ"))
    magnitude = abs(signed_input)
    if magnitude == 0:
        raise DomainError("수량이 0입니다.")

    raw_qty = magnitude
    raw_unit = units.normalize_unit(entered_unit)
    convert_note = ""

    if raw_unit:
        it = db.query_one(
            "SELECT unit, pack_unit, pack_size FROM items WHERE id = ?", (item_id,))
        if it and not units.same_unit(raw_unit, it["unit"]):
            magnitude, convert_note = units.to_base(
                magnitude, raw_unit, it["unit"], it["pack_size"], it["pack_unit"])
            if not convert_note:
                raise DomainError(
                    f"'{raw_unit}' 를 이 품목의 단위 '{it['unit']}' 로 바꿀 근거가 없습니다.\n"
                    f"품목 화면에서 포장단위를 등록하거나(예: 1{raw_unit} = ?{it['unit']}), "
                    f"'{it['unit']}' 단위로 입력해 주세요.")

    signed = magnitude if txn_type == "ADJ" and signed_input > 0 else (
        -magnitude if txn_type == "ADJ" and signed_input < 0 else magnitude * TXN_SIGN[txn_type])

    if convert_note:
        memo = (memo + " " if memo else "") + f"[{convert_note}]"

    d = _clean_date(txn_date)
    params = (item_id, d, txn_type, magnitude, signed,
              _num(unit_cost or 0, "단가"), (raw_name or "").strip(),
              raw_qty, raw_unit,
              (partner or "").strip(), (doc_no or "").strip(), (memo or "").strip(),
              batch_id, source)
    sql = """INSERT INTO transactions
             (item_id, txn_date, txn_type, qty, signed_qty, unit_cost,
              raw_name, entered_qty, entered_unit,
              partner, doc_no, memo, batch_id, source)
             VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)"""

    def _do(c) -> int:
        if txn_type == "OUT" and not allow_negative:
            on_hand = c.execute(
                "SELECT COALESCE(SUM(signed_qty),0) FROM transactions WHERE item_id = ? AND voided = 0",
                (item_id,),
            ).fetchone()[0]
            if on_hand < magnitude:
                raise DomainError(f"재고 부족: 현재고 {on_hand:g}, 출고요청 {magnitude:g}")
        txn_id = int(c.execute(sql, params).lastrowid)
        if raw_name:
            matching.learn_alias(item_id, raw_name, source="learned", conn=c)
        return txn_id

    if conn is not None:
        return _do(conn)
    with db.tx() as c:
        return _do(c)


def void_txn(txn_id: int, reason: str = "") -> None:
    """행을 지우지 않고 무효화한다. 감사 추적이 남는다."""
    row = db.query_one("SELECT voided FROM transactions WHERE id = ?", (txn_id,))
    if row is None:
        raise DomainError("해당 전표를 찾을 수 없습니다.")
    if row["voided"]:
        raise DomainError("이미 취소된 전표입니다.")
    with db.tx() as c:
        c.execute(
            "UPDATE transactions SET voided = 1, "
            "memo = TRIM(memo || ' [취소' || CASE WHEN ?='' THEN '' ELSE ': '||? END || ']') "
            "WHERE id = ?",
            (reason, reason, txn_id),
        )


def register_by_name(*, raw_name: str, txn_type: str, qty: float, txn_date: str | None = None,
                     partner: str = "", doc_no: str = "", memo: str = "", unit_cost: float = 0,
                     batch_id: str = "", source: str = "manual", entered_unit: str = "",
                     conn=None) -> TxnResult:
    """
    상품명으로 입출고를 등록한다. 3층 매칭 흐름의 진입점.
      매칭 성공 -> 바로 원장 기록 (필요 시 별칭 학습)
      매칭 실패 -> 미매핑 큐에 적재하고 후보를 돌려준다
    """
    result = matching.match(raw_name)

    if result.matched:
        txn_id = post_txn(
            item_id=result.item_id, txn_type=txn_type, qty=qty, txn_date=txn_date,
            raw_name=raw_name, partner=partner, doc_no=doc_no, memo=memo,
            unit_cost=unit_cost, batch_id=batch_id, source=source,
            entered_unit=entered_unit, conn=conn,
        )
        return TxnResult(txn_id, result.item_id, "posted",
                         candidates=[c.as_dict() for c in result.candidates],
                         message="자동 매칭" if result.status == "auto" else "")

    pending_id = enqueue_pending(
        raw_name=raw_name, norm=result.norm, qty=qty, txn_date=_clean_date(txn_date),
        txn_type=txn_type, partner=partner, doc_no=doc_no, unit_cost=unit_cost,
        batch_id=batch_id, candidates=[c.as_dict() for c in result.candidates], conn=conn,
    )
    return TxnResult(None, None, "pending", pending_id,
                     [c.as_dict() for c in result.candidates],
                     "표준품목을 찾지 못해 확인 대기 목록에 넣었습니다.")


def _txn_filter(item_id, date_from, date_to, txn_type, keyword,
                include_voided) -> tuple[str, list]:
    where, params = [], []
    if item_id:
        where.append("t.item_id = ?"); params.append(item_id)
    if date_from:
        where.append("t.txn_date >= ?"); params.append(_clean_date(date_from))
    if date_to:
        where.append("t.txn_date <= ?"); params.append(_clean_date(date_to))
    if txn_type in ("IN", "OUT", "ADJ"):
        where.append("t.txn_type = ?"); params.append(txn_type)
    if keyword:
        kw = f"%{keyword.strip()}%"
        where.append("(i.code LIKE ? OR i.name LIKE ? OR i.spec LIKE ? "
                     "OR COALESCE(i.internal_code,'') LIKE ? "
                     "OR t.raw_name LIKE ? OR t.partner LIKE ? OR t.doc_no LIKE ? "
                     "OR t.memo LIKE ?)")
        params += [kw] * 8
    if not include_voided:
        where.append("t.voided = 0")
    return (" WHERE " + " AND ".join(where)) if where else "", params


def txn_count(*, item_id: int | None = None, date_from: str = "", date_to: str = "",
              txn_type: str = "", keyword: str = "", include_voided: bool = False) -> int:
    """조건에 맞는 전체 건수. 화면이 몇 건 중 몇 건을 보여주는지 알리기 위해 쓴다."""
    clause, params = _txn_filter(item_id, date_from, date_to, txn_type, keyword, include_voided)
    return db.scalar(
        "SELECT COUNT(*) FROM transactions t JOIN items i ON i.id = t.item_id" + clause,
        params, 0)


def txn_history(*, item_id: int | None = None, date_from: str = "", date_to: str = "",
                txn_type: str = "", keyword: str = "", include_voided: bool = False,
                limit: int = 300, offset: int = 0) -> list:
    clause, params = _txn_filter(item_id, date_from, date_to, txn_type, keyword, include_voided)
    sql = ("SELECT t.*, i.code, i.name, i.spec, i.unit "
           "FROM transactions t JOIN items i ON i.id = t.item_id" + clause +
           " ORDER BY t.txn_date DESC, t.id DESC LIMIT ? OFFSET ?")
    return db.query(sql, params + [int(limit), int(offset)])


# =====================================================================
# 미매핑 큐
# =====================================================================
def enqueue_pending(*, raw_name: str, norm: str, qty: float, txn_date: str,
                    txn_type: str = "IN", partner: str = "", doc_no: str = "",
                    unit_cost: float = 0, batch_id: str = "",
                    candidates: list | None = None, conn=None) -> int:
    sql = """INSERT INTO pending_names
             (raw_name, norm_name, qty, unit_cost, txn_date, txn_type,
              partner, doc_no, batch_id, candidates)
             VALUES (?,?,?,?,?,?,?,?,?,?)"""
    params = (raw_name, norm, _num(qty, "수량"), _num(unit_cost or 0, "단가"),
              txn_date, txn_type, partner, doc_no, batch_id,
              json.dumps(candidates or [], ensure_ascii=False))
    if conn is not None:
        return int(conn.execute(sql, params).lastrowid)
    with db.tx() as c:
        return int(c.execute(sql, params).lastrowid)


def list_pending(status: str = "open") -> list:
    rows = db.query(
        "SELECT * FROM pending_names WHERE status = ? ORDER BY created_at DESC, id DESC",
        (status,),
    )
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["candidates"] = json.loads(r["candidates"])
        except (ValueError, TypeError):
            d["candidates"] = []
        out.append(d)
    return out


def resolve_pending(pending_id: int, item_id: int, *, learn: bool = True) -> int:
    """
    미매핑 건을 표준품목에 연결한다.
    이 클릭 한 번으로 원장이 기록되고, 동시에 별칭이 학습되어 다음부터 자동 통과한다.
    """
    row = db.query_one("SELECT * FROM pending_names WHERE id = ? AND status = 'open'", (pending_id,))
    if row is None:
        raise DomainError("이미 처리되었거나 존재하지 않는 항목입니다.")

    with db.tx() as c:
        txn_id = post_txn(
            item_id=item_id, txn_type=row["txn_type"], qty=row["qty"],
            txn_date=row["txn_date"], raw_name=row["raw_name"], partner=row["partner"],
            doc_no=row["doc_no"], unit_cost=row["unit_cost"], batch_id=row["batch_id"],
            source="import", conn=c,
        )
        if learn:
            matching.learn_alias(item_id, row["raw_name"], source="learned", conn=c)
        c.execute(
            "UPDATE pending_names SET status='resolved', resolved_item_id=? WHERE id=?",
            (item_id, pending_id),
        )
    return txn_id


def resolve_pending_as_new_item(pending_id: int, item_data: dict) -> tuple[int, int]:
    """미매핑 건을 신규 품목으로 등록하면서 동시에 입고까지 처리한다."""
    row = db.query_one("SELECT * FROM pending_names WHERE id = ? AND status = 'open'", (pending_id,))
    if row is None:
        raise DomainError("이미 처리되었거나 존재하지 않는 항목입니다.")

    payload = dict(item_data)
    payload.setdefault("name", row["raw_name"])
    if not payload.get("unit_cost"):
        payload["unit_cost"] = row["unit_cost"]

    with db.tx() as c:
        item_id = create_item(payload, conn=c)
        matching.learn_alias(item_id, row["raw_name"], source="learned", conn=c)
        txn_id = post_txn(
            item_id=item_id, txn_type=row["txn_type"], qty=row["qty"],
            txn_date=row["txn_date"], raw_name=row["raw_name"], partner=row["partner"],
            doc_no=row["doc_no"], unit_cost=row["unit_cost"], batch_id=row["batch_id"],
            source="import", conn=c,
        )
        c.execute(
            "UPDATE pending_names SET status='resolved', resolved_item_id=? WHERE id=?",
            (item_id, pending_id),
        )
    return item_id, txn_id


def discard_pending(pending_id: int) -> None:
    with db.tx() as c:
        c.execute("UPDATE pending_names SET status='discarded' WHERE id=?", (pending_id,))


# =====================================================================
# 별칭 관리
# =====================================================================
def item_aliases(item_id: int) -> list:
    return db.query(
        "SELECT * FROM aliases WHERE item_id = ? ORDER BY hit_count DESC, id", (item_id,)
    )


def add_alias(item_id: int, raw_name: str) -> None:
    norm = matching.normalize(raw_name)
    if not norm:
        raise DomainError("별칭이 비어 있습니다.")
    owner = db.query_one(
        "SELECT a.item_id, i.code, i.name FROM aliases a JOIN items i ON i.id=a.item_id "
        "WHERE a.norm_name = ?", (norm,))
    if owner and owner["item_id"] != item_id:
        raise DomainError(f"이 별칭은 이미 [{owner['code']}] {owner['name']} 에 연결되어 있습니다.")
    matching.learn_alias(item_id, raw_name, source="manual")


def delete_alias(alias_id: int) -> None:
    with db.tx() as c:
        c.execute("DELETE FROM aliases WHERE id = ? AND source <> 'self'", (alias_id,))


# =====================================================================
# 실사(재고조사)
# =====================================================================
def create_stocktake(title: str = "", take_date: str | None = None,
                     category: str = "", only_active: bool = True) -> int:
    d = _clean_date(take_date)
    with db.tx() as c:
        st_id = int(c.execute(
            "INSERT INTO stocktakes(take_date, title) VALUES (?,?)",
            (d, title or f"{d} 재고실사"),
        ).lastrowid)
        sql = "SELECT item_id, on_hand FROM v_stock WHERE 1=1"
        params: list = []
        if only_active:
            sql += " AND active = 1"
        if category:
            sql += " AND category = ?"
            params.append(category)
        for row in c.execute(sql, params).fetchall():
            c.execute(
                "INSERT INTO stocktake_lines(stocktake_id, item_id, system_qty) VALUES (?,?,?)",
                (st_id, row["item_id"], row["on_hand"]),
            )
    return st_id


def stocktake_lines(stocktake_id: int) -> list:
    return db.query(
        """SELECT sl.*, i.code, i.name, i.spec, i.unit, i.location, i.unit_cost,
                  (sl.counted_qty - sl.system_qty) AS diff
             FROM stocktake_lines sl JOIN items i ON i.id = sl.item_id
            WHERE sl.stocktake_id = ?
            ORDER BY i.location, i.code""",
        (stocktake_id,),
    )


def set_count(stocktake_id: int, item_id: int, counted_qty, memo: str = "") -> None:
    value = None if counted_qty in ("", None) else _num(counted_qty, "실사수량")
    with db.tx() as c:
        c.execute(
            "UPDATE stocktake_lines SET counted_qty = ?, memo = ? "
            "WHERE stocktake_id = ? AND item_id = ?",
            (value, memo, stocktake_id, item_id),
        )


def commit_stocktake(stocktake_id: int) -> dict:
    """차이가 있는 품목에 대해 ADJ 전표를 자동 생성한다."""
    st = db.query_one("SELECT * FROM stocktakes WHERE id = ?", (stocktake_id,))
    if st is None:
        raise DomainError("실사 건을 찾을 수 없습니다.")
    if st["status"] == "committed":
        raise DomainError("이미 확정된 실사입니다.")

    db.backup(tag="stocktake")
    adjusted = 0
    total_diff = 0.0
    batch = new_batch_id("ST")

    with db.tx() as c:
        rows = c.execute(
            "SELECT item_id, system_qty, counted_qty FROM stocktake_lines "
            "WHERE stocktake_id = ? AND counted_qty IS NOT NULL",
            (stocktake_id,),
        ).fetchall()
        for r in rows:
            # 확정 시점의 실제 재고로 다시 계산한다 (실사 중 입출고가 있었을 수 있다)
            live = c.execute(
                "SELECT COALESCE(SUM(signed_qty),0) FROM transactions WHERE item_id=? AND voided=0",
                (r["item_id"],),
            ).fetchone()[0]
            diff = r["counted_qty"] - live
            if abs(diff) < 1e-9:
                continue
            post_txn(
                item_id=r["item_id"], txn_type="ADJ", qty=diff,
                txn_date=st["take_date"], memo=f"실사조정 (전산 {live:g} → 실물 {r['counted_qty']:g})",
                batch_id=batch, source="stocktake", conn=c,
            )
            adjusted += 1
            total_diff += diff
        c.execute(
            "UPDATE stocktakes SET status='committed', committed_at=datetime('now','localtime') WHERE id=?",
            (stocktake_id,),
        )
    return {"adjusted": adjusted, "total_diff": total_diff, "batch_id": batch}
