from copilot_aic_report import snapshots


def test_list_and_earliest_empty(tmp_path):
    assert snapshots.list_snapshot_months(None) == []
    assert snapshots.list_snapshot_months(str(tmp_path)) == []
    assert snapshots.earliest_snapshot_month(str(tmp_path)) is None


def test_write_read_roundtrip(tmp_path):
    store = str(tmp_path / "snaps")
    recs = [{"user_login": "mona", "org_login": "acme"}]
    path = snapshots.write_snapshot(store, "2026-03", recs, meta={"v": 1})
    assert path is not None
    payload = snapshots.read_snapshot(store, "2026-03")
    assert payload["billing_period"] == "2026-03"
    assert payload["records"] == recs
    assert payload["meta"] == {"v": 1}
    assert snapshots.read_snapshot_records(store, "2026-03") == recs


def test_list_months_and_earliest(tmp_path):
    store = str(tmp_path / "s")
    snapshots.write_snapshot(store, "2026-03", [])
    snapshots.write_snapshot(store, "2026-01", [])
    assert snapshots.list_snapshot_months(store) == ["2026-01", "2026-03"]
    assert snapshots.earliest_snapshot_month(store) == "2026-01"


def test_read_missing_returns_none(tmp_path):
    store = str(tmp_path / "s")
    assert snapshots.read_snapshot(store, "2099-01") is None
    assert snapshots.read_snapshot_records(store, "2099-01") is None


def test_write_disabled_when_no_store():
    assert snapshots.write_snapshot(None, "2026-03", []) is None


def test_read_corrupt(tmp_path):
    store = tmp_path / "s"
    (store / "2026-03").mkdir(parents=True)
    (store / "2026-03" / "snapshot.json").write_text("{bad", encoding="utf-8")
    assert snapshots.read_snapshot(str(store), "2026-03") is None
