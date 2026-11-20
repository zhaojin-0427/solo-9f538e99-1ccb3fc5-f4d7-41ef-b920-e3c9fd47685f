"""SQLite schema and connection handling.

Concurrency model
-----------------
* Every mutating operation opens a ``BEGIN IMMEDIATE`` transaction, so only
  one writer can be inside the write path at a time. Readers use short
  transactions and see a consistent snapshot.
* Month facts, knowledge versions, version ranges and audit rows are
  append-only. There are triggers preventing UPDATE/DELETE on them.
* A knowledge version is inserted in the same transaction as its ranges;
  the transaction either commits the whole group or rolls it back.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS months (
    month                TEXT PRIMARY KEY,
    source_filename      TEXT NOT NULL,
    source_sha256        TEXT NOT NULL,
    row_count            INTEGER NOT NULL,
    total_boxes          INTEGER NOT NULL,
    frozen_at            TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS shipments (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    month                TEXT NOT NULL REFERENCES months(month),
    sequence_no          TEXT NOT NULL,
    sku                  TEXT NOT NULL,
    occurred_at          TEXT NOT NULL,
    occurred_at_us       INTEGER NOT NULL,
    boxes                INTEGER NOT NULL CHECK (boxes > 0),
    source_row           INTEGER NOT NULL,
    UNIQUE (sequence_no)
);
CREATE INDEX IF NOT EXISTS idx_shipments_month ON shipments(month);
CREATE INDEX IF NOT EXISTS idx_shipments_month_sku ON shipments(month, sku);

CREATE TABLE IF NOT EXISTS knowledge_versions (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    version_no           INTEGER NOT NULL UNIQUE,
    based_on_version     INTEGER REFERENCES knowledge_versions(version_no),
    reason               TEXT NOT NULL,
    created_at           TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS version_ranges (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    version_no           INTEGER NOT NULL REFERENCES knowledge_versions(version_no),
    sku                  TEXT NOT NULL,
    zone                 TEXT NOT NULL,
    valid_from           TEXT NOT NULL,
    valid_from_us        INTEGER NOT NULL,
    valid_to             TEXT,
    valid_to_us          INTEGER,
    UNIQUE (version_no, sku, valid_from_us),
    CHECK (valid_to_us IS NULL OR valid_to_us > valid_from_us)
);
CREATE INDEX IF NOT EXISTS idx_ranges_version_sku
    ON version_ranges(version_no, sku, valid_from_us, valid_to_us);

CREATE TABLE IF NOT EXISTS audit_log (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    at                   TEXT NOT NULL,
    action               TEXT NOT NULL,
    month                TEXT,
    version_no           INTEGER,
    summary              TEXT NOT NULL
);

-- Recall checks: reconstruct where mixed-loaded lots of one SKU went when the
-- lot-level detail of the outbound month was lost. A check is only persisted
-- when the box assignment problem has at least one feasible solution.
CREATE TABLE IF NOT EXISTS recall_checks (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    check_no             TEXT NOT NULL UNIQUE,
    month                TEXT NOT NULL REFERENCES months(month),
    sku                  TEXT NOT NULL,
    created_at           TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_recall_checks_month_sku ON recall_checks(month, sku);

CREATE TABLE IF NOT EXISTS recall_lots (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    check_id             INTEGER NOT NULL REFERENCES recall_checks(id),
    lot_no               TEXT NOT NULL,
    boxes                INTEGER NOT NULL CHECK (boxes > 0),
    row_order            INTEGER NOT NULL,
    UNIQUE (check_id, lot_no)
);

-- Every candidate (lot, shipment) pair named in the upload. A shipment appears
-- here at most once per check and must belong to the check's frozen month and
-- SKU; its frozen box count is snapshotted so later reads need no joins.
CREATE TABLE IF NOT EXISTS recall_candidates (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    check_id             INTEGER NOT NULL REFERENCES recall_checks(id),
    lot_id               INTEGER NOT NULL REFERENCES recall_lots(id),
    shipment_id          INTEGER NOT NULL REFERENCES shipments(id),
    sequence_no          TEXT NOT NULL,
    frozen_boxes         INTEGER NOT NULL,
    UNIQUE (check_id, lot_id, shipment_id),
    UNIQUE (check_id, shipment_id, lot_id)
);
CREATE INDEX IF NOT EXISTS idx_recall_candidates_check ON recall_candidates(check_id);

-- Verified box counts reported back by the receiving party. Zero is allowed.
-- The same pair may only be re-submitted with the identical value; a different
-- value is rejected, so every row stays immutable once written.
CREATE TABLE IF NOT EXISTS recall_verifications (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    check_id             INTEGER NOT NULL REFERENCES recall_checks(id),
    lot_id               INTEGER NOT NULL REFERENCES recall_lots(id),
    shipment_id          INTEGER NOT NULL REFERENCES shipments(id),
    verified_boxes       INTEGER NOT NULL CHECK (verified_boxes >= 0),
    created_at           TEXT NOT NULL,
    UNIQUE (check_id, lot_id, shipment_id)
);
CREATE INDEX IF NOT EXISTS idx_recall_verifications_check ON recall_verifications(check_id);

-- Conclusion snapshot of every involved (lot, shipment) pair as of the latest
-- accepted verification. Recomputed inside the same write transaction, so a
-- check always opens with the conclusion that was current at last save.
CREATE TABLE IF NOT EXISTS recall_conclusions (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    check_id             INTEGER NOT NULL REFERENCES recall_checks(id),
    lot_id               INTEGER NOT NULL REFERENCES recall_lots(id),
    shipment_id          INTEGER NOT NULL REFERENCES shipments(id),
    conclusion           TEXT NOT NULL CHECK (conclusion IN ('always','partial','never')),
    UNIQUE (check_id, lot_id, shipment_id)
);
"""

