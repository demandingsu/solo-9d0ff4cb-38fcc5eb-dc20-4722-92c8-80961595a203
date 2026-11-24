"""SQLite connection helpers and schema definition.

All state lives in SQLite.  Every request gets its own connection.  Write
operations use ``BEGIN IMMEDIATE`` so number allocation, delivery registration
and splitting are serialised even when the API is started with multiple
workers or receives concurrent requests.

Key modelling decisions
-----------------------
* ``project_rows`` gives each ``(project, source table, source row)`` one
  anonymous project row number that never changes.
* ``project_student_numbers`` gives each merged student exactly one anonymous
  project student number, shared by every table of the project.  A split group
  receives its own row in the same table with ``kind = 'split'``.
* ``delivery_items`` records every successfully registered delivery line: one
  row per delivered ``(delivery, source row)`` with the student number the
  project saw at that moment.  This is the "successful delivery record" the
  correction sheet is derived from; it is never rewritten.
* ``delivery_rollups`` keeps one counter per ``(project, source row, student)``.
  Delivery updates it in the same transaction as the delivery item, so a later
  split can learn how many old-number deliveries a row has by reading one
  counter instead of scanning every delivery record.
* ``correction_ranges`` stores *one row per moved source row*, plus the block
  of correction numbers reserved for it.  A correction sheet fans that block
  out over the recorded deliveries at read time.  Consequently a split reads
  and writes a number of business records proportional to the moved rows, not
  to how often those rows happened to be delivered.
* ``legacy_correction_items`` keeps correction lines produced by the original
  per-delivery writer.  It is read as-is for data volumes created before the
  range format, so existing correction numbers and sheet digests are returned
  byte-for-byte.  New splits do not append to it.
* ``correction_items`` is a compatibility view: legacy rows verbatim plus the
  range blocks expanded over the recorded deliveries.  Readers and internal
  reconciliation queries keep using this single name.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS projects (
    project_id   TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS project_counters (
    project_id      TEXT PRIMARY KEY
                        REFERENCES projects(project_id) ON DELETE CASCADE,
    next_row_no     INTEGER NOT NULL DEFAULT 1,
    next_student_no INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS source_rows (
    table_name        TEXT NOT NULL,
    row_no            INTEGER NOT NULL,
    merged_student_no TEXT NOT NULL,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    PRIMARY KEY (table_name, row_no)
);

CREATE TABLE IF NOT EXISTS project_student_numbers (
    project_id        TEXT NOT NULL REFERENCES projects(project_id)
                          ON DELETE CASCADE,
    student_no        INTEGER NOT NULL,
    kind              TEXT NOT NULL CHECK (kind IN ('merged', 'split')),
    merged_student_no TEXT,
    origin_merged_student_no TEXT NOT NULL,
    split_id          INTEGER,
    created_at        TEXT NOT NULL,
    PRIMARY KEY (project_id, student_no),
    CHECK (
        (kind = 'merged' AND merged_student_no IS NOT NULL AND split_id IS NULL)
        OR (kind = 'split' AND merged_student_no IS NULL)
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_project_student_numbers_merged
    ON project_student_numbers(project_id, merged_student_no)
    WHERE merged_student_no IS NOT NULL;

CREATE TABLE IF NOT EXISTS project_rows (
    project_id          TEXT NOT NULL REFERENCES projects(project_id)
                            ON DELETE CASCADE,
    table_name          TEXT NOT NULL,
    row_no              INTEGER NOT NULL,
    project_row_no      INTEGER NOT NULL,
    current_student_no  INTEGER NOT NULL,
    original_student_no INTEGER NOT NULL,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    PRIMARY KEY (project_id, table_name, row_no),
    UNIQUE (project_id, project_row_no),
    FOREIGN KEY (table_name, row_no)
        REFERENCES source_rows(table_name, row_no),
    FOREIGN KEY (project_id, current_student_no)
        REFERENCES project_student_numbers(project_id, student_no),
    FOREIGN KEY (project_id, original_student_no)
        REFERENCES project_student_numbers(project_id, student_no)
);

CREATE TABLE IF NOT EXISTS deliveries (
    delivery_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id            TEXT NOT NULL REFERENCES projects(project_id)
                              ON DELETE CASCADE,
    request_id            TEXT,
    request_fingerprint   TEXT NOT NULL,
    requested_count       INTEGER NOT NULL,
    new_project_row_count INTEGER NOT NULL DEFAULT 0,
    created_at            TEXT NOT NULL,
    UNIQUE (project_id, request_id)
);

CREATE TABLE IF NOT EXISTS delivery_items (
    delivery_item_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    delivery_id       INTEGER NOT NULL REFERENCES deliveries(delivery_id)
                          ON DELETE CASCADE,
    project_id        TEXT NOT NULL,
    ordinal           INTEGER NOT NULL,
    table_name        TEXT NOT NULL,
    row_no            INTEGER NOT NULL,
    project_row_no    INTEGER NOT NULL,
    student_no        INTEGER NOT NULL,
    source_merged_student_no TEXT NOT NULL,
    first_registration INTEGER NOT NULL CHECK (first_registration IN (0, 1)),
    created_at        TEXT NOT NULL,
    UNIQUE (delivery_id, ordinal),
    FOREIGN KEY (project_id, table_name, row_no)
        REFERENCES project_rows(project_id, table_name, row_no),
    FOREIGN KEY (project_id, student_no)
        REFERENCES project_student_numbers(project_id, student_no)
);

CREATE TABLE IF NOT EXISTS splits (
    split_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id         TEXT NOT NULL REFERENCES projects(project_id)
                           ON DELETE CASCADE,
    request_id         TEXT,
    request_fingerprint TEXT NOT NULL,
    old_student_no     INTEGER NOT NULL,
    new_student_no     INTEGER NOT NULL,
    row_count          INTEGER NOT NULL,
    correction_count   INTEGER NOT NULL DEFAULT 0,
    -- 'per_line': legacy splits whose correction lines live in correction_items.
    -- 'range':    splits that reserved one correction-id block per moved row.
    correction_format  TEXT NOT NULL DEFAULT 'per_line'
                           CHECK (correction_format IN ('per_line', 'range')),
    created_at         TEXT NOT NULL,
    UNIQUE (project_id, request_id),
    UNIQUE (project_id, new_student_no),
    FOREIGN KEY (project_id, old_student_no)
        REFERENCES project_student_numbers(project_id, student_no)
);

CREATE TABLE IF NOT EXISTS split_rows (
    split_id       INTEGER NOT NULL REFERENCES splits(split_id)
                       ON DELETE CASCADE,
    project_id     TEXT NOT NULL,
    table_name     TEXT NOT NULL,
    row_no         INTEGER NOT NULL,
    project_row_no INTEGER NOT NULL,
    old_student_no INTEGER NOT NULL,
    new_student_no INTEGER NOT NULL,
    was_in_project INTEGER NOT NULL CHECK (was_in_project IN (0, 1)),
    PRIMARY KEY (split_id, table_name, row_no),
    FOREIGN KEY (project_id, table_name, row_no)
        REFERENCES project_rows(project_id, table_name, row_no)
);

-- Corrections written by the original per-line writer.  On an upgraded v1
-- volume the old ``correction_items`` table is renamed to this name and kept
-- untouched; new volumes create it empty.  New splits never append to it.
CREATE TABLE IF NOT EXISTS legacy_correction_items (
    correction_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id       TEXT NOT NULL REFERENCES projects(project_id)
                         ON DELETE CASCADE,
    split_id         INTEGER NOT NULL REFERENCES splits(split_id)
                         ON DELETE CASCADE,
    delivery_item_id INTEGER NOT NULL
                         REFERENCES delivery_items(delivery_item_id),
    delivery_id      INTEGER NOT NULL REFERENCES deliveries(delivery_id),
    table_name       TEXT NOT NULL,
    row_no           INTEGER NOT NULL,
    original_row_no  INTEGER NOT NULL,
    old_student_no   INTEGER NOT NULL,
    new_student_no   INTEGER NOT NULL,
    recorded_at      TEXT NOT NULL,
    UNIQUE (split_id, delivery_item_id)
);

-- One counter per (project, source row, project student number).  Deliveries
-- bump it in the same transaction as the delivery item, so a split can obtain
-- the number of old-number deliveries of every moved row with one row read
-- each, instead of scanning all delivery records.
CREATE TABLE IF NOT EXISTS delivery_rollups (
    project_id     TEXT NOT NULL REFERENCES projects(project_id)
                       ON DELETE CASCADE,
    table_name     TEXT NOT NULL,
    row_no         INTEGER NOT NULL,
    student_no     INTEGER NOT NULL,
    delivery_count INTEGER NOT NULL CHECK (delivery_count >= 0),
    PRIMARY KEY (project_id, table_name, row_no, student_no),
    FOREIGN KEY (project_id, table_name, row_no)
        REFERENCES project_rows(project_id, table_name, row_no),
    FOREIGN KEY (project_id, student_no)
        REFERENCES project_student_numbers(project_id, student_no)
);

-- One row per moved source row plus the contiguous block of correction ids
-- reserved for it.  The sheet expands the block over the row's recorded
-- deliveries at read time (ordered by delivery item id, exactly the order the
-- old per-line writer used).  A split therefore reads and writes O(moved
-- rows) records regardless of how often those rows were delivered.
CREATE TABLE IF NOT EXISTS correction_ranges (
    correction_range_id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id          TEXT NOT NULL REFERENCES projects(project_id)
                            ON DELETE CASCADE,
    split_id            INTEGER NOT NULL REFERENCES splits(split_id)
                            ON DELETE CASCADE,
    table_name          TEXT NOT NULL,
    row_no              INTEGER NOT NULL,
    project_row_no      INTEGER NOT NULL,
    old_student_no      INTEGER NOT NULL,
    new_student_no      INTEGER NOT NULL,
    first_correction_id INTEGER,
    correction_count    INTEGER NOT NULL CHECK (correction_count >= 0),
    recorded_at         TEXT NOT NULL,
    UNIQUE (split_id, table_name, row_no),
    CHECK (
        (correction_count = 0 AND first_correction_id IS NULL)
        OR (correction_count > 0 AND first_correction_id IS NOT NULL)
    )
);

-- Global correction-id allocator.  It starts above every id in the legacy
-- correction_items table, so new range-derived lines never collide with
-- correction numbers already handed out.
CREATE TABLE IF NOT EXISTS correction_id_state (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    next_id   INTEGER NOT NULL CHECK (next_id >= 1)
);

CREATE INDEX IF NOT EXISTS ix_project_rows_student
    ON project_rows(project_id, current_student_no);
CREATE INDEX IF NOT EXISTS ix_delivery_items_project
    ON delivery_items(project_id, table_name, row_no);
CREATE INDEX IF NOT EXISTS ix_delivery_items_rollup
    ON delivery_items(project_id, table_name, row_no, student_no,
                      delivery_item_id);
CREATE INDEX IF NOT EXISTS ix_correction_items_project_row
    ON legacy_correction_items(project_id, original_row_no);
CREATE INDEX IF NOT EXISTS ix_correction_ranges_project
    ON correction_ranges(project_id, split_id);
CREATE INDEX IF NOT EXISTS ix_splits_project
    ON splits(project_id, split_id);

-- Unified read model.  ``correction_items`` presents the same columns to every
-- reader and reconciliation query: legacy per-line rows verbatim plus lines
-- expanded from each range's reserved id block.  A line's id is its offset
-- inside the block ordered by delivery item id, which is exactly the order the
-- legacy writer allocated ids in, and it is computed before any scope filter
-- so a line keeps one id in project-, split- and delivery-scoped sheets.
CREATE VIEW IF NOT EXISTS correction_items AS
WITH numbered AS (
    SELECT cr.correction_range_id,
           cr.project_id,
           cr.split_id,
           di.delivery_item_id,
           di.delivery_id,
           cr.table_name,
           cr.row_no,
           cr.project_row_no AS original_row_no,
           cr.old_student_no,
           cr.new_student_no,
           cr.recorded_at,
           cr.first_correction_id,
           ROW_NUMBER() OVER (
               PARTITION BY cr.correction_range_id
               ORDER BY di.delivery_item_id
           ) AS position
    FROM correction_ranges cr
    JOIN delivery_items di
      ON di.project_id = cr.project_id
     AND di.table_name = cr.table_name
     AND di.row_no = cr.row_no
     AND di.student_no = cr.old_student_no
    WHERE cr.correction_count > 0
)
SELECT first_correction_id + position - 1 AS correction_id,
       project_id, split_id, delivery_item_id, delivery_id, table_name,
       row_no, original_row_no, old_student_no, new_student_no, recorded_at
FROM numbered
UNION ALL
SELECT correction_id, project_id, split_id, delivery_item_id, delivery_id,
       table_name, row_no, original_row_no, old_student_no, new_student_no,
       recorded_at
FROM legacy_correction_items;
"""

