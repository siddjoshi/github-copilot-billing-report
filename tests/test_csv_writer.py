import csv

from copilot_aic_report import csv_writer


def _read(path):
    with open(path, "r", encoding="utf-8", newline="") as fh:
        return list(csv.reader(fh))


def test_header_order_required_first(tmp_path):
    out = tmp_path / "r.csv"
    csv_writer.write_report(str(out), [])
    rows = _read(str(out))
    assert rows[0] == csv_writer.ALL_COLUMNS
    assert rows[0][:8] == csv_writer.REQUIRED_COLUMNS


def test_write_report_values_and_missing(tmp_path):
    out = tmp_path / "r.csv"
    n = csv_writer.write_report(
        str(out),
        [
            {"user_login": "mona", "aic_consumed": 5, "user_status": "active"},
        ],
    )
    assert n == 1
    rows = _read(str(out))
    header, data = rows[0], rows[1]
    idx = {c: i for i, c in enumerate(header)}
    assert data[idx["user_login"]] == "mona"
    assert data[idx["aic_consumed"]] == "5"
    # Missing columns emitted as empty, never "null".
    assert data[idx["org_login"]] == ""


def test_null_string_becomes_empty(tmp_path):
    out = tmp_path / "r.csv"
    csv_writer.write_report(str(out), [{"user_login": "null"}])
    rows = _read(str(out))
    idx = rows[0].index("user_login")
    assert rows[1][idx] == ""


def test_quoting_special_chars(tmp_path):
    out = tmp_path / "r.csv"
    csv_writer.write_report(str(out), [{"data_quality_notes": 'a,b "c"\nd'}])
    rows = _read(str(out))
    idx = rows[0].index("data_quality_notes")
    assert rows[1][idx] == 'a,b "c"\nd'


def test_write_rollup(tmp_path):
    out = tmp_path / "roll.csv"
    n = csv_writer.write_rollup(str(out), [{"user_login": "mona", "any_active": "yes"}])
    assert n == 1
    rows = _read(str(out))
    assert rows[0] == csv_writer.ROLLUP_COLUMNS


def test_new_identity_columns_present(tmp_path):
    assert "github_user_id" in csv_writer.ALL_COLUMNS
    assert "resolved_user_login" in csv_writer.ALL_COLUMNS
    assert "github_user_id" in csv_writer.ROLLUP_COLUMNS
    assert "resolved_user_login" in csv_writer.ROLLUP_COLUMNS
    out = tmp_path / "r.csv"
    csv_writer.write_report(
        str(out),
        [{"user_login": "hash_LTIMPG", "github_user_id": 555, "resolved_user_login": "mona_acme"}],
    )
    rows = _read(str(out))
    idx = {c: i for i, c in enumerate(rows[0])}
    assert rows[1][idx["github_user_id"]] == "555"
    assert rows[1][idx["resolved_user_login"]] == "mona_acme"


def test_write_creates_missing_parent_dir(tmp_path):
    out = tmp_path / "nested" / "sub" / "report.csv"
    csv_writer.write_report(str(out), [{"user_login": "mona"}])
    assert out.exists()
