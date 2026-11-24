"""Split cost, correction numbering and upgrade guarantees."""

from __future__ import annotations

import hashlib
import json
import sqlite3

from app import service
from app.db import SCHEMA_VERSION, connect, init_db


def deliver(client, project_id, items, request_id=None):
    body = {"items": items}
    if request_id is not None:
        body["request_id"] = request_id
    response = client.post(f"/projects/{project_id}/deliveries", json=body)
    assert response.status_code == 201, response.text
    return response.json()


def split(client, project_id, merged_student_no, rows, request_id=None):
    body = {"merged_student_no": merged_student_no, "rows": rows}
    if request_id is not None:
        body["request_id"] = request_id
    return client.post(f"/projects/{project_id}/splits", json=body)


def corrections(client, project_id, **params):
    response = client.get(f"/projects/{project_id}/corrections", params=params)
    assert response.status_code == 200, response.text
    return response.json()


def test_split_write_volume_does_not_grow_with_repeated_deliveries(
    client, project, seeded
):
    """The same row delivered many times must not multiply split writes."""
    item = [{"table_name": "assessment", "row_no": 11}]
    deliveries = []
    for index in range(12):
        deliveries.append(deliver(client, project, item, request_id=f"repeat-{index}"))

    before = _counts(client)
    response = split(client, project, "S002", item)
    assert response.status_code == 201, response.text
    result = response.json()
    after = _counts(client)

    # Receipt reports the exact number of corrections straight away.
    assert result["correction_count"] == 12

    # Split writes one range, not twelve correction rows.
    assert after["legacy_correction_items"] - before["legacy_correction_items"] == 0
    assert after["correction_ranges"] - before["correction_ranges"] == 1
    assert after["splits"] - before["splits"] == 1

    sheet = corrections(client, project, split_id=result["split_id"])
    assert sheet["line_count"] == 12
    assert {line["delivery_id"] for line in sheet["lines"]} == {
        d["delivery_id"] for d in deliveries
    }
    ids = [line["correction_id"] for line in sheet["lines"]]
    assert ids == list(range(ids[0], ids[0] + 12))
    # A later delivery reads the new number and adds no line.
    later = deliver(client, project, item, request_id="after-split")
    assert later["items"][0]["student_no"] == result["new_student_no"]
    assert corrections(client, project, split_id=result["split_id"]) == sheet


def test_split_writes_are_proportional_to_moved_rows(client, project, seeded):
    """Moved-row count drives split writes; delivery history does not."""
    item = [{"table_name": "assessment", "row_no": 11}]
    for index in range(20):
        deliver(client, project, item, request_id=f"bulk-{index}")

    before = _counts(client)
    response = split(client, project, "S002", item)
    assert response.status_code == 201, response.text
    after = _counts(client)
    # split + split student number + project_rows update + split_rows +
    # correction_ranges + counter updates; no correction row per delivery.
    assert after["delivery_items"] == before["delivery_items"]
    assert after["correction_ranges"] == before["correction_ranges"] + 1
    assert (
        after["project_student_numbers"] - before["project_student_numbers"] == 1
    )
    assert after["legacy_correction_items"] == before["legacy_correction_items"]
    assert response.json()["correction_count"] == 20


def test_correction_ids_are_identical_across_sheet_scopes(client, project, seeded):
    d1 = deliver(client, project, [{"table_name": "assessment", "row_no": 11}])
    d2 = deliver(client, project, [{"table_name": "assessment", "row_no": 11}])
    d3 = deliver(client, project, [{"table_name": "assessment", "row_no": 12}])
    result = split(
        client,
        project,
        "S002",
        [
            {"table_name": "assessment", "row_no": 11},
            {"table_name": "assessment", "row_no": 12},
        ],
    )
    assert result.status_code == 201, result.text
    split_id = result.json()["split_id"]

    full = corrections(client, project, split_id=split_id)
    project_sheet = corrections(client, project)
    by_delivery = {
        delivery_id: corrections(client, project, delivery_id=delivery_id)
        for delivery_id in (d1["delivery_id"], d2["delivery_id"], d3["delivery_id"])
    }

    project_by_id = {line["correction_id"]: line for line in project_sheet["lines"]}
    full_by_id = {line["correction_id"]: line for line in full["lines"]}
    assert set(full_by_id) <= set(project_by_id)
    for sheet in by_delivery.values():
        assert sheet["line_count"] == 1
        line = sheet["lines"][0]
        assert line == project_by_id[line["correction_id"]] == full_by_id[
            line["correction_id"]
        ]
    assert full["line_count"] == 3

    # Re-fetching any scope returns an identical sheet (including digest).
    for params in ({"split_id": split_id}, {}, {"delivery_id": d1["delivery_id"]}):
        assert corrections(client, project, **params) == corrections(
            client, project, **params
        )


