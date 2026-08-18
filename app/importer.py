"""
엑셀/CSV 임포트.

두 가지 용도를 모두 처리한다.
  1) 기존 재고현황 엑셀 이관 (품목 마스터 + 기초재고 + 과거 입출고)
  2) 거래명세서 일괄 업로드 (드래그&드롭 -> 자동 매칭 -> 미매핑만 큐로)

컬럼명을 고정하지 않는다. 헤더를 읽어 뜻을 추측하고, 사람이 화면에서
매핑을 고쳐 확정하는 방식이라 어떤 양식이 와도 받아낼 수 있다.
"""
from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

from . import config, db, matching, models

# --------------------------------------------------------------------------
# 헤더 자동 인식 사전
# --------------------------------------------------------------------------
HEADER_HINTS: dict[str, list[str]] = {
    "code":          ["품목코드", "품번", "품목번호", "코드", "관리번호", "제품코드", "자재코드", "sku", "code", "item code", "품목 코드"],
    "name":          ["품목명", "품명", "제품명", "상품명", "자재명", "품목", "name", "item", "품 명"],
    "spec":          ["규격", "사양", "스펙", "옵션", "size", "spec", "모델", "형식"],
    "unit":          ["단위", "unit", "uom", "포장단위"],
    "category":      ["분류", "품목분류", "카테고리", "대분류", "구분류", "group", "category"],
    "barcode":       ["바코드", "barcode", "ean", "jan"],
    "location":      ["위치", "보관위치", "창고", "적재위치", "location", "loc"],
    "reorder_point": ["재고기준", "안전재고", "최소재고", "적정재고", "발주점", "기준재고", "reorder", "safety"],
    "reorder_qty":   ["발주량", "권장발주", "발주수량", "재발주량", "order qty"],
    "unit_cost":     ["단가", "매입단가", "구매단가", "원가", "price", "cost"],
    "on_hand":       ["현재고", "재고", "재고수량", "현재수량", "기초재고", "잔량", "잔여수량", "on hand", "stock", "qty on hand"],
    "qty":           ["수량", "입출고수량", "거래수량", "개수", "qty", "quantity"],
    "in_qty":        ["입고", "입고수량", "입고량", "받은수량", "in"],
    "out_qty":       ["출고", "출고수량", "출고량", "사용수량", "불출", "out"],
    "txn_date":      ["일자", "날짜", "거래일", "입출고일", "등록일", "date", "거래일자", "입고일", "출고일"],
    "partner":       ["거래처", "매입처", "공급처", "업체", "납품처", "vendor", "supplier"],
    "doc_no":        ["전표번호", "명세서번호", "주문번호", "발주번호", "doc", "no", "번호"],
    "memo":          ["비고", "메모", "적요", "note", "memo", "remark"],
}

_NORM_HDR_RE = re.compile(r"[\s\-_/().]+")


def _norm_header(h: Any) -> str:
    return _NORM_HDR_RE.sub("", str(h or "").strip().lower())


_HINT_INDEX: dict[str, str] = {}
for _field, _words in HEADER_HINTS.items():
    for _w in _words:
        _HINT_INDEX.setdefault(_norm_header(_w), _field)


def guess_field(header: Any) -> str | None:
    h = _norm_header(header)
    if not h:
        return None
    if h in _HINT_INDEX:
        return _HINT_INDEX[h]
    # 부분 일치 (긴 힌트 우선)
    for hint in sorted(_HINT_INDEX, key=len, reverse=True):
        if len(hint) >= 2 and hint in h:
            return _HINT_INDEX[hint]
    return None


# --------------------------------------------------------------------------
# 파일 읽기
# --------------------------------------------------------------------------
@dataclass
class Sheet:
    name: str
    headers: list[str]
    rows: list[list[Any]]
    header_row: int = 0                      # 엑셀 기준 헤더 행 번호 (1-base)
    row_nums: list[int] = field(default_factory=list)   # 각 데이터 행의 엑셀 실제 행 번호

    def excel_row(self, i: int) -> int:
        """i번째 데이터 행이 원본 엑셀에서 몇 행이었는지. 오류 메시지용."""
        return self.row_nums[i] if i < len(self.row_nums) else self.header_row + 1 + i

    @property
    def row_count(self) -> int:
        return len(self.rows)

    def preview(self, n: int = 8) -> list[list[Any]]:
        return [[_cell_str(c) for c in r] for r in self.rows[:n]]


