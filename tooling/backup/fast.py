"""Scoped update snapshots and independently restorable, verified backups.

Standard library only: no service startup, migrations, credentials or model I/O.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import time


def digest(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for chunk in iter(lambda:f.read(1024*1024),b''):h.update(chunk)
    return h.hexdigest()


def read(path):return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def write_new(path,value):
    with Path(path).open('x',encoding='utf-8') as f:
        json.dump(value,f,ensure_ascii=False,sort_keys=True,separators=(',',':'))


def inside(root,relative):
    rel=Path(relative)
    if rel.is_absolute() or '..' in rel.parts or not rel.parts or rel.parts[0]!='attachments':
        raise ValueError('附件路径越界')
    p=(root/rel).resolve()
    if not p.is_relative_to(root) or p==root:raise ValueError('附件路径越界')
    return p


def empty_target(destination,*protected):
    target=Path(destination).resolve()
    for root in protected:
        root=Path(root).resolve()
        if target==root or target.is_relative_to(root) or root.is_relative_to(target):
            raise ValueError('目标必须在来源目录之外，不能覆盖或包含原目录')
    if target.exists() and any(target.iterdir()):raise ValueError('目标目录必须不存在或为空')
    target.mkdir(parents=True,exist_ok=True)
    return target


def database_rows(path,*,check=True):
    with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)) as c:
        c.execute('pragma query_only=on')
        if check:
            if c.execute('pragma integrity_check').fetchone()[0]!='ok':raise ValueError('数据库完整性检查失败')
            if c.execute('pragma foreign_key_check').fetchall():raise ValueError('数据库外键检查失败')
        rows=[{'id':i,'sha256':s,'relative_path':p,'size':n} for i,s,p,n in c.execute('select id,sha256,relative_path,size from attachments order by id')]
        version=c.execute('select version_num from alembic_version').fetchone()[0]
    unique={}
    for row in rows:
        relative=row['relative_path'];identity=(row['sha256'],row['size'])
        if relative in unique and unique[relative]!=identity:raise ValueError('同一附件路径存在冲突登记')
        unique[relative]=identity
    return rows,unique,version


def check_file(path,expected,size):
    if path.stat().st_size!=size or digest(path)!=expected:raise ValueError('附件内容校验失败：'+path.name)


def copy_verified(source,dest,expected,size):
    # Exclusive creation is essential: never overwrite an existing backup link.
    h=hashlib.sha256();count=0
    with source.open('rb') as src,dest.open('xb') as dst:
        for chunk in iter(lambda:src.read(1024*1024),b''):
            dst.write(chunk);h.update(chunk);count+=len(chunk)
    if count!=size or h.hexdigest()!=expected:raise ValueError('源附件内容校验失败：'+source.name)
    check_file(dest,expected,size)


def parallel(items,operation,workers,progress):
    if not 1<=workers<=8:raise ValueError('并行数须为1至8')
    results=[];last=time.monotonic()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for result in pool.map(operation,items):
            results.append(result)
            if progress and time.monotonic()-last>=15:
                print(f'已核验 {len(results)}/{len(items)} 份文件',flush=True);last=time.monotonic()
    return results


def backup(data_root,destination,*,mode='full',reuse_backup=None,workers=4,progress=False):
    if mode not in {'full','snapshot'}:raise ValueError('未知备份模式')
    if not 1<=workers<=8:raise ValueError('并行数须为1至8')
    if mode=='snapshot' and reuse_backup:raise ValueError('更新快照不复制附件，无需指定旧备份')
    root=Path(data_root).resolve();source_db=root/'db/xau-next.sqlite3'
    if not source_db.is_file():raise ValueError('来源没有业务数据库')
    prior_root=Path(reuse_backup).resolve() if reuse_backup else None
    if prior_root and (prior_root==root or prior_root.is_relative_to(root) or root.is_relative_to(prior_root)):
        raise ValueError('不能将运行目录当作旧备份复用')
    target=empty_target(destination,root,*([prior_root] if prior_root else []))
    started=time.monotonic();db=target/'db/xau-next.sqlite3';db.parent.mkdir()
    if progress:print('正在创建一致数据库副本；不暂停当前系统。',flush=True)
    with closing(sqlite3.connect(source_db.as_uri()+'?mode=ro',uri=True)) as src,closing(sqlite3.connect(db)) as dst:
        src.backup(dst)
    if progress:print('正在核验数据库副本；'+('此模式不读取或复制附件。' if mode=='snapshot' else '随后并行核验附件。'),flush=True)
    rows,unique,version=database_rows(db)
    # Validate every path even in snapshot mode; never follow arbitrary manifest paths.
    for relative in unique:inside(root,relative)
    manifest={'format':'xau-next-backup/1' if mode=='full' else 'xau-next-update-snapshot/1',
        'schema_version':version,'created_at':datetime.now(timezone.utc).isoformat().replace('+00:00','Z'),
        'database':{'path':'db/xau-next.sqlite3','sha256':digest(db)},'attachments':rows}
    counts={'linked_prior_files':0,'copied_files':0,'reused_bytes':0,'copied_bytes':0}
    if mode=='snapshot':
        manifest.update(attachment_root=str(root),self_contained=False,
            limitation='仅数据库更新快照；附件未复制、未重读校验。恢复依赖原附件；不是独立灾难恢复备份。')
    else:
        known={};prior_manifest_hash=None
        if prior_root:
            prior=read(prior_root/'manifest.json')
            if prior['format']!='xau-next-backup/1':raise ValueError('复用来源必须是完整备份')
            prior_manifest_hash=digest(prior_root/'manifest.json')
            known={r['relative_path']:(r['sha256'],r['size']) for r in prior['attachments']}
        def transfer(item):
            relative,(sha,size)=item;dest=inside(target,relative);dest.parent.mkdir(parents=True,exist_ok=True)
            candidate=inside(prior_root,relative) if prior_root and known.get(relative)==(sha,size) else None
            linked=False
            if candidate and candidate.is_file():
                live=inside(root,relative)
                if not (live.exists() and os.path.samefile(candidate,live)):
                    try:os.link(candidate,dest);linked=True
                    except OSError:
                        if dest.exists():raise
                if linked:check_file(dest,sha,size);return 'linked_prior_files',size
                copy_verified(candidate,dest,sha,size)
            else:copy_verified(inside(root,relative),dest,sha,size)
            return 'copied_files',size
        for kind,size in parallel(list(unique.items()),transfer,workers,progress):
            counts[kind]+=1;counts['reused_bytes' if kind=='linked_prior_files' else 'copied_bytes']+=size
        if prior_root and digest(prior_root/'manifest.json')!=prior_manifest_hash:raise ValueError('旧备份清单在操作中发生变化')
    name='manifest.json' if mode=='full' else 'snapshot-manifest.json'
    write_new(target/name,manifest)
    report={'mode':mode,'self_contained':mode=='full','registered_attachments':len(rows),'unique_files':len(unique),
        'database_bytes':db.stat().st_size,'database_integrity':'ok','foreign_keys':'ok',
        'attachment_hashes_verified':len(unique) if mode=='full' else 0,'live_old_files_rehashed':False,
        'links_to_live_data':False,'prior_backup':str(prior_root) if prior_root else None,
        'seconds':round(time.monotonic()-started,3),**counts}
    write_new(target/'backup-report.json',report)
    return manifest


def restore(backup_root,destination,*,workers=4):
    root=Path(backup_root).resolve()
    full=(root/'manifest.json').is_file();manifest=read(root/('manifest.json' if full else 'snapshot-manifest.json'))
    expected='xau-next-backup/1' if full else 'xau-next-update-snapshot/1'
    if manifest.get('format')!=expected or manifest['database']['path']!='db/xau-next.sqlite3':raise ValueError('不支持的备份格式')
    attachment_root=root if full else Path(manifest['attachment_root']).resolve()
    target=empty_target(destination,root,attachment_root)
    database=root/'db/xau-next.sqlite3'
    if digest(database)!=manifest['database']['sha256']:raise ValueError('数据库副本校验失败')
    rows,unique,version=database_rows(database)
    if rows!=manifest['attachments'] or version!=manifest['schema_version']:raise ValueError('数据库与清单不一致')
    def transfer(item):
        relative,(sha,size)=item;source=inside(attachment_root,relative);dest=inside(target,relative)
        dest.parent.mkdir(parents=True,exist_ok=True)
        copy_verified(source,dest,sha,size)
    parallel(list(unique.items()),transfer,workers,False)
    (target/'db').mkdir();shutil.copy2(database,target/'db/xau-next.sqlite3')
    if digest(target/'db/xau-next.sqlite3')!=manifest['database']['sha256']:raise ValueError('恢复数据库复制校验失败')
    return {'integrity':'ok','schema_version':version,'data_root':str(target),'attachments_verified':len(unique),'mode':expected}


def main():
    p=argparse.ArgumentParser(description='更新快照或独立完整备份；不启动服务，不读取凭据')
    p.add_argument('action',choices=['backup','restore']);p.add_argument('--data-root');p.add_argument('--destination',required=True)
    p.add_argument('--backup-root');p.add_argument('--reuse-backup');p.add_argument('--mode',choices=['full','snapshot'],default='full');p.add_argument('--workers',type=int,default=4)
    a=p.parse_args()
    try:
        if a.action=='backup':
            if not a.data_root:raise ValueError('请指定数据目录')
            backup(a.data_root,a.destination,mode=a.mode,reuse_backup=a.reuse_backup,workers=a.workers,progress=True)
            result=read(Path(a.destination)/'backup-report.json')
            print('完整备份完成' if a.mode=='full' else '更新快照完成：附件未复制，恢复依赖原附件')
        else:
            if not a.backup_root:raise ValueError('请指定备份目录')
            result=restore(a.backup_root,a.destination,workers=a.workers)
        print(json.dumps(result,ensure_ascii=False));return 0
    except (ValueError,OSError,sqlite3.Error,KeyError,TypeError) as e:
        print('未完成：'+str(e),file=__import__('sys').stderr);return 1


if __name__=='__main__':
    import sys
    sys.stdout.reconfigure(encoding='utf-8');sys.stderr.reconfigure(encoding='utf-8')
    raise SystemExit(main())
