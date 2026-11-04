from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session
from xau_next.shared.values import new_id, parse_utc, utc_now
from xau_next.shared.preparation_timing import preparation_span, preparation_writer_lock

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()
_active = threading.local()


def canonical_json(value) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def normalize_time(value: str) -> str:
    return parse_utc(value).isoformat(timespec="microseconds").replace("+00:00", "Z")


class ReadScope:
    """An opaque, thread-bound scope; only platform code may resolve its session."""

    __slots__ = ("_active", "_db", "_failed", "_session", "_thread")

    def __init__(self, db: Database, session: Session):
        self._db, self._session = db, session
        self._thread, self._active, self._failed = threading.get_ident(), True, False


class UnitOfWork(ReadScope):
    pass


def session_for(scope: ReadScope, db: Database, *, write: bool = False) -> Session:
    if not isinstance(scope, ReadScope) or not scope._active or scope._db is not db:
        raise RuntimeError("事务作用域无效或不属于当前数据库")
    if scope._thread != threading.get_ident():
        raise RuntimeError("事务作用域不能在线程之间共享")
    if write and not isinstance(scope, UnitOfWork):
        raise RuntimeError("只读作用域不能写入")
    if scope._failed:
        raise RuntimeError("事务已失败，必须回滚")
    return scope._session


def poison(scope: ReadScope) -> None:
    scope._failed = True


class Database:
    def __init__(self, data_root: str | Path, *, clock: Callable[[], str] = utc_now):
        raw = str(data_root)
        if raw.startswith(("\\\\", "//")):
            raise ValueError("数据库数据目录不能位于网络共享")
        self.data_root = Path(data_root).expanduser().resolve()
        self.path = self.data_root / "db" / "xau-next.sqlite3"
        self._clock = clock
        self._key = str(self.path).casefold()
        with _locks_guard:
            self._writer = _locks.setdefault(self._key, threading.Lock())
        self._engine = create_engine(
            "sqlite:///" + self.path.as_posix(),
            connect_args={"check_same_thread": False, "timeout": 5},
            pool_size=5,
            max_overflow=0,
        )

        @event.listens_for(self._engine, "connect")
        def configure(connection, _):
            connection.isolation_level = None
            cursor = connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.execute("PRAGMA synchronous=FULL")
            cursor.close()

    def now(self) -> str:
        return normalize_time(self._clock())

    def initialize(self, *, revision: str = "head") -> None:
        self.data_root.mkdir(parents=True, exist_ok=True)
        for name in ("db", "attachments", "staging", "logs", "backups"):
            (self.data_root / name).mkdir(exist_ok=True)
        with self._writer, self._engine.connect() as connection:
            connection.exec_driver_sql("PRAGMA journal_mode=WAL")
            connection.commit()
            config = self._migration_config()
            exists = connection.exec_driver_sql(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='alembic_version'"
            ).scalar()
            if exists:
                current = connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar()
                target = ScriptDirectory.from_config(config).get_revision(revision).revision
                connection.commit()
                if current and current != target:
                    self.backup(self.data_root / "backups" / f"pre-migration-{current}-{new_id()}")
            config.attributes["connection"] = connection
            connection.commit()
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            try:
                command.upgrade(config, revision)
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    def _migration_config(self) -> Config:
        config = Config()
        bundled = Path(__file__).resolve().parents[2] / "_migrations"
        source = Path(__file__).resolve().parents[4] / "migrations"
        config.set_main_option("script_location", str(bundled if bundled.is_dir() else source))
        return config

    @contextmanager
    def read(self) -> Iterator[ReadScope]:
        with self._engine.connect() as connection:
            connection.exec_driver_sql("PRAGMA query_only=ON")
            connection.exec_driver_sql("BEGIN")
            scope = ReadScope(self, Session(bind=connection, expire_on_commit=False))
            try:
                yield scope
            finally:
                scope._active = False
                scope._session.close()
                connection.rollback()
                connection.exec_driver_sql("PRAGMA query_only=OFF")
                connection.commit()

    @contextmanager
    def write(self) -> Iterator[UnitOfWork]:
        held = getattr(_active, "roots", set())
        if self._key in held:
            raise RuntimeError("嵌套写事务必须复用原 UnitOfWork")
        with preparation_writer_lock(self._writer), preparation_span("write_transaction"):
            _active.roots = held | {self._key}
            try:
                with self._engine.connect() as connection:
                    with preparation_span("sqlite_write_begin"):
                        connection.exec_driver_sql("BEGIN IMMEDIATE")
                    scope = UnitOfWork(self, Session(bind=connection, expire_on_commit=False))
                    try:
                        yield scope
                        if scope._failed:
                            raise RuntimeError("事务已失败，全部写入已回滚")
                        scope._session.flush()
                        with preparation_span("sqlite_commit"):
                            connection.commit()
                    except BaseException:
                        connection.rollback()
                        raise
                    finally:
                        scope._active = False
                        scope._session.close()
            finally:
                _active.roots = held

    def documents(self, owner: str):
        from .documents import DocumentStore

        return DocumentStore(self, owner)

    def payload_codec(self):
        from .payloads import PayloadCodec

        return PayloadCodec(self)

    def compact_payloads(
        self, *, batch_size: int = 100, progress: Callable[[dict], None] | None = None
    ) -> dict:
        from .payloads import compact_payloads

        return compact_payloads(self, batch_size=batch_size, progress=progress)

    def check_integrity(self) -> dict:
        with self._engine.connect() as connection:
            integrity = connection.exec_driver_sql("PRAGMA integrity_check").scalars().all()
            foreign_keys = connection.exec_driver_sql("PRAGMA foreign_key_check").all()
            version = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        if integrity != ["ok"] or foreign_keys:
            raise ValueError("数据库完整性或外键检查失败")
        return {"integrity": "ok", "schema_version": version}

    def backup(self, target_root: str | Path) -> dict:
        from xau_next.platform.files.backup import create_backup

        return create_backup(self, target_root)

    @classmethod
    def restore(cls, backup_root: str | Path, data_root: str | Path) -> Database:
        from xau_next.platform.files.backup import restore_backup

        return restore_backup(backup_root, data_root)

    def close(self) -> None:
        self._engine.dispose()

    def _backup_database(self, target: Path) -> None:
        with sqlite3.connect(self.path) as source, sqlite3.connect(target) as destination:
            source.backup(destination)
