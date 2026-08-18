#!/usr/bin/env python3
"""
실제 업무 파일 이관 스크립트 (1회성).

승인받은 방침
  · C.xlsm 은 D_Auto.xlsm 의 완전한 부분집합이므로 제외한다.
  · B.xlsx 는 2026-06-21 이전 중복 47행을 빼고, 판정 불가 6행 + 6/23 이후 74행만 넣는다.
  · 품목코드는 분류 기반으로 자동 부여하고, A.xlsx 의 사내 10자리 코드는 별도 칸에 넣는다.
  · 단위는 대표 표기로 통합하고(EA=PC 등), 포장단위는 환산 근거를 등록한다.

실행:  python migrate/migrate_real.py <자료폴더> [--commit]
       --commit 없이 실행하면 결과만 계산하고 DB 를 바꾸지 않는다.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from openpyxl import load_workbook

from app import config, db, matching, models, units

# --------------------------------------------------------------------------
CATEGORY_PREFIX = {
    "계측기": "MT", "필터류": "FL", "페인트 및 오일": "PT", "공구류": "TL",
    "약품류": "CH", "기타자재류": "ET", "펌프자재류": "PM", "볼트,피팅류": "BF",
    "전기자재류": "EL", "밸브류": "VL", "배관류": "PP",
}
DEFAULT_PREFIX = "GN"

# B.xlsx 축약 표기 -> D 제품명 (자료 대조로 확인된 것만)
B_ALIAS_HINT = {
    "폴리머": "고분자응집제", "폴리머분말": "고분자응집제", "폴리머 분말": "고분자응집제",
    "4보정액": "PH표준용액(1000ml,PH4)", "ph4 보정액": "PH표준용액(1000ml,PH4)",
    "7보정액": "PH표준용액(1000ml,PH7)", "ph7 보정액": "PH표준용액(1000ml,PH7)",
    "묽은염산": "묽은염산용액", "가성소다 고상": "가성소다 고상", "고상 가성소다": "가성소다 고상",
    "유리전극": "유리전극", "소포제": "소포제", "와이프올": "와이프올",
    "슬러지펌프 판막": "ARO 판막", "슬러지펌프 메인 판막": "ARO 판막",
}

# 자료에서 확인된 포장 환산 (제품명에서 못 뽑는 것 보강)
EXTRA_PACK = {
    "고분자응집제": (15.0, "BAG"),      # B: 3포 = D 45KG
}

# B.xlsx 6/21 이전 중 '신규'로 반영하기로 한 행 (판정 불가 5 + 신규 가능성 1)
B_KEEP_BEFORE = {
    ("2026-05-23", "하계바지"),
    ("2026-05-23", "버터플라이 밸브 150A"),
    ("2026-05-23", "압력계"),
    ("2026-06-08", "DDR RAM"),
    ("2026-06-18", "가성소다 고상"),
    ("2026-06-19", "폴리머분말"),
}
B_CUTOFF = "2026-06-21"


def c(v) -> str:
    if v is None:
        return ""
    if hasattr(v, "strftime"):
        return v.strftime("%Y-%m-%d")
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v).strip()


def sheet_rows(path: Path, name: str) -> list[list]:
    wb = load_workbook(path, data_only=True, read_only=True)
    try:
        return [list(r) for r in wb[name].iter_rows(values_only=True)]
    finally:
        wb.close()


class Report:
    def __init__(self):
        self.lines: list[str] = []
        self.stats: Counter = Counter()
        self.issues: list[dict] = []

    def say(self, msg: str = "") -> None:
        print(msg)
        self.lines.append(msg)

    def issue(self, kind: str, detail: str, data: dict | None = None) -> None:
        self.issues.append({"kind": kind, "detail": detail, **(data or {})})
        self.stats[kind] += 1


# ==========================================================================
# 1) D_Auto.xlsm — 품목 마스터
# ==========================================================================
_MEMO_RP = re.compile(r"재고기준\s*[:：]?\s*([\d,\.]+)\s*([A-Za-z가-힣]*)")
_MEMO_COST = re.compile(r"단가\s*[:：]?\s*([\d,]+)")


def parse_memo(memo: str) -> tuple[float, str, float]:
    """비고란의 '재고기준:10PC, 단가:4,220원/PC' 를 숫자로 뽑는다."""
    rp, rpu, cost = 0.0, "", 0.0
    if not memo:
        return rp, rpu, cost
    m = _MEMO_RP.search(memo)
    if m:
        try:
            rp = float(m.group(1).replace(",", ""))
            rpu = units.normalize_unit(m.group(2))
        except ValueError:
            pass
    m = _MEMO_COST.search(memo)
    if m:
        try:
            cost = float(m.group(1).replace(",", ""))
        except ValueError:
            pass
    return rp, rpu, cost


def build_items(dpath: Path, rep: Report) -> tuple[dict, dict]:
    prod = sheet_rows(dpath, "제품")
    stock = sheet_rows(dpath, "재고")
    cats = {c(r[0]): c(r[1]) for r in sheet_rows(dpath, "제품구분")[1:] if c(r[0])}
    vendors = {c(r[0]): c(r[1]) for r in sheet_rows(dpath, "거래처")[1:] if c(r[0]) and c(r[1])}

    # 재고 시트에서 실제로 쓰인 단위를 세어 둔다 (제품 시트보다 현실을 반영한다)
    used_unit: dict[str, Counter] = defaultdict(Counter)
    for r in stock[1:]:
        if c(r[0]) and c(r[1]) and c(r[5]):
            used_unit[c(r[1])][units.normalize_unit(c(r[5]))] += 1

    seq: Counter = Counter()
    items: dict[str, dict] = {}
    for r in prod[1:]:
        pid = c(r[0])
        if not pid:
            continue
        name = c(r[4])
        if not name:
            rep.issue("제품명 없음", f"제품 ID {pid}")
            continue

        cat = cats.get(c(r[2]), "")
        prefix = CATEGORY_PREFIX.get(cat, DEFAULT_PREFIX)
        seq[prefix] += 1
        code = f"{prefix}-{seq[prefix]:04d}"

        sheet_unit = units.normalize_unit(c(r[5]))
        real_unit = used_unit[pid].most_common(1)[0][0] if used_unit.get(pid) else ""
        unit = real_unit or sheet_unit or "PC"
        if sheet_unit and real_unit and sheet_unit != real_unit:
            rep.issue("단위 불일치", f"[{code}] {name}: 제품시트 {sheet_unit} / 재고시트 {real_unit} → {unit} 채택",
                      {"code": code, "name": name})

        memo = c(r[6])
        rp, rpu, cost = parse_memo(memo)
        if rp and rpu and not units.same_unit(rpu, unit):
            rep.issue("재고기준 단위 다름", f"[{code}] {name}: 기준 {rp}{rpu} vs 품목단위 {unit}",
                      {"code": code, "name": name})

        pack = units.extract_pack(name)
        pack_size, pack_unit = 0.0, ""
        if pack and not units.same_unit(pack[1], unit):
            pass  # 뽑은 단위가 기본단위와 다르면 환산 근거가 되지 않는다
        elif pack:
            pack_size, pack_unit = pack[0], "BAG"
        for key, (sz, pu) in EXTRA_PACK.items():
            if key in name:
                pack_size, pack_unit = sz, pu
        if pack and units.same_unit(pack[1], unit) and not pack_unit:
            pack_size, pack_unit = pack[0], "BAG"

        items[pid] = dict(
            code=code, name=name, spec="", unit=unit, category=cat,
            pack_unit=pack_unit, pack_size=pack_size,
            reorder_point=rp, unit_cost=cost, memo=memo,
            vendor=vendors.get(c(r[1]), ""),
        )
    return items, vendors


# ==========================================================================
# 2) A.xlsx — 사내 품목코드 / 재고기준 / 단가 보강
# ==========================================================================
def load_a(apath: Path) -> list[dict]:
    wb = load_workbook(apath, data_only=True, read_only=True)
    out = []
    try:
        for ws in wb.worksheets:
            rows = [list(r) for r in ws.iter_rows(values_only=True)]
            hi = next((i for i, r in enumerate(rows[:8]) if c(r[0]) == "품목코드"), None)
            if hi is None:
                continue
            hdr = [c(x) for x in rows[hi]]
            idx = {h: i for i, h in enumerate(hdr)}
            for r in rows[hi + 1:]:
                if not c(r[0]):
                    continue
                def g(k):
                    return c(r[idx[k]]) if k in idx and idx[k] < len(r) else ""
                out.append(dict(
                    sheet=ws.title.strip(), code=c(r[0]), name=g("품명"),
                    spec=g("규격(Maker)"), vendor=g("공급처"), unit=g("단위"),
                    cost=g("단가(원)"), qty=g("수량"),
                    reorder=g("재고기준"), onhand=g("재고현황"), reason=g("신청사유"),
                ))
    finally:
        wb.close()
    return out


def korean_core(name: str) -> str:
    """'[영진화학] Sodium Hydroxide,가성소다' -> '가성소다'"""
    s = re.sub(r"^\[[^\]]*\]\s*", "", name or "").strip()
    if "," in s:
        s = s.split(",")[-1].strip()
    return s


def link_a_to_items(arows: list[dict], items: dict, rep: Report) -> tuple[dict, list]:
    """A 의 사내코드를 D 품목에 연결한다. 확신이 서는 것만 붙이고 나머지는 검토 목록으로."""
    pool = [(pid, it["name"], matching.normalize(it["name"])) for pid, it in items.items()]
    linked: dict[str, dict] = {}
    unlinked: list[dict] = []

    latest: dict[str, dict] = {}
    order = ["25년 12월", "1월", "2월", "3월", "4월", "5월", "6월", "7월", "8월"]
    for a in arows:
        pos = order.index(a["sheet"]) if a["sheet"] in order else -1
        prev = latest.get(a["code"])
        if prev is None or pos >= prev["_pos"]:
            latest[a["code"]] = {**a, "_pos": pos}

    for code, a in latest.items():
        core = korean_core(a["name"])
        q1 = matching.normalize(f"{core} {a['spec']}")
        q2 = matching.normalize(core)
        best = (0.0, None)
        for pid, nm, nn in pool:
            s = max(matching.similarity(q1, nn), matching.similarity(q2, nn))
            if s > best[0]:
                best = (s, pid)
        if best[0] >= 0.75:
            linked[best[1]] = {**a, "_score": round(best[0], 3)}
        else:
            unlinked.append({**a, "_score": round(best[0], 3),
                             "_best": items[best[1]]["name"] if best[1] else ""})
    rep.say(f"    A 사내코드 연결 {len(linked)}건 / 미연결 {len(unlinked)}건")
    return linked, unlinked


# ==========================================================================
# 3) 입출고
# ==========================================================================
def d_transactions(dpath: Path, items: dict, rep: Report) -> list[dict]:
    rows = sheet_rows(dpath, "재고")
    out = []
    for r in rows[1:]:
        if not c(r[0]):
            continue
        pid = c(r[1]); d = c(r[2])
        qin, _ = units.parse_qty(r[3])
        qout, _ = units.parse_qty(r[4])
        unit = units.normalize_unit(c(r[5]))
        memo = " ".join(x for x in (c(r[6]), c(r[7])) if x)

        if pid not in items:
            rep.issue("고아 참조", f"재고행이 없는 제품 ID {pid} 를 가리킴 (일자 {d})", {"pid": pid, "date": d})
            continue
        if not d or d == "0":
            rep.issue("일자 이상", f"[{items[pid]['code']}] {items[pid]['name']}: 일자가 '{d}' 라 건너뜀",
                      {"pid": pid})
            continue
        if qin == 0 and qout == 0:
            rep.issue("수량 0", f"[{items[pid]['code']}] {items[pid]['name']} {d}: 입고·출고 모두 0", {"pid": pid})
            continue
        if qin > 0 and qout > 0:
            rep.issue("입출고 동시", f"[{items[pid]['code']}] {items[pid]['name']} {d}: 입 {qin} 출 {qout} → 두 건으로 분리",
                      {"pid": pid})
        if qin > 0:
            out.append(dict(pid=pid, date=d, type="IN", qty=qin, unit=unit, memo=memo, src="D"))
        if qout > 0:
            out.append(dict(pid=pid, date=d, type="OUT", qty=qout, unit=unit, memo=memo, src="D"))
    return out


def b_transactions(bpath: Path, rep: Report) -> list[dict]:
    rows = sheet_rows(bpath, "Sheet1")
    hi = next(i for i, r in enumerate(rows) if c(r[0]) == "일자")
    out, skipped = [], 0
    for r in rows[hi + 1:]:
        d = c(r[0])
        if not d:
            continue
        name = c(r[1])
        qin, uin = units.parse_qty(r[2])
        qout, uout = units.parse_qty(r[3])
        memo = c(r[4]) if len(r) > 4 else ""

        if d <= B_CUTOFF and (d, name) not in B_KEEP_BEFORE:
            skipped += 1
            continue
        if qin == 0 and qout == 0:
            continue
        if qin > 0:
            out.append(dict(name=name, date=d, type="IN", qty=qin, unit=uin, memo=memo, src="B"))
        if qout > 0:
            out.append(dict(name=name, date=d, type="OUT", qty=qout, unit=uout, memo=memo, src="B"))
    rep.say(f"    B: 중복으로 제외한 행 {skipped} / 반영할 행 {len(out)}")
    return out


# ==========================================================================
# 4) 실행
# ==========================================================================
def run(folder: Path, commit: bool) -> Report:
    rep = Report()
    dpath = folder / "D_Auto.xlsm"
    bpath = folder / "B.xlsx"
    apath = folder / "A.xlsx"
    for p in (dpath, bpath, apath):
        if not p.exists():
            raise SystemExit(f"파일을 찾을 수 없습니다: {p}")

    rep.say("=" * 88)
    rep.say("  실제 자료 이관" + ("" if commit else "  [시험 실행 — DB 를 바꾸지 않습니다]"))
    rep.say("=" * 88)

    rep.say("\n[1] D_Auto.xlsm 품목 마스터 해석")
    items, vendors = build_items(dpath, rep)
    rep.say(f"    품목 {len(items)}건")
    withrp = sum(1 for v in items.values() if v["reorder_point"])
    withcost = sum(1 for v in items.values() if v["unit_cost"])
    withpack = sum(1 for v in items.values() if v["pack_size"])
    rep.say(f"    비고에서 재고기준 {withrp}건 · 단가 {withcost}건 추출")
    rep.say(f"    제품명에서 포장 환산 {withpack}건 추출")
    ucnt = Counter(v["unit"] for v in items.values())
    rep.say(f"    단위 통합 결과: {dict(ucnt.most_common())}")

    rep.say("\n[2] A.xlsx 사내 품목코드 연결")
    arows = load_a(apath)
    rep.say(f"    신청 기록 {len(arows)}행 (입출고로는 반영하지 않음)")
    linked, unlinked = link_a_to_items(arows, items, rep)

    rep.say("\n[3] 입출고 원장 구성")
    dtx = d_transactions(dpath, items, rep)
    rep.say(f"    D_Auto 재고 → 원장 {len(dtx)}건")
    btx = b_transactions(bpath, rep)
    rep.say(f"    합계 {len(dtx) + len(btx)}건")

    if not commit:
        rep.say("\n[4] 시험 실행이므로 여기서 멈춥니다. 반영하려면 --commit 을 붙이세요.")
        summarize(rep, items, dtx, btx, linked, unlinked)
        return rep

    rep.say("\n[4] DB 반영")
    db.init_db()
    if db.scalar("SELECT COUNT(*) FROM items", (), 0):
        db.backup(tag="before_real_migration")
        rep.say("    기존 데이터가 있어 백업했습니다.")

    id_map: dict[str, int] = {}
    batch = models.new_batch_id("MIG")

    with db.tx() as conn:
        for pid, it in items.items():
            a = linked.get(pid)
            payload = dict(
                code=it["code"], name=it["name"], spec=it["spec"], unit=it["unit"],
                category=it["category"], pack_unit=it["pack_unit"], pack_size=it["pack_size"],
                reorder_point=it["reorder_point"], unit_cost=it["unit_cost"],
                memo=it["memo"],
            )
            if a:
                payload["internal_code"] = a["code"]
                if not payload["unit_cost"]:
                    try:
                        payload["unit_cost"] = float(str(a["cost"]).replace(",", "") or 0)
                    except ValueError:
                        pass
                if not payload["reorder_point"] and re.fullmatch(r"[\d,\.]+", a["reorder"] or ""):
                    payload["reorder_point"] = float(a["reorder"].replace(",", ""))
                if a["vendor"]:
                    payload["memo"] = (payload["memo"] + " " if payload["memo"] else "") + f"공급처:{a['vendor']}"
            elif it["vendor"]:
                payload["memo"] = (payload["memo"] + " " if payload["memo"] else "") + f"거래처:{it['vendor']}"
            id_map[pid] = models.create_item(payload, conn=conn)

        rep.say(f"    품목 {len(id_map)}건 등록")

        posted = 0
        for t in dtx:
            models.post_txn(
                item_id=id_map[t["pid"]], txn_type=t["type"], qty=t["qty"],
                txn_date=t["date"], memo=t["memo"], batch_id=batch,
                source="migration", entered_unit="", conn=conn,
            )
            posted += 1
        rep.say(f"    D_Auto 입출고 {posted}건 반영")

    # B 는 이름 매칭이 필요하므로 별도 처리 (확인 대기로 빠질 수 있다)
    hint_alias(id_map, items, rep)
    bposted, bpending, bunit = 0, 0, 0
    with db.tx() as conn:
        for t in btx:
            try:
                res = models.register_by_name(
                    raw_name=t["name"], txn_type=t["type"], qty=t["qty"],
                    txn_date=t["date"], memo=t["memo"], batch_id=batch,
                    source="import", entered_unit=t["unit"], conn=conn,
                )
                if res.status == "posted":
                    bposted += 1
                else:
                    bpending += 1
            except models.DomainError as exc:
                # 단위를 바꿀 근거가 없는 행. 임의로 넣으면 재고가 틀어지므로
                # 원래 수량·단위 그대로 확인 대기에 넣어 사람이 판단하게 한다.
                bunit += 1
                bpending += 1
                rep.issue("단위 환산 불가",
                          f"{t['date']} {t['name']} {t['qty']:g}{t['unit']} — {str(exc).splitlines()[0]}",
                          {"date": t["date"], "name": t["name"],
                           "qty": t["qty"], "unit": t["unit"]})
                models.enqueue_pending(
                    raw_name=f"{t['name']} ({t['qty']:g}{t['unit']})",
                    norm=matching.normalize(t["name"]), qty=t["qty"],
                    txn_date=t["date"], txn_type=t["type"], batch_id=batch,
                    candidates=[c.as_dict() for c in matching.match(t["name"]).candidates],
                    conn=conn,
                )
    rep.say(f"    B.xlsx 입출고 {bposted}건 반영 / {bpending}건 확인 대기"
            + (f" (그중 단위 환산 불가 {bunit}건)" if bunit else ""))

    rep.stats["품목"] = len(id_map)
    rep.stats["원장"] = posted + bposted
    rep.stats["확인대기"] = bpending
    summarize(rep, items, dtx, btx, linked, unlinked)
    return rep


def hint_alias(id_map: dict, items: dict, rep: Report) -> None:
    """자료 대조로 확인된 B 축약 표기를 미리 별칭으로 심어 자동 매칭시킨다."""
    n = 0
    with db.tx() as conn:
        for short, full_key in B_ALIAS_HINT.items():
            target = None
            for pid, it in items.items():
                if full_key.lower() in it["name"].lower():
                    target = id_map.get(pid)
                    break
            if target:
                if matching.learn_alias(target, short, source="manual", conn=conn):
                    n += 1
    rep.say(f"    축약 표기 별칭 {n}건 사전 등록")


def summarize(rep: Report, items, dtx, btx, linked, unlinked) -> None:
    rep.say("\n" + "=" * 88)
    rep.say("  이관 요약")
    rep.say("=" * 88)
    rep.say(f"  품목            {len(items):>6}건")
    rep.say(f"  입출고(D_Auto)  {len(dtx):>6}건")
    rep.say(f"  입출고(B.xlsx)  {len(btx):>6}건")
    rep.say(f"  사내코드 연결   {len(linked):>6}건 / 미연결 {len(unlinked)}건")
    if rep.stats:
        rep.say("\n  발견한 문제")
        for k, v in rep.stats.most_common():
            if k in ("품목", "원장", "확인대기"):
                continue
            rep.say(f"    · {k:<16} {v:>5}건")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("folder", type=Path)
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--report", type=Path, default=None)
    args = ap.parse_args()

    rep = run(args.folder, args.commit)
    if args.report:
        args.report.write_text("\n".join(rep.lines), encoding="utf-8")
        (args.report.with_suffix(".issues.json")).write_text(
            json.dumps(rep.issues, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n리포트 저장: {args.report}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
