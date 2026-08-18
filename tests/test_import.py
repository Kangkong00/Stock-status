"""엑셀 이관 검증: 헤더 자동인식 -> 품목/기초재고 -> 과거 입출고 -> 미매핑 큐."""
import os, sys, tempfile, shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
_tmp = Path(tempfile.mkdtemp(prefix="stockimp_"))
os.environ["STOCK_DATA_DIR"] = str(_tmp)
os.environ["STOCK_DB_PATH"] = str(_tmp / "t.db")

from app import config, db, models, importer  # noqa: E402
config.DATA_DIR = _tmp; config.DB_PATH = _tmp / "t.db"
config.BACKUP_DIR = _tmp / "backups"; config.UPLOAD_DIR = _tmp / "uploads"; config.EXPORT_DIR = _tmp / "exports"

XLSX = sys.argv[1] if len(sys.argv) > 1 else "/tmp/claude-0/-home-user-Stock-status/dace85f9-a601-5d67-9ed0-732ef081d23d/scratchpad/기존_재고현황.xlsx"
PASS = FAIL = 0

def check(label, cond, extra=""):
    global PASS, FAIL
    if cond: PASS += 1; print(f"  PASS  {label}")
    else:    FAIL += 1; print(f"  FAIL  {label} {extra}")

