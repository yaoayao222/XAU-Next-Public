"""Migrate an isolated database clone; hash all original rows and copy only raw answers for read checks."""

import argparse
import hashlib
import json
import shutil
import sqlite3
import sys
import zlib
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine
from xau_next.domains.analysis.public import AnalysisService
from xau_next.domains.portfolio.public import PortfolioService
from xau_next.platform.db.public import Database
from xau_next.platform.files.public import AttachmentStore

ROOT = Path(__file__).resolve().parents[2]


def hash_file(path):
    with path.open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def table_hashes(path, tables=None):
    result = {}
    with sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True) as db:
        db.execute("PRAGMA query_only=ON")
        tables = tables or [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name!='alembic_version' ORDER BY name")]
        for table in tables:
            if not table.replace("_", "").isalnum():
                raise ValueError("Unexpected table name")
            columns = db.execute(f'PRAGMA table_info("{table}")').fetchall()
            primary = [r[1] for r in sorted(columns, key=lambda r: r[5]) if r[5]]
            order = ",".join('"' + name + '"' for name in primary) or "rowid"
            digest, count = hashlib.sha256(), 0
            for row in db.execute(f'SELECT * FROM "{table}" ORDER BY {order}'):
                value = [({"bytes_sha256": hashlib.sha256(v).hexdigest(), "length": len(v)} if isinstance(v, bytes) else v) for v in row]
                digest.update(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n")
                count += 1
            result[table] = {"rows": count, "sha256": digest.hexdigest()}
    return result


def run(source, output):
    source, output = Path(source).resolve(), Path(output).resolve()
    if output.exists() or output.is_relative_to(source) or source.is_relative_to(output):
        raise ValueError("A new independent validation directory is required")
    output.mkdir(parents=True)
    snapshot = output / "before-upgrade.sqlite3"
    with sqlite3.connect(f"file:{(source / 'db/xau-next.sqlite3').as_posix()}?mode=ro", uri=True) as original, sqlite3.connect(snapshot) as target:
        original.execute("PRAGMA query_only=ON")
        original.backup(target)
    before = table_hashes(snapshot)
    migrated = output / "database-clone"
    (migrated / "db").mkdir(parents=True)
    db_path = migrated / "db/xau-next.sqlite3"
    shutil.copy2(snapshot, db_path)
    answers = []
    with sqlite3.connect(snapshot) as db:
        old_schema = db.execute("SELECT version_num FROM alembic_version").fetchone()[0]
        for version_id, payload in db.execute("SELECT id,payload FROM documents WHERE owner='analysis' AND kind='version'"):
            if payload.startswith("@xau-payload-v1:"):
                expected = payload.split(":", 1)[1]
                payload = zlib.decompress(db.execute("SELECT compressed FROM payload_blobs WHERE sha256=?", (expected,)).fetchone()[0])
                assert hashlib.sha256(payload).hexdigest() == expected
            version = json.loads(payload)
            digest, relative = db.execute("SELECT sha256,relative_path FROM attachments WHERE id=?", (version["raw_attachment_id"],)).fetchone()
            raw_path = (source / relative).resolve()
            assert raw_path.is_relative_to(source) and hash_file(raw_path) == digest
            destination = (migrated / relative).resolve()
            assert destination.is_relative_to(migrated)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(raw_path, destination)
            answers.append({"version_id": version_id, "raw_sha256": digest, "relative_path": relative})
    # This is explicitly a DB-only clone test. A complete DATA_ROOT backup/restore is tested separately.
    engine = create_engine("sqlite:///" + db_path.as_posix())
    config = Config()
    config.set_main_option("script_location", str(ROOT / "apps/api/migrations"))
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "head")
    engine.dispose()
    after = table_hashes(db_path, tables=list(before))
    assert before == after
    database = Database(migrated)
    schema = json.loads((ROOT / "contracts/analysis-answer.schema.json").read_text(encoding="utf-8"))
    analysis = AnalysisService(database, AttachmentStore(database), schema)
    for answer in answers:
        version = analysis.get_version(answer["version_id"]).to_dict()
        assert hashlib.sha256(version["raw_text"].encode("utf-8")).hexdigest() == answer["raw_sha256"]
        assert hash_file(source / answer["relative_path"]) == answer["raw_sha256"]
    # The one-use projection only appends derived evidence. Compare every original
    # row by its exact primary key after the new service has initialized.
    PortfolioService(database)
    projection_counts = {}
    with sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True) as db:
        db.execute("PRAGMA query_only=ON")
        db.execute("ATTACH DATABASE ? AS original", (f"file:{snapshot.as_posix()}?mode=ro",))
        for table in before:
            if table == "sqlite_sequence":
                # Append-only projections advance SQLite's allocation counters;
                # the old rows they identify must still be checked below.
                assert db.execute('SELECT 1 FROM original.sqlite_sequence o LEFT JOIN main.sqlite_sequence n '
                                  'ON o.name=n.name WHERE n.seq IS NULL OR n.seq<o.seq LIMIT 1').fetchone() is None
                continue
            columns = db.execute(f'PRAGMA original.table_info("{table}")').fetchall()
            primary = [r[1] for r in columns if r[5]]
            assert primary, table
            match = " AND ".join(f'o."{name}" IS n."{name}"' for name in primary)
            changed = " OR ".join(f'o."{c[1]}" IS NOT n."{c[1]}"' for c in columns)
            different = db.execute(f'SELECT 1 FROM original."{table}" o LEFT JOIN main."{table}" n ON {match} '
                                   f'WHERE n."{primary[0]}" IS NULL OR {changed} LIMIT 1').fetchone()
            assert different is None, table
            count = db.execute(f'SELECT count(*) FROM main."{table}"').fetchone()[0]
            projection_counts[table] = count - before[table]["rows"]
    integrity = database.check_integrity()
    database.close()
    report = {"status": "passed", "source_data_root_written": False, "validation_kind": "database-only-clone-plus-raw-answer-reads",
              "source_schema": old_schema, "target_schema": integrity["schema_version"], "all_original_tables_unchanged": before == after,
              "tables": before, "raw_answers": answers, "original_snapshot_sha256": hash_file(snapshot),
              "original_business_rows_preserved_after_usage_projection": True, "projection_rows_added": projection_counts,
              "sqlite_sequence_policy": "allocation counters may advance for appended rows; never regress",
              "limitations": ["本目录不是完整可运行 DATA_ROOT；其它附件未复制。完整安装与备份恢复由独立候选测试覆盖。"]}
    (output / "preservation-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"status": report["status"], "tables_preserved": len(before), "raw_answers_preserved": len(answers),
                      "source_data_root_written": False, "report": str(output / "preservation-report.json")}, ensure_ascii=True))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-data-root", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    run(args.source_data_root, args.output)