def test_failed_split_rolls_back_range_and_numbers(client, project, seeded):
    deliver(client, project, [{"table_name": "assessment", "row_no": 11}])
    first = split(client, project, "S002", [{"table_name": "assessment", "row_no": 11}])
    assert first.status_code == 201
    sheet_before = corrections(client, project)
    rows_before = _rows(client, project)
    counts_before = _counts(client)

    failed = split(client, project, "S002", [{"table_name": "assessment", "row_no": 11}])
    assert failed.status_code == 409
    assert corrections(client, project) == sheet_before
    assert _rows(client, project) == rows_before
    assert _counts(client) == counts_before


def test_correction_view_matches_api_sheets(client, project, seeded):
    """The compatibility view and the API return the same lines and ids."""
    from app.main import app

    d1 = deliver(client, project, [{"table_name": "assessment", "row_no": 11}])
    d2 = deliver(client, project, [{"table_name": "assessment", "row_no": 11}])
    deliver(client, project, [{"table_name": "assessment", "row_no": 12}])
    result = split(
        client,
        project,
        "S002",
        [
            {"table_name": "assessment", "row_no": 11},
            {"table_name": "assessment", "row_no": 12},
        ],
    )
    assert result.status_code == 201, result.text
    split_id = result.json()["split_id"]

    conn = connect(app.state.db_path)
    try:
        for params, sql, args in (
            (
                {"split_id": split_id},
                "SELECT correction_id, split_id, table_name, original_row_no, "
                "old_student_no, new_student_no, delivery_id, recorded_at "
                "FROM correction_items WHERE project_id = ? AND split_id = ? "
                "ORDER BY correction_id",
                (project, split_id),
            ),
            (
                {"delivery_id": d2["delivery_id"]},
                "SELECT correction_id, split_id, table_name, original_row_no, "
                "old_student_no, new_student_no, delivery_id, recorded_at "
                "FROM correction_items WHERE project_id = ? AND delivery_id = ? "
                "ORDER BY correction_id",
                (project, d2["delivery_id"]),
            ),
            (
                {},
                "SELECT correction_id, split_id, table_name, original_row_no, "
                "old_student_no, new_student_no, delivery_id, recorded_at "
                "FROM correction_items WHERE project_id = ? "
                "ORDER BY correction_id",
                (project,),
            ),
        ):
            view_lines = []
            for row in conn.execute(sql, args).fetchall():
                view_lines.append(
                    {
                        "correction_id": row["correction_id"],
                        "split_id": row["split_id"],
                        "table_name": row["table_name"],
                        "original_row_no": row["original_row_no"],
                        "old_student_no": row["old_student_no"],
                        "new_student_no": row["new_student_no"],
                        "delivery_id": row["delivery_id"],
                        "recorded_at": row["recorded_at"],
                    }
                )
            assert corrections(client, project, **params)["lines"] == view_lines
    finally:
        conn.close()