SCHEMA_VERSION = 2


def connect(db_path: str) -> sqlite3.Connection:
    """Open a connection configured for this application."""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(
        str(path),
        timeout=30.0,
        isolation_level=None,  # explicit transactions only
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def init_db(db_path: str) -> None:
    """Create the schema, migrate old volumes and switch to WAL mode.

    Migration v1 -> v2 is additive and idempotent:

    * ``delivery_rollups`` is backfilled from the immutable delivery items;
    * the v1 ``correction_items`` table is renamed to
      ``legacy_correction_items`` and left untouched, and existing splits are
      tagged ``correction_format = 'per_line'``, so their numbers and sheet
      digests remain byte-for-byte identical;
    * the global correction-id allocator is seeded above the largest legacy
      correction id; splits that predate ranges get no ranges.
    """
    conn = connect(db_path)
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        # Version 1 stored per-line corrections under the name that version 2
        # uses for the unified read view.  Run this before creating the schema
        # so ``CREATE VIEW correction_items`` has a free name.
        with write_tx(conn):
            _rename_v1_correction_table(conn)
        conn.executescript(SCHEMA)
        if version < SCHEMA_VERSION:
            with write_tx(conn):
                _migrate_to_v2(conn)
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        with write_tx(conn):
            conn.execute(
                """
                INSERT OR IGNORE INTO correction_id_state (singleton, next_id)
                SELECT 1, COALESCE(MAX(correction_id), 0) + 1
                FROM legacy_correction_items
                """
            )
    finally:
        conn.close()


def _rename_v1_correction_table(conn: sqlite3.Connection) -> None:
    """Move a v1 ``correction_items`` table aside for the compatibility view."""
    row = conn.execute(
        """
        SELECT type FROM sqlite_master
        WHERE name = 'correction_items' AND type IN ('table', 'view')
        """
    ).fetchone()
    if row is None or row["type"] != "table":
        return
    existing = conn.execute(
        "SELECT name FROM sqlite_master WHERE name = 'legacy_correction_items'"
    ).fetchone()
    if existing is not None:
        raise RuntimeError(
            "database has both correction_items (table) and "
            "legacy_correction_items; refusing to guess which one is current"
        )
    conn.execute(
        "ALTER TABLE correction_items RENAME TO legacy_correction_items"
    )


def _migrate_to_v2(conn: sqlite3.Connection) -> None:
    """Backfill derived counters for a volume written by version 1."""
    columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(splits)").fetchall()
    }
    if "correction_format" not in columns:
        # SQLite cannot add a column with a non-constant default, so add the
        # plain column and tag existing rows explicitly.
        conn.execute(
            "ALTER TABLE splits ADD COLUMN correction_format TEXT NOT NULL "
            "DEFAULT 'per_line'"
        )
        conn.execute("UPDATE splits SET correction_format = 'per_line'")
    # Superseded by ix_delivery_items_rollup, which adds delivery_item_id for
    # the correction-range expansion.
    conn.execute("DROP INDEX IF EXISTS ix_delivery_items_project_row_student")
    conn.execute(
        """
        INSERT OR IGNORE INTO delivery_rollups
            (project_id, table_name, row_no, student_no, delivery_count)
        SELECT project_id, table_name, row_no, student_no, COUNT(*)
        FROM delivery_items
        GROUP BY project_id, table_name, row_no, student_no
        """
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO correction_id_state (singleton, next_id)
        SELECT 1, COALESCE(MAX(correction_id), 0) + 1
        FROM legacy_correction_items
        """
    )


@contextmanager
def write_tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """An immediate write transaction.

    ``BEGIN IMMEDIATE`` acquires SQLite's write lock before any read that is
    used for allocation.  This keeps project row/student numbers unique when
    delivery and split requests arrive concurrently.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


@contextmanager
def read_tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """A read transaction giving a consistent snapshot for multi-query reads."""
    conn.execute("BEGIN")
    try:
        yield conn
    finally:
        conn.execute("COMMIT")
