from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from sync_weekly import WEEKLY_URL, fetch_weekly_index, resolve_issue_date, validate_issue_date


class _FakePage:
    def __init__(self) -> None:
        self.requested_url = ""
        self.url = "about:blank"
        self.title = ""
        self.html = ""
        self.wait = SimpleNamespace(load_start=lambda: None)

    def get(self, url: str) -> None:
        self.requested_url = url
        self.url = url

    def run_js(self, _script: str) -> str:
        return ""


class IssueDateTests(unittest.TestCase):
    def test_friday_maps_to_next_saturday(self) -> None:
        self.assertEqual(resolve_issue_date("2026-08-14"), ("2026-08-15", ""))
        self.assertEqual(validate_issue_date("2026-08-14"), (True, ""))

    def test_saturday_is_preserved(self) -> None:
        self.assertEqual(resolve_issue_date("2026-08-15"), ("2026-08-15", ""))
        self.assertEqual(validate_issue_date("2026-08-15"), (True, ""))

    def test_other_weekdays_are_rejected(self) -> None:
        resolved, error = resolve_issue_date("2026-08-13")
        self.assertIsNone(resolved)
        self.assertIn("仅支持周五或周六", error)
        self.assertEqual(validate_issue_date("2026-08-13"), (False, error))

    def test_invalid_date_is_rejected(self) -> None:
        resolved, error = resolve_issue_date("2026-02-30")
        self.assertIsNone(resolved)
        self.assertIn("日期格式错误", error)

    def test_friday_index_request_uses_saturday_url(self) -> None:
        page = _FakePage()
        with (
            patch("sync_weekly.time.sleep", return_value=None),
            patch("sync_weekly._parse_weeklyedition_html_v2", return_value=[]),
            patch("sync_weekly._visible_weekly_cover_url", return_value=""),
        ):
            self.assertEqual(fetch_weekly_index(page, "2026-08-14"), [])
        self.assertEqual(page.requested_url, f"{WEEKLY_URL}/2026-08-15")


if __name__ == "__main__":
    unittest.main()
