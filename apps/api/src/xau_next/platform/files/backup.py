from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from pathlib import Path

from alembic.script import ScriptDirectory
from xau_next.platform.db import Database
from xau_next.platform.db.core import canonical_json


def _empty_target(path: Path) -> None:
    if path.exists() and any(path.iterdir()):
        raise ValueError("备份或恢复目标目录必须为空")
    path.mkdir(parents=True, exist_ok=True)


def _file(root: Path, relative: str) -> Path:
    result = (root / relative).resolve()
    if not result.is_relative_to(root) or result == root:
        raise ValueError("备份路径越界")
    return result


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def create_backup(db: Database, target_root: str | Path) -> dict:
    target = Path(target_root).expanduser().resolve()
    if target == db.data_root or db.data_root.is_relative_to(target):
        raise ValueError("备份目录不能包含正在使用的数据目录")
    _empty_target(target)
    database_relative = db.path.relative_to(db.data_root).as_posix()
    backup_db = target / database_relative
    backup_db.parent.mkdir(parents=True, exist_ok=True)
    db._backup_database(backup_db)
    with sqlite3.connect(backup_db) as snapshot:
        rows = snapshot.execute(
            "SELECT id, sha256, relative_path, size FROM attachments ORDER BY id"
        ).fetchall()
        version = snapshot.execute("SELECT version_num FROM alembic_version").fetchone()[0]
        if snapshot.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("备份数据库完整性检查失败")
        if snapshot.execute("PRAGMA foreign_key_check").fetchall():
            raise ValueError("备份数据库外键检查失败")
    attachments = []
    for id, sha256, relative, size in rows:
        source, destination = _file(db.data_root, relative), _file(target, relative)
        if source.stat().st_size != size or _hash(source) != sha256:
            raise ValueError("备份源附件校验失败")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        if _hash(destination) != sha256:
            raise ValueError("备份附件复制校验失败")
        attachments.append({"id": id, "relative_path": relative, "sha256": sha256, "size": size})
    manifest = {
        "format": "xau-next-backup/1",
        "schema_version": version,
        "created_at": db.now(),
        "database": {"path": database_relative, "sha256": _hash(backup_db)},
        "attachments": attachments,
    }
    (target / "manifest.json").write_text(canonical_json(manifest), encoding="utf-8")
    return manifest


def restore_backup(backup_root: str | Path, data_root: str | Path) -> Database:
    source, target = Path(backup_root).resolve(), Path(data_root).expanduser().resolve()
    manifest = json.loads((source / "manifest.json").read_text(encoding="utf-8"))
    if manifest["format"] != "xau-next-backup/1" or manifest["database"]["path"] != "db/xau-next.sqlite3":
        raise ValueError("不支持的备份格式")
    database = _file(source, manifest["database"]["path"])
    if _hash(database) != manifest["database"]["sha256"]:
        raise ValueError("备份数据库 SHA256 校验失败")
    with sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True) as snapshot:
        if snapshot.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("恢复数据库完整性检查失败")
        if snapshot.execute("PRAGMA foreign_key_check").fetchall():
            raise ValueError("恢复数据库外键检查失败")
        rows = snapshot.execute("SELECT id,sha256,relative_path,size FROM attachments ORDER BY id").fetchall()
        expected = [
            {"id": id, "sha256": sha, "relative_path": rel, "size": size} for id, sha, rel, size in rows
        ]
        if expected != manifest["attachments"]:
            raise ValueError("附件清单与数据库引用不符")
        if (
            snapshot.execute("SELECT version_num FROM alembic_version").fetchone()[0]
            != manifest["schema_version"]
        ):
            raise ValueError("备份 schema 版本不一致")
    db = Database(target)
    if ScriptDirectory.from_config(db._migration_config()).get_revision(manifest["schema_version"]) is None:
        raise ValueError("当前应用不支持此数据库版本")
    for row in expected:
        attachment = _file(source, row["relative_path"])
        if attachment.stat().st_size != row["size"] or _hash(attachment) != row["sha256"]:
            raise ValueError("恢复附件校验失败")
    _empty_target(target)
    db.path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(database, db.path)
    for row in expected:
        destination = _file(target, row["relative_path"])
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(_file(source, row["relative_path"]), destination)
    db.initialize()
    db.check_integrity()
    return db
