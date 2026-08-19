"""
품목코드 자동 부여.

사람은 코드를 외우지 않는다. 분류만 고르면 다음 번호가 자동으로 붙는다.
이관 때 쓴 규칙과 같은 규칙을 프로그램도 쓰도록 여기 한 곳에 모아 둔다.
"""
from __future__ import annotations

import re
import string

from . import db

# 이관 시 사용한 분류 -> 접두어. 기존 코드 체계와 어긋나지 않게 유지한다.
CATEGORY_PREFIX: dict[str, str] = {
    "계측기": "MT",
    "필터류": "FL",
    "페인트 및 오일": "PT",
    "공구류": "TL",
    "약품류": "CH",
    "기타자재류": "ET",
    "펌프자재류": "PM",
    "볼트,피팅류": "BF",
    "전기자재류": "EL",
    "밸브류": "VL",
    "배관류": "PP",
}
FALLBACK_PREFIX = "GN"
CODE_RE = re.compile(r"^([A-Z]{2})-(\d{4})$")


def _norm(name: str) -> str:
    return re.sub(r"\s+", "", (name or "")).lower()


def used_prefixes() -> dict[str, str]:
    """실제 DB 에서 쓰이고 있는 분류 -> 접두어. 이쪽이 사전보다 우선한다."""
    out: dict[str, str] = {}
    for r in db.query(
        """SELECT category, substr(code, 1, 2) AS pre, COUNT(*) n
             FROM items
            WHERE code LIKE '__-____' AND category <> ''
            GROUP BY category, pre ORDER BY n DESC"""
    ):
        out.setdefault(_norm(r["category"]), r["pre"])
    return out


def prefix_for(category: str) -> str:
    """분류에 대응하는 접두어. 없으면 겹치지 않는 새 접두어를 만든다."""
    key = _norm(category)
    if not key:
        return FALLBACK_PREFIX

    inuse = used_prefixes()
    if key in inuse:
        return inuse[key]

    for name, pre in CATEGORY_PREFIX.items():
        if _norm(name) == key:
            return pre

    taken = set(inuse.values()) | set(CATEGORY_PREFIX.values())
    taken |= {r["pre"] for r in db.query(
        "SELECT DISTINCT substr(code,1,2) AS pre FROM items WHERE code LIKE '__-____'")}

    # 분류명에 영문이 있으면 그 두 글자를 먼저 써 본다
    letters = re.sub(r"[^A-Za-z]", "", category).upper()
    if len(letters) >= 2 and letters[:2] not in taken:
        return letters[:2]

    for ch in string.ascii_uppercase:
        cand = "G" + ch
        if cand not in taken:
            return cand
    for a in string.ascii_uppercase:
        for b in string.ascii_uppercase:
            if a + b not in taken:
                return a + b
    return FALLBACK_PREFIX


def next_code(category: str = "") -> str:
    """해당 분류의 다음 품목코드. 예: 약품류 -> CH-0025"""
    pre = prefix_for(category)
    rows = db.query(
        "SELECT code FROM items WHERE code LIKE ? ORDER BY code", (pre + "-%",))
    used = set()
    for r in rows:
        m = CODE_RE.match(r["code"])
        if m and m.group(1) == pre:
            used.add(int(m.group(2)))
    n = 1
    while n in used:
        n += 1
    return f"{pre}-{n:04d}"


def category_table() -> list[dict]:
    """화면에 보여줄 분류표. 어떤 분류가 어떤 접두어를 쓰는지."""
    inuse = used_prefixes()
    counts = {_norm(r["category"]): r["n"] for r in db.query(
        "SELECT category, COUNT(*) n FROM items WHERE category <> '' GROUP BY category")}
    names: dict[str, str] = {}
    for r in db.query("SELECT DISTINCT category FROM items WHERE category <> ''"):
        names[_norm(r["category"])] = r["category"]
    for name in CATEGORY_PREFIX:
        names.setdefault(_norm(name), name)

    out = []
    for key, label in names.items():
        pre = inuse.get(key) or CATEGORY_PREFIX.get(label, "")
        if not pre:
            for nm, p in CATEGORY_PREFIX.items():
                if _norm(nm) == key:
                    pre = p
                    break
        out.append({"category": label, "prefix": pre or "-",
                    "count": counts.get(key, 0), "next": next_code(label)})
    return sorted(out, key=lambda x: (-x["count"], x["category"]))
