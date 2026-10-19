from datetime import date, datetime
from unittest.mock import MagicMock

import sunrise_timelapse.sleep_to_sunrise as sts


def test_sunrise_timelapse_complete_time(monkeypatch):
    sunrise = datetime(2025, 5, 28, 6, 0, 0)
    now = datetime(2025, 5, 28, 5, 0, 0)

    mock_sun = MagicMock()
    mock_sun.sun.return_value = {"sunrise": sunrise}
    monkeypatch.setattr(sts, "sun", mock_sun)

    mock_dt = MagicMock()
    mock_dt.now.return_value = now
    monkeypatch.setattr(sts, "datetime", mock_dt)

    mock_date = MagicMock()
    mock_date.today.return_value = date(2025, 5, 28)
    monkeypatch.setattr(sts, "date", mock_date)

    monkeypatch.setattr(sts, "ZoneInfo", lambda tz: None)

    result = sts.sunrise_timelapse_complete_time()
    assert result == 6720.0  # 1 hour to sunrise + 52 min buffer


def test_sleep_time_waits(monkeypatch):
    called = {}
    monkeypatch.setattr(sts, "sunrise_timelapse_complete_time", lambda: 0.1)
    monkeypatch.setattr(sts, "todays_video_published", lambda: True)
    monkeypatch.setattr(sts, "sleep", lambda t: called.setdefault("slept", t))
    sts.sleep_time()
    assert called["slept"] == 0.1


def test_sleep_time_then_waits_for_video(monkeypatch):
    calls = []
    monkeypatch.setattr(sts, "sunrise_timelapse_complete_time", lambda: 0)
    monkeypatch.setattr(sts, "wait_for_todays_video", lambda: calls.append("wait"))
    monkeypatch.setattr(sts, "sleep", lambda t: calls.append(t))
    sts.sleep_time()
    assert calls == ["wait"]


def test_sleep_time_skips_video_wait_when_sleep_skipped(monkeypatch):
    calls = []
    monkeypatch.setattr(sts, "sunrise_timelapse_complete_time", lambda: 4 * 60 * 60)
    monkeypatch.setattr(sts, "wait_for_todays_video", lambda: calls.append("wait"))
    sts.sleep_time()
    assert calls == []


def test_wait_for_video_returns_immediately_when_published(monkeypatch):
    slept = []
    monkeypatch.setattr(sts, "todays_video_published", lambda: True)
    monkeypatch.setattr(sts, "sleep", slept.append)
    sts.wait_for_todays_video()
    assert slept == []


def test_wait_for_video_polls_until_published(monkeypatch):
    results = iter([False, False, True])
    slept = []
    monkeypatch.setattr(sts, "todays_video_published", lambda: next(results))
    monkeypatch.setattr(sts, "sleep", slept.append)
    sts.wait_for_todays_video()
    assert slept == [sts.VIDEO_POLL_SECONDS] * 2


def test_wait_for_video_gives_up_after_max(monkeypatch):
    slept = []
    monkeypatch.setattr(sts, "todays_video_published", lambda: False)
    monkeypatch.setattr(sts, "sleep", slept.append)
    sts.wait_for_todays_video()
    assert sum(slept) == sts.VIDEO_WAIT_MAX_SECONDS


def _feeds(timelapse, thumbnails):
    return lambda kind: timelapse if kind == "timelapse" else thumbnails


def test_todays_video_published_true(monkeypatch):
    monkeypatch.setattr(
        sts,
        "select_video",
        lambda data: ("10_19_2026_sunrise_timelapse", "u", "This Morning's"),
    )
    monkeypatch.setattr(
        sts,
        "fetch_glacier_data",
        _feeds([], [{"path": "/daily/sunrise_still/10_19_2026_sunrise.jpg"}]),
    )
    assert sts.todays_video_published() is True


def test_todays_video_published_false_on_latest(monkeypatch):
    monkeypatch.setattr(
        sts,
        "select_video",
        lambda data: ("10_18_2026_sunrise_timelapse", "u", "Latest"),
    )
    monkeypatch.setattr(sts, "fetch_glacier_data", _feeds([], []))
    assert sts.todays_video_published() is False


def test_todays_video_published_false_without_thumbnail(monkeypatch):
    monkeypatch.setattr(
        sts,
        "select_video",
        lambda data: ("10_19_2026_sunrise_timelapse", "u", "This Morning's"),
    )
    monkeypatch.setattr(
        sts,
        "fetch_glacier_data",
        _feeds([], [{"path": "/daily/sunrise_still/10_18_2026_sunrise.jpg"}]),
    )
    assert sts.todays_video_published() is False