# Append-only protections. Facts and the knowledge ledger are never rewritten;
# corrections are new versions, not edits.
IMMUTABLE_TRIGGERS = """
CREATE TRIGGER IF NOT EXISTS months_no_update
BEFORE UPDATE ON months BEGIN
    SELECT RAISE(ABORT, 'month facts are immutable');
END;
CREATE TRIGGER IF NOT EXISTS months_no_delete
BEFORE DELETE ON months BEGIN
    SELECT RAISE(ABORT, 'month facts are immutable');
END;
CREATE TRIGGER IF NOT EXISTS shipments_no_update
BEFORE UPDATE ON shipments BEGIN
    SELECT RAISE(ABORT, 'shipment facts are immutable');
END;
CREATE TRIGGER IF NOT EXISTS shipments_no_delete
BEFORE DELETE ON shipments BEGIN
    SELECT RAISE(ABORT, 'shipment facts are immutable');
END;
CREATE TRIGGER IF NOT EXISTS versions_no_update
BEFORE UPDATE ON knowledge_versions BEGIN
    SELECT RAISE(ABORT, 'knowledge versions are immutable');
END;
CREATE TRIGGER IF NOT EXISTS versions_no_delete
BEFORE DELETE ON knowledge_versions BEGIN
    SELECT RAISE(ABORT, 'knowledge versions are immutable');
END;
CREATE TRIGGER IF NOT EXISTS ranges_no_update
BEFORE UPDATE ON version_ranges BEGIN
    SELECT RAISE(ABORT, 'knowledge versions are immutable');
END;
CREATE TRIGGER IF NOT EXISTS ranges_no_delete
BEFORE DELETE ON version_ranges BEGIN
    SELECT RAISE(ABORT, 'knowledge versions are immutable');
END;
CREATE TRIGGER IF NOT EXISTS ranges_no_overlap
BEFORE INSERT ON version_ranges
WHEN EXISTS (
    SELECT 1 FROM version_ranges r
    WHERE r.version_no = NEW.version_no
      AND r.sku = NEW.sku
      AND (NEW.valid_to_us IS NULL OR r.valid_from_us < NEW.valid_to_us)
      AND (r.valid_to_us IS NULL OR r.valid_to_us > NEW.valid_from_us)
)
BEGIN
    SELECT RAISE(ABORT, 'overlapping ranges for the same sku in one version');
END;
CREATE TRIGGER IF NOT EXISTS audit_no_update
BEFORE UPDATE ON audit_log BEGIN
    SELECT RAISE(ABORT, 'audit log is append-only');
END;
CREATE TRIGGER IF NOT EXISTS audit_no_delete
BEFORE DELETE ON audit_log BEGIN
    SELECT RAISE(ABORT, 'audit log is append-only');
END;
CREATE TRIGGER IF NOT EXISTS recall_checks_no_update
BEFORE UPDATE ON recall_checks BEGIN
    SELECT RAISE(ABORT, 'recall checks are immutable');
END;
CREATE TRIGGER IF NOT EXISTS recall_checks_no_delete
BEFORE DELETE ON recall_checks BEGIN
    SELECT RAISE(ABORT, 'recall checks are immutable');
END;
CREATE TRIGGER IF NOT EXISTS recall_lots_no_update
BEFORE UPDATE ON recall_lots BEGIN
    SELECT RAISE(ABORT, 'recall lots are immutable');
END;
CREATE TRIGGER IF NOT EXISTS recall_lots_no_delete
BEFORE DELETE ON recall_lots BEGIN
    SELECT RAISE(ABORT, 'recall lots are immutable');
END;
CREATE TRIGGER IF NOT EXISTS recall_candidates_no_update
BEFORE UPDATE ON recall_candidates BEGIN
    SELECT RAISE(ABORT, 'recall candidates are immutable');
END;
CREATE TRIGGER IF NOT EXISTS recall_candidates_no_delete
BEFORE DELETE ON recall_candidates BEGIN
    SELECT RAISE(ABORT, 'recall candidates are immutable');
END;
CREATE TRIGGER IF NOT EXISTS recall_verifications_no_update
BEFORE UPDATE ON recall_verifications BEGIN
    SELECT RAISE(ABORT, 'verified records are immutable');
END;
CREATE TRIGGER IF NOT EXISTS recall_verifications_no_delete
BEFORE DELETE ON recall_verifications BEGIN
    SELECT RAISE(ABORT, 'verified records are immutable');
END;
"""


