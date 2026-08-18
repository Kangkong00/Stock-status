"""전 화면 + 전 API 통합 검증 (실제 HTTP 스택 경유)."""
import io, os, sys, tempfile, shutil, json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
_tmp = Path(tempfile.mkdtemp(prefix="stockweb_"))
os.environ["STOCK_DATA_DIR"] = str(_tmp); os.environ["STOCK_DB_PATH"] = str(_tmp / "t.db")

from app import config, db, models  # noqa: E402
config.DATA_DIR = _tmp; config.DB_PATH = _tmp/"t.db"
config.BACKUP_DIR = _tmp/"backups"; config.UPLOAD_DIR = _tmp/"uploads"; config.EXPORT_DIR = _tmp/"exports"
from app.web import create_app  # noqa: E402

XLSX = sys.argv[1] if len(sys.argv) > 1 else \
    "/tmp/claude-0/-home-user-Stock-status/dace85f9-a601-5d67-9ed0-732ef081d23d/scratchpad/기존_재고현황.xlsx"

PASS = FAIL = 0
def check(l, c, e=""):
    global PASS, FAIL
    if c: PASS += 1; print(f"  PASS  {l}")
    else: FAIL += 1; print(f"  FAIL  {l} {e}")

def main():
    db.init_db()
    app = create_app()
    app.config["TESTING"] = True
    c = app.test_client()

    def get(url, **kw):  return c.get(url, **kw)
    def jpost(url, data):
        return c.post(url, json=data, headers={"X-Requested-With": "fetch"})

    print("\n[1] 모든 화면이 200 으로 열린다 (빈 DB 상태)")
    for url in ["/", "/stock", "/register", "/pending", "/history",
                "/stocktake", "/upload", "/settings", "/report/preview"]:
        r = get(url)
        check(f"GET {url} -> {r.status_code}", r.status_code == 200,
              f"body={r.data[:180]}")

    print("\n[2] 엑셀 업로드 마법사 — 분석 단계")
    with open(XLSX, "rb") as f:
        data = {"file": (io.BytesIO(f.read()), "기존_재고현황.xlsx")}
    r = c.post("/upload/analyze", data=data, content_type="multipart/form-data",
               headers={"X-Requested-With": "fetch"})
    check(f"분석 요청 {r.status_code}", r.status_code == 200, r.data[:200])
    d = r.get_json()
    check("시트 2개 인식", len(d["sheets"]) == 2, str([s["name"] for s in d["sheets"]]))
    check("한글 파일명 보존", d["filename"] == "기존_재고현황.xlsx", d.get("filename"))
    m0 = d["sheets"][0]["mapping"]
    check("마스터 자동 매핑됨", "code" in m0.values() and "on_hand" in m0.values(), str(m0))

    print("\n[3] 시험 실행은 DB 를 바꾸지 않는다")
    r = jpost("/upload/run", {"sheet_index": 0, "mode": "items", "mapping": m0,
                              "dry_run": True, "opening_stock_date": "2025-12-31"})
    check(f"시험실행 {r.status_code}", r.status_code == 200, r.data[:200])
    rep = r.get_json()["report"]
    check("신규 8건으로 계산", rep["items_created"] == 8, str(rep))
    check("DB 는 그대로 비어있음", db.scalar("SELECT COUNT(*) FROM items", (), 0) == 0)

    print("\n[4] 실제 반영 — 품목 마스터")
    r = jpost("/upload/run", {"sheet_index": 0, "mode": "items", "mapping": m0,
                              "dry_run": False, "opening_stock_date": "2025-12-31"})
    rep = r.get_json()["report"]
    check("품목 8건 반영", db.scalar("SELECT COUNT(*) FROM items", (), 0) == 8,
          f"got {db.scalar('SELECT COUNT(*) FROM items', (), 0)}")
    check("오류 없음", not rep["errors"], str(rep["errors"][:2]))
    check("반영 직전 백업 생성", len(list(config.BACKUP_DIR.glob("*.db"))) >= 1)

    print("\n[5] 실제 반영 — 과거 입출고")
    m1 = d["sheets"][1]["mapping"]
    r = jpost("/upload/run", {"sheet_index": 1, "mode": "transactions",
                              "mapping": m1, "dry_run": False})
    rep = r.get_json()["report"]
    total = rep["txns_posted"] + rep["pending"]
    print(f"       기록 {rep['txns_posted']} / 확인대기 {rep['pending']} "
          f"/ 자동처리율 {rep['txns_posted']/total*100:.0f}%")
    check("13행 처리", total == 13, f"got {total}")
    check("오류 없음", not rep["errors"], str(rep["errors"][:2]))

    print("\n[6] 데이터가 있는 상태로 전 화면 재확인")
    for url in ["/", "/stock", "/stock?status=ALERT", "/stock?keyword=볼펜",
                "/register", "/pending", "/history", "/stocktake", "/settings",
                "/report/preview?kind=weekly"]:
        r = get(url)
        check(f"GET {url} -> {r.status_code}", r.status_code == 200, r.data[:180])

    first = db.query_one("SELECT id FROM items ORDER BY id LIMIT 1")["id"]
    r = get(f"/stock/{first}")
    check(f"GET /stock/{first} (품목 상세)", r.status_code == 200, r.data[:180])
    check("상세에 별칭 섹션 노출", "별칭".encode() in r.data)

    print("\n[7] 대시보드에 실제 수치가 렌더링된다")
    r = get("/")
    body = r.data.decode()
    check("발주 필요 섹션 존재", "지금 발주해야 할 품목" in body)
    check("품목코드가 표에 찍힘", "PA-0001" in body or "BT-0001" in body)
    check("확인 대기 건수 노출", "확인 대기" in body)

    print("\n[8] 검색 / 매칭 API")
    r = get("/api/search?q=볼펜")
    items = r.get_json()["items"]
    check("검색 결과 반환", len(items) >= 2, str(len(items)))
    check("검색 결과에 현재고 포함", "on_hand" in items[0])
    r = get("/api/match?q=" + "모나미 볼펜 검정 0.5mm")
    mj = r.get_json()
    check("매칭 API 동작", mj["status"] in ("exact", "auto", "suggest"), str(mj["status"]))

    print("\n[9] 입출고 등록 API")
    pen = db.query_one("SELECT id FROM items WHERE code='PL-0012'")["id"]
    before = models.get_item(pen)["on_hand"]
    r = jpost("/api/txn", {"item_id": pen, "txn_type": "IN", "qty": 25,
                           "txn_date": "2026-05-01", "partner": "테스트상사"})
    j = r.get_json()
    check("입고 등록 성공", j["status"] == "posted", str(j))
    check("현재고 +25 반영", models.get_item(pen)["on_hand"] == before + 25,
          f"{before} -> {models.get_item(pen)['on_hand']}")
    check("응답에 현재고 포함", j["on_hand"] == before + 25)

    r = jpost("/api/txn", {"raw_name": "듣도보도못한물건 XYZ", "txn_type": "IN", "qty": 3})
    check("모르는 이름은 확인 대기로", r.get_json()["status"] == "pending", str(r.get_json()))

    r = jpost("/api/txn", {"item_id": pen, "txn_type": "IN", "qty": 0})
    check("수량 0 은 거부 (400)", r.status_code == 400, str(r.status_code))
    check("거부 사유가 한글로 옴", "수량" in r.get_json()["error"], r.get_json().get("error"))

    print("\n[10] 전표 취소")
    tid = j["txn_id"]
    r = jpost(f"/api/txn/{tid}/void", {"reason": "테스트"})
    check("취소 성공", r.status_code == 200)
    check("재고 원복", models.get_item(pen)["on_hand"] == before, f"got {models.get_item(pen)['on_hand']}")
    r = jpost(f"/api/txn/{tid}/void", {})
    check("이중 취소 거부", r.status_code == 400)

    print("\n[11] 확인 대기 처리 -> 별칭 학습")
    pend = models.list_pending()
    check("확인 대기 목록 있음", len(pend) > 0, f"got {len(pend)}")
    target = next((p for p in pend if p["candidates"]), None)
    if target:
        r = jpost(f"/api/pending/{target['id']}/resolve", {"item_id": target["candidates"][0]["item_id"]})
        check("후보로 연결 성공", r.status_code == 200, r.data[:180])
        check("응답에 안내 문구", "자동 처리" in r.get_json()["message"], r.get_json().get("message"))
        r2 = get("/api/match?q=" + target["raw_name"])
        check("연결한 이름이 이제 즉시 매칭됨", r2.get_json()["status"] == "exact",
              str(r2.get_json()["status"]))

    xyz = next((p for p in models.list_pending() if "XYZ" in p["raw_name"]), None)
    if xyz:
        r = jpost(f"/api/pending/{xyz['id']}/resolve",
                  {"code": "NEW-0001", "name": "테스트 신규품목", "unit": "EA", "reorder_point": 5})
        check("신규 품목으로 등록 + 입고 동시 처리", r.status_code == 200, r.data[:180])
        nid = db.query_one("SELECT id FROM items WHERE code='NEW-0001'")
        check("신규 품목 생성됨", nid is not None)
        check("입고 3개까지 반영", models.get_item(nid["id"])["on_hand"] == 3,
              f"got {models.get_item(nid['id'])['on_hand']}")

    print("\n[12] 품목 CRUD + 별칭 API")
    r = jpost("/api/items", {"code": "TEST-999", "name": "테스트품목", "spec": "규격",
                             "unit": "EA", "reorder_point": 10})
    check("품목 생성", r.status_code == 200, r.data[:180])
    nid = r.get_json()["item_id"]
    r = jpost("/api/items", {"code": "TEST-999", "name": "중복"})
    check("중복 코드 거부", r.status_code == 400 and "이미" in r.get_json()["error"])
    r = jpost(f"/api/items/{nid}", {"reorder_point": 50})
    check("품목 수정", r.status_code == 200)
    check("수정 반영", models.get_item(nid)["reorder_point"] == 50)
    r = jpost(f"/api/items/{nid}/aliases", {"raw_name": "테스트 별명입니다"})
    check("별칭 추가", r.status_code == 200, r.data[:180])
    r = get(f"/api/items/{nid}/aliases")
    check("별칭 목록 조회", any("테스트 별명입니다" == a["raw_name"] for a in r.get_json()["aliases"]))
    r = get("/api/match?q=테스트 별명입니다")
    check("추가한 별칭으로 즉시 매칭", r.get_json()["item_id"] == nid, str(r.get_json()))
    r = jpost(f"/api/items/{nid}/delete", {})
    check("이력 없는 품목은 삭제됨", r.status_code == 200 and models.get_item(nid) is None)

    print("\n[13] 재고실사 전체 흐름")
    r = c.post("/stocktake/new", data={"title": "웹 테스트 실사", "take_date": "2026-06-30"})
    check("실사 생성 후 리다이렉트", r.status_code == 302, str(r.status_code))
    st_id = db.scalar("SELECT MAX(id) FROM stocktakes")
    r = get(f"/stocktake/{st_id}")
    check("실사 화면 열림", r.status_code == 200, r.data[:180])
    lines = models.stocktake_lines(st_id)
    check("전 품목이 실사 대상에 잡힘", len(lines) >= 8, f"got {len(lines)}")
    tgt = lines[0]
    r = jpost(f"/api/stocktake/{st_id}/count",
              {"item_id": tgt["item_id"], "counted_qty": tgt["system_qty"] - 3})
    check("실사 수량 저장", r.status_code == 200, r.data[:180])
    r = get(f"/stocktake/{st_id}")
    check("차이가 화면에 반영", r.status_code == 200)
    sysqty_before = models.get_item(tgt["item_id"])["on_hand"]
    r = c.post(f"/stocktake/{st_id}/commit")
    check("실사 확정", r.status_code == 302)
    check("재고가 실물에 맞춰짐", models.get_item(tgt["item_id"])["on_hand"] == sysqty_before - 3,
          f"{sysqty_before} -> {models.get_item(tgt['item_id'])['on_hand']}")
    check("조정 전표(ADJ) 생성", db.scalar("SELECT COUNT(*) FROM transactions WHERE txn_type='ADJ'", (), 0) >= 1)
    r = c.post(f"/stocktake/{st_id}/commit")
    check("재확정은 막힘", r.status_code == 302)

    print("\n[14] 엑셀 내보내기")
    for url, name in [("/export/stock", "재고현황"), ("/export/transactions", "입출고내역"),
                      ("/export/template", "양식"), (f"/export/stocktake/{st_id}", "실사시트")]:
        r = get(url)
        ok = (r.status_code == 200 and len(r.data) > 3000
              and r.data[:2] == b"PK")   # xlsx = zip
        check(f"{name} 다운로드 ({len(r.data):,} bytes)", ok, f"status={r.status_code}")

    print("\n[15] 설정 저장 / 백업 / 상태")
    r = c.post("/settings", data={
        "company_name": "테스트상사", "dormant_days": "60", "backup_keep": "20",
        "report_to": "a@b.com", "report_daily_enabled": "1", "report_daily_time": "07:00",
        "report_weekly_day": "3", "report_weekly_time": "09:00",
        "smtp_host": "smtp.naver.com", "smtp_port": "587", "smtp_user": "me",
        "smtp_password": "secret", "smtp_security": "starttls", "smtp_from": "me@naver.com"})
    check("설정 저장", r.status_code == 302)
    check("회사명 저장됨", db.get_setting("company_name") == "테스트상사")
    check("주간 요일 저장됨", db.get_setting("report_weekly_day") == "3")
    check("비밀번호 저장됨", db.get_setting("smtp_password") == "secret")

    r = c.post("/settings", data={"company_name": "테스트상사", "smtp_password": "",
                                  "smtp_host": "smtp.naver.com", "report_to": "a@b.com"})
    check("빈 비밀번호는 기존 값을 지우지 않음", db.get_setting("smtp_password") == "secret",
          f"got {db.get_setting('smtp_password')!r}")

    r = jpost("/api/backup", {})
    check("수동 백업", r.status_code == 200 and "백업" in r.get_json()["message"])
    r = get("/api/health")
    check("health 엔드포인트", r.status_code == 200 and r.get_json()["ok"])

    print("\n[16] 회사명이 화면·리포트에 반영")
    r = get("/")
    check("상단바에 회사명", "테스트상사" in r.data.decode())
    r = get("/report/preview?kind=weekly")
    check("리포트에 회사명", "테스트상사" in r.data.decode())

    print("\n[17] 메일 미설정/오설정 시 사용자에게 한글로 안내")
    r = jpost("/api/mail/test", {"to": "nobody@example.invalid"})
    check("발송 실패해도 500 이 아니라 400 + 한글 메시지",
          r.status_code == 400 and r.get_json().get("error"),
          f"status={r.status_code} body={r.data[:200]}")
    print(f"       메시지: {r.get_json().get('error','')[:110]}")

    print("\n[18] 최종 정합성 — 원장 합산 == 화면 현재고")
    ok = True
    for row in models.list_stock(include_inactive=True):
        calc = db.scalar("SELECT COALESCE(SUM(signed_qty),0) FROM transactions "
                         "WHERE item_id=? AND voided=0", (row["item_id"],), 0)
        if abs(calc - row["on_hand"]) > 1e-9:
            ok = False; print(f"       불일치 {row['code']}: 뷰={row['on_hand']} 합산={calc}")
    check("전 품목 일치", ok)

    print("\n[19] 열이 많은 파일도 세션이 넘치지 않는다 (쿠키 4KB 한계)")
    from openpyxl import Workbook
    wb = Workbook(); ws = wb.active; ws.title = "재고현황"
    ws.append(["품목코드","품목명","규격","단위","분류","안전재고","현재고","매입단가","보관위치","비고"]
              + [f"부가컬럼{i}" for i in range(1, 16)])
    for i in range(1, 31):
        ws.append([f"W-{i:04d}", f"아주긴한글품목명입니다테스트용{i}", f"규격설명이길게들어가는경우{i}",
                   "EA", "사무용품", 10, i*3, 1000+i, f"창고A-{i}", "비고내용이제법길게들어갑니다"]
                  + [f"부가값{j}번째의긴한글내용{i}" for j in range(1, 16)])
    wide = _tmp / "wide.xlsx"; wb.save(wide)

    with c.session_transaction() as sess:
        sess.clear()
    with open(wide, "rb") as f:
        r = c.post("/upload/analyze", data={"file": (io.BytesIO(f.read()), "wide.xlsx")},
                   content_type="multipart/form-data", headers={"X-Requested-With": "fetch"})
    check(f"25열 파일 분석 {r.status_code}", r.status_code == 200, r.data[:200])
    cookie = r.headers.get("Set-Cookie", "")
    check(f"세션 쿠키가 4KB 미만 ({len(cookie)} bytes)", len(cookie) < 4000, f"got {len(cookie)}")
    wd = r.get_json()
    check("25열 전부 노출", len(wd["sheets"][0]["headers"]) == 25, str(len(wd["sheets"][0]["headers"])))
    r = jpost("/upload/run", {"sheet_index": 0, "mode": "items",
                              "mapping": wd["sheets"][0]["mapping"], "dry_run": False})
    check(f"열 많은 파일 반영 {r.status_code}", r.status_code == 200, r.data[:200])
    check("30건 등록", r.get_json()["report"]["items_created"] == 30,
          str(r.get_json()["report"]["items_created"]))

    print("\n[20] 404 / 잘못된 입력")
    check("없는 품목 상세 -> 404", get("/stock/99999").status_code == 404)
    check("없는 실사 -> 404", get("/stocktake/99999").status_code == 404)
    r = jpost("/api/txn", {"raw_name": "", "txn_type": "IN", "qty": 5})
    check("빈 품목명 거부", r.status_code == 400, str(r.status_code))

    print(f"\n{'='*58}\n  통과 {PASS} / 실패 {FAIL}\n{'='*58}")
    return 0 if FAIL == 0 else 1

if __name__ == "__main__":
    rc = main()
    shutil.rmtree(_tmp, ignore_errors=True)
    sys.exit(rc)
