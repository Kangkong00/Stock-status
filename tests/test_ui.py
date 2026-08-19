"""
브라우저에서 실제로 클릭해보는 회귀 테스트.

API 만 검증하면 화면의 버튼이 눌리지 않는 사고를 못 잡는다.
(상품명의 따옴표가 onclick 속성을 끊어 '신규 품목으로 등록' 이 먹통이던 건이 그랬다)
"""
import os, socket, subprocess, sys, tempfile, shutil, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
_tmp = Path(tempfile.mkdtemp(prefix="stockui_"))

PASS = FAIL = 0
def check(l, c, e=""):
    global PASS, FAIL
    if c: PASS += 1; print(f"  PASS  {l}")
    else: FAIL += 1; print(f"  FAIL  {l} {e}")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0)); return s.getsockname()[1]


SEED_SCRIPT = """
import sys, os
from pathlib import Path
sys.path.insert(0, os.environ["STOCK_ROOT"])
from app import config, db, models
config.DATA_DIR = Path(os.environ["STOCK_DATA_DIR"])
config.DB_PATH = Path(os.environ["STOCK_DB_PATH"])
config.BACKUP_DIR = config.DATA_DIR / "b"
config.UPLOAD_DIR = config.DATA_DIR / "u"
config.EXPORT_DIR = config.DATA_DIR / "e"
db.init_db()
soda = models.create_item(dict(code="CH-0017", name="가성소다 고상(98%)", unit="KG",
                               pack_unit="포", pack_size=25, reorder_point=100,
                               category="약품류"))
models.post_txn(item_id=soda, txn_type="IN", qty=1000, txn_date="2026-05-01")
# 따옴표·꺾쇠·앰퍼샌드가 든 이름으로 확인 대기를 만든다 (이번 버그의 재현 조건)
names = ["3M장갑", "수량계 25A", '내경 6\" 호스', "볼트 M8 'A'형", "필터 <대형> & 부속"]
for nm in names:
    models.register_by_name(raw_name=nm, txn_type="IN", qty=2, txn_date="2026-08-01")
models.register_by_name(raw_name="가성소다고상", txn_type="IN", qty=3, txn_date="2026-08-02")
print(db.scalar("SELECT COUNT(*) FROM pending_names WHERE status='open'"), "건 대기")
"""


def seed_data(db_path: Path):
    script = _tmp / "seed.py"
    script.write_text(SEED_SCRIPT, encoding="utf-8")
    env = dict(os.environ, STOCK_ROOT=str(ROOT), STOCK_DATA_DIR=str(_tmp),
               STOCK_DB_PATH=str(db_path))
    r = subprocess.run([sys.executable, str(script)], env=env,
                       capture_output=True, text=True)
    if r.returncode:
        print(r.stdout, r.stderr)
        raise SystemExit("시드 실패")
    print("   ", r.stdout.strip())


