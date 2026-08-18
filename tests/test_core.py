"""핵심 불변식 검증: 원장 합산 = 현재고, 별칭 자동학습, 실사 조정, 규격 오매칭 차단."""
import os, sys, tempfile, shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_tmp = Path(tempfile.mkdtemp(prefix="stocktest_"))
os.environ["STOCK_DATA_DIR"] = str(_tmp)
os.environ["STOCK_DB_PATH"] = str(_tmp / "t.db")

from app import config, db, models, matching  # noqa: E402

config.DATA_DIR = _tmp
config.DB_PATH = _tmp / "t.db"
config.BACKUP_DIR = _tmp / "backups"
config.UPLOAD_DIR = _tmp / "uploads"
config.EXPORT_DIR = _tmp / "exports"

PASS = FAIL = 0


def check(label, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {label}")
    else:
        FAIL += 1
        print(f"  FAIL  {label} {extra}")


def main():
    db.init_db()

    print("\n[1] 품목 등록 + 자기 별칭 자동 생성")
    pen = models.create_item(dict(code="PL-0012", name="모나미 볼펜", spec="검정 0.5mm",
                                  unit="EA", category="필기구", reorder_point=20,
                                  reorder_qty=100, unit_cost=300))
    paper = models.create_item(dict(code="PA-0001", name="A4 복사용지", spec="80g 500매",
                                    unit="BOX", category="용지", reorder_point=5,
                                    reorder_qty=20, unit_cost=25000))
    check("품목 2건 생성", models.get_item(pen) is not None and models.get_item(paper) is not None)
    check("중복 코드 거부", _raises(lambda: models.create_item(dict(code="PL-0012", name="중복"))))

    print("\n[2] 원장 기록 -> 현재고 자동 합산")
    models.post_txn(item_id=pen, txn_type="IN", qty=100, txn_date="2026-01-05", raw_name="모나미 볼펜 검정 0.5mm")
    models.post_txn(item_id=pen, txn_type="IN", qty=50,  txn_date="2026-02-01")
    models.post_txn(item_id=pen, txn_type="OUT", qty=30, txn_date="2026-02-10")
    row = models.get_item(pen)
    check("현재고 = 100+50-30 = 120", row["on_hand"] == 120, f"got {row['on_hand']}")
    check("누적입고 150", row["total_in"] == 150, f"got {row['total_in']}")
    check("누적출고 30", row["total_out"] == 30, f"got {row['total_out']}")
    check("재고금액 = 120*300", row["stock_value"] == 36000, f"got {row['stock_value']}")

    print("\n[3] 전표 취소는 삭제가 아니라 무효화")
    tid = models.post_txn(item_id=pen, txn_type="OUT", qty=20, txn_date="2026-02-11")
    check("출고 후 100", models.get_item(pen)["on_hand"] == 100)
    models.void_txn(tid, "오입력")
    check("취소 후 120 복원", models.get_item(pen)["on_hand"] == 120)
    check("취소행은 남아있음", db.scalar("SELECT COUNT(*) FROM transactions WHERE voided=1") == 1)
    check("재취소 거부", _raises(lambda: models.void_txn(tid)))

    print("\n[4] 별칭 자동학습: 첫 등록은 확인 대기, 확인 후에는 자동 통과")
    r1 = models.register_by_name(raw_name="흑색볼펜 0.5mm 모나미", txn_type="IN", qty=10, txn_date="2026-03-01")
    print(f"       1차 결과: status={r1.status}, 후보={[c['code'] for c in (r1.candidates or [])]}")
    if r1.status == "pending":
        check("후보에 PL-0012 포함", any(c["code"] == "PL-0012" for c in r1.candidates),
              f"got {r1.candidates}")
        models.resolve_pending(r1.pending_id, pen)
    check("확인 후 재고 130", models.get_item(pen)["on_hand"] == 130, f"got {models.get_item(pen)['on_hand']}")

    r2 = models.register_by_name(raw_name="흑색볼펜 0.5mm 모나미", txn_type="IN", qty=10, txn_date="2026-03-02")
    check("2차는 자동 통과(별칭 학습됨)", r2.status == "posted", f"got {r2.status}")
    check("재고 140", models.get_item(pen)["on_hand"] == 140)

    r3 = models.register_by_name(raw_name="모나미 흑색 볼펜 0.5mm", txn_type="IN", qty=5, txn_date="2026-03-03")
    check("어순만 바뀐 이름도 자동 통과", r3.status == "posted", f"got {r3.status}")

    print("\n[5] 규격이 다르면 절대 자동 매칭되지 않는다  ★ 안전장치")
    r4 = models.register_by_name(raw_name="모나미 볼펜 검정 0.7mm", txn_type="IN", qty=99, txn_date="2026-03-04")
    check("0.7mm 는 0.5mm 로 자동매칭 안 됨", r4.status == "pending", f"got {r4.status}")
    check("0.7mm 재고가 0.5mm 에 반영되지 않음", models.get_item(pen)["on_hand"] == 145,
          f"got {models.get_item(pen)['on_hand']}")

    r5 = models.register_by_name(raw_name="A4 복사용지 75g 500매", txn_type="IN", qty=10, txn_date="2026-03-05")
    check("75g 는 80g 로 자동매칭 안 됨", r5.status == "pending", f"got {r5.status}")

    print("\n[6] 품목코드/바코드 직접 입력은 완전일치")
    r6 = models.register_by_name(raw_name="PA-0001", txn_type="IN", qty=12, txn_date="2026-03-06")
    check("품목코드 입력 즉시 매칭", r6.status == "posted" and r6.item_id == paper, f"got {r6.status}")
    check("복사용지 재고 12", models.get_item(paper)["on_hand"] == 12)

    print("\n[7] 재고기준(안전재고) 판정")
    models.post_txn(item_id=paper, txn_type="OUT", qty=8, txn_date="2026-03-07")
    check("12-8=4 <= 기준5 이므로 LOW", models.get_item(paper)["stock_status"] == "LOW",
          f"got {models.get_item(paper)['stock_status']}")
    models.post_txn(item_id=paper, txn_type="OUT", qty=4, txn_date="2026-03-08")
    check("0 이면 OUT(품절)", models.get_item(paper)["stock_status"] == "OUT")
    check("볼펜은 145 > 20 이므로 OK", models.get_item(pen)["stock_status"] == "OK")
    alerts = models.list_stock(status="ALERT")
    check("알림 대상 조회에 복사용지 포함", any(a["code"] == "PA-0001" for a in alerts))

    print("\n[8] 실사: 실물 카운트만 넣으면 차이 자동 조정")
    st = models.create_stocktake(title="3월 실사", take_date="2026-03-31")
    lines = models.stocktake_lines(st)
    check("실사 라인 생성", len(lines) == 2, f"got {len(lines)}")
    models.set_count(st, pen, 140)      # 전산 145 -> 실물 140 (감모 5)
    models.set_count(st, paper, 0)      # 차이 없음
    res = models.commit_stocktake(st)
    check("조정 1건 발생", res["adjusted"] == 1, f"got {res}")
    check("실사 후 재고 140", models.get_item(pen)["on_hand"] == 140,
          f"got {models.get_item(pen)['on_hand']}")
    check("조정 전표가 ADJ 로 남음", db.scalar("SELECT COUNT(*) FROM transactions WHERE txn_type='ADJ'") == 1)
    check("재확정 거부", _raises(lambda: models.commit_stocktake(st)))

    print("\n[9] 원장 합산 == 뷰 현재고 (전 품목 교차검증)")
    ok = True
    for row in models.list_stock(include_inactive=True):
        s = db.scalar("SELECT COALESCE(SUM(signed_qty),0) FROM transactions WHERE item_id=? AND voided=0",
                      (row["item_id"],), 0)
        if abs(s - row["on_hand"]) > 1e-9:
            ok = False
            print(f"       불일치: {row['code']} 뷰={row['on_hand']} 합산={s}")
    check("모든 품목에서 일치", ok)

    print("\n[10] 이력 있는 품목은 삭제 대신 사용중지")
    check("삭제 거부 + 비활성화", _raises(lambda: models.delete_item(pen)))
    check("active=0 으로 전환", db.scalar("SELECT active FROM items WHERE id=?", (pen,)) == 0)

    print(f"\n{'='*58}\n  통과 {PASS} / 실패 {FAIL}\n{'='*58}")
    return 0 if FAIL == 0 else 1


def _raises(fn):
    try:
        fn()
        return False
    except models.DomainError:
        return True


if __name__ == "__main__":
    code = main()
    shutil.rmtree(_tmp, ignore_errors=True)
    sys.exit(code)