def test_upgraded_v1_volume_returns_old_sheets_verbatim(tmp_path):
    db_path = str(tmp_path / "v1.db")
    _make_v1_volume(db_path)

    conn = connect(db_path)
    try:
        expected_all = _v1_sheet(conn)
        expected_split = _v1_sheet(conn, split_id=1)
        expected_delivery = _v1_sheet(conn, delivery_id=2)
        old_ids = [line["correction_id"] for line in expected_all["lines"]]
        assert old_ids == [1, 2, 3]
    finally:
        conn.close()

    init_db(db_path)  # v1 -> v2

    conn = connect(db_path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        project_id = "prj_v1"
        assert service.correction_sheet(conn, project_id, None, None) == expected_all
        assert (
            service.correction_sheet(conn, project_id, 1, None) == expected_split
        )
        assert (
            service.correction_sheet(conn, project_id, None, 2) == expected_delivery
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) AS n FROM legacy_correction_items"
            ).fetchone()["n"]
            == 3
        )

        # A new split on the upgraded volume gets fresh numbers above the old
        # ones; the pre-upgrade sheet stays byte-identical.
        service.upsert_source_rows(
            conn,
            [
                {
                    "table_name": "assessment",
                    "row_no": 13,
                    "merged_student_no": "S003",
                }
            ],
        )
        service.deliver(
            conn,
            project_id,
            [{"table_name": "assessment", "row_no": 13}],
            request_id="after-upgrade-delivery",
        )
        new_split = service.split_student(
            conn,
            project_id,
            "S003",
            [{"table_name": "assessment", "row_no": 13}],
            request_id="after-upgrade-split",
        )
        assert new_split["correction_count"] == 1
        new_lines = [
            line
            for line in service.correction_sheet(conn, project_id, None, None)["lines"]
            if line["split_id"] == new_split["split_id"]
        ]
        assert len(new_lines) == 1
        assert new_lines[0]["correction_id"] > max(old_ids)
        assert (
            service.correction_sheet(conn, project_id, 1, None) == expected_split
        )
    finally:
        conn.close()


def test_rollups_are_backfilled_on_upgrade(tmp_path):
    db_path = str(tmp_path / "rollup.db")
    _make_v1_volume(db_path)
    init_db(db_path)
    conn = connect(db_path)
    try:
        rows = conn.execute(
            """
            SELECT delivery_count FROM delivery_rollups
            ORDER BY table_name, row_no, student_no
            """
        ).fetchall()
        # registration:2 once, assessment:11 twice, assessment:12 once.
        assert sorted(r["delivery_count"] for r in rows) == [1, 1, 2]
        assert sum(r["delivery_count"] for r in rows) == 4
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _counts(client) -> dict[str, int]:
    from app.main import app

    conn = connect(app.state.db_path)
    try:
        tables = (
            "project_student_numbers",
            "project_rows",
            "deliveries",
            "delivery_items",
            "splits",
            "split_rows",
            "correction_ranges",
            "legacy_correction_items",
            "correction_id_state",
        )
        counts = {
            table: conn.execute(
                f"SELECT COUNT(*) AS n FROM {table}"
            ).fetchone()["n"]
            for table in tables
        }
        # Also record counters so a rolled-back reservation is visible.
        counter = conn.execute(
            "SELECT next_row_no, next_student_no FROM project_counters"
        ).fetchone()
        counts["next_row_no"] = counter["next_row_no"]
        counts["next_student_no"] = counter["next_student_no"]
        state = conn.execute(
            "SELECT next_id FROM correction_id_state"
        ).fetchone()
        counts["next_correction_id"] = None if state is None else state["next_id"]
        return counts
    finally:
        conn.close()


def _rows(client, project_id) -> list[dict]:
    response = client.get(f"/projects/{project_id}/rows", params={"limit": 1000})
    return response.json()["items"]


