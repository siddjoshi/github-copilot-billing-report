import json

from copilot_aic_report import audit_archive


def test_load_none_and_missing():
    assert audit_archive.load_archive_events(None) == []
    assert audit_archive.load_archive_events("C:/does/not/exist/xyz") == []


def test_load_json_array(tmp_path):
    f = tmp_path / "a.json"
    f.write_text(
        json.dumps(
            [
                {"action": "copilot.seat_assigned", "user": {"login": "mona"}, "org": {"login": "acme"}, "@timestamp": 1700000000000},
                {"action": "copilot.seat_cancelled", "user_login": "mona", "org": "acme", "@timestamp": 1700100000000},
                {"action": "repo.create", "user": {"login": "x"}, "@timestamp": 1},
            ]
        ),
        encoding="utf-8",
    )
    events = audit_archive.load_archive_events(str(f))
    assert len(events) == 2
    assert events[0].action == "copilot.seat_assigned"
    assert events[0].user_login == "mona"
    assert events[0].org_login == "acme"
    assert events[1].user_login == "mona"


def test_load_jsonl(tmp_path):
    f = tmp_path / "a.jsonl"
    f.write_text(
        '{"action":"copilot.seat_assigned","user":"octocat","org":"acme","@timestamp":1700000000000}\n'
        "\n"
        '{"action":"copilot.seat_refresh","actor":{"login":"octocat"},"org":"acme","@timestamp":1700200000000}\n',
        encoding="utf-8",
    )
    events = audit_archive.load_archive_events(str(f))
    assert len(events) == 2
    assert events[0].user_login == "octocat"
    assert events[1].action == "copilot.seat_refresh"


def test_load_wrapped_events_key(tmp_path):
    f = tmp_path / "a.json"
    f.write_text(
        json.dumps({"events": [{"action": "copilot.seat_assigned", "user": "m", "org": "o", "@timestamp": 5}]}),
        encoding="utf-8",
    )
    events = audit_archive.load_archive_events(str(f))
    assert len(events) == 1


def test_load_directory(tmp_path):
    d = tmp_path / "arch"
    d.mkdir()
    (d / "1.json").write_text(json.dumps([{"action": "copilot.seat_assigned", "user": "a", "org": "o", "@timestamp": 10}]), encoding="utf-8")
    (d / "2.jsonl").write_text('{"action":"copilot.seat_cancelled","user":"b","org":"o","@timestamp":20}\n', encoding="utf-8")
    events = audit_archive.load_archive_events(str(d))
    assert len(events) == 2


def test_archive_start_month():
    events = audit_archive.load_archive_events(None)
    assert audit_archive.archive_start_month(events) is None
    from copilot_aic_report.models import AuditEvent

    evs = [AuditEvent("copilot.seat_assigned", "a", "o", 1700000000000), AuditEvent("copilot.seat_cancelled", "a", "o", 1690000000000)]
    # 1690000000000 ms -> 2023-07
    assert audit_archive.archive_start_month(evs) == "2023-07"


def test_bad_timestamp_tolerated(tmp_path):
    f = tmp_path / "a.json"
    f.write_text(json.dumps([{"action": "copilot.seat_assigned", "user": "a", "org": "o", "@timestamp": "notnum"}]), encoding="utf-8")
    events = audit_archive.load_archive_events(str(f))
    assert events[0].timestamp_ms is None


def test_actor_not_used_as_seat_holder(tmp_path):
    # Event has only an admin 'actor' and no seat-holder field -> holder must be None,
    # never the admin. This guards against attributing seats to the admin.
    f = tmp_path / "a.json"
    f.write_text(
        json.dumps([{"action": "copilot.seat_cancelled", "actor": {"login": "admin"}, "org": "o", "@timestamp": 10}]),
        encoding="utf-8",
    )
    events = audit_archive.load_archive_events(str(f))
    assert events[0].user_login is None


def test_assignee_preferred_over_actor(tmp_path):
    f = tmp_path / "a.json"
    f.write_text(
        json.dumps([{"action": "copilot.seat_assigned", "actor": {"login": "admin"}, "assignee": {"login": "mona"}, "org": "o", "@timestamp": 10}]),
        encoding="utf-8",
    )
    events = audit_archive.load_archive_events(str(f))
    assert events[0].user_login == "mona"
