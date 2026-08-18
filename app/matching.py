"""
상품명 정규화 + 유사도 매칭.

3층 구조의 2층(별칭)을 구동하는 엔진.
  ① 별칭 완전일치        -> 사람 손 0
  ② 유사도 매칭 + 클릭   -> 그 순간 별칭 학습, 다음부터 ①
  ③ 후보 없음            -> 미매핑 큐
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher

from . import config, db

# --------------------------------------------------------------------------
# 정규화 규칙
# --------------------------------------------------------------------------

# 재고 식별과 무관한 수식어. 있으나 없으나 같은 물건이다.
_NOISE_WORDS = [
    "정품", "신품", "새제품", "국산", "수입", "무료배송", "당일발송", "특가",
    "행사", "사은품", "증정", "묶음", "세트상품", "낱개", "벌크", "리퍼",
    "택배", "본사직송", "재입고", "한정", "옵션", "선택",
]

# 표기만 다르고 뜻이 같은 것들 -> 대표 표기로 통일
_SYNONYMS = {
    "블랙": "검정", "black": "검정", "bk": "검정", "흑색": "검정", "흑": "검정",
    "화이트": "흰색", "white": "흰색", "wh": "흰색", "백색": "흰색",
    "레드": "빨강", "red": "빨강", "적색": "빨강",
    "블루": "파랑", "blue": "파랑", "청색": "파랑",
    "그린": "초록", "green": "초록", "녹색": "초록",
    "옐로우": "노랑", "yellow": "노랑", "황색": "노랑",
    "그레이": "회색", "gray": "회색", "grey": "회색",
    "실버": "은색", "silver": "은색",
    "박스": "box", "상자": "box", "케이스": "box",
    "개입": "개", "낱개": "개", "ea": "개", "pcs": "개", "pc": "개",
}

# 단위 표기 통일 (전각/한글/약어 -> 소문자 영문)
_UNIT_MAP = {
    "㎜": "mm", "밀리": "mm", "미리": "mm",
    "㎝": "cm", "센치": "cm", "센티": "cm",
    "㎖": "ml", "리터": "l", "ℓ": "l", "㎗": "dl",
    "㎏": "kg", "킬로": "kg", "키로": "kg",
    "㎍": "ug", "㎛": "um", "㎥": "m3", "㎡": "m2",
    "인치": "in", '"': "in",
}

# 구분자. 소수점(.)은 여기에 넣지 않는다 — 0.5mm 가 0 과 5mm 로 쪼개지면 안 된다.
_SEP_RE = re.compile(r"[\s\-_/\\|,·•~+*'\"“”‘’()\[\]{}<>:;!?#@&]+")
# 숫자에 붙지 않은 마침표만 구분자로 취급한다.
_LONE_DOT_RE = re.compile(r"(?<!\d)\.|\.(?!\d)")
# 숫자와 단위를 한 토큰으로 묶는다: "0.5 mm" -> "0.5mm"
_NUM_UNIT_RE = re.compile(r"(\d+(?:\.\d+)?)\s+(mm|cm|m|ml|l|dl|kg|g|mg|in|m2|m3|개|매|권|병|캔|롤|팩|장|셋|조|호|색|구|단|박스|box)\b")
# 한글과 영문/숫자의 경계만 분리한다: "볼펜0.5mm" -> "볼펜 0.5mm", "복사지A4" -> "복사지 A4".
# 영문+숫자(A4, 80g, B5)는 하나의 규격 토큰이므로 절대 쪼개지 않는다.
_SPLIT_NUM_RE = re.compile(r"(?<=[가-힣])(?=[0-9a-z])|(?<=[0-9a-z])(?=[가-힣])")
_TRAILING_ZERO_RE = re.compile(r"\b(\d+)\.0+\b")
_LEADING_ZERO_RE = re.compile(r"\b0+(\d)")
_DIGIT_RE = re.compile(r"\d")

# 부분문자열로 치환해도 안전한 동의어(2글자 이상)만 여기서 처리한다.
# 1글자 동의어는 오탐 위험이 커서 토큰 단위로만 적용한다.
_SUBSTR_SYNONYMS = sorted(
    ((k, v) for k, v in _SYNONYMS.items() if len(k) >= 2),
    key=lambda kv: -len(kv[0]),
)


def normalize(name: str) -> str:
    """
    매칭용 정규화 키를 만든다. 결과는 '정렬된 토큰을 공백으로 이은 문자열'.

    같은 물건이면 같은 키가 나오되, 규격 숫자는 절대 훼손하지 않는다.
    (0.5mm 와 0.7mm 는 반드시 다른 키여야 한다)
    """
    if not name:
        return ""

    s = unicodedata.normalize("NFKC", str(name))   # 전각 -> 반각
    s = s.lower().strip()

    # 단위 표기 통일
    for src, dst in _UNIT_MAP.items():
        s = s.replace(src, dst)

    # 붙여 쓴 동의어 처리 ("흑색볼펜" -> "검정볼펜")
    for src, dst in _SUBSTR_SYNONYMS:
        if src in s:
            s = s.replace(src, dst)

    s = _LONE_DOT_RE.sub(" ", s)   # 숫자와 무관한 마침표만 제거
    s = _SEP_RE.sub(" ", s)
    s = _NUM_UNIT_RE.sub(r"\1\2", s)
    s = _SPLIT_NUM_RE.sub(" ", s)  # 한글/숫자 붙은 것 분리
    s = _NUM_UNIT_RE.sub(r"\1\2", s)
    s = _TRAILING_ZERO_RE.sub(r"\1", s)   # 10.0 -> 10
    s = _LEADING_ZERO_RE.sub(r"\1", s)    # 05 -> 5

    tokens: list[str] = []
    for tok in s.split():
        if not tok or tok in _NOISE_WORDS:
            continue
        tokens.append(_SYNONYMS.get(tok, tok))

    # 토큰 순서가 달라도 같은 키가 나오도록 정렬한다.
    return " ".join(sorted(set(tokens)))


def spec_signature(norm: str) -> frozenset[str]:
    """숫자를 포함한 토큰들의 집합 = 규격 지문. 이게 다르면 다른 물건이다."""
    return frozenset(t for t in norm.split() if _DIGIT_RE.search(t))


def _bigrams(s: str) -> set[str]:
    s = s.replace(" ", "")
    return {s[i:i + 2] for i in range(len(s) - 1)} if len(s) > 1 else {s}


def similarity(a: str, b: str) -> float:
    """
    0.0 ~ 1.0. 정규화 키끼리 비교한다.

    안전장치: 규격 지문(숫자 토큰)이 다르면 점수를 강하게 깎는다.
    "볼펜 0.5mm" 와 "볼펜 0.7mm" 가 자동 매칭되는 사고를 구조적으로 막는다.
    """
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0

    ta, tb = set(a.split()), set(b.split())
    token_jac = len(ta & tb) / len(ta | tb) if (ta | tb) else 0.0

    ga, gb = _bigrams(a), _bigrams(b)
    char_jac = len(ga & gb) / len(ga | gb) if (ga | gb) else 0.0

    seq = SequenceMatcher(None, a.replace(" ", ""), b.replace(" ", "")).ratio()

    score = 0.45 * token_jac + 0.30 * char_jac + 0.25 * seq

    # 규격 지문 비교 — '축약'과 '규격 충돌'을 구분하는 것이 핵심이다.
    sa, sb = spec_signature(a), spec_signature(b)
    if sa == sb:
        pass                                   # 규격 동일. 감점 없음.
    elif not sa or not sb:
        score *= 0.80                          # 한쪽에만 규격이 있음 ("볼펜" vs "볼펜 0.5mm")
    elif sa < sb or sb < sa:
        # 한쪽이 다른 쪽의 부분집합 = 축약 표기일 가능성이 높다.
        # ("A4 복사용지 80g" 는 "A4 복사용지 80g 500매" 의 축약)
        # 후보로는 띄우되, 자동 채택 구간에는 못 들어가게 한다.
        score *= 0.85
    else:
        # 양쪽에 서로 없는 숫자가 있다 = 규격이 충돌한다 = 다른 물건이다.
        # (0.5mm vs 0.7mm, 80g vs 75g) 자동 매칭 사고를 여기서 막는다.
        overlap = len(sa & sb) / len(sa | sb)
        score *= 0.35 + 0.30 * overlap

    return round(min(score, 0.999), 4)


# --------------------------------------------------------------------------
# 매칭 결과
# --------------------------------------------------------------------------
@dataclass
class Candidate:
    item_id: int
    code: str
    name: str
    spec: str
    unit: str
    score: float
    via: str          # 어떤 근거로 나온 후보인지: alias | code | barcode | name

    def as_dict(self) -> dict:
        return {
            "item_id": self.item_id, "code": self.code, "name": self.name,
            "spec": self.spec, "unit": self.unit, "score": self.score, "via": self.via,
        }


@dataclass
class MatchResult:
    status: str                    # exact | auto | suggest | none
    item_id: int | None
    candidates: list[Candidate]
    norm: str

    @property
    def matched(self) -> bool:
        return self.status in ("exact", "auto")


def match(raw_name: str, *, auto_accept: bool = True) -> MatchResult:
    """
    상품명 하나를 표준품목에 매칭한다.

    status:
      exact   - 품목코드/바코드/별칭 완전일치. 무조건 신뢰.
      auto    - 유사도가 임계값 이상이라 자동 채택 (호출측이 별칭 학습).
      suggest - 후보는 있으나 사람 확인 필요.
      none    - 후보 없음. 미매핑 큐로.
    """
    raw = (raw_name or "").strip()
    norm = normalize(raw)
    if not norm:
        return MatchResult("none", None, [], norm)

    with db.get_conn() as conn:
        # ① 품목코드 / 바코드 완전일치 — 스캐너 입력이 여기로 들어온다
        row = conn.execute(
            "SELECT id, code, name, spec, unit FROM items "
            "WHERE active = 1 AND (code = ? COLLATE NOCASE OR barcode = ?) LIMIT 1",
            (raw, raw),
        ).fetchone()
        if row:
            cand = Candidate(row["id"], row["code"], row["name"], row["spec"], row["unit"], 1.0, "code")
            return MatchResult("exact", row["id"], [cand], norm)

        # ② 별칭 완전일치 — 여기에 걸리면 사람 손이 전혀 안 간다
        row = conn.execute(
            "SELECT i.id, i.code, i.name, i.spec, i.unit "
            "FROM aliases a JOIN items i ON i.id = a.item_id "
            "WHERE a.norm_name = ? LIMIT 1",
            (norm,),
        ).fetchone()
        if row:
            cand = Candidate(row["id"], row["code"], row["name"], row["spec"], row["unit"], 1.0, "alias")
            return MatchResult("exact", row["id"], [cand], norm)

        # ③ 유사도 스캔 — 별칭 + 표준품목명 전체를 훑는다
        pool = conn.execute(
            """
            SELECT i.id, i.code, i.name, i.spec, i.unit, a.norm_name AS key, 'alias' AS via
              FROM aliases a JOIN items i ON i.id = a.item_id
             WHERE i.active = 1
            """
        ).fetchall()
        items = conn.execute(
            "SELECT id, code, name, spec, unit FROM items WHERE active = 1"
        ).fetchall()

    scored: dict[int, Candidate] = {}

    def consider(item_id, code, name, spec, unit, key, via):
        score = similarity(norm, key)
        if score < config.SUGGEST_SCORE:
            return
        prev = scored.get(item_id)
        if prev is None or score > prev.score:
            scored[item_id] = Candidate(item_id, code, name, spec, unit, score, via)

    for r in pool:
        consider(r["id"], r["code"], r["name"], r["spec"], r["unit"], r["key"], "alias")
    for r in items:
        key = normalize(f"{r['name']} {r['spec']}")
        consider(r["id"], r["code"], r["name"], r["spec"], r["unit"], key, "name")

    candidates = sorted(scored.values(), key=lambda c: c.score, reverse=True)[: config.MAX_CANDIDATES]

    if not candidates:
        return MatchResult("none", None, [], norm)

    top = candidates[0]
    runner_up = candidates[1].score if len(candidates) > 1 else 0.0
    # 1등이 충분히 높고, 2등과 뚜렷하게 벌어져 있을 때만 자동 채택한다.
    if auto_accept and top.score >= config.AUTO_ACCEPT_SCORE and (top.score - runner_up) >= 0.05:
        return MatchResult("auto", top.item_id, candidates, norm)

    return MatchResult("suggest", None, candidates, norm)


# --------------------------------------------------------------------------
# 별칭 학습
# --------------------------------------------------------------------------
def learn_alias(item_id: int, raw_name: str, source: str = "learned", conn=None) -> bool:
    """
    상품명을 품목의 별칭으로 등록한다. 다음부터 이 이름은 자동 통과한다.
    이미 다른 품목에 물려 있으면 건드리지 않는다 (충돌 시 기존 매핑 우선).
    """
    raw = (raw_name or "").strip()
    norm = normalize(raw)
    if not norm:
        return False

    sql_check = "SELECT item_id FROM aliases WHERE norm_name = ?"
    sql_insert = (
        "INSERT INTO aliases(item_id, raw_name, norm_name, source, hit_count) "
        "VALUES (?, ?, ?, ?, 1)"
    )
    sql_bump = "UPDATE aliases SET hit_count = hit_count + 1 WHERE norm_name = ?"

    def _do(c) -> bool:
        existing = c.execute(sql_check, (norm,)).fetchone()
        if existing:
            c.execute(sql_bump, (norm,))
            return existing["item_id"] == item_id
        c.execute(sql_insert, (item_id, raw, norm, source))
        return True

    if conn is not None:
        return _do(conn)
    with db.tx() as c:
        return _do(c)


def bump_alias(norm_name: str, conn=None) -> None:
    sql = "UPDATE aliases SET hit_count = hit_count + 1 WHERE norm_name = ?"
    if conn is not None:
        conn.execute(sql, (norm_name,))
        return
    with db.tx() as c:
        c.execute(sql, (norm_name,))
