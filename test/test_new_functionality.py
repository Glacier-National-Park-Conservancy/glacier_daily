"""
Tests for new functionality added during the code quality audit remediation.

Covers:
- 5.1: now_mountain() timezone helper
- 5.2: AM/PM parsing in gnpc_datetime
- 5.3: Product selection max iterations
- 5.4: Drip API timeout and retry scenarios
- 5.5: Subscriber list error propagation
- 5.6: Startup config validation
- 5.7: Sleep-to-sunrise max timeout
"""

import json
from datetime import datetime
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

import pytest

# ============================================================================
# 5.1: Timezone helper tests
# ============================================================================


class TestNowMountain:
    """Tests for the now_mountain() timezone-aware datetime helper."""

    def test_returns_timezone_aware(self):
        from shared.datetime_utils import now_mountain

        result = now_mountain()
        assert result.tzinfo is not None

    def test_returns_mountain_timezone(self):
        from shared.datetime_utils import now_mountain

        result = now_mountain()
        tz_name = result.tzname()
        assert tz_name in ("MST", "MDT"), f"Expected MST or MDT, got {tz_name}"

    def test_returns_datetime_type(self):
        from shared.datetime_utils import now_mountain

        result = now_mountain()
        assert isinstance(result, datetime)

    def test_is_close_to_current_time(self):
        from shared.datetime_utils import now_mountain

        result = now_mountain()
        # Should be within 2 seconds of now
        from zoneinfo import ZoneInfo

        expected = datetime.now(tz=ZoneInfo("America/Denver"))
        diff = abs((result - expected).total_seconds())
        assert diff < 2


# ============================================================================
# 5.2: AM/PM parsing in gnpc_datetime
# ============================================================================


class TestGNPCDatetimeAMPM:
    """Tests for AM/PM parsing in convert_gnpc_datetimes."""

    @pytest.fixture
    def mst(self):
        return ZoneInfo("America/Denver")

    def test_pm_time(self, mst):
        from activities.gnpc_datetime import convert_gnpc_datetimes

        result = convert_gnpc_datetimes("July 15, 2024 3:30 pm")
        expected = datetime(2024, 7, 15, 15, 30, tzinfo=mst)
        assert result == expected

    def test_am_time(self, mst):
        from activities.gnpc_datetime import convert_gnpc_datetimes

        result = convert_gnpc_datetimes("July 15, 2024 9:00 am")
        expected = datetime(2024, 7, 15, 9, 0, tzinfo=mst)
        assert result == expected

    def test_noon(self, mst):
        from activities.gnpc_datetime import convert_gnpc_datetimes

        result = convert_gnpc_datetimes("July 15, 2024 12:00 pm")
        expected = datetime(2024, 7, 15, 12, 0, tzinfo=mst)
        assert result == expected

    def test_midnight(self, mst):
        from activities.gnpc_datetime import convert_gnpc_datetimes

        result = convert_gnpc_datetimes("July 15, 2024 12:00 am")
        expected = datetime(2024, 7, 15, 0, 0, tzinfo=mst)
        assert result == expected

    def test_no_ampm_defaults_to_pm(self, mst):
        """Without AM/PM indicator, should default to PM (GNPC convention)."""
        from activities.gnpc_datetime import convert_gnpc_datetimes

        result = convert_gnpc_datetimes("July 15, 2024 3:30")
        expected = datetime(2024, 7, 15, 15, 30, tzinfo=mst)
        assert result == expected

    def test_pm_with_dots(self, mst):
        from activities.gnpc_datetime import convert_gnpc_datetimes

        result = convert_gnpc_datetimes("July 15, 2024 3:30 p.m.")
        expected = datetime(2024, 7, 15, 15, 30, tzinfo=mst)
        assert result == expected

    def test_am_with_dots(self, mst):
        from activities.gnpc_datetime import convert_gnpc_datetimes

        result = convert_gnpc_datetimes("July 15, 2024 9:00 a.m.")
        expected = datetime(2024, 7, 15, 9, 0, tzinfo=mst)
        assert result == expected


# ============================================================================
# 5.3: Product selection max iterations
# ============================================================================


class TestProductMaxIterations:
    """Test that product selection doesn't loop forever."""

    def test_returns_empty_after_max_attempts(self, monkeypatch):
        monkeypatch.setenv("SHOPIFY_STORE_DOMAIN", "test.myshopify.com")
        monkeypatch.setenv("SHOPIFY_ACCESS_TOKEN", "test")

        # Every product has no featuredImage, so the search loop should
        # exhaust MAX_PRODUCT_SEARCH_ATTEMPTS and return the empty tuple.
        no_image_response = {
            "data": {
                "products": {
                    "edges": [
                        {
                            "node": {
                                "handle": "no-image",
                                "title": "No Image Product",
                                "description": "desc",
                                "totalInventory": 5,
                                "tracksInventory": True,
                                "seo": {"description": "desc"},
                                "featuredImage": None,
                            }
                        }
                    ],
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                }
            }
        }

        with patch("requests.post") as mock_post:
            mock_post.return_value = Mock(
                status_code=200,
                text=json.dumps(no_image_response),
                raise_for_status=Mock(),
            )

            from product_otd.product import get_product

            result = get_product()
            assert result == ("", "", "", "")


