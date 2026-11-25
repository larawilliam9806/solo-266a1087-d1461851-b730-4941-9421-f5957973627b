"""SQLite 连接与建表。"""

import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS work_orders (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    order_no      TEXT NOT NULL UNIQUE,   -- 单号
    assembly_date TEXT NOT NULL,          -- 装配日 ISO YYYY-MM-DD
    product       TEXT NOT NULL,          -- 成品料号
    quantity      REAL NOT NULL           -- 产量
);

CREATE TABLE IF NOT EXISTS boms (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    part_no        TEXT NOT NULL,         -- 料号
    effective_date TEXT NOT NULL,         -- 生效日
    UNIQUE (part_no, effective_date)
);

CREATE TABLE IF NOT EXISTS bom_items (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    bom_id    INTEGER NOT NULL REFERENCES boms (id) ON DELETE CASCADE,
    component TEXT NOT NULL,              -- 子件料号
    qty_per   REAL NOT NULL               -- 单套用量
);

-- 核算结果缓存：每张工单各基础件的总用量
CREATE TABLE IF NOT EXISTS work_order_usage (
    work_order_id INTEGER NOT NULL REFERENCES work_orders (id) ON DELETE CASCADE,
    part_no       TEXT NOT NULL,          -- 基础件料号
    total_qty     REAL NOT NULL,
    PRIMARY KEY (work_order_id, part_no)
);

-- 每张工单展开时涉及的全部料号（含成品本身、中间件、基础件），
-- 用于补登/更正用料单时精确定位需要重核的工单。
CREATE TABLE IF NOT EXISTS work_order_parts (
    work_order_id INTEGER NOT NULL REFERENCES work_orders (id) ON DELETE CASCADE,
    part_no       TEXT NOT NULL,
    PRIMARY KEY (work_order_id, part_no)
);
"""


def connect(db_path: str) -> sqlite3.Connection:
    # isolation_level=None：使用 autocommit，事务边界由 service 显式
    # BEGIN [IMMEDIATE] / COMMIT / ROLLBACK 控制，保证展开快照与
    # 入账前的并发校验在各 Python 版本下行为一致。
    conn = sqlite3.connect(
        db_path, timeout=10, check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


def init_db(db_path: str) -> None:
    with connect(db_path) as conn:
        conn.executescript(SCHEMA)
        conn.execute("PRAGMA journal_mode = WAL")
