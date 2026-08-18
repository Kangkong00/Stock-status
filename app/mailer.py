"""이메일 리포트 발송. 첨부로 재고현황 엑셀을 함께 보낸다."""
from __future__ import annotations

import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, formatdate
from html import escape
from pathlib import Path

from . import db, exporter, reports


class MailError(Exception):
    """설정 문제 등 사용자에게 그대로 보여줄 발송 오류."""


def _recipients(raw: str) -> list[str]:
    return [a.strip() for a in raw.replace(";", ",").split(",") if a.strip()]


def smtp_config() -> dict:
    s = db.get_settings()
    return {
        "host": s.get("smtp_host", "").strip(),
        "port": int(s.get("smtp_port") or 587),
        "user": s.get("smtp_user", "").strip(),
        "password": s.get("smtp_password", ""),
        "security": (s.get("smtp_security") or "starttls").lower(),
        "sender": (s.get("smtp_from") or s.get("smtp_user") or "").strip(),
        "to": _recipients(s.get("report_to", "")),
        "company": s.get("company_name", ""),
    }


def is_configured() -> bool:
    c = smtp_config()
    return bool(c["host"] and c["sender"] and c["to"])


def send_mail(subject: str, html: str, text: str = "",
              attachments: list[Path] | None = None,
              to: list[str] | None = None) -> None:
    c = smtp_config()
    if not c["host"]:
        raise MailError("SMTP 서버가 설정되지 않았습니다. [설정] 화면에서 입력해 주세요.")
    recipients = to or c["to"]
    if not recipients:
        raise MailError("받는 사람이 지정되지 않았습니다. [설정] 화면에서 입력해 주세요.")

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr((c["company"] or "재고관리", c["sender"]))
    msg["To"] = ", ".join(recipients)
    msg["Date"] = formatdate(localtime=True)
    msg.set_content(text or "HTML 메일입니다. HTML 보기를 지원하는 메일 앱에서 열어 주세요.")
    msg.add_alternative(html, subtype="html")

    for path in attachments or []:
        path = Path(path)
        if not path.exists():
            continue
        msg.add_attachment(
            path.read_bytes(),
            maintype="application",
            subtype="vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            filename=path.name,
        )

    try:
        if c["security"] == "ssl":
            with smtplib.SMTP_SSL(c["host"], c["port"], timeout=30,
                                  context=ssl.create_default_context()) as srv:
                if c["user"]:
                    srv.login(c["user"], c["password"])
                srv.send_message(msg)
        else:
            with smtplib.SMTP(c["host"], c["port"], timeout=30) as srv:
                srv.ehlo()
                if c["security"] == "starttls":
                    srv.starttls(context=ssl.create_default_context())
                    srv.ehlo()
                if c["user"]:
                    srv.login(c["user"], c["password"])
                srv.send_message(msg)
    except smtplib.SMTPAuthenticationError:
        raise MailError(
            "SMTP 로그인에 실패했습니다. 아이디/비밀번호를 확인해 주세요.\n"
            "네이버·구글 메일은 계정 비밀번호가 아니라 '앱 비밀번호'를 발급받아 넣어야 합니다."
        )
    except (smtplib.SMTPException, OSError) as exc:
        raise MailError(f"메일 발송 실패: {exc}")


# ---------------------------------------------------------------- 본문 작성
_CSS = """
body{font-family:'Malgun Gothic','Apple SD Gothic Neo',sans-serif;color:#1f2933;margin:0;padding:20px;background:#f5f7fa}
.wrap{max-width:820px;margin:0 auto;background:#fff;border-radius:10px;overflow:hidden;
      box-shadow:0 1px 4px rgba(0,0,0,.08)}
.hd{background:#1f3a5f;color:#fff;padding:20px 24px}
.hd h1{margin:0;font-size:19px}.hd p{margin:6px 0 0;font-size:13px;opacity:.85}
.body{padding:22px 24px}
h2{font-size:15px;margin:26px 0 10px;padding-bottom:7px;border-bottom:2px solid #e3e8ef}
h2:first-child{margin-top:0}
.cards{display:table;width:100%;border-spacing:8px 0;margin-bottom:6px}
.card{display:table-cell;background:#f7f9fc;border:1px solid #e3e8ef;border-radius:8px;
      padding:12px;text-align:center;width:25%}
.card .n{font-size:22px;font-weight:700;color:#1f3a5f}
.card .l{font-size:11px;color:#6b7684;margin-top:3px}
.card.warn .n{color:#c92a2a}
table{width:100%;border-collapse:collapse;font-size:12.5px}
th{background:#eef2f7;text-align:left;padding:8px 9px;border-bottom:2px solid #d7dee8;font-weight:600}
td{padding:7px 9px;border-bottom:1px solid #eef1f5}
.r{text-align:right}
.out{background:#fff5f5}.low{background:#fffbeb}
.tag{display:inline-block;padding:1px 7px;border-radius:10px;font-size:11px;font-weight:600}
.tag.out{background:#ffe3e3;color:#c92a2a}.tag.low{background:#fff3bf;color:#a67c00}
.empty{color:#868e96;font-size:13px;padding:10px 0}
.ft{padding:14px 24px;background:#f7f9fc;color:#868e96;font-size:11.5px;border-top:1px solid #e3e8ef}
"""


