from copilot_aic_report.checkpoint import Checkpoint


def test_disabled_when_no_path():
    cp = Checkpoint.load(None)
    assert cp.enabled is False
    assert cp.is_org_complete("o1") is False


def test_mark_and_persist(tmp_path):
    path = tmp_path / "cp.json"
    cp = Checkpoint.load(str(path))
    assert cp.enabled is True
    cp.mark_org_complete("o1", {"seats": [1, 2]})
    assert cp.is_org_complete("o1")
    assert cp.get_org_cache("o1") == {"seats": [1, 2]}

    # Reload from disk retains state.
    cp2 = Checkpoint.load(str(path))
    assert cp2.is_org_complete("o1")
    assert cp2.get_org_cache("o1") == {"seats": [1, 2]}


def test_mark_idempotent(tmp_path):
    path = tmp_path / "cp.json"
    cp = Checkpoint.load(str(path))
    cp.mark_org_complete("o1", {})
    cp.mark_org_complete("o1", {})
    assert cp.data["completed_orgs"] == ["o1"]


def test_clear(tmp_path):
    path = tmp_path / "cp.json"
    cp = Checkpoint.load(str(path))
    cp.mark_org_complete("o1", {})
    cp.clear()
    assert cp.is_org_complete("o1") is False
    assert not path.exists()


def test_load_corrupt_file(tmp_path):
    path = tmp_path / "cp.json"
    path.write_text("{not valid json", encoding="utf-8")
    cp = Checkpoint.load(str(path))
    assert cp.data["completed_orgs"] == []
