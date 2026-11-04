"""Restore a backup to a new directory, compact payloads, and prove logical equality."""

import argparse
import hashlib
import json
import sqlite3
import sys
from pathlib import Path

from xau_next.platform.db.public import Database


def logical_digest(db):
    """Include exact decoded UTF-8 payload text, every business column and stable keys."""
    result = {}
    connection = sqlite3.connect(db.path.as_uri() + "?mode=ro", uri=True)
    try:
        tables = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
            if row[0] != "payload_blobs"
        ]
        codec = db.payload_codec()
        with db.read() as scope:
            for table in tables:
                quoted = '"' + table.replace('"', '""') + '"'
                columns = connection.execute("PRAGMA table_info(" + quoted + ")").fetchall()
                names = [row[1] for row in columns]
                primary = [row[1] for row in sorted(columns, key=lambda row: row[5]) if row[5]]
                order = ",".join('"' + name.replace('"', '""') + '"' for name in primary or names)
                digest = hashlib.sha256()
                count = 0
                for row in connection.execute("SELECT * FROM " + quoted + " ORDER BY " + order):
                    count += 1
                    for name, value in zip(names, row):
                        if table in {"documents", "document_revisions"} and name == "payload":
                            value = codec.decode_text(scope, value)
                        if isinstance(value, bytes):
                            encoded = b"bytes:" + value
                        else:
                            encoded = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
                        digest.update(len(encoded).to_bytes(8, "big"))
                        digest.update(encoded)
                result[table] = {"rows": count, "columns": names, "sha256": digest.hexdigest()}
    finally:
        connection.close()
    return result


def restore_and_compact(backup_root, destination):
    destination = Path(destination).resolve()
    source = Path(backup_root).resolve()
    app_root = Path(__file__).resolve().parents[2]
    if destination == app_root or destination.is_relative_to(app_root):
        raise ValueError("压缩目标必须位于程序目录之外。")
    if destination == source or destination.is_relative_to(source) or source.is_relative_to(destination):
        raise ValueError("压缩目标与原始备份必须相互独立。")
    if destination.exists() and any(destination.iterdir()):
        raise ValueError("压缩目标必须不存在或为空，原数据不能原地改写。")
    print("Restoring verified backup to isolated destination...", flush=True)
    db = Database.restore(source, destination)
    try:
        before_bytes = db.path.stat().st_size
        print("Hashing all logical business rows before compaction...", flush=True)
        before = logical_digest(db)

        def progress(value):
            if value["committed_batches"] % 10 == 0:
                print("Compacted batches: " + str(value["committed_batches"]), flush=True)

        stats = db.compact_payloads(batch_size=100, progress=progress)
        print("Verifying exact logical content after compaction...", flush=True)
        after = logical_digest(db)
        if before != after:
            raise ValueError("压缩前后逻辑内容不一致，目标不能用于运行。")
        integrity = db.check_integrity()
    finally:
        db.close()
    print("Reclaiming free pages in isolated database...", flush=True)
    connection = sqlite3.connect(destination / "db/xau-next.sqlite3")
    try:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        connection.execute("VACUUM")
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("数据库完整性检查失败。")
    finally:
        connection.close()
    db = Database(destination)
    try:
        if logical_digest(db) != before:
            raise ValueError("回收空间后的逻辑内容发生变化。")
        integrity = db.check_integrity()
    finally:
        db.close()
    report = {
        "format": "xau-next-isolated-compaction/1",
        "backup_root": str(source),
        "data_root": str(destination),
        "database_before_bytes": before_bytes,
        "database_after_bytes": (destination / "db/xau-next.sqlite3").stat().st_size,
        "all_logical_rows_equal": True,
        "logical_tables": before,
        "compaction": stats,
        "integrity": integrity,
    }
    report_path = destination / "validation/storage-compaction.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                key: report[key]
                for key in ("database_before_bytes", "database_after_bytes", "all_logical_rows_equal")
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    return report


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="从备份恢复到独立目录并无损压缩历史载荷")
    parser.add_argument("--backup-root", required=True)
    parser.add_argument("--destination", required=True)
    args = parser.parse_args()
    restore_and_compact(args.backup_root, args.destination)