def _fmt(v, digits: int = 0) -> str:
    if v is None or v == "":
        return "-"
    try:
        f = float(v)
    except (TypeError, ValueError):
        return escape(str(v))
    return f"{f:,.{digits}f}" if digits else f"{f:,.0f}"


def render_report_html(rep: dict) -> str:
    s = rep["summary"]
    company = escape(rep.get("company") or "")
    parts: list[str] = [
        f"<!doctype html><html><head><meta charset='utf-8'><style>{_CSS}</style></head><body><div class='wrap'>",
        f"<div class='hd'><h1>{escape(rep['title'])}</h1>",
        f"<p>{company + ' · ' if company else ''}{escape(rep['period'])} · 생성 {escape(rep['generated_at'])}</p></div>",
        "<div class='body'>",
        "<div class='cards'>",
        f"<div class='card'><div class='n'>{_fmt(s.get('item_count'))}</div><div class='l'>관리 품목</div></div>",
        f"<div class='card {'warn' if s.get('alert_count') else ''}'>"
        f"<div class='n'>{_fmt(s.get('alert_count'))}</div><div class='l'>발주 필요</div></div>",
        f"<div class='card {'warn' if s.get('pending_count') else ''}'>"
        f"<div class='n'>{_fmt(s.get('pending_count'))}</div><div class='l'>확인 대기</div></div>",
        f"<div class='card'><div class='n'>{_fmt(s.get('total_value'))}</div><div class='l'>재고금액(원)</div></div>",
        "</div>",
    ]

    # 발주 필요 — 리포트의 핵심
    parts.append("<h2>지금 발주해야 할 품목</h2>")
    if rep["reorder"]:
        parts.append("<table><tr><th>품목코드</th><th>품목명</th><th>규격</th>"
                     "<th class='r'>현재고</th><th class='r'>재고기준</th>"
                     "<th class='r'>권장발주</th><th>상태</th></tr>")
        for r in rep["reorder"][:25]:
            cls = "out" if r["stock_status"] == "OUT" else "low"
            label = "품절" if r["stock_status"] == "OUT" else "기준도달"
            parts.append(
                f"<tr class='{cls}'><td>{escape(r['code'])}</td><td>{escape(r['name'])}</td>"
                f"<td>{escape(r['spec'] or '')}</td><td class='r'>{_fmt(r['on_hand'])}</td>"
                f"<td class='r'>{_fmt(r['reorder_point'])}</td>"
                f"<td class='r'><b>{_fmt(r['suggest_qty'])}</b> {escape(r['unit'] or '')}</td>"
                f"<td><span class='tag {cls}'>{label}</span></td></tr>")
        parts.append("</table>")
        if len(rep["reorder"]) > 25:
            parts.append(f"<p class='empty'>외 {len(rep['reorder']) - 25}건 더 있습니다.</p>")
    else:
        parts.append("<p class='empty'>재고기준에 도달한 품목이 없습니다.</p>")

    # 확인 대기
    if rep["pending"]:
        parts.append("<h2>확인 대기 — 상품명을 찾지 못한 건</h2><table>"
                     "<tr><th>일자</th><th>원문 상품명</th><th class='r'>수량</th><th>추천 후보</th></tr>")
        for p in rep["pending"]:
            cands = ", ".join(escape(c["code"]) for c in (p.get("candidates") or [])[:2]) or "-"
            parts.append(f"<tr><td>{escape(p['txn_date'])}</td><td>{escape(p['raw_name'])}</td>"
                         f"<td class='r'>{_fmt(p['qty'])}</td><td>{cands}</td></tr>")
        parts.append("</table><p class='empty'>프로그램 화면에서 클릭 한 번으로 연결하면 "
                     "다음부터는 자동 처리됩니다.</p>")

    # 기간 입출고
    parts.append(f"<h2>기간 입출고 ({escape(rep['period'])})</h2>")
    if rep["movement"]:
        parts.append("<table><tr><th>품목코드</th><th>품목명</th>"
                     "<th class='r'>입고</th><th class='r'>출고</th><th class='r'>조정</th></tr>")
        for m in rep["movement"][:20]:
            parts.append(f"<tr><td>{escape(m['code'])}</td><td>{escape(m['name'])}</td>"
                         f"<td class='r'>{_fmt(m['in_qty'])}</td><td class='r'>{_fmt(m['out_qty'])}</td>"
                         f"<td class='r'>{_fmt(m['adj_qty'])}</td></tr>")
        parts.append("</table>")
    else:
        parts.append("<p class='empty'>해당 기간에 입출고가 없습니다.</p>")

    if rep.get("dormant"):
        parts.append("<h2>장기 미사용 재고 — 자금이 묶여 있는 곳</h2><table>"
                     "<tr><th>품목코드</th><th>품목명</th><th class='r'>현재고</th>"
                     "<th class='r'>재고금액</th><th class='r'>미사용일</th></tr>")
        for d in rep["dormant"][:15]:
            parts.append(f"<tr><td>{escape(d['code'])}</td><td>{escape(d['name'])}</td>"
                         f"<td class='r'>{_fmt(d['on_hand'])}</td><td class='r'>{_fmt(d['stock_value'])}</td>"
                         f"<td class='r'>{d['idle_days'] if d['idle_days'] is not None else '-'}</td></tr>")
        parts.append("</table>")

    if rep.get("top_movers"):
        parts.append("<h2>많이 나간 품목</h2><table>"
                     "<tr><th>품목코드</th><th>품목명</th><th class='r'>출고수량</th><th class='r'>건수</th></tr>")
        for t in rep["top_movers"]:
            parts.append(f"<tr><td>{escape(t['code'])}</td><td>{escape(t['name'])}</td>"
                         f"<td class='r'>{_fmt(t['out_qty'])}</td><td class='r'>{_fmt(t['cnt'])}</td></tr>")
        parts.append("</table>")

    parts.append("</div><div class='ft'>이 메일은 재고관리 프로그램이 자동 발송했습니다. "
                 "발송 시각·주기는 프로그램 [설정] 화면에서 변경할 수 있습니다.</div></div></body></html>")
    return "".join(parts)


