-- =====================================================================
--  재고관리 시스템 스키마
--  설계 원칙: 현재고는 절대 직접 저장하지 않는다.
--             입출고 '사건'만 기록하고 현재고는 항상 합산으로 도출한다.
-- =====================================================================

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------------
-- 1층: 표준품목 — 재고를 세는 유일한 단위
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS items (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    code            TEXT    NOT NULL UNIQUE,      -- 사내 품목코드 (기존 체계 그대로)
    name            TEXT    NOT NULL,             -- 표준품목명
    spec            TEXT    NOT NULL DEFAULT '',  -- 규격 / 색상
    unit            TEXT    NOT NULL DEFAULT 'EA',
    category        TEXT    NOT NULL DEFAULT '',
    barcode         TEXT,
    location        TEXT    NOT NULL DEFAULT '',  -- 보관위치
    reorder_point   REAL    NOT NULL DEFAULT 0,   -- 재고기준: 이 수량 이하면 경고
    reorder_qty     REAL    NOT NULL DEFAULT 0,   -- 권장 발주량
    unit_cost       REAL    NOT NULL DEFAULT 0,
    active          INTEGER NOT NULL DEFAULT 1,
    memo            TEXT    NOT NULL DEFAULT '',
    created_at      TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at      TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS ix_items_name     ON items(name);
CREATE INDEX IF NOT EXISTS ix_items_category ON items(category);
CREATE INDEX IF NOT EXISTS ix_items_active   ON items(active);
CREATE INDEX IF NOT EXISTS ix_items_barcode  ON items(barcode);

-- ---------------------------------------------------------------------
-- 2층: 별칭 — "주문서엔 이렇게 적혀 온다" 목록. 1품목 : N별칭
--       norm_name 이 매칭 키. 한 번 학습하면 다음부터 자동 통과.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS aliases (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id     INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    raw_name    TEXT    NOT NULL,               -- 원본 표기 (사람이 읽는 용도)
    norm_name   TEXT    NOT NULL UNIQUE,        -- 정규화 키 (매칭 용도)
    source      TEXT    NOT NULL DEFAULT 'manual',  -- manual | learned | import | self
    hit_count   INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS ix_aliases_item ON aliases(item_id);

-- ---------------------------------------------------------------------
-- 입출고 원장 — 이 테이블이 유일한 진실(single source of truth)
--   txn_type : IN(입고) | OUT(출고) | ADJ(실사조정)
--   qty      : 항상 양수. 부호는 signed_qty 가 담당.
--   raw_name : 주문서 원문 상품명. 절대 수정하지 않는다 (3층: 원문 보존)
--   voided   : 취소 플래그. 행을 지우지 않고 무효화만 한다 (감사 추적)
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS transactions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id     INTEGER NOT NULL REFERENCES items(id) ON DELETE RESTRICT,
    txn_date    TEXT    NOT NULL,                    -- YYYY-MM-DD
    txn_type    TEXT    NOT NULL CHECK (txn_type IN ('IN','OUT','ADJ')),
    qty         REAL    NOT NULL CHECK (qty >= 0),
    signed_qty  REAL    NOT NULL,
    unit_cost   REAL    NOT NULL DEFAULT 0,
    raw_name    TEXT    NOT NULL DEFAULT '',         -- 원문 보존
    partner     TEXT    NOT NULL DEFAULT '',         -- 거래처 (선택 입력)
    doc_no      TEXT    NOT NULL DEFAULT '',         -- 전표/명세서 번호
    memo        TEXT    NOT NULL DEFAULT '',
    batch_id    TEXT    NOT NULL DEFAULT '',         -- 일괄 업로드 묶음 식별자
    source      TEXT    NOT NULL DEFAULT 'manual',   -- manual | import | stocktake | migration
    voided      INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS ix_txn_item   ON transactions(item_id);
CREATE INDEX IF NOT EXISTS ix_txn_date   ON transactions(txn_date);
CREATE INDEX IF NOT EXISTS ix_txn_batch  ON transactions(batch_id);
CREATE INDEX IF NOT EXISTS ix_txn_live   ON transactions(voided, item_id);

-- ---------------------------------------------------------------------
-- 미매핑 큐 — 자동으로 못 맞춘 상품명이 쌓이는 곳.
--   하루 끝에 몰아서 처리하면 되도록 설계.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS pending_names (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    raw_name     TEXT    NOT NULL,
    norm_name    TEXT    NOT NULL,
    qty          REAL    NOT NULL DEFAULT 0,
    unit_cost    REAL    NOT NULL DEFAULT 0,
    txn_date     TEXT    NOT NULL,
    txn_type     TEXT    NOT NULL DEFAULT 'IN',
    partner      TEXT    NOT NULL DEFAULT '',
    doc_no       TEXT    NOT NULL DEFAULT '',
    batch_id     TEXT    NOT NULL DEFAULT '',
    candidates   TEXT    NOT NULL DEFAULT '[]',      -- JSON: 유사도 후보 목록
    status       TEXT    NOT NULL DEFAULT 'open',    -- open | resolved | discarded
    resolved_item_id INTEGER REFERENCES items(id) ON DELETE SET NULL,
    created_at   TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS ix_pending_status ON pending_names(status);

-- ---------------------------------------------------------------------
-- 실사(재고조사) — 실물 카운트만 넣으면 차이를 자동 계산하고
--                  확정 시 ADJ 전표를 자동 생성한다.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS stocktakes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    take_date   TEXT    NOT NULL,
    title       TEXT    NOT NULL DEFAULT '',
    status      TEXT    NOT NULL DEFAULT 'draft',    -- draft | committed
    memo        TEXT    NOT NULL DEFAULT '',
    created_at  TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    committed_at TEXT
);

CREATE TABLE IF NOT EXISTS stocktake_lines (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    stocktake_id INTEGER NOT NULL REFERENCES stocktakes(id) ON DELETE CASCADE,
    item_id      INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    system_qty   REAL    NOT NULL DEFAULT 0,   -- 실사 시점 전산 재고 (스냅샷)
    counted_qty  REAL,                          -- 실물 카운트. NULL = 미카운트
    memo         TEXT    NOT NULL DEFAULT '',
    UNIQUE (stocktake_id, item_id)
);

-- ---------------------------------------------------------------------
-- 설정 (SMTP, 리포트 시각, 회사명 등)
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT ''
);

-- 알림 발송 이력 (중복 발송 방지)
CREATE TABLE IF NOT EXISTS notify_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT NOT NULL,          -- daily | weekly
    period_key TEXT NOT NULL,          -- 2026-08-18 / 2026-W34
    sent_at    TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    ok         INTEGER NOT NULL DEFAULT 1,
    detail     TEXT NOT NULL DEFAULT '',
    UNIQUE (kind, period_key)
);

