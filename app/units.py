"""
단위 정규화와 포장단위 환산.

현장에서 같은 뜻을 여러 표기로 씁니다 (EA/PC/개, ROLL/ROL/롤, BOT/BT/병).
그대로 두면 같은 물건이 다른 단위로 갈려 재고가 어긋나므로 대표 표기로 모읍니다.

또한 "가성소다 3포"처럼 포장 단위로 세는 품목이 있습니다.
품목에 1포 = 25KG 을 등록해 두면 입력은 포로 하고 저장은 KG 으로 합니다.
(실제 자료에서 이 환산 누락으로 25배 오차가 있었습니다)
"""
from __future__ import annotations

import re
import unicodedata

# --------------------------------------------------------------------------
# 단위 동의어 -> 대표 표기
#   대표 표기는 기존 자료에서 가장 많이 쓰인 것을 골랐다.
# --------------------------------------------------------------------------
UNIT_GROUPS: dict[str, list[str]] = {
    "PC":   ["pc", "pcs", "piece", "pieces", "ea", "each", "개", "낱개", "ea.", "pc."],
    "SET":  ["set", "세트", "셋", "조"],
    "BOX":  ["box", "박스", "상자", "bx"],
    "BOT":  ["bot", "bt", "bottle", "병", "btl"],
    "BAG":  ["bag", "포", "포대", "부대", "bg"],
    "PAIL": ["pail", "말", "can", "캔", "통", "관"],
    "ROL":  ["rol", "roll", "롤", "rl"],
    "KG":   ["kg", "킬로", "키로", "킬로그램", "㎏"],
    "G":    ["g", "그램", "gram"],
    "L":    ["l", "리터", "ℓ", "litre", "liter"],
    "ML":   ["ml", "㎖", "밀리리터", "cc"],
    "M":    ["m", "미터", "메타", "meter"],
    "PR":   ["pr", "pair", "켤레", "쌍", "조(짝)"],
    "PAC":  ["pac", "pack", "팩", "pk"],
    "CTN":  ["ctn", "carton", "카톤"],
    "PLT":  ["plt", "pallet", "파렛트", "팔레트"],
    "장":   ["장", "매", "sheet", "sht"],
    "권":   ["권", "book"],
}

_UNIT_LOOKUP: dict[str, str] = {}
for _canon, _aliases in UNIT_GROUPS.items():
    _UNIT_LOOKUP[_canon.lower()] = _canon
    for _a in _aliases:
        _UNIT_LOOKUP[_a.lower()] = _canon

# 같은 물리량 계열 (환산 가능 여부 판단용)
WEIGHT = {"KG", "G"}
VOLUME = {"L", "ML"}
LENGTH = {"M"}
COUNT = {"PC", "SET", "BOX", "BOT", "BAG", "PAIL", "ROL", "PR", "PAC", "CTN", "PLT", "장", "권"}

# 같은 계열 안에서의 배수
SCALE = {("KG", "G"): 1000.0, ("G", "KG"): 0.001,
         ("L", "ML"): 1000.0, ("ML", "L"): 0.001}

# PC 는 "낱개"를 뜻하는 범용 표기라, 물건 하나가 곧 한 병/한 롤/한 장인 단위와는
# 1:1 로 봐도 안전하다. ("묽은염산 2EA" = "2BOT")
#
# 반대로 BAG·BOX·PAIL·CTN·PLT 처럼 여러 개를 담는 묶음 단위와,
# 여러 짝이 모인 SET·PR 은 1:1 로 보면 안 된다. 몇 개가 들었는지는
# 품목마다 다르므로 반드시 포장 규격(pack_size)을 등록해야 한다.
PC_EQUIVALENT = {"BOT", "ROL", "장", "권"}

# 여러 개를 담는 용기 표기. 한 품목의 포장은 보통 한 가지뿐이라,
# 포장 규격(pack_size)이 등록돼 있으면 이 중 어떤 표기로 적어도 그 포장으로 본다.
# (소포제 20kg 통을 누구는 '말', 누구는 '포', 누구는 'PAIL' 로 적는다)
CONTAINER_UNITS = {"BAG", "BOX", "PAIL", "CTN", "PLT", "PAC"}


def normalize_unit(unit: str | None) -> str:
    """단위 표기를 대표 표기로 모은다. 모르는 단위는 대문자로만 정리해 그대로 둔다."""
    if not unit:
        return ""
    s = unicodedata.normalize("NFKC", str(unit)).strip()
    s = re.sub(r"[\s./()]+", "", s)
    if not s:
        return ""
    return _UNIT_LOOKUP.get(s.lower(), s.upper())


def same_unit(a: str | None, b: str | None) -> bool:
    return normalize_unit(a) == normalize_unit(b)