def main():
    db.init_db()
    sheets = importer.read_workbook(XLSX)
    print(f"\n[1] 파일 구조 인식  (시트 {len(sheets)}개)")
    for s in sheets:
        print(f"       '{s.name}': 헤더 {s.header_row+1}행, 데이터 {s.row_count}행")
        print(f"          헤더 = {s.headers}")
    check("시트 2개 인식", len(sheets) == 2)
    master, hist = sheets[0], sheets[1]
    check("제목행 건너뛰고 3행을 헤더로 인식", master.header_row == 3, f"got {master.header_row}")

    print("\n[2] 컬럼 자동 매핑")
    m_map = importer.auto_mapping(master.headers)
    h_map = importer.auto_mapping(hist.headers)
    print(f"       마스터: {[(master.headers[i], f) for i, f in sorted(m_map.items())]}")
    print(f"       내역  : {[(hist.headers[i], f) for i, f in sorted(h_map.items())]}")
    for want in ("code","name","spec","unit","category","reorder_point","on_hand","unit_cost","location"):
        check(f"마스터 '{want}' 인식", want in m_map.values())
    for want in ("txn_date","name","in_qty","out_qty","partner","doc_no"):
        check(f"내역 '{want}' 인식", want in h_map.values())

    print("\n[3] 시험실행(dry run)은 DB를 바꾸지 않는다")
    dry = importer.import_items(master, m_map, opening_stock_date="2025-12-31", dry_run=True)
    print(f"       예상: 신규 {dry.items_created}건, 기초재고 {dry.txns_posted}건")
    check("dry run 후 DB 비어있음", db.scalar("SELECT COUNT(*) FROM items", (), 0) == 0,
          f"got {db.scalar('SELECT COUNT(*) FROM items', (), 0)}")
    check("dry run 이 건수는 계산함", dry.items_created == 8, f"got {dry.items_created}")

    print("\n[4] 품목 마스터 + 기초재고 이관")
    rep = importer.import_items(master, m_map, opening_stock_date="2025-12-31")
    print(f"       신규 {rep.items_created} / 갱신 {rep.items_updated} / 기초재고전표 {rep.txns_posted} / 오류 {len(rep.errors)}")
    for e in rep.errors[:5]: print(f"          ! {e}")
    check("품목 8건 등록", rep.items_created == 8, f"got {rep.items_created}")
    check("오류 없음", not rep.errors, str(rep.errors[:3]))
    check("기초재고 전표 7건 (재고 0인 포스트잇 제외)", rep.txns_posted == 7, f"got {rep.txns_posted}")
    check("PL-0012 기초재고 145", models.get_item(models.find_item_by_code("PL-0012")["id"])["on_hand"] == 145)
    check("재고기준 이관됨", models.get_item(models.find_item_by_code("PA-0001")["id"])["reorder_point"] == 5)
    check("포스트잇은 재고 0 -> 품절", models.get_item(models.find_item_by_code("OF-0100")["id"])["stock_status"] == "OUT")

    print("\n[5] 과거 입출고 이관 — 상품명 표기가 제각각인 실제 상황")
    rep2 = importer.import_transactions(hist, h_map)
    print(f"       처리 {rep2.total_rows}행 / 원장기록 {rep2.txns_posted} / 자동매칭 {rep2.auto_matched} / 미매핑 {rep2.pending}")
    for e in rep2.errors[:5]: print(f"          ! {e}")
    check("오류 없음", not rep2.errors, str(rep2.errors[:3]))
    total = rep2.txns_posted + rep2.pending
    check("13행 전부 처리됨", total == 13, f"got {total}")
    matched_rate = rep2.txns_posted / total * 100
    print(f"       => 자동 처리율 {matched_rate:.0f}%  (나머지 {rep2.pending}건만 클릭 확인)")
    check("자동 처리율 60% 이상", matched_rate >= 60, f"got {matched_rate:.0f}%")

    print("\n[6] 미매핑 큐 — 못 맞춘 것만 여기로")
    pend = models.list_pending()
    for p in pend:
        cands = ", ".join(f"{c['code']}({c['score']:.2f})" for c in p["candidates"][:3]) or "후보없음"
        print(f"       · {p['raw_name']:28s} -> {cands}")
    check("신규품목이 큐에 있음", any("스테이플러" in p["raw_name"] for p in pend))

    print("\n[7] 확인 클릭 -> 원장 기록 + 별칭 학습 (다음부터 자동)")
    before = len(models.list_pending())
    for p in pend:
        if p["candidates"] and p["candidates"][0]["score"] >= 0.45:
            models.resolve_pending(p["id"], p["candidates"][0]["item_id"])
    after = len(models.list_pending())
    print(f"       미매핑 {before}건 -> {after}건")
    still = models.list_pending()
    if still:
        newitem = still[0]
        iid, tid = models.resolve_pending_as_new_item(newitem["id"], dict(
            code="OF-0200", name="스테이플러", spec="중형", unit="EA", category="사무용품", reorder_point=3))
        check("신규 품목으로 등록하며 입고까지 완료", models.get_item(iid)["on_hand"] == 10,
              f"got {models.get_item(iid)['on_hand']}")
    check("미매핑 큐 비워짐", len(models.list_pending()) == 0, f"got {len(models.list_pending())}")

    print("\n[8] 같은 이름 재등록 시 자동 통과 (학습 확인)")
    r = models.register_by_name(raw_name="흑색볼펜0.5mm 모나미", txn_type="IN", qty=1, txn_date="2026-05-01")
    check("한 번 확인한 이름은 무인 통과", r.status == "posted", f"got {r.status}")
    r = models.register_by_name(raw_name="AAA건전지 4개입", txn_type="IN", qty=1, txn_date="2026-05-01")
    check("AAA 도 학습됨", r.status == "posted", f"got {r.status}")

    print("\n[9] 최종 재고 현황")
    print(f"       {'코드':<10}{'품목명':<16}{'규격':<14}{'현재고':>7}{'기준':>6}  상태")
    for s in models.list_stock():
        print(f"       {s['code']:<10}{s['name']:<16}{s['spec']:<14}{s['on_hand']:>7g}{s['reorder_point']:>6g}  {s['stock_status']}")
    ok = True
    for row in models.list_stock(include_inactive=True):
        calc = db.scalar("SELECT COALESCE(SUM(signed_qty),0) FROM transactions WHERE item_id=? AND voided=0", (row["item_id"],), 0)
        if abs(calc - row["on_hand"]) > 1e-9: ok = False
    check("원장 합산 == 현재고 (전 품목)", ok)
    check("AA건전지는 재고기준 미달 경고", models.get_item(models.find_item_by_code("BT-0001")["id"])["stock_status"] in ("LOW","OUT"))

    print(f"\n{'='*58}\n  통과 {PASS} / 실패 {FAIL}\n{'='*58}")
    return 0 if FAIL == 0 else 1

if __name__ == "__main__":
    code = main()
    shutil.rmtree(_tmp, ignore_errors=True)
    sys.exit(code)
