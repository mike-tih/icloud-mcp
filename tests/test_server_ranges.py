from icloud_mcp.server import validate_date_range


def test_valid_ranges():
    assert validate_date_range("2026-08-27", "2026-08-27") is None
    assert validate_date_range("2026-08-01", "2026-10-31") is None  # 91 days


def test_missing_or_malformed():
    assert "YYYY-MM-DD" in validate_date_range(None, "2026-08-27")
    assert "YYYY-MM-DD" in validate_date_range("2026-08-27T00:00:00", "2026-08-28")
    assert "YYYY-MM-DD" in validate_date_range("27.08.2026", "28.08.2026")


def test_order_and_length():
    assert "before" in validate_date_range("2026-08-28", "2026-08-27")
    assert "too long" in validate_date_range("2026-01-01", "2026-06-01")