def render_report_text(rep: dict) -> str:
    s = rep["summary"]
    lines = [
        f"{rep['title']}  ({rep['period']})",
        "=" * 46,
        f"관리 품목 {_fmt(s.get('item_count'))} / 발주 필요 {_fmt(s.get('alert_count'))} / "
        f"확인 대기 {_fmt(s.get('pending_count'))}",
        f"재고금액 {_fmt(s.get('total_value'))}원",
        "",
        "[ 발주 필요 ]",
    ]
    if rep["reorder"]:
        for r in rep["reorder"][:25]:
            mark = "품절" if r["stock_status"] == "OUT" else "기준도달"
            lines.append(f"  · [{r['code']}] {r['name']} {r['spec']}  "
                         f"현재고 {_fmt(r['on_hand'])} / 기준 {_fmt(r['reorder_point'])}  "
                         f"→ {_fmt(r['suggest_qty'])}{r['unit']} 발주 ({mark})")
    else:
        lines.append("  없음")

    if rep["pending"]:
        lines += ["", "[ 확인 대기 ]"]
        for p in rep["pending"]:
            lines.append(f"  · {p['raw_name']}  {_fmt(p['qty'])}")
    return "\n".join(lines)


def send_report(kind: str = "daily", *, attach_excel: bool = True,
                to: list[str] | None = None, force: bool = False) -> dict:
    """리포트를 만들어 보낸다. 보고할 게 없으면 조용히 건너뛴다."""
    rep = reports.build_report(kind)
    if not force and not reports.has_anything_to_report(rep):
        return {"sent": False, "reason": "보고할 변화가 없어 발송하지 않았습니다."}

    attachments: list[Path] = []
    if attach_excel:
        try:
            attachments.append(exporter.export_stock())
        except Exception:
            pass  # 첨부 실패가 메일 자체를 막지는 않는다

    company = db.get_setting("company_name", "")
    prefix = f"[{company}] " if company else ""
    alert = rep["summary"].get("alert_count") or 0
    subject = f"{prefix}{rep['title']} ({rep['period']})"
    if alert:
        subject += f" - 발주 필요 {alert}건"

    send_mail(subject, render_report_html(rep), render_report_text(rep), attachments, to)
    return {"sent": True, "subject": subject, "alert_count": alert,
            "attachments": [p.name for p in attachments]}


def send_test(to: str = "") -> dict:
    recipients = _recipients(to) if to else None
    html = ("<div style='font-family:sans-serif;padding:20px'>"
            "<h2 style='color:#1f3a5f'>메일 설정 테스트</h2>"
            "<p>이 메일이 보인다면 SMTP 설정이 정상입니다.</p>"
            "<p style='color:#868e96;font-size:13px'>재고관리 프로그램</p></div>")
    send_mail("[재고관리] 메일 설정 테스트", html, "메일 설정이 정상입니다.", to=recipients)
    return {"sent": True}
