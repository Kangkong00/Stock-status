"""단위 동의어 통합 · 포장 환산 · 오환산 차단 검증."""
import os, sys, tempfile, shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
_tmp = Path(tempfile.mkdtemp(prefix="stockunit_"))
os.environ["STOCK_DATA_DIR"] = str(_tmp); os.environ["STOCK_DB_PATH"] = str(_tmp / "t.db")

from app import config, db, models, units  # noqa: E402
config.DATA_DIR = _tmp; config.DB_PATH = _tmp/"t.db"
config.BACKUP_DIR = _tmp/"b"; config.UPLOAD_DIR = _tmp/"u"; config.EXPORT_DIR = _tmp/"e"

PASS = FAIL = 0
def check(l, c, e=""):
    global PASS, FAIL
    if c: PASS += 1; print(f"  PASS  {l}")
    else: FAIL += 1; print(f"  FAIL  {l} {e}")

def main():
    db.init_db()
    nu = units.normalize_unit

    print("\n[1] 같은 뜻 단위는 하나로 모인다")
    for group, want in [(["PC","EA","ea","개","PCS","pc."], "PC"),
                        (["ROLL","ROL","롤"], "ROL"),
                        (["BOT","BT","병"], "BOT"),
                        (["BAG","포","포대","부대"], "BAG"),
                        (["PAIL","말","CAN","통"], "PAIL"),
                        (["KG","kg","㎏","킬로"], "KG"),
                        (["장","매","sheet"], "장")]:
        got = {nu(x) for x in group}
        check(f"{group} → {want}", got == {want}, f"got {got}")
    check("모르는 단위는 그대로 대문자", nu("BON") == "BON")
    check("빈 값은 빈 값", nu("") == "" and nu(None) == "")

    print("\n[2] 제품명에서 포장 규격 추출")
    for name, want in [("가성소다 고상(98%)(25kg)", (25.0,"KG")),
                       ("소포제 - 신규([FC-210] 20Kg)", (20.0,"KG")),
                       ("PH표준용액(1000ml,PH4)", (1000.0,"ML")),
                       ("에어호스(Air호스)6mm (100m)", (100.0,"M")),
                       ("무균채수병(2ℓ)", (2.0,"L"))]:
        check(f"{name[:28]} → {want}", units.extract_pack(name) == want,
              f"got {units.extract_pack(name)}")
    print("     규격 숫자를 포장량으로 오인하지 않는다:")
    for name in ["PVC 배관25A 부속류", "압력계60Ø 0.6MPa, 1/4\"", "체크밸브(50A)",
                 "EOCR(DSP-AOM-70Z7-A)", "인버터(SV220iS7-4NOFD)"]:
        check(f"  {name[:30]} → None", units.extract_pack(name) is None,
              f"got {units.extract_pack(name)}")

    print("\n[3] 수량+단위가 붙은 문자열 파싱 (실제 자료 형태)")
    for text, want in [("34포",(34.0,"BAG")), ("1EA",(1.0,"PC")), ("30ea",(30.0,"PC")),
                       ("20개",(20.0,"PC")), ("10말",(10.0,"PAIL")), ("2롤",(2.0,"ROL")),
                       ("1,200kg",(1200.0,"KG")), ("5박스",(5.0,"BOX")), ("",(0.0,""))]:
        check(f"{text!r} → {want}", units.parse_qty(text) == want, f"got {units.parse_qty(text)}")

    print("\n[4] 포장 환산 (실제 자료에서 확인된 배율)")
    soda = models.create_item(dict(code="CH-0017", name="가성소다 고상(98%)", unit="KG",
                                   pack_unit="포", pack_size=25, reorder_point=100))
    poly = models.create_item(dict(code="CH-0024", name="고분자응집제", unit="KG",
                                   pack_unit="포", pack_size=15, reorder_point=500))
    models.post_txn(item_id=soda, txn_type="IN", qty=34, entered_unit="포", txn_date="2026-05-22")
    check("가성소다 34포 → 850KG", models.get_item(soda)["on_hand"] == 850,
          f"got {models.get_item(soda)['on_hand']}")
    models.post_txn(item_id=poly, txn_type="OUT", qty=3, entered_unit="포", txn_date="2026-05-23")
    check("폴리머 3포 출고 → -45KG", models.get_item(poly)["on_hand"] == -45,
          f"got {models.get_item(poly)['on_hand']}")
    models.post_txn(item_id=soda, txn_type="IN", qty=10, entered_unit="말", txn_date="2026-07-13")
    check("용기 표기가 달라도(말) 포장으로 인정 → +250KG",
          models.get_item(soda)["on_hand"] == 1100, f"got {models.get_item(soda)['on_hand']}")

    print("\n[5] 입력 원본이 원장에 보존된다")
    row = db.query_one("SELECT entered_qty, entered_unit, qty, memo FROM transactions "
                       "WHERE item_id=? ORDER BY id LIMIT 1", (soda,))
    check("친 수량 34 보존", row["entered_qty"] == 34, f"got {row['entered_qty']}")
    check("친 단위 BAG 보존", row["entered_unit"] == "BAG", f"got {row['entered_unit']}")
    check("환산 근거가 비고에 남음", "× 25" in row["memo"], f"got {row['memo']!r}")

    print("\n[6] 낱개 단위끼리는 1:1 (PC ↔ BOT/ROL/장)")
    acid = models.create_item(dict(code="CH-0095", name="묽은염산용액(1000ml)", unit="BOT"))
    models.post_txn(item_id=acid, txn_type="IN", qty=2, entered_unit="EA", txn_date="2026-06-23")
    check("묽은염산 2EA → 2BOT", models.get_item(acid)["on_hand"] == 2,
          f"got {models.get_item(acid)['on_hand']}")

    print("\n[7] ★ 근거 없는 환산은 반드시 막는다")
    pen = models.create_item(dict(code="PC-0001", name="볼펜", unit="PC"))
    for bad_unit, label in [("포","묶음 단위 BAG"), ("박스","BOX"), ("SET","세트"), ("PR","켤레")]:
        try:
            models.post_txn(item_id=pen, txn_type="IN", qty=3, entered_unit=bad_unit)
            check(f"{label} → PC 환산 차단", False, "통과되어 버림")
        except models.DomainError:
            check(f"{label} → PC 환산 차단", True)
    check("차단된 뒤 재고는 그대로 0", models.get_item(pen)["on_hand"] == 0,
          f"got {models.get_item(pen)['on_hand']}")

    print("\n[8] 무게·부피 계열 배수")
    g = models.create_item(dict(code="CH-9999", name="시약", unit="KG"))
    models.post_txn(item_id=g, txn_type="IN", qty=500, entered_unit="G")
    check("500G → 0.5KG", models.get_item(g)["on_hand"] == 0.5, f"got {models.get_item(g)['on_hand']}")

    print("\n[9] 품목 등록 시 단위가 대표 표기로 저장된다")
    e = models.create_item(dict(code="X-1", name="테스트", unit="ea"))
    check("'ea' 로 등록해도 PC 로 저장", db.query_one("SELECT unit FROM items WHERE id=?", (e,))["unit"] == "PC")
    p = models.create_item(dict(code="X-2", name="테스트2", unit="KG", pack_unit="포", pack_size=20))
    check("포장단위 '포' → BAG 로 저장",
          db.query_one("SELECT pack_unit FROM items WHERE id=?", (p,))["pack_unit"] == "BAG")

    print("\n[10] 사내 품목코드")
    ic = models.create_item(dict(code="CH-0001", name="가성소다", unit="KG",
                                 internal_code="1275495700"))
    check("사내코드 저장", db.query_one("SELECT internal_code FROM items WHERE id=?", (ic,))["internal_code"] == "1275495700")
    found = models.list_stock(keyword="1275495700")
    check("사내코드로 검색됨", len(found) == 1 and found[0]["item_id"] == ic, f"got {len(found)}")
    from app import matching
    check("사내코드로 즉시 매칭", matching.match("1275495700").item_id == ic)

    print(f"\n{'='*58}\n  통과 {PASS} / 실패 {FAIL}\n{'='*58}")
    return 0 if FAIL == 0 else 1

if __name__ == "__main__":
    rc = main(); shutil.rmtree(_tmp, ignore_errors=True); sys.exit(rc)