def unit_family(unit: str) -> str:
    u = normalize_unit(unit)
    if u in WEIGHT: return "weight"
    if u in VOLUME: return "volume"
    if u in LENGTH: return "length"
    if u in COUNT:  return "count"
    return "etc"


# --------------------------------------------------------------------------
# 제품명에서 포장 규격 추출
#   "가성소다 고상(98%)(25kg)"  -> (25, "KG")
#   "에어호스(Air호스)6mm (100m)" -> (100, "M")
#   "PH표준용액(1000ml,PH4)"     -> (1000, "ML")
# --------------------------------------------------------------------------
_PACK_RE = re.compile(
    r"(?<![\d.])(\d+(?:\.\d+)?)\s*"
    r"(kg|㎏|g|ml|㎖|cc|l|ℓ|m|매|장|pcs|pc|ea|개|roll|rol|롤)"
    r"(?![a-z가-힣0-9])",
    re.IGNORECASE,
)

# 제품명 안의 숫자가 포장량이 아니라 '규격'인 경우를 걸러낸다.
# (배관 25A, 압력계 0.6MPa, 볼트 M8 등)
_SPEC_HINT = re.compile(r"\d+\s*(a|mpa|inch|인치|파이|ø|φ|mm|사이즈|호|w|v|hz|micron|㎛)\b", re.IGNORECASE)


def extract_pack(name: str, base_unit: str = "") -> tuple[float, str] | None:
    """
    제품명에서 1포장에 담긴 양을 뽑는다.
    무게/부피/길이 단위일 때만 인정한다 (개수 단위는 환산 의미가 약하다).
    """
    if not name:
        return None
    text = unicodedata.normalize("NFKC", str(name))

    best: tuple[float, str] | None = None
    for m in _PACK_RE.finditer(text):
        qty = float(m.group(1))
        unit = normalize_unit(m.group(2))
        if qty <= 0:
            continue
        # 규격 표기 근처면 건너뛴다
        around = text[max(0, m.start() - 4): m.end() + 4]
        if _SPEC_HINT.search(around):
            continue
        if unit_family(unit) in ("weight", "volume", "length"):
            # 뒤에 나온 것(보통 괄호 안 포장량)을 우선한다
            best = (qty, unit)
    return best


def to_base(qty: float, entered_unit: str, base_unit: str,
            pack_size: float = 0, pack_unit: str = "") -> tuple[float, str]:
    """
    입력 수량을 품목의 기본 단위로 환산한다.

    반환: (환산된 수량, 설명)
      · 단위가 같으면 그대로
      · 같은 계열이면 배수 적용 (KG <-> G)
      · 입력이 포장단위이고 품목에 포장 규격이 있으면 곱한다 (3포 -> 75KG)
    """
    eu = normalize_unit(entered_unit)
    bu = normalize_unit(base_unit)
    pu = normalize_unit(pack_unit)

    if not eu or eu == bu:
        return qty, ""

    if (eu, bu) in SCALE:
        v = qty * SCALE[(eu, bu)]
        return v, f"{qty:g}{eu} = {v:g}{bu}"

    if pack_size and pu and eu == pu:
        v = qty * pack_size
        return v, f"{qty:g}{pu} × {pack_size:g} = {v:g}{bu}"

    # 포장 규격은 있는데 용기 표기만 다른 경우 (말/포/통/PAIL …)
    if pack_size and eu in CONTAINER_UNITS and unit_family(bu) in ("weight", "volume", "length"):
        v = qty * pack_size
        note = f"{qty:g}{eu} × {pack_size:g} = {v:g}{bu}"
        if pu and eu != pu:
            note += f" ({pu} 포장으로 간주)"
        return v, note

    # 낱개를 뜻하는 표기끼리는 1:1 (PC ↔ BOT/ROL/장/권)
    if ("PC" in (eu, bu)) and ({eu, bu} - {"PC"}) <= PC_EQUIVALENT:
        return qty, f"{qty:g}{eu} = {qty:g}{bu} (낱개 단위 동일 취급)"

    # 환산 근거가 없으면 수량을 건드리지 않는다. 임의 환산이 더 위험하다.
    return qty, ""


def parse_qty(text) -> tuple[float, str]:
    """
    '34포', '1EA', '20 kg' 처럼 수량과 단위가 붙은 값을 나눈다.
    실제 자료(B.xlsx)가 이 형태였다.
    """
    if text is None:
        return 0.0, ""
    if isinstance(text, (int, float)):
        return float(text), ""
    s = unicodedata.normalize("NFKC", str(text)).strip()
    if not s:
        return 0.0, ""
    m = re.match(r"^\s*([\d,]+(?:\.\d+)?)\s*([A-Za-z가-힣]*)", s)
    if not m:
        return 0.0, ""
    qty = float(m.group(1).replace(",", ""))
    return qty, normalize_unit(m.group(2))
