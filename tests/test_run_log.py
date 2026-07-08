import json

from copilot_aic_report.run_log import RunLog


def test_bump_resolution_and_finish():
    log = RunLog()
    log.bump_resolution("seat")
    log.bump_resolution("seat")
    log.bump_resolution("audit")
    assert log.resolution_by_source == {"seat": 2, "audit": 1}
    log.finish()
    assert log.finished_at


def test_warn_and_error():
    log = RunLog()
    log.warn("w1")
    log.error("e1")
    assert log.warnings == ["w1"]
    assert log.errors == ["e1"]


def test_render_text_contains_sections():
    log = RunLog()
    log.config = {"enterprise_slug": "acme", "billing_period": "2026-03"}
    log.orgs_scanned = ["o1", "o2"]
    log.seats_found = 3
    log.reconciliation = [{"name": "count", "ok": True, "detail": "d"}]
    log.bump_resolution("seat")
    text = log.render_text()
    assert "Run Log" in text
    assert "acme" in text
    assert "o1, o2" in text
    assert "[OK] count" in text


def test_write_creates_text_and_json(tmp_path):
    log = RunLog()
    log.config = {"enterprise_slug": "acme", "billing_period": "2026-03"}
    log.finish()
    path = tmp_path / "run.log"
    log.write(str(path))
    assert path.exists()
    json_path = tmp_path / "run.log.json"
    assert json_path.exists()
    data = json.loads(json_path.read_text(encoding="utf-8"))
    assert data["config"]["enterprise_slug"] == "acme"


def test_reconciliation_mismatch_label():
    log = RunLog()
    log.reconciliation = [{"name": "sum", "ok": False, "detail": "off"}]
    assert "[MISMATCH] sum" in log.render_text()
