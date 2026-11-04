import pytest
from xau_next.domains.settings.public import SettingsService
from xau_next.platform.db import Database
from xau_next.shared.errors import DomainError


def test_ac18_defaults_revision_and_restart(tmp_path):
    db = Database(tmp_path)
    db.initialize()
    service = SettingsService(db)
    service.initialize()
    before = service.get_snapshot().to_dict()
    assert before["values"]["default_volume_lots"] is None
    assert before["values"]["per_trade_loss_cap"] is None
    assert before["values"]["experiment_enabled"] is False
    change = service.get_snapshot().to_dict()["values"]
    change["default_volume_lots"] = "0.02"
    result = service.update_settings({"expected_revision": 1, "values": change}).to_dict()
    assert result["revision"] == 2
    assert service.get_snapshot(1).to_dict() == before
    with pytest.raises(DomainError):
        service.update_settings({"expected_revision": 1, "values": change})
    db.close()
    db = Database(tmp_path)
    db.initialize()
    service = SettingsService(db)
    assert service.get_snapshot().to_dict() == result
    db.close()