def _cell_str(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, (datetime, date)):
        return v.strftime("%Y-%m-%d")
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v).strip()


def _looks_like_header(row: list[Any]) -> int:
    """헤더로 인식되는 셀의 개수. 가장 높은 행을 헤더로 삼는다."""
    return sum(1 for c in row if guess_field(c))


def read_workbook(path: Path | str, *, max_scan: int = 12) -> list[Sheet]:
    """
    엑셀/CSV 를 읽어 시트 목록으로 돌려준다.
    첫 행이 제목/공백인 양식이 흔하므로 상위 몇 줄을 훑어 진짜 헤더 행을 찾는다.
    """
    path = Path(path)
    suffix = path.suffix.lower()

    if suffix in (".csv", ".txt", ".tsv"):
        return [_read_csv(path)]
    if suffix in (".xlsx", ".xlsm", ".xltx", ".xltm"):
        return _read_excel(path, max_scan=max_scan)
    raise models.DomainError(
        f"지원하지 않는 파일 형식입니다: {suffix}\n"
        "(.xlsx / .xlsm / .csv 를 올려주세요. 옛 .xls 는 엑셀에서 .xlsx 로 저장 후 사용)"
    )


def _read_csv(path: Path) -> Sheet:
    raw = path.read_bytes()
    text = None
    for enc in ("utf-8-sig", "cp949", "euc-kr", "utf-8"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        text = raw.decode("utf-8", errors="replace")

    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",\t;|")
    except csv.Error:
        dialect = csv.excel
    numbered = [(n, list(r)) for n, r in enumerate(csv.reader(io.StringIO(text), dialect), start=1)
                if any(str(c).strip() for c in r)]
    if not numbered:
        raise models.DomainError("빈 파일입니다.")

    hi = max(range(min(len(numbered), 12)), key=lambda i: _looks_like_header(numbered[i][1]))
    headers = [_cell_str(c) for c in numbered[hi][1]]
    body = numbered[hi + 1:]
    return Sheet(path.stem, headers, [r for _, r in body], numbered[hi][0], [n for n, _ in body])


def _read_excel(path: Path, *, max_scan: int = 12) -> list[Sheet]:
    from openpyxl import load_workbook

    wb = load_workbook(path, data_only=True, read_only=True)
    sheets: list[Sheet] = []
    try:
        for ws in wb.worksheets:
            numbered = [(n, list(r)) for n, r in enumerate(ws.iter_rows(values_only=True), start=1)
                        if any(_cell_str(c) for c in r)]
            if not numbered:
                continue
            scan = min(len(numbered), max_scan)
            hi = max(range(scan), key=lambda i: _looks_like_header(numbered[i][1]))
            if _looks_like_header(numbered[hi][1]) == 0:
                hi = 0
            headers = [_cell_str(c) for c in numbered[hi][1]]
            body = numbered[hi + 1:]
            sheets.append(Sheet(ws.title, headers, [r for _, r in body],
                                numbered[hi][0], [n for n, _ in body]))
    finally:
        wb.close()

    if not sheets:
        raise models.DomainError("읽을 수 있는 시트가 없습니다.")
    return sheets


def auto_mapping(headers: list[str]) -> dict[int, str]:
    """열 인덱스 -> 표준 필드명. 화면에서 사람이 고칠 수 있다."""
    mapping: dict[int, str] = {}
    used: set[str] = set()
    for idx, h in enumerate(headers):
        f = guess_field(h)
        if f and f not in used:
            mapping[idx] = f
            used.add(f)
    return mapping


# --------------------------------------------------------------------------
# 임포트 실행
# --------------------------------------------------------------------------
@dataclass
class ImportReport:
    mode: str
    batch_id: str = ""
    total_rows: int = 0
    items_created: int = 0
    items_updated: int = 0
    txns_posted: int = 0
    auto_matched: int = 0
    pending: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)
    samples: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        d = self.__dict__.copy()
        d["errors"] = self.errors[:50]
        return d