def _v1_sheet(conn, split_id=None, delivery_id=None) -> dict:
    """Reproduce the pre-upgrade correction sheet straight from v1 rows."""
    where = ["project_id = 'prj_v1'"]
    if split_id is not None:
        where.append(f"split_id = {split_id}")
    if delivery_id is not None:
        where.append(f"delivery_id = {delivery_id}")
    rows = conn.execute(
        f"""
        SELECT correction_id, split_id, table_name, original_row_no,
               old_student_no, new_student_no, delivery_id, recorded_at
        FROM correction_items
        WHERE {' AND '.join(where)}
        ORDER BY correction_id
        """
    ).fetchall()
    lines = [
        {
            "correction_id": r["correction_id"],
            "split_id": r["split_id"],
            "table_name": r["table_name"],
            "original_row_no": r["original_row_no"],
            "old_student_no": r["old_student_no"],
            "new_student_no": r["new_student_no"],
            "delivery_id": r["delivery_id"],
            "recorded_at": r["recorded_at"],
        }
        for r in rows
    ]
    canonical = json.dumps(
        lines, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    scope = "split" if split_id is not None else (
        "delivery" if delivery_id is not None else "project"
    )
    return {
        "project_id": "prj_v1",
        "scope": scope,
        "split_id": split_id,
        "delivery_id": delivery_id,
        "line_count": len(lines),
        "digest_sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        "lines": lines,
    }


V1_SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE projects (
    project_id TEXT PRIMARY KEY, name TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE project_counters (
    project_id TEXT PRIMARY KEY, next_row_no INTEGER NOT NULL DEFAULT 1,
    next_student_no INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE source_rows (
    table_name TEXT NOT NULL, row_no INTEGER NOT NULL,
    merged_student_no TEXT NOT NULL, created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL, PRIMARY KEY (table_name, row_no)
);
CREATE TABLE project_student_numbers (
    project_id TEXT NOT NULL, student_no INTEGER NOT NULL, kind TEXT NOT NULL,
    merged_student_no TEXT, origin_merged_student_no TEXT NOT NULL,
    split_id INTEGER, created_at TEXT NOT NULL,
    PRIMARY KEY (project_id, student_no)
);
CREATE UNIQUE INDEX ux_project_student_numbers_merged
    ON project_student_numbers(project_id, merged_student_no)
    WHERE merged_student_no IS NOT NULL;
CREATE TABLE project_rows (
    project_id TEXT NOT NULL, table_name TEXT NOT NULL, row_no INTEGER NOT NULL,
    project_row_no INTEGER NOT NULL, current_student_no INTEGER NOT NULL,
    original_student_no INTEGER NOT NULL, created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (project_id, table_name, row_no),
    UNIQUE (project_id, project_row_no)
);
CREATE TABLE deliveries (
    delivery_id INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT NOT NULL,
    request_id TEXT, request_fingerprint TEXT NOT NULL,
    requested_count INTEGER NOT NULL, new_project_row_count INTEGER NOT NULL
        DEFAULT 0, created_at TEXT NOT NULL, UNIQUE (project_id, request_id)
);
CREATE TABLE delivery_items (
    delivery_item_id INTEGER PRIMARY KEY AUTOINCREMENT,
    delivery_id INTEGER NOT NULL, project_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL, table_name TEXT NOT NULL, row_no INTEGER NOT NULL,
    project_row_no INTEGER NOT NULL, student_no INTEGER NOT NULL,
    source_merged_student_no TEXT NOT NULL,
    first_registration INTEGER NOT NULL, created_at TEXT NOT NULL,
    UNIQUE (delivery_id, ordinal)
);
CREATE TABLE splits (
    split_id INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT NOT NULL,
    request_id TEXT, request_fingerprint TEXT NOT NULL,
    old_student_no INTEGER NOT NULL, new_student_no INTEGER NOT NULL,
    row_count INTEGER NOT NULL, correction_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL, UNIQUE (project_id, request_id),
    UNIQUE (project_id, new_student_no)
);
CREATE TABLE split_rows (
    split_id INTEGER NOT NULL, project_id TEXT NOT NULL, table_name TEXT NOT NULL,
    row_no INTEGER NOT NULL, project_row_no INTEGER NOT NULL,
    old_student_no INTEGER NOT NULL, new_student_no INTEGER NOT NULL,
    was_in_project INTEGER NOT NULL,
    PRIMARY KEY (split_id, table_name, row_no)
);
CREATE TABLE correction_items (
    correction_id INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT NOT NULL,
    split_id INTEGER NOT NULL, delivery_item_id INTEGER NOT NULL,
    delivery_id INTEGER NOT NULL, table_name TEXT NOT NULL, row_no INTEGER NOT NULL,
    original_row_no INTEGER NOT NULL, old_student_no INTEGER NOT NULL,
    new_student_no INTEGER NOT NULL, recorded_at TEXT NOT NULL,
    UNIQUE (split_id, delivery_item_id)
);
"""


def _make_v1_volume(db_path: str) -> None:
    """Create a database exactly as the pre-range writer left it."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript(V1_SCHEMA)
        conn.execute("PRAGMA user_version = 1")

        now = "2026-01-01T00:00:00.000000Z"
        conn.execute("INSERT INTO projects VALUES ('prj_v1', 'legacy', ?)", (now,))
        conn.execute("INSERT INTO project_counters VALUES ('prj_v1', 100, 100)")
        for table, row_no, merged in (
            ("registration", 1, "S001"),
            ("assessment", 10, "S001"),
            ("registration", 2, "S002"),
            ("assessment", 11, "S002"),
            ("assessment", 12, "S002"),
        ):
            conn.execute(
                "INSERT INTO source_rows VALUES (?, ?, ?, ?, ?)",
                (table, row_no, merged, now, now),
            )
        for student_no, merged in ((1, "S001"), (2, "S002")):
            conn.execute(
                "INSERT INTO project_student_numbers VALUES "
                "('prj_v1', ?, 'merged', ?, ?, NULL, ?)",
                (student_no, merged, merged, now),
            )
        for table, row_no, project_row_no, student_no in (
            ("registration", 1, 1, 1),
            ("assessment", 10, 2, 1),
            ("registration", 2, 3, 2),
            ("assessment", 11, 4, 2),
            ("assessment", 12, 5, 2),
        ):
            conn.execute(
                "INSERT INTO project_rows VALUES "
                "('prj_v1', ?, ?, ?, ?, ?, ?, ?)",
                (table, row_no, project_row_no, student_no, student_no, now, now),
            )

        item_id = 0
        for request_id, items in (
            ("d1", [("registration", 2, 3), ("assessment", 11, 4)]),
            ("d2", [("assessment", 12, 5)]),
            ("d3", [("assessment", 11, 4)]),
        ):
            cursor = conn.execute(
                "INSERT INTO deliveries (project_id, request_id, "
                "request_fingerprint, requested_count, new_project_row_count, "
                "created_at) VALUES ('prj_v1', ?, 'fp', ?, 0, ?)",
                (request_id, len(items), now),
            )
            delivery_id = cursor.lastrowid
            for ordinal, (table, row_no, project_row_no) in enumerate(items, 1):
                item_id += 1
                conn.execute(
                    "INSERT INTO delivery_items (delivery_item_id, delivery_id, "
                    "project_id, ordinal, table_name, row_no, project_row_no, "
                    "student_no, source_merged_student_no, first_registration, "
                    "created_at) VALUES (?, ?, 'prj_v1', ?, ?, ?, ?, 2, 'S002', 0, ?)",
                    (item_id, delivery_id, ordinal, table, row_no, project_row_no, now),
                )

        conn.execute(
            "INSERT INTO project_student_numbers VALUES "
            "('prj_v1', 3, 'split', NULL, 'S002', 1, ?)",
            (now,),
        )
        conn.execute(
            "INSERT INTO splits (split_id, project_id, request_id, "
            "request_fingerprint, old_student_no, new_student_no, row_count, "
            "correction_count, created_at) VALUES "
            "(1, 'prj_v1', 's1', 'sfp', 2, 3, 2, 3, ?)",
            (now,),
        )
        conn.execute(
            "UPDATE project_rows SET current_student_no = 3 "
            "WHERE project_id = 'prj_v1' AND table_name = 'assessment' "
            "AND row_no IN (11, 12)"
        )
        for table, row_no, project_row_no in (
            ("assessment", 11, 4),
            ("assessment", 12, 5),
        ):
            conn.execute(
                "INSERT INTO split_rows VALUES (1, 'prj_v1', ?, ?, ?, 2, 3, 1)",
                (table, row_no, project_row_no),
            )
        # v1 writer order: for each moved split row in (table, row) order, all
        # of that row's delivery items ordered by item id.  Row assessment:11
        # has items 2 (delivery 1) and 4 (delivery 3); assessment:12 has item
        # 3 (delivery 2).  So ids 1..3 are items 2, 4, 3.
        for correction_id, delivery_item_id, delivery_id, table, row_no, project_row_no in (
            (1, 2, 1, "assessment", 11, 4),
            (2, 4, 3, "assessment", 11, 4),
            (3, 3, 2, "assessment", 12, 5),
        ):
            conn.execute(
                "INSERT INTO correction_items VALUES "
                "(?, 'prj_v1', 1, ?, ?, ?, ?, ?, 2, 3, ?)",
                (
                    correction_id,
                    delivery_item_id,
                    delivery_id,
                    table,
                    row_no,
                    project_row_no,
                    now,
                ),
            )
        conn.commit()
    finally:
        conn.close()
