"""Consistent database + immutable-attachment backup and isolated restore."""
import argparse
import json
import sqlite3
import sys
from pathlib import Path


def external_destination(value):
    destination = Path(value).resolve()
    app_root = Path(__file__).resolve().parents[2]
    if destination == app_root or destination.is_relative_to(app_root):
        raise ValueError("备份和恢复目录必须位于程序目录之外。")
    return destination


def _engine():
    # Script entry points also work from an arbitrary current directory.
    import importlib.util
    spec=importlib.util.spec_from_file_location('xau_backup_engine',Path(__file__).with_name('fast.py'))
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


def backup(data_root, destination, *, mode='full', reuse_backup=None, workers=4):
    return _engine().backup(data_root,external_destination(destination),mode=mode,reuse_backup=reuse_backup,workers=workers)


def restore(backup_root, destination):
    target = external_destination(destination)
    if target.exists() and any(target.iterdir()):
        raise ValueError("恢复目录必须不存在或为空；不能覆盖正在使用的数据目录。")
    return _engine().restore(backup_root,target)


def main():
    parser = argparse.ArgumentParser(description="备份数据库及其全部已登记附件，或恢复到独立目录")
    parser.add_argument("action", choices=("backup", "restore"))
    parser.add_argument("--data-root", required=True)
    parser.add_argument("--destination", required=True)
    parser.add_argument("--backup-root")
    parser.add_argument('--mode',choices=['full','snapshot'],default='full')
    parser.add_argument('--reuse-backup')
    parser.add_argument('--workers',type=int,default=4)
    args = parser.parse_args()
    try:
        if args.action == "backup":
            result = backup(args.data_root,args.destination,mode=args.mode,reuse_backup=args.reuse_backup,workers=args.workers)
            print(("完整备份完成：" if args.mode=='full' else '更新快照完成（附件未复制，恢复依赖原附件）：') + str(Path(args.destination).resolve()))
        else:
            if not args.backup_root:
                raise ValueError("恢复必须明确 --backup-root。")
            result = restore(args.backup_root, args.destination)
            print("恢复完成，原运行数据未切换：" + result["data_root"])
        print(json.dumps({"schema_version": result["schema_version"]}, ensure_ascii=False))
        return 0
    except (ValueError, OSError, sqlite3.Error, KeyError, TypeError) as exc:
        print("未完成：" + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