# ============================================================================
# 5.6: Startup config validation
# ============================================================================


class TestConfigValidation:
    """Tests for startup configuration validation."""

    def test_passes_with_all_required_vars(self, monkeypatch):
        for var in [
            "NPS",
            "DRIP_TOKEN",
            "DRIP_ACCOUNT",
            "FTP_USERNAME",
            "FTP_PASSWORD",
            "MAPBOX_TOKEN",
        ]:
            monkeypatch.setenv(var, "test_value")

        from shared.config_validation import validate_config

        # Should not raise or exit
        validate_config()

    def test_exits_with_missing_required_var(self, monkeypatch):
        # Set all except MAPBOX_TOKEN (stays "" from conftest seeding)
        for var in [
            "NPS",
            "DRIP_TOKEN",
            "DRIP_ACCOUNT",
            "FTP_USERNAME",
            "FTP_PASSWORD",
        ]:
            monkeypatch.setenv(var, "test_value")

        from shared.config_validation import validate_config

        with pytest.raises(SystemExit):
            validate_config()

    def test_warns_for_optional_vars(self, monkeypatch, caplog):
        import logging

        # CACHE_PURGE and ZONE_ID stay "" from conftest seeding
        for var in [
            "NPS",
            "DRIP_TOKEN",
            "DRIP_ACCOUNT",
            "FTP_USERNAME",
            "FTP_PASSWORD",
            "MAPBOX_TOKEN",
        ]:
            monkeypatch.setenv(var, "test_value")

        from shared.config_validation import validate_config

        with caplog.at_level(logging.WARNING):
            validate_config()

        assert "CACHE_PURGE" in caplog.text
        assert "ZONE_ID" in caplog.text


# ============================================================================
# 5.7: Sleep-to-sunrise max timeout
# ============================================================================


class TestSleepToSunriseMaxTimeout:
    """Test that sleep_to_sunrise respects the maximum wait time."""

    def test_skips_sleep_when_exceeds_max(self, monkeypatch):
        import sunrise_timelapse.sleep_to_sunrise as sts

        # Return a time exceeding MAX_WAIT_SECONDS
        monkeypatch.setattr(sts, "sunrise_timelapse_complete_time", lambda: 4 * 60 * 60)
        slept = {}
        monkeypatch.setattr(sts, "sleep", lambda t: slept.setdefault("time", t))

        sts.sleep_time()
        assert "time" not in slept  # Should NOT have slept

    def test_sleeps_when_under_max(self, monkeypatch):
        import sunrise_timelapse.sleep_to_sunrise as sts

        monkeypatch.setattr(sts, "sunrise_timelapse_complete_time", lambda: 100)
        monkeypatch.setattr(sts, "todays_video_published", lambda: True)
        slept = {}
        monkeypatch.setattr(sts, "sleep", lambda t: slept.setdefault("time", t))

        sts.sleep_time()
        assert slept["time"] == 100

    def test_max_wait_constant_is_3_hours(self):
        import sunrise_timelapse.sleep_to_sunrise as sts

        assert sts.MAX_WAIT_SECONDS == 3 * 60 * 60


# ============================================================================
# 5.4 + 5.5: Drip API error handling
# ============================================================================


class TestDripErrorHandling:
    """Tests for Drip API timeout, JSON errors, and subscriber list error propagation."""

    def test_subscriber_list_has_timeout(self, monkeypatch):
        """Verify subscriber_list passes timeout to requests.get."""
        monkeypatch.setenv("DRIP_TOKEN", "test")
        monkeypatch.setenv("DRIP_ACCOUNT", "test_id")

        call_kwargs = []

        def capture_get(*args, **kwargs):
            call_kwargs.append(kwargs)
            resp = Mock()
            resp.status_code = 200
            resp.json.return_value = {
                "subscribers": [],
                "meta": {"total_pages": 1},
            }
            return resp

        with patch("drip.subscriber_list.requests.get", side_effect=capture_get):
            from drip.subscriber_list import subscriber_list

            subscriber_list("Test Tag")

        assert any("timeout" in kw for kw in call_kwargs)

    def test_bulk_trigger_handles_json_decode_error(self, monkeypatch, caplog):
        """Verify bulk_workflow_trigger handles malformed JSON responses."""
        import logging

        monkeypatch.setenv("DRIP_TOKEN", "test")
        monkeypatch.setenv("DRIP_ACCOUNT", "test_id")

        mock_resp = Mock()
        mock_resp.status_code = 201
        mock_resp.text = "invalid json response"
        mock_resp.json.side_effect = json.JSONDecodeError("fail", "", 0)

        with (
            patch("drip.drip_actions.requests.post", return_value=mock_resp),
            caplog.at_level(logging.ERROR),
        ):
            from drip.drip_actions import bulk_workflow_trigger

            # Should not raise - handles error gracefully
            bulk_workflow_trigger(["test@example.com"])