-- ---------------------------------------------------------------------
-- 현재고 뷰 — 원장 합산. 어떤 화면도 이 뷰만 본다.
-- ---------------------------------------------------------------------
CREATE VIEW IF NOT EXISTS v_stock AS
SELECT
    i.id                AS item_id,
    i.code              AS code,
    i.name              AS name,
    i.spec              AS spec,
    i.unit              AS unit,
    i.category          AS category,
    i.location          AS location,
    i.reorder_point     AS reorder_point,
    i.reorder_qty       AS reorder_qty,
    i.unit_cost         AS unit_cost,
    i.active            AS active,
    COALESCE(t.on_hand, 0)                       AS on_hand,
    COALESCE(t.in_qty, 0)                        AS total_in,
    COALESCE(t.out_qty, 0)                       AS total_out,
    t.last_in_date                               AS last_in_date,
    t.last_out_date                              AS last_out_date,
    t.last_move_date                             AS last_move_date,
    COALESCE(t.on_hand, 0) * i.unit_cost         AS stock_value,
    CASE
        WHEN COALESCE(t.on_hand, 0) <= 0                  THEN 'OUT'      -- 품절
        WHEN COALESCE(t.on_hand, 0) <= i.reorder_point    THEN 'LOW'      -- 재고기준 도달
        ELSE 'OK'
    END                                          AS stock_status
FROM items i
LEFT JOIN (
    SELECT
        item_id,
        SUM(signed_qty)                                            AS on_hand,
        SUM(CASE WHEN txn_type = 'IN'  THEN qty ELSE 0 END)        AS in_qty,
        SUM(CASE WHEN txn_type = 'OUT' THEN qty ELSE 0 END)        AS out_qty,
        MAX(CASE WHEN txn_type = 'IN'  THEN txn_date END)          AS last_in_date,
        MAX(CASE WHEN txn_type = 'OUT' THEN txn_date END)          AS last_out_date,
        MAX(txn_date)                                              AS last_move_date
    FROM transactions
    WHERE voided = 0
    GROUP BY item_id
) t ON t.item_id = i.id;
