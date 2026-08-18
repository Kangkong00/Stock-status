"""로컬 전용 웹 UI. 127.0.0.1 에만 바인딩되며 외부에서 접근할 수 없다."""
from __future__ import annotations

import json
import traceback
from datetime import date, datetime, timedelta
from pathlib import Path

from flask import (Flask, abort, flash, jsonify, redirect, render_template,
                   request, send_file, session, url_for)
from werkzeug.utils import secure_filename

from .. import config, db, exporter, importer, mailer, matching, models, reports
from ..scheduler import scheduler

bp_name = "web"


# ---------------------------------------------------------------- 필터
def _number(v, digits=None):
    if v is None or v == "":
        return "0"
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    if digits is None:
        digits = 0 if abs(f - round(f)) < 1e-9 else 2
    return f"{f:,.{digits}f}"


TXN_KIND = {"IN": "입고", "OUT": "출고", "ADJ": "조정"}
STATUS_KO = {"OK": "정상", "LOW": "기준도달", "OUT": "품절"}


def create_app() -> Flask:
    here = Path(__file__).resolve().parent
    app = Flask(__name__, template_folder=str(here / "templates"),
                static_folder=str(here / "static"), static_url_path="/static")
    app.secret_key = _secret_key()
    app.config["MAX_CONTENT_LENGTH"] = 64 * 1024 * 1024      # 업로드 64MB
    app.jinja_env.filters["number"] = _number
    app.jinja_env.filters["txnkind"] = lambda v: TXN_KIND.get(v, v)
    app.jinja_env.filters["statusko"] = lambda v: STATUS_KO.get(v, v)

    from flask import Blueprint
    bp = Blueprint(bp_name, __name__)
    _register(bp)
    app.register_blueprint(bp)

    @app.context_processor
    def inject():
        try:
            pending = db.scalar("SELECT COUNT(*) FROM pending_names WHERE status='open'", (), 0)
            company = db.get_setting("company_name", "")
        except Exception:
            pending, company = 0, ""
        return {"nav_pending": pending, "company": company,
                "now": datetime.now().strftime("%Y-%m-%d %H:%M"), "active": ""}

    @app.errorhandler(models.DomainError)
    def _domain_error(exc):
        if _wants_json():
            return jsonify(ok=False, error=str(exc)), 400
        flash(str(exc), "err")
        return redirect(request.referrer or url_for("web.dashboard"))

    @app.errorhandler(mailer.MailError)
    def _mail_error(exc):
        if _wants_json():
            return jsonify(ok=False, error=str(exc)), 400
        flash(str(exc), "err")
        return redirect(request.referrer or url_for("web.settings"))

    @app.errorhandler(500)
    def _server_error(exc):
        tb = traceback.format_exc()
        app.logger.error(tb)
        if _wants_json():
            return jsonify(ok=False, error="처리 중 오류가 발생했습니다.\n" + tb.strip().splitlines()[-1]), 500
        return render_template("error.html", detail=tb), 500

    @app.errorhandler(413)
    def _too_large(exc):
        msg = "파일이 너무 큽니다. 64MB 이하로 나눠서 올려주세요."
        if _wants_json():
            return jsonify(ok=False, error=msg), 413
        flash(msg, "err")
        return redirect(url_for("web.upload"))

    return app


def _secret_key() -> bytes:
    """로컬 세션용 키. 최초 1회 생성 후 재사용한다."""
    import os
    key_file = config.DATA_DIR / ".secret"
    config.ensure_dirs()
    if key_file.exists():
        return key_file.read_bytes()
    key = os.urandom(32)
    key_file.write_bytes(key)
    try:
        key_file.chmod(0o600)
    except OSError:
        pass
    return key


def _wants_json() -> bool:
    return (request.headers.get("X-Requested-With") == "fetch"
            or request.path.startswith("/api/")
            or "application/json" in (request.headers.get("Accept") or ""))


def _body() -> dict:
    if request.is_json:
        return request.get_json(silent=True) or {}
    return request.form.to_dict()


def _arg(name: str, default: str = "") -> str:
    return (request.args.get(name) or default).strip()