def main():
    from playwright.sync_api import sync_playwright

    db_path = _tmp / "t.db"
    seed_data(db_path)
    port = free_port()
    env = dict(os.environ, STOCK_DATA_DIR=str(_tmp), STOCK_DB_PATH=str(db_path))
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "run.py"), "--no-browser", "--no-scheduler",
         "--host", "127.0.0.1", "--port", str(port)],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT, cwd=str(ROOT))
    base = f"http://127.0.0.1:{port}"

    import urllib.request
    for _ in range(60):
        try:
            urllib.request.urlopen(base + "/api/health", timeout=1); break
        except Exception:
            time.sleep(0.4)
    else:
        proc.terminate(); raise SystemExit("서버가 뜨지 않았습니다")

    errors = []
    import json
    import urllib.request as ur
    try:
        with sync_playwright() as p:
            b = p.chromium.launch(executable_path="/opt/pw-browsers/chromium")
            pg = b.new_page(viewport={"width": 1400, "height": 950})
            pg.on("pageerror", lambda e: errors.append(f"{pg.url}: {e}"))
            pg.on("console", lambda m: errors.append(f"{pg.url}: {m.text}") if m.type == "error" else None)
            pg.on("dialog", lambda d: d.accept())

            print("\n[1] 모든 화면이 자바스크립트 오류 없이 열린다")
            for path in ["/", "/stock", "/register", "/pending", "/history",
                         "/stocktake", "/upload", "/settings"]:
                before = len(errors)
                pg.goto(base + path, wait_until="networkidle"); pg.wait_for_timeout(250)
                check(f"{path}", len(errors) == before, str(errors[before:])[:160])

            print("\n[2] ★ '신규 품목으로 등록' 버튼이 실제로 눌린다")
            pg.goto(base + "/pending", wait_until="networkidle")
            n0 = pg.eval_on_selector_all("[id^=pend]", "e=>e.length")
            check(f"확인 대기 {n0}건 표시", n0 >= 5, f"got {n0}")

            # 따옴표가 들어간 이름의 카드를 고른다
            target = pg.query_selector('[data-act="new"][data-name*="\\""]') \
                     or pg.query_selector('[data-act="new"]')
            name = target.get_attribute("data-name")
            print(f"       고른 상품명: {name!r}")
            target.click()
            pg.wait_for_timeout(400)
            visible = pg.is_visible("#newModal")
            check("클릭하면 등록 창이 열린다", visible, "창이 열리지 않음 — 이번에 고친 버그")
            if visible:
                check("상품명이 창에 채워진다", pg.input_value("#nName") == name,
                      f"got {pg.input_value('#nName')!r}")

            print("\n[3] ★ 품목코드가 분류에 따라 자동으로 붙는다")
            auto0 = pg.input_value("#nCode")
            check("창이 열리자마자 코드가 채워져 있다", bool(auto0.strip()), f"got {auto0!r}")
            pg.select_option("#nCat", value="약품류") if False else pg.fill("#nCat", "약품류")
            pg.dispatch_event("#nCat", "change"); pg.wait_for_timeout(600)
            auto1 = pg.input_value("#nCode")
            check(f"분류(약품류)를 고르면 코드가 바뀐다 → {auto1}", auto1.startswith("CH-"),
                  f"got {auto1!r}")
            hint = pg.inner_text("#nCodeHint")
            check("어떤 규칙인지 안내가 뜬다", "접두어" in hint, hint)
            pg.click("a:has-text('분류표 보기')"); pg.wait_for_timeout(400)
            check("분류표를 볼 수 있다", pg.is_visible("#catTableModal"))
            nrows = pg.eval_on_selector_all("#catTableModal tbody tr", "e=>e.length")
            check(f"분류표에 {nrows}개 분류가 나온다", nrows >= 1, f"got {nrows}")
            pg.click("#catTableModal [data-close]"); pg.wait_for_timeout(300)

            pg.fill("#nCat", "안전용품")
            pg.dispatch_event("#nCat", "change"); pg.wait_for_timeout(700)
            auto2 = pg.input_value("#nCode")
            check(f"새 분류에도 겹치지 않는 코드를 만들어 준다 → {auto2}",
                  auto2 and auto2 != auto1, f"got {auto2!r}")

            print("\n[3-2] ★ 직접 친 코드를 자동 부여가 덮어쓰지 않는다")
            pg.fill("#nCat", "약품류")            # 응답이 오는 중에
            pg.fill("#nCode", "NEW-0001")        # 곧바로 코드를 직접 입력
            pg.wait_for_timeout(1200)            # 늦게 온 응답이 덮어쓰는지 확인
            check("직접 입력한 코드가 유지된다", pg.input_value("#nCode") == "NEW-0001",
                  f"got {pg.input_value('#nCode')!r} — 늦게 온 응답이 덮어씀")
            pg.click("#newModal button:has-text('자동')"); pg.wait_for_timeout(800)
            check("[자동] 버튼을 누르면 다시 부여된다",
                  pg.input_value("#nCode").startswith("CH-"),
                  f"got {pg.input_value('#nCode')!r}")

            print("\n[3-3] 신규 품목 등록 + 입고까지 한 번에 처리된다")
            pg.fill("#nCode", "NEW-0001")
            pg.fill("#nUnit", "PC")
            pg.fill("#nRP", "5")
            pg.click("#newModal button[type=submit]")
            pg.wait_for_timeout(1200)
            n1 = pg.eval_on_selector_all("[id^=pend]", "e=>e.length")
            check(f"확인 대기가 줄어든다 ({n0} → {n1})", n1 == n0 - 1, f"got {n1}")

            got = json.loads(ur.urlopen(base + "/api/search?q=NEW-0001", timeout=5).read())["items"]
            check("품목이 실제로 생성됐다", len(got) == 1, str(got))
            check("입고 수량 2 가 반영됐다", got and got[0]["on_hand"] == 2,
                  f"got {got[0]['on_hand'] if got else None}")

            print("\n[4] 추천 후보 클릭으로 연결된다")
            pg.goto(base + "/pending", wait_until="networkidle")
            card = pg.query_selector('[data-act="resolve"]')
            if card:
                n2 = pg.eval_on_selector_all("[id^=pend]", "e=>e.length")
                card.click(); pg.wait_for_timeout(1200)
                n3 = pg.eval_on_selector_all("[id^=pend]", "e=>e.length")
                check(f"후보 클릭으로 처리된다 ({n2} → {n3})", n3 < n2, f"got {n3}")
            else:
                check("추천 후보 카드 존재", False, "후보가 하나도 없음")

            print("\n[5] '제외' 버튼이 눌린다")
            pg.goto(base + "/pending", wait_until="networkidle")
            n4 = pg.eval_on_selector_all("[id^=pend]", "e=>e.length")
            d = pg.query_selector('[data-act="discard"]')
            if d:
                d.click(); pg.wait_for_timeout(1000)
                n5 = pg.eval_on_selector_all("[id^=pend]", "e=>e.length")
                check(f"제외로 목록에서 사라진다 ({n4} → {n5})", n5 < n4, f"got {n5}")

            print("\n[6] '직접 찾기' 버튼이 눌린다")
            pg.goto(base + "/pending", wait_until="networkidle")
            f = pg.query_selector('[data-act="find"]')
            if f:
                f.click(); pg.wait_for_timeout(400)
                check("찾기 창이 열린다", pg.is_visible("#findModal"))
                pg.fill("#findQ", "가성소다"); pg.wait_for_timeout(800)
                check("검색 결과가 나온다",
                      pg.eval_on_selector_all("#findResults .card", "e=>e.length") > 0)
                pg.keyboard.press("Escape")

            print("\n[7] 입출고 등록에서 포장단위로 입력된다")
            def soda_stock():
                d = json.loads(ur.urlopen(base + "/api/search?q=CH-0017", timeout=5).read())
                return d["items"][0]["on_hand"]

            before = soda_stock()
            pg.goto(base + "/register", wait_until="networkidle")
            pg.fill("#fSearch", "가성소다"); pg.wait_for_timeout(800)
            pg.click("#sug .card"); pg.wait_for_timeout(400)
            check("포장단위 안내가 뜬다", "25KG" in pg.inner_text("#unitHint"),
                  pg.inner_text("#unitHint"))
            pg.fill("#fQty", "4"); pg.fill("#fUnit", "포")
            pg.click("#btnSave"); pg.wait_for_timeout(1400)
            after = soda_stock()
            check(f"4포 = 100KG 환산되어 등록된다 ({before:g} → {after:g})",
                  after - before == 100, f"증가분 {after - before:g}")
            check("등록 완료 메시지가 뜬다", "등록 완료" in pg.inner_text("#result"),
                  pg.inner_text("#result")[:120])

            print("\n[8] 품목 수정 창이 열린다")
            pg.goto(base + "/stock", wait_until="networkidle")
            pg.click("button:has-text('수정')"); pg.wait_for_timeout(700)
            check("수정 창이 열린다", pg.is_visible("#itemModal"))
            check("사내코드 칸이 있다", pg.is_visible("#imIntCode"))
            check("포장단위 칸이 있다", pg.is_visible("#imPackU"))
            existing = pg.input_value("#imCode")
            pg.fill("#imCat", "약품류"); pg.dispatch_event("#imCat", "change")
            pg.wait_for_timeout(600)
            check("수정 중에는 기존 코드를 함부로 바꾸지 않는다",
                  pg.input_value("#imCode") == existing,
                  f"{existing} → {pg.input_value('#imCode')}")
            pg.keyboard.press("Escape"); pg.wait_for_timeout(300)

            print("\n[9] 재고현황에서 새 품목을 만들 때도 코드가 자동으로 붙는다")
            pg.click("button:has-text('+ 품목 등록')"); pg.wait_for_timeout(700)
            check("등록 창이 열린다", pg.is_visible("#itemModal"))
            c0 = pg.input_value("#imCode")
            check(f"코드가 미리 채워져 있다 → {c0}", bool(c0.strip()), f"got {c0!r}")
            pg.fill("#imCat", "약품류"); pg.dispatch_event("#imCat", "change")
            pg.wait_for_timeout(600)
            c1 = pg.input_value("#imCode")
            check(f"분류에 맞는 코드로 바뀐다 → {c1}", c1.startswith("CH-"), f"got {c1!r}")
            pg.keyboard.press("Escape")

            b.close()
    finally:
        proc.terminate()
        try: proc.wait(timeout=5)
        except Exception: proc.kill()

    print(f"\n  자바스크립트 오류: {errors if errors else '없음'}")
    if errors:
        global FAIL; FAIL += 1

    print(f"\n{'='*58}\n  통과 {PASS} / 실패 {FAIL}\n{'='*58}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    rc = main()
    shutil.rmtree(_tmp, ignore_errors=True)
    sys.exit(rc)
