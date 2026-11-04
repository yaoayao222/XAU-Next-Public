"""Only isolated SYNTHETIC SQLite/files; real data, models and MT5 are not used."""
import hashlib,json,os,sqlite3
from pathlib import Path

import pytest

from tooling.backup import fast
from tooling.backup.manage import backup,restore
from xau_next.platform.db.public import Database
from xau_next.platform.files.public import AttachmentStore


@pytest.fixture
def source(tmp_path):
    root=tmp_path/'source';db=Database(root);db.initialize();files=AttachmentStore(db)
    for i in range(3):
        a=files.prepare(('SYNTHETIC原文-'+str(i)).encode(),media_type='text/plain')
        with db.write() as tx:
            ref=files.register(tx,a);db.documents('analysis').insert(tx,'raw',str(i),{'attachment_id':ref.id})
    yield root,db,files
    db.close()


def test_snapshot_skips_attachment_reads_and_restores_exact_originals(source,tmp_path,monkeypatch):
    root,db,files=source;snapshot=tmp_path/'snapshot'
    original=fast.digest;seen=[]
    def tracked(p):
        seen.append(str(p));assert 'attachments' not in Path(p).parts
        return original(p)
    with monkeypatch.context() as m:
        m.setattr(fast,'digest',tracked)
        manifest=fast.backup(root,snapshot,mode='snapshot')
        assert seen
    assert manifest['format']=='xau-next-update-snapshot/1' and not manifest['self_contained']
    assert not (snapshot/'attachments').exists() and not (snapshot/'manifest.json').exists()
    assert restore(snapshot,tmp_path/'restored')['attachments_verified']==3
    for row in manifest['attachments']:
        assert (root/row['relative_path']).read_bytes()==(tmp_path/'restored'/row['relative_path']).read_bytes()


def test_full_reuses_prior_not_live_and_is_restorable_without_prior_directory(source,tmp_path):
    root,db,files=source;prior=tmp_path/'prior';a=backup(root,prior)
    with db.write() as tx:files.register(tx,files.prepare(b'SYNTHETIC-new',media_type='text/plain'))
    b=backup(root,tmp_path/'next',reuse_backup=prior)
    report=fast.read(tmp_path/'next/backup-report.json')
    assert report['linked_prior_files']==3 and report['copied_files']==1
    for row in a['attachments']:
        assert not os.path.samefile(root/row['relative_path'],tmp_path/'next'/row['relative_path'])
    # Rename only an isolated test fixture, demonstrating no parent path dependency.
    prior.rename(tmp_path/'prior-offline')
    assert restore(tmp_path/'next',tmp_path/'restored')['attachments_verified']==4


def test_corrupt_prior_fails_without_success_manifest_or_overwrite(source,tmp_path):
    root,db,_=source;prior=tmp_path/'prior';a=backup(root,prior)
    file=prior/a['attachments'][0]['relative_path'];file.write_bytes(b'CORRUPT')
    with pytest.raises(ValueError,match='校验失败'):backup(root,tmp_path/'next',reuse_backup=prior)
    assert not (tmp_path/'next/manifest.json').exists() and file.read_bytes()==b'CORRUPT'


def test_snapshot_missing_original_attachment_fails_restore(source,tmp_path):
    root,_,_=source;snapshot=tmp_path/'snapshot';a=backup(root,snapshot,mode='snapshot')
    p=root/a['attachments'][0]['relative_path'];p.rename(p.with_suffix('.unavailable'))
    with pytest.raises(FileNotFoundError):restore(snapshot,tmp_path/'restored')
    assert not (tmp_path/'restored/db/xau-next.sqlite3').exists()


def test_existing_or_nested_destinations_are_never_overwritten(source,tmp_path):
    root,_,_=source
    with pytest.raises(ValueError):backup(root,root/'nested')
    target=tmp_path/'used';target.mkdir();(target/'keep').write_text('keep')
    with pytest.raises(ValueError):backup(root,target)
    assert (target/'keep').read_text()=='keep'


def test_cross_volume_link_failure_falls_back_to_verified_copy(source,tmp_path,monkeypatch):
    root,_,_=source;prior=tmp_path/'prior';backup(root,prior)
    monkeypatch.setattr(fast.os,'link',lambda *_:(_ for _ in ()).throw(OSError('SYNTHETIC cross volume')))
    # Call the engine directly so the injected filesystem error applies.
    fast.backup(root,tmp_path/'next',reuse_backup=prior)
    report=fast.read(tmp_path/'next/backup-report.json')
    assert report['copied_files']==3 and report['linked_prior_files']==0
    assert restore(tmp_path/'next',tmp_path/'restored')['integrity']=='ok'


def test_tampered_paths_and_database_hash_are_rejected(source,tmp_path):
    root,_,_=source;target=tmp_path/'backup';backup(root,target)
    p=target/'manifest.json';m=fast.read(p);m['database']['path']='../outside.sqlite3';p.write_text(json.dumps(m))
    with pytest.raises(ValueError,match='格式'):restore(target,tmp_path/'restored')
    with pytest.raises(ValueError,match='越界'):fast.inside(root,'../private')


def test_preexisting_link_to_live_is_copied_instead_of_shared(source,tmp_path):
    root,_,_=source;prior=tmp_path/'prior';m=backup(root,prior)
    row=m['attachments'][0];p=prior/row['relative_path'];p.unlink();os.link(root/row['relative_path'],p)
    fast.backup(root,tmp_path/'next',reuse_backup=prior)
    assert not os.path.samefile(root/row['relative_path'],tmp_path/'next'/row['relative_path'])


def test_new_full_backup_remains_compatible_with_existing_platform_restore(source,tmp_path):
    root,_,_=source;target=tmp_path/'full';backup(root,target)
    restored=Database.restore(target,tmp_path/'legacy-restored')
    try:assert restored.check_integrity()['integrity']=='ok'
    finally:restored.close()


def test_corrupt_database_checksum_stops_restore_before_copying_attachments(source,tmp_path):
    root,_,_=source;target=tmp_path/'full';backup(root,target)
    m=fast.read(target/'manifest.json');m['database']['sha256']='0'*64
    (target/'manifest.json').write_text(json.dumps(m))
    with pytest.raises(ValueError,match='数据库副本校验失败'):restore(target,tmp_path/'restored')
    assert not (tmp_path/'restored/attachments').exists()
