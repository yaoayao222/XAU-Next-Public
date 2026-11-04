from copy import deepcopy

from test_review import inputs, at
from xau_next.domains.review.public import ReviewService, compute_prediction
from xau_next.platform.db.public import Database
from xau_next.shared.snapshots import Snapshot


def test_new_snapshot_identity_does_not_duplicate_result_and_preserves_old_refs(tmp_path):
    version, _, market, _ = inputs()
    clock = [at(4)]
    db = Database(tmp_path, clock=lambda: clock[0]); db.initialize()
    service = ReviewService(db, clock=lambda: clock[0])
    try:
        first = compute_prediction(version, market, horizon_minutes=3, as_of=clock[0])
        with db.write() as tx:
            original = service.commit_refresh(first, uow=tx).to_dict()
        changed = deepcopy(market); changed['snapshot_id'] = 'new-same-evidence'
        second = compute_prediction(version, changed, horizon_minutes=3, as_of=clock[0])
        with db.write() as tx:
            repeated = service.commit_refresh(second, uow=tx).to_dict()
        assert repeated == original
        assert original['source_refs'] != second.to_dict()['review']['source_refs']
        assert len(service.list_reviews(revisions='all').to_dict()['items']) == 1
        assert not service.refresh_needed('forecast', version['forecast_id'],
            version_id=version['version_id'], horizon=3)
    finally:
        db.close()


def test_incomplete_data_waits_for_changed_evidence_and_appends_completed_revision(tmp_path):
    version, _, market, _ = inputs()
    missing = deepcopy(market); missing['bars'].pop(1)
    clock = [at(4)]
    db = Database(tmp_path, clock=lambda: clock[0]); db.initialize()
    service = ReviewService(db, clock=lambda: clock[0])
    try:
        with db.write() as tx:
            original = service.commit_refresh(compute_prediction(version, missing,
                horizon_minutes=3, as_of=clock[0]), uow=tx, evidence_token='missing-v1').to_dict()
        assert original['outcome'] == 'DATA_INCOMPLETE'
        assert not service.refresh_needed('forecast', version['forecast_id'],
            version_id=version['version_id'], horizon=3)
        clock[0] = at(9)
        assert not service.refresh_needed('forecast', version['forecast_id'],
            version_id=version['version_id'], horizon=3)
        assert not service.refresh_needed('forecast', version['forecast_id'],
            version_id=version['version_id'], horizon=3, evidence_token='missing-v1')
        assert service.refresh_needed('forecast', version['forecast_id'],
            version_id=version['version_id'], horizon=3, evidence_token='complete-v2')
        with db.write() as tx:
            completed = service.commit_refresh(compute_prediction(version, market,
                horizon_minutes=3, as_of=clock[0]), uow=tx, evidence_token='complete-v2').to_dict()
        assert completed['revision'] == 2 and completed['outcome'] == 'EVALUATED'
        assert service.get_review(original['review_id'], revision=1).to_dict() == original
    finally:
        db.close()


def test_legacy_unknown_without_checkpoint_waits_instead_of_looping(tmp_path):
    version, _, market, _ = inputs()
    missing = deepcopy(market); missing['bars'].pop(1)
    db = Database(tmp_path, clock=lambda: at(4)); db.initialize()
    service = ReviewService(db, clock=lambda: at(59))
    try:
        prepared = compute_prediction(version, missing, horizon_minutes=3, as_of=at(4))
        service.commit_review(prepared)
        assert not service.refresh_needed('forecast', version['forecast_id'],
            version_id=version['version_id'], horizon=3)
        assert service.refresh_needed('forecast', version['forecast_id'],
            version_id=version['version_id'], horizon=3, evidence_token='new-stored-historical-content')
    finally:
        db.close()


def test_missing_scalp_eligibility_is_not_unblocked_by_more_quotes(tmp_path):
    version, _, market, _ = inputs()
    missing = deepcopy(market); missing['bars'].pop(1)
    draft = compute_prediction(version, missing, horizon_minutes=3, as_of=at(4)).to_dict()
    draft['review'].update(target_kind='reference_plan', target_id='scalp-plan',
                           method_version='bidask_reference/3')
    draft['review']['inputs']['horizon_minutes'] = None
    db = Database(tmp_path, clock=lambda: at(4)); db.initialize()
    service = ReviewService(db, clock=lambda: at(59))
    try:
        with db.write() as tx:
            service.commit_refresh(Snapshot.of(draft), uow=tx, evidence_token='old-quotes')
        assert not service.refresh_needed('reference_plan', 'scalp-plan',
            version_id=version['version_id'], evidence_token='new-quotes')
    finally:
        db.close()