# ======================================================================
#  라우트
# ======================================================================
def _register(bp):

    # ------------------------------------------------------------ 대시보드
    @bp.route("/")
    def dashboard():
        s = reports.summary()
        reorder = reports.reorder_list(limit=60)
        for r in reorder:
            rp = r["reorder_point"] or 0
            r["level_pct"] = min(100, round((r["on_hand"] / rp * 100), 1)) if rp > 0 else 0
        trend = reports.daily_trend(30)
        return render_template(
            "dashboard.html", active="dash", s=s, reorder=reorder,
            pending=models.list_pending(),
            recent=models.txn_history(limit=8),
            trend=trend,
            trend_max=max([max(d["in_qty"], d["out_qty"]) for d in trend] + [0]),
            categories=reports.category_breakdown(),
        )

    # ------------------------------------------------------------ 재고현황
    @bp.route("/stock")
    def stock():
        keyword, category = _arg("keyword"), _arg("category")
        status, order = _arg("status"), _arg("order", "code")
        show_all = _arg("all") == "1"
        rows = models.list_stock(keyword=keyword, category=category, status=status,
                                 include_inactive=show_all, order=order)
        totals = {
            "count": len(rows),
            "qty": sum(r["on_hand"] or 0 for r in rows),
            "value": sum(r["stock_value"] or 0 for r in rows),
            "alert": sum(1 for r in rows if r["stock_status"] in ("LOW", "OUT")),
        }
        return render_template("stock.html", active="stock", rows=rows, totals=totals,
                               categories=models.categories(), keyword=keyword,
                               category=category, status=status, order=order, show_all=show_all)

    @bp.route("/stock/<int:item_id>")
    def item_detail(item_id):
        item = models.get_item(item_id)
        if item is None:
            abort(404)
        d_from = _arg("from", (date.today() - timedelta(days=90)).isoformat())
        d_to = _arg("to", models.today())
        return render_template(
            "item_detail.html", active="stock", item=item,
            aliases=models.item_aliases(item_id),
            txns=models.txn_history(item_id=item_id, include_voided=True, limit=300),
            flow=reports.opening_closing(item_id, d_from, d_to),
            avg_out=reports._avg_daily_out(item_id),
            d_from=d_from, d_to=d_to,
        )

    # ------------------------------------------------------------ 품목 CRUD
    @bp.post("/api/items")
    def api_item_create():
        item_id = models.create_item(_body())
        return jsonify(ok=True, item_id=item_id, message="품목을 등록했습니다.")

    @bp.post("/api/items/<int:item_id>")
    def api_item_update(item_id):
        models.update_item(item_id, _body())
        return jsonify(ok=True, message="저장했습니다.")

    @bp.post("/api/items/<int:item_id>/delete")
    def api_item_delete(item_id):
        models.delete_item(item_id)
        return jsonify(ok=True, message="삭제했습니다.")

    @bp.get("/api/items/<int:item_id>")
    def api_item_get(item_id):
        row = db.query_one("SELECT * FROM items WHERE id = ?", (item_id,))
        if row is None:
            return jsonify(ok=False, error="품목을 찾을 수 없습니다."), 404
        return jsonify(ok=True, item=dict(row))

    # ------------------------------------------------------------ 별칭
    @bp.get("/api/items/<int:item_id>/aliases")
    def api_alias_list(item_id):
        return jsonify(ok=True, aliases=[dict(a) for a in models.item_aliases(item_id)])

    @bp.post("/api/items/<int:item_id>/aliases")
    def api_alias_add(item_id):
        models.add_alias(item_id, (_body().get("raw_name") or "").strip())
        return jsonify(ok=True, message="별칭을 추가했습니다.",
                       aliases=[dict(a) for a in models.item_aliases(item_id)])

    @bp.post("/api/aliases/<int:alias_id>/delete")
    def api_alias_delete(alias_id):
        models.delete_alias(alias_id)
        return jsonify(ok=True, message="별칭을 삭제했습니다.")

    # ------------------------------------------------------------ 검색/매칭
    @bp.get("/api/search")
    def api_search():
        q = _arg("q")
        if not q:
            return jsonify(ok=True, items=[])
        rows = models.list_stock(keyword=q, limit=15)
        return jsonify(ok=True, items=[{
            "item_id": r["item_id"], "code": r["code"], "name": r["name"], "spec": r["spec"],
            "unit": r["unit"], "on_hand": r["on_hand"], "reorder_point": r["reorder_point"],
            "unit_cost": r["unit_cost"], "status": r["stock_status"], "location": r["location"],
            "pack_unit": r["pack_unit"], "pack_size": r["pack_size"],
            "internal_code": r["internal_code"],
        } for r in rows])

    @bp.get("/api/match")
    def api_match():
        """상품명을 입력하는 즉시 후보를 보여준다 (등록 화면의 실시간 도우미)."""
        q = _arg("q")
        if not q:
            return jsonify(ok=True, status="none", candidates=[])
        res = matching.match(q)
        return jsonify(ok=True, status=res.status, item_id=res.item_id,
                       norm=res.norm, candidates=[c.as_dict() for c in res.candidates])

    # ------------------------------------------------------------ 입출고 등록
    @bp.route("/register")
    def register():
        preset = None
        item_id = request.args.get("item_id", type=int)
        if item_id:
            row = models.get_item(item_id)
            if row:
                preset = dict(row)
        return render_template("register.html", active="reg", preset=preset,
                               txn_type=_arg("type", "IN"), today=models.today(),
                               recent=models.txn_history(limit=15))

    @bp.post("/api/txn")
    def api_txn_create():
        b = _body()
        qty = b.get("qty")
        txn_type = (b.get("txn_type") or "IN").upper()
        item_id = b.get("item_id")

        common = dict(txn_type=txn_type, qty=qty, txn_date=b.get("txn_date"),
                      partner=b.get("partner", ""), doc_no=b.get("doc_no", ""),
                      memo=b.get("memo", ""), unit_cost=b.get("unit_cost") or 0,
                      entered_unit=b.get("entered_unit", ""))

        if item_id:
            txn_id = models.post_txn(item_id=int(item_id), raw_name=b.get("raw_name", ""),
                                     allow_negative=b.get("allow_negative", "1") != "0", **common)
            item = models.get_item(int(item_id))
            return jsonify(ok=True, status="posted", txn_id=txn_id,
                           on_hand=item["on_hand"], stock_status=item["stock_status"],
                           message=f"[{item['code']}] {item['name']} · 현재고 {_number(item['on_hand'])}{item['unit']}")

        raw = (b.get("raw_name") or "").strip()
        if not raw:
            raise models.DomainError("품목을 선택하거나 상품명을 입력해 주세요.")
        res = models.register_by_name(raw_name=raw, **common)
        if res.status == "posted":
            item = models.get_item(res.item_id)
            return jsonify(ok=True, status="posted", txn_id=res.txn_id,
                           on_hand=item["on_hand"], stock_status=item["stock_status"],
                           message=f"[{item['code']}] {item['name']} · 현재고 {_number(item['on_hand'])}{item['unit']}")
        return jsonify(ok=True, status="pending", pending_id=res.pending_id,
                       candidates=res.candidates, message=res.message)

    @bp.post("/api/txn/<int:txn_id>/void")
    def api_txn_void(txn_id):
        models.void_txn(txn_id, (_body().get("reason") or "").strip())
        return jsonify(ok=True, message="전표를 취소했습니다.")

    # ------------------------------------------------------------ 확인 대기
    @bp.route("/pending")
    def pending():
        return render_template("pending.html", active="pending",
                               rows=models.list_pending(),
                               resolved=models.list_pending("resolved")[:20])

    @bp.post("/api/pending/<int:pending_id>/resolve")
    def api_pending_resolve(pending_id):
        b = _body()
        item_id = b.get("item_id")
        if item_id:
            models.resolve_pending(pending_id, int(item_id))
            item = models.get_item(int(item_id))
            return jsonify(ok=True, message=f"[{item['code']}] {item['name']} 로 연결했습니다. "
                                            f"이 이름은 다음부터 자동 처리됩니다.")
        new_item, _ = models.resolve_pending_as_new_item(pending_id, b)
        item = models.get_item(new_item)
        return jsonify(ok=True, message=f"신규 품목 [{item['code']}] {item['name']} 등록 후 입고까지 완료했습니다.")

    @bp.post("/api/pending/<int:pending_id>/discard")
    def api_pending_discard(pending_id):
        models.discard_pending(pending_id)
        return jsonify(ok=True, message="목록에서 제외했습니다.")

    # ------------------------------------------------------------ 입출고 내역
    @bp.route("/history")
    def history():
        d_from = _arg("from", (date.today() - timedelta(days=30)).isoformat())
        d_to = _arg("to", models.today())
        txn_type, keyword = _arg("type"), _arg("keyword")
        show_voided = _arg("voided") == "1"
        rows = models.txn_history(date_from=d_from, date_to=d_to, txn_type=txn_type,
                                  keyword=keyword, include_voided=show_voided, limit=1000)
        totals = {
            "in": sum(r["qty"] for r in rows if r["txn_type"] == "IN" and not r["voided"]),
            "out": sum(r["qty"] for r in rows if r["txn_type"] == "OUT" and not r["voided"]),
            "count": len(rows),
        }
        return render_template("history.html", active="hist", rows=rows, totals=totals,
                               d_from=d_from, d_to=d_to, txn_type=txn_type,
                               keyword=keyword, show_voided=show_voided)

    # ------------------------------------------------------------ 재고실사
    @bp.route("/stocktake")
    def stocktake_list():
        rows = db.query(
            """SELECT s.*,
                      (SELECT COUNT(*) FROM stocktake_lines WHERE stocktake_id=s.id) AS line_count,
                      (SELECT COUNT(*) FROM stocktake_lines
                        WHERE stocktake_id=s.id AND counted_qty IS NOT NULL) AS counted
                 FROM stocktakes s ORDER BY s.id DESC""")
        return render_template("stocktake_list.html", active="take", rows=rows,
                               categories=models.categories(), today=models.today())

    @bp.post("/stocktake/new")
    def stocktake_new():
        b = _body()
        st_id = models.create_stocktake(title=b.get("title", ""), take_date=b.get("take_date"),
                                        category=b.get("category", ""))
        flash("실사를 시작했습니다. 실물 수량만 입력하면 차이는 자동으로 계산됩니다.", "ok")
        return redirect(url_for("web.stocktake_detail", stocktake_id=st_id))

    @bp.route("/stocktake/<int:stocktake_id>")
    def stocktake_detail(stocktake_id):
        st = db.query_one("SELECT * FROM stocktakes WHERE id = ?", (stocktake_id,))
        if st is None:
            abort(404)
        lines = models.stocktake_lines(stocktake_id)
        counted = [l for l in lines if l["counted_qty"] is not None]
        diffs = [l for l in counted if abs((l["counted_qty"] or 0) - l["system_qty"]) > 1e-9]
        summary = {
            "total": len(lines), "counted": len(counted), "diff_count": len(diffs),
            "diff_qty": sum((l["counted_qty"] or 0) - l["system_qty"] for l in diffs),
            "diff_value": sum(((l["counted_qty"] or 0) - l["system_qty"]) * (l["unit_cost"] or 0)
                              for l in diffs),
        }
        return render_template("stocktake_detail.html", active="take", st=st,
                               lines=lines, summary=summary)

    @bp.post("/api/stocktake/<int:stocktake_id>/count")
    def api_stocktake_count(stocktake_id):
        b = _body()
        models.set_count(stocktake_id, int(b["item_id"]), b.get("counted_qty"), b.get("memo", ""))
        return jsonify(ok=True)

    @bp.post("/stocktake/<int:stocktake_id>/commit")
    def stocktake_commit(stocktake_id):
        res = models.commit_stocktake(stocktake_id)
        if res["adjusted"]:
            flash(f"실사를 확정했습니다. {res['adjusted']}개 품목에 조정 전표를 만들었고 "
                  f"총 {_number(res['total_diff'])}만큼 재고를 맞췄습니다.", "ok")
        else:
            flash("실사를 확정했습니다. 전산 재고와 실물이 모두 일치했습니다.", "ok")
        return redirect(url_for("web.stocktake_detail", stocktake_id=stocktake_id))

    @bp.post("/stocktake/<int:stocktake_id>/delete")
    def stocktake_delete(stocktake_id):
        st = db.query_one("SELECT status FROM stocktakes WHERE id=?", (stocktake_id,))
        if st and st["status"] == "committed":
            raise models.DomainError("확정된 실사는 삭제할 수 없습니다.")
        with db.tx() as c:
            c.execute("DELETE FROM stocktakes WHERE id = ?", (stocktake_id,))
        flash("실사를 삭제했습니다.", "ok")
        return redirect(url_for("web.stocktake_list"))

    # ------------------------------------------------------------ 엑셀 업로드
    @bp.route("/upload")
    def upload():
        return render_template("upload.html", active="upload", today=models.today())

    @bp.post("/upload/analyze")
    def upload_analyze():
        """1단계: 파일을 읽어 시트/헤더/자동 매핑 결과를 보여준다. DB는 건드리지 않는다."""
        f = request.files.get("file")
        if not f or not f.filename:
            raise models.DomainError("파일을 선택해 주세요.")

        config.ensure_dirs()
        safe = secure_filename(f.filename) or "upload.xlsx"
        # secure_filename 은 한글을 지운다. 확장자만 살리고 저장명은 시간으로 만든다.
        ext = Path(f.filename).suffix.lower() or Path(safe).suffix.lower()
        dest = config.UPLOAD_DIR / f"{datetime.now():%Y%m%d_%H%M%S}{ext}"
        f.save(dest)

        sheets = importer.read_workbook(dest)
        payload = {
            "path": str(dest), "filename": f.filename,
            "sheets": [{
                "name": s.name, "headers": s.headers, "rows": s.row_count,
                "header_row": s.header_row, "preview": s.preview(6),
                "mapping": {str(k): v for k, v in importer.auto_mapping(s.headers).items()},
            } for s in sheets],
        }
        # 세션 쿠키는 4KB 를 넘으면 브라우저가 조용히 버린다.
        # 열이 많은 파일에서도 안전하도록 경로만 남기고, 나머지는 응답으로만 보낸다.
        session["upload_path"] = str(dest)
        session["upload_name"] = f.filename
        return jsonify(ok=True, **payload, fields=_FIELD_LABELS)

    @bp.post("/upload/run")
    def upload_run():
        """2단계: 사람이 확정한 매핑으로 실행. dry_run 이면 결과만 계산하고 되돌린다."""
        b = _body() if not request.is_json else request.get_json()
        staged = session.get("upload_path")
        if not staged:
            raise models.DomainError("업로드 정보가 만료되었습니다. 파일을 다시 올려주세요.")

        path = Path(staged)
        if not path.exists():
            raise models.DomainError("업로드한 파일을 찾을 수 없습니다. 다시 올려주세요.")

        sheet_index = int(b.get("sheet_index", 0))
        mode = b.get("mode", "items")
        dry = bool(b.get("dry_run"))
        mapping = {int(k): v for k, v in (b.get("mapping") or {}).items() if v}
        if not mapping:
            raise models.DomainError("최소 한 개 이상의 열을 지정해 주세요.")

        sheets = importer.read_workbook(path)
        if sheet_index >= len(sheets):
            raise models.DomainError("시트를 찾을 수 없습니다.")
        sheet = sheets[sheet_index]

        if mode == "items":
            if "code" not in mapping.values() and "name" not in mapping.values():
                raise models.DomainError("'품목코드' 또는 '품목명' 열을 지정해야 합니다.")
            rep = importer.import_items(
                sheet, mapping, dry_run=dry,
                update_existing=b.get("update_existing", True),
                opening_stock_date=b.get("opening_stock_date", ""))
        else:
            if "name" not in mapping.values() and "code" not in mapping.values():
                raise models.DomainError("'상품명' 또는 '품목코드' 열을 지정해야 합니다.")
            vals = set(mapping.values())
            if not ({"qty", "in_qty", "out_qty"} & vals):
                raise models.DomainError("'수량' 또는 '입고'/'출고' 열을 지정해야 합니다.")
            rep = importer.import_transactions(
                sheet, mapping, dry_run=dry,
                default_type=b.get("default_type", "IN"),
                default_date=b.get("default_date", ""))

        return jsonify(ok=True, dry_run=dry, report=rep.as_dict())

    # ------------------------------------------------------------ 내보내기
    @bp.get("/export/stock")
    def export_stock_file():
        path = exporter.export_stock(include_inactive=_arg("all") == "1")
        return send_file(path, as_attachment=True, download_name=path.name)

    @bp.get("/export/transactions")
    def export_txn_file():
        path = exporter.export_transactions(_arg("from"), _arg("to"))
        return send_file(path, as_attachment=True, download_name=path.name)

    @bp.get("/export/stocktake/<int:stocktake_id>")
    def export_stocktake_file(stocktake_id):
        path = exporter.export_stocktake(stocktake_id)
        return send_file(path, as_attachment=True, download_name=path.name)

    @bp.get("/export/template")
    def export_template_file():
        path = exporter.export_template()
        return send_file(path, as_attachment=True, download_name="재고관리_업로드양식.xlsx")

    # ------------------------------------------------------------ 설정
    @bp.route("/settings")
    def settings():
        return render_template("settings.html", active="set", s=db.get_settings(),
                               sched=scheduler.status(),
                               backups=sorted(config.BACKUP_DIR.glob("stock_*.db"),
                                              key=lambda p: p.stat().st_mtime, reverse=True)[:15],
                               db_path=str(config.DB_PATH),
                               db_size=config.DB_PATH.stat().st_size if config.DB_PATH.exists() else 0)

    @bp.post("/settings")
    def settings_save():
        b = request.form.to_dict()
        keep = {k: v for k, v in b.items() if k in config.DEFAULT_SETTINGS}
        for flagname in ("report_daily_enabled", "report_weekly_enabled"):
            keep[flagname] = "1" if b.get(flagname) else "0"
        # 비밀번호를 비워서 저장하면 기존 값을 지우지 않고 유지한다
        if not keep.get("smtp_password"):
            keep.pop("smtp_password", None)
        db.set_settings(keep)
        flash("설정을 저장했습니다.", "ok")
        return redirect(url_for("web.settings"))

    @bp.post("/api/mail/test")
    def api_mail_test():
        return jsonify(ok=True, message="테스트 메일을 보냈습니다. 받은편지함을 확인해 주세요.",
                       **mailer.send_test((_body().get("to") or "").strip()))

    @bp.post("/api/mail/send-now")
    def api_mail_send_now():
        kind = (_body().get("kind") or "daily").strip()
        res = mailer.send_report(kind, force=True)
        return jsonify(ok=True, message=f"{'일일' if kind=='daily' else '주간'} 리포트를 발송했습니다.", **res)

    @bp.get("/report/preview")
    def report_preview():
        return mailer.render_report_html(reports.build_report(_arg("kind", "daily")))

    @bp.post("/api/backup")
    def api_backup():
        path = db.backup(tag="manual")
        return jsonify(ok=True, message=f"백업했습니다: {path.name}", path=str(path))

    @bp.get("/api/health")
    def api_health():
        return jsonify(ok=True, db=str(config.DB_PATH), scheduler=scheduler.status())


_FIELD_LABELS = {
    "": "(사용 안 함)",
    "code": "품목코드", "name": "품목명 / 상품명", "spec": "규격", "unit": "단위",
    "category": "분류", "barcode": "바코드", "location": "보관위치",
    "reorder_point": "재고기준(안전재고)", "reorder_qty": "권장발주량",
    "unit_cost": "단가", "on_hand": "현재고(기초재고)",
    "qty": "수량", "in_qty": "입고수량", "out_qty": "출고수량",
    "txn_date": "일자", "partner": "거래처", "doc_no": "전표번호", "memo": "비고",
}