def db_path_from_env(default=None):
    configured = os.environ.get("COLDCHAIN_DB")
    if configured:
        return Path(configured)
    if default:
        return Path(default)
    return Path(__file__).resolve().parent.parent / "data" / "coldchain.sqlite3"


def connect(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = FULL")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def init_db(path):
    conn = connect(path)
    try:
        conn.executescript(SCHEMA)
        conn.executescript(IMMUTABLE_TRIGGERS)
    finally:
        conn.close()


class transaction:
    """``BEGIN IMMEDIATE`` context manager that never leaves a half write.

    Any exception rolls the whole group back. ``commit`` happens exactly once
    when the block finishes without error.
    """

    def __init__(self, conn, immediate=True):
        self.conn = conn
        self.immediate = immediate
        self.depth = 0

    def __enter__(self):
        if self.depth == 0:
            self.conn.execute("BEGIN IMMEDIATE" if self.immediate else "BEGIN")
        self.depth += 1
        return self.conn

    def __exit__(self, exc_type, exc, tb):
        self.depth -= 1
        if self.depth > 0:
            return False
        if exc_type is not None:
            if self.conn.in_transaction:
                self.conn.execute("ROLLBACK")
            return False
        try:
            self.conn.execute("COMMIT")
        except Exception:
            if self.conn.in_transaction:
                self.conn.execute("ROLLBACK")
            raise
        return False