def _to_float(v: Any, default: float = 0.0) -> float:
    s = _cell_str(v).replace(",", "").replace("원", "").strip()
    if not s:
        return default
    s = re.sub(r"[^\d.\-]", "", s)
    if s in ("", "-", "."):
        return default
    try:
        return float(s)
    except ValueError:
        return default


def _to_date(v: Any, default: str) -> str:
    if isinstance(v, (datetime, date)):
        return v.strftime("%Y-%m-%d")
    s = _cell_str(v)
    if not s:
        return default
    s = s.replace("/", "-").replace(".", "-").strip()
    m = re.match(r"(\d{4})-?(\d{1,2})-?(\d{1,2})", s)
    if m:
        y, mo, d = (int(x) for x in m.groups())
        try:
            return date(y, mo, d).isoformat()
        except ValueError:
            return default
    m = re.match(r"^(\d{8})$", s)
    if m:
        try:
            return datetime.strptime(m.group(1), "%Y%m%d").strftime("%Y-%m-%d")
        except ValueError:
            return default
    return default


def _extract(row: list[Any], mapping: dict[int, str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for idx, fieldname in mapping.items():
        if idx < len(row):
            out[fieldname] = row[idx]
    return out


def import_items(sheet: Sheet, mapping: dict[int, str], *,
                 update_existing: bool = True,
                 opening_stock_date: str = "",
                 dry_run: bool = False) -> ImportReport:
    """
    품목 마스터 이관.
    on_hand(현재고) 열이 매핑되어 있으면 기초재고를 IN 전표로 자동 생성한다.
    """
    rep = ImportReport("items")
    rep.batch_id = models.new_batch_id("OPEN")
    opening_date = opening_stock_date or models.today()
    has_opening = "on_hand" in mapping.values()

    if not dry_run:
        db.backup(tag="import_items")

    conn_ctx = db.tx()
    conn = conn_ctx.__enter__()
    try:
        for i, row in enumerate(sheet.rows):
            lineno = sheet.excel_row(i)
            data = _extract(row, mapping)
            code = _cell_str(data.get("code"))
            name = _cell_str(data.get("name"))
            if not code and not name:
                rep.skipped += 1
                continue
            rep.total_rows += 1

            # 품목코드가 비어 있으면 품목명 기반으로 임시 코드를 만든다
            if not code:
                code = "TMP-" + (re.sub(r"\W+", "", name)[:12].upper() or str(lineno))

            payload = dict(
                code=code, name=name or code,
                spec=_cell_str(data.get("spec")),
                unit=_cell_str(data.get("unit")) or "EA",
                category=_cell_str(data.get("category")),
                barcode=_cell_str(data.get("barcode")),
                location=_cell_str(data.get("location")),
                reorder_point=_to_float(data.get("reorder_point")),
                reorder_qty=_to_float(data.get("reorder_qty")),
                unit_cost=_to_float(data.get("unit_cost")),
                memo=_cell_str(data.get("memo")),
            )

            try:
                existing = conn.execute(
                    "SELECT id FROM items WHERE code = ? COLLATE NOCASE", (code,)
                ).fetchone()

                if existing:
                    item_id = existing["id"]
                    if update_existing:
                        sets = [f"{k} = ?" for k in payload if k != "code"]
                        vals = [payload[k] for k in payload if k != "code"]
                        conn.execute(
                            f"UPDATE items SET {', '.join(sets)}, "
                            "updated_at = datetime('now','localtime') WHERE id = ?",
                            vals + [item_id],
                        )
                        rep.items_updated += 1
                    else:
                        rep.skipped += 1
                        continue
                else:
                    item_id = models.create_item(payload, conn=conn)
                    rep.items_created += 1

                # 엑셀 품목명을 별칭으로 심어둔다 -> 이후 명세서가 자동 매칭된다
                if name:
                    matching.learn_alias(item_id, name, source="import", conn=conn)
                    if payload["spec"]:
                        matching.learn_alias(item_id, f"{name} {payload['spec']}",
                                             source="import", conn=conn)

                # 기초재고
                if has_opening:
                    qty = _to_float(data.get("on_hand"))
                    already = conn.execute(
                        "SELECT COUNT(*) FROM transactions WHERE item_id=? AND source='migration'",
                        (item_id,),
                    ).fetchone()[0]
                    if qty and not already:
                        models.post_txn(
                            item_id=item_id, txn_type="IN", qty=qty,
                            txn_date=opening_date, raw_name=name,
                            memo="기초재고 이관", batch_id=rep.batch_id,
                            source="migration", conn=conn,
                        )
                        rep.txns_posted += 1

                if len(rep.samples) < 5:
                    rep.samples.append({"code": code, "name": name,
                                        "on_hand": _to_float(data.get("on_hand")) if has_opening else None})

            except Exception as exc:
                rep.errors.append(f"{lineno}행 [{code}]: {exc}")

        if dry_run:
            raise _DryRun()
        conn_ctx.__exit__(None, None, None)
    except _DryRun:
        conn_ctx.__exit__(_DryRun, _DryRun(), None)
    except Exception:
        conn_ctx.__exit__(Exception, Exception(), None)
        raise
    return rep


def import_transactions(sheet: Sheet, mapping: dict[int, str], *,
                        default_type: str = "IN",
                        default_date: str = "",
                        dry_run: bool = False) -> ImportReport:
    """
    입출고 내역 임포트 (과거 이력 이관 + 거래명세서 일괄 등록 공용).

    입고/출고 열이 따로 있는 양식(엑셀 매크로에서 흔한 형태)과
    수량 한 열 + 구분 열 형태를 모두 처리한다.
    매칭 실패 건은 예외를 던지지 않고 미매핑 큐에 쌓는다.
    """
    rep = ImportReport("transactions")
    rep.batch_id = models.new_batch_id("IMP")
    fallback_date = default_date or models.today()
    fields = set(mapping.values())
    split_columns = "in_qty" in fields or "out_qty" in fields

    if not dry_run:
        db.backup(tag="import_txn")

    conn_ctx = db.tx()
    conn = conn_ctx.__enter__()
    try:
        for i, row in enumerate(sheet.rows):
            lineno = sheet.excel_row(i)
            data = _extract(row, mapping)
            raw_name = _cell_str(data.get("name")) or _cell_str(data.get("code"))
            if not raw_name:
                rep.skipped += 1
                continue

            d = _to_date(data.get("txn_date"), fallback_date)
            partner = _cell_str(data.get("partner"))
            doc_no = _cell_str(data.get("doc_no"))
            memo = _cell_str(data.get("memo"))
            cost = _to_float(data.get("unit_cost"))

            # 이 행이 만들어낼 (구분, 수량) 조합
            entries: list[tuple[str, float]] = []
            if split_columns:
                qin = _to_float(data.get("in_qty"))
                qout = _to_float(data.get("out_qty"))
                if qin:
                    entries.append(("IN", abs(qin)))
                if qout:
                    entries.append(("OUT", abs(qout)))
            else:
                q = _to_float(data.get("qty"))
                if q:
                    # 음수 수량은 출고로 해석한다
                    entries.append((default_type if q > 0 else "OUT", abs(q)))

            if not entries:
                rep.skipped += 1
                continue

            rep.total_rows += 1
            for txn_type, qty in entries:
                try:
                    res = models.register_by_name(
                        raw_name=raw_name, txn_type=txn_type, qty=qty, txn_date=d,
                        partner=partner, doc_no=doc_no, memo=memo, unit_cost=cost,
                        batch_id=rep.batch_id, source="import", conn=conn,
                    )
                    if res.status == "posted":
                        rep.txns_posted += 1
                        if res.message == "자동 매칭":
                            rep.auto_matched += 1
                    else:
                        rep.pending += 1
                    if len(rep.samples) < 5:
                        rep.samples.append({"raw_name": raw_name, "type": txn_type,
                                            "qty": qty, "status": res.status})
                except Exception as exc:
                    rep.errors.append(f"{lineno}행 [{raw_name}]: {exc}")

        if dry_run:
            raise _DryRun()
        conn_ctx.__exit__(None, None, None)
    except _DryRun:
        conn_ctx.__exit__(_DryRun, _DryRun(), None)
    except Exception:
        conn_ctx.__exit__(Exception, Exception(), None)
        raise
    return rep


class _DryRun(Exception):
    """시험 실행 후 롤백을 유도하는 내부 신호."""
