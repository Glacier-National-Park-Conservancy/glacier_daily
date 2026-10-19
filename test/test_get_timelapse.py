import json
from datetime import datetime
from unittest.mock import Mock, patch

import requests

from sunrise_timelapse.get_timelapse import (
    fetch_glacier_data,
    find_matching_thumbnail,
    process_video,
    select_video,
)

# Mock data for testing
MOCK_TIMELAPSE_DATA = [
    {"date": "2025-08-20 07:19:06.770128"},
    {
        "id": "latest",
        "vid_src": "/daily/sunrise_vid/8_20_2025_sunrise_timelapse.mp4",
        "url": "https://glacier.org/webcam-timelapse/?type=daily&id=latest",
        "title": "Latest Sunrise Timelapse",
        "string": "Latest Sunrise",
    },
    {
        "id": "8_20_2025_sunrise_timelapse",
        "vid_src": "/daily/sunrise_vid/8_20_2025_sunrise_timelapse.mp4",
        "url": "https://glacier.org/webcam-timelapse/?type=daily&id=8_20_2025_sunrise_timelapse",
        "title": "8-20 Sunrise Timelapse",
        "string": "8-20 Sunrise",
    },
    {
        "id": "8_19_2025_sunrise_timelapse",
        "vid_src": "/daily/sunrise_vid/8_19_2025_sunrise_timelapse.mp4",
        "url": "https://glacier.org/webcam-timelapse/?type=daily&id=8_19_2025_sunrise_timelapse",
        "title": "8-19 Sunrise Timelapse",
        "string": "8-19 Sunrise",
    },
]

MOCK_THUMBNAIL_DATA = [
    {"date": "2025-08-20 07:19:30.366413"},
    {"path": "/daily/sunrise_still/8_20_2025_sunrise.jpg"},
    {"path": "/daily/sunrise_still/8_19_2025_sunrise.jpg"},
    {"path": "/daily/sunrise_still/8_18_2025_sunrise.jpg"},
]


class TestFetchGlacierData:
    @patch("requests.get")
    def test_fetch_timelapse_success(self, mock_get):
        """Test successful timelapse data fetch."""
        mock_response = Mock()
        mock_response.json.return_value = MOCK_TIMELAPSE_DATA
        mock_response.raise_for_status.return_value = None
        mock_get.return_value = mock_response

        result = fetch_glacier_data("timelapse")

        assert result == MOCK_TIMELAPSE_DATA
        mock_get.assert_called_once()
        args, kwargs = mock_get.call_args
        assert (
            "http://timelapse.glacierconservancy.org/daily_timelapse_data.json"
            in args[0]
        )
        assert "User-Agent" in kwargs["headers"]

    @patch("requests.get")
    def test_fetch_thumbnail_success(self, mock_get):
        """Test successful thumbnail data fetch."""
        mock_response = Mock()
        mock_response.json.return_value = MOCK_THUMBNAIL_DATA
        mock_response.raise_for_status.return_value = None
        mock_get.return_value = mock_response

        result = fetch_glacier_data("thumbnails")

        assert result == MOCK_THUMBNAIL_DATA
        mock_get.assert_called_once()
        args, kwargs = mock_get.call_args
        assert (
            "http://timelapse.glacierconservancy.org/sunrise_thumbnails.json" in args[0]
        )
        assert "User-Agent" in kwargs["headers"]

    def test_fetch_invalid_endpoint(self):
        """Test handling of invalid endpoint type."""
        result = fetch_glacier_data("invalid")
        assert result == []

    @patch("requests.get")
    def test_fetch_request_exception(self, mock_get):
        """Test handling of request exceptions."""
        mock_get.side_effect = requests.RequestException("Network error")

        result = fetch_glacier_data("timelapse")

        assert result == []

    @patch("requests.get")
    def test_fetch_timeout(self, mock_get):
        """Test handling of timeout."""
        mock_get.side_effect = requests.Timeout("Request timeout")

        result = fetch_glacier_data("timelapse")

        assert result == []

    @patch("requests.get")
    def test_fetch_json_decode_error(self, mock_get):
        """Test handling of JSON decode errors."""
        mock_response = Mock()
        mock_response.json.side_effect = json.JSONDecodeError("Invalid JSON", "", 0)
        mock_response.raise_for_status.return_value = None
        mock_get.return_value = mock_response

        result = fetch_glacier_data("timelapse")

        assert result == []


class TestSelectVideo:
    @patch(
        "sunrise_timelapse.get_timelapse.now_mountain",
        return_value=datetime(2025, 8, 20),
    )
    def test_select_today_video(self, mock_now):
        """Test selecting today's video when available."""
        video_id, video_url, descriptor = select_video(MOCK_TIMELAPSE_DATA)

        assert video_id == "8_20_2025_sunrise_timelapse"
        assert (
            video_url
            == "https://glacier.org/webcam-timelapse/?type=daily&id=8_20_2025_sunrise_timelapse"
        )
        assert descriptor == "This Morning's"

    @patch(
        "sunrise_timelapse.get_timelapse.now_mountain",
        return_value=datetime(2025, 8, 21),
    )
    def test_select_latest_video(self, mock_now):
        """Test falling back to latest video when today's is not available."""
        video_id, video_url, descriptor = select_video(MOCK_TIMELAPSE_DATA)

        # Extract expected ID from the mock data's "latest" entry vid_src
        latest_entry = next(
            entry
            for entry in MOCK_TIMELAPSE_DATA
            if isinstance(entry, dict) and entry.get("id") == "latest"
        )
        expected_id = latest_entry["vid_src"].split("/")[-1].rsplit("_", 2)[0]

        assert video_id == expected_id
        assert video_url == "https://glacier.org/webcam-timelapse/?type=daily&id=latest"
        assert descriptor == "Latest"

    @patch("sunrise_timelapse.get_timelapse.logger")
    @patch(
        "sunrise_timelapse.get_timelapse.now_mountain",
        return_value=datetime(2025, 8, 21),
    )
    def test_select_latest_video_logs_fallback(self, mock_now, mock_logger):
        """Falling back to the latest video is logged with today's missing id."""
        select_video(MOCK_TIMELAPSE_DATA)

        args = mock_logger.info.call_args.args
        assert "using latest" in args[0]
        assert args[1] == "8_21_2025_sunrise_timelapse"

    @patch("sunrise_timelapse.get_timelapse.logger")
    @patch(
        "sunrise_timelapse.get_timelapse.now_mountain",
        return_value=datetime(2025, 8, 20),
    )
    def test_select_today_video_does_not_log_fallback(self, mock_now, mock_logger):
        select_video(MOCK_TIMELAPSE_DATA)

        mock_logger.info.assert_not_called()

    def test_select_video_empty_data(self):
        """Test handling empty data."""
        result = select_video({})

        assert result == (None, None, None)

    def test_select_video_no_valid_entries(self):
        """Test handling data with no valid video entries."""
        invalid_data = [{"date": "2025-08-20 07:19:06.770128"}]

        result = select_video(invalid_data)

        assert result == (None, None, None)

    @patch(
        "sunrise_timelapse.get_timelapse.now_mountain",
        return_value=datetime(2025, 8, 21),
    )
    def test_select_video_first_fallback(self, mock_now):
        # Data without 'latest' entry
        data_no_latest = [
            {"date": "2025-08-20 07:19:06.770128"},
            {
                "id": "8_19_2025_sunrise_timelapse",
                "vid_src": "/daily/sunrise_vid/8_19_2025_sunrise_timelapse.mp4",
                "url": "https://glacier.org/webcam-timelapse/?type=daily&id=8_19_2025_sunrise_timelapse",
                "title": "8-19 Sunrise Timelapse",
                "string": "8-19 Sunrise",
            },
        ]

        video_id, video_url, descriptor = select_video(data_no_latest)

        assert video_id == "8_19_2025_sunrise_timelapse"
        assert (
            video_url
            == "https://glacier.org/webcam-timelapse/?type=daily&id=8_19_2025_sunrise_timelapse"
        )
        assert descriptor == "Latest"


class TestFindMatchingThumbnail:
    def test_find_matching_thumbnail_success(self):
        """Test finding matching thumbnail for a video."""
        video_id = "8_20_2025_sunrise_timelapse"

        result = find_matching_thumbnail(video_id, MOCK_THUMBNAIL_DATA)

        assert result == "https://glacier.org/daily/sunrise_still/8_20_2025_sunrise.jpg"

    def test_find_matching_thumbnail_no_match(self):
        """Test when no matching thumbnail is found."""
        video_id = "8_22_2025_sunrise_timelapse"  # Not in thumbnail data

        result = find_matching_thumbnail(video_id, MOCK_THUMBNAIL_DATA)

        assert result is None

    def test_find_matching_thumbnail_empty_data(self):
        """Test handling empty thumbnail data."""
        result = find_matching_thumbnail("8_20_2025_sunrise_timelapse", {})

        assert result is None

    def test_find_matching_thumbnail_empty_video_id(self):
        """Test handling empty video ID."""
        result = find_matching_thumbnail("", MOCK_THUMBNAIL_DATA)

        assert result is None


class TestProcessVideo:
    @patch("sunrise_timelapse.get_timelapse.fetch_glacier_data")
    @patch("sunrise_timelapse.get_timelapse.select_video")
    @patch("sunrise_timelapse.get_timelapse.find_matching_thumbnail")
    def test_process_video_success(
        self,
        mock_find_thumbnail,
        mock_select_video,
        mock_fetch_data,
    ):
        """Test successful video processing."""

        def fetch_side_effect(endpoint_type):
            if endpoint_type == "timelapse":
                return MOCK_TIMELAPSE_DATA
            if endpoint_type == "thumbnails":
                return MOCK_THUMBNAIL_DATA
            return {}

        mock_fetch_data.side_effect = fetch_side_effect
        mock_select_video.return_value = (
            "8_20_2025_sunrise_timelapse",
            "https://glacier.org/webcam-timelapse/?type=daily&id=8_20_2025_sunrise_timelapse",
            "This Morning's",
        )
        mock_find_thumbnail.return_value = (
            "https://glacier.org/daily/sunrise_still/8_20_2025_sunrise.jpg"
        )

        result = process_video()

        assert result == (
            "https://glacier.org/webcam-timelapse/?type=daily&id=8_20_2025_sunrise_timelapse",
            "https://glacier.org/daily/sunrise_still/8_20_2025_sunrise.jpg",
            "This Morning's",
        )

    @patch("sunrise_timelapse.get_timelapse.fetch_glacier_data")
    @patch("sunrise_timelapse.get_timelapse.select_video")
    @patch("sunrise_timelapse.get_timelapse.find_matching_thumbnail")
    def test_process_video_gets_latest_sunrise(
        self,
        mock_find_thumbnail,
        mock_select_video,
        mock_fetch_data,
    ):
        """Test that video processing now gets latest sunrise regardless of timing."""

        def fetch_side_effect(endpoint_type):
            if endpoint_type == "timelapse":
                return MOCK_TIMELAPSE_DATA
            if endpoint_type == "thumbnails":
                return MOCK_THUMBNAIL_DATA
            return {}

        mock_fetch_data.side_effect = fetch_side_effect
        mock_select_video.return_value = (
            "latest",
            "https://glacier.org/webcam-timelapse/?type=daily&id=latest",
            "Latest",
        )
        mock_find_thumbnail.return_value = (
            "https://glacier.org/daily/sunrise_still/8_20_2025_sunrise.jpg"
        )

        result = process_video()

        assert result == (
            "https://glacier.org/webcam-timelapse/?type=daily&id=latest",
            "https://glacier.org/daily/sunrise_still/8_20_2025_sunrise.jpg",
            "Latest",
        )

    @patch("sunrise_timelapse.get_timelapse.fetch_glacier_data")
    def test_process_video_fetch_failure(self, mock_fetch_data):
        """Test handling when data fetching fails."""

        def fetch_side_effect(endpoint_type):
            if endpoint_type == "timelapse":
                return {}  # Empty data
            if endpoint_type == "thumbnails":
                return MOCK_THUMBNAIL_DATA
            return {}

        mock_fetch_data.side_effect = fetch_side_effect

        result = process_video()

        assert result == ("", "", "")

    @patch("sunrise_timelapse.get_timelapse.fetch_glacier_data")
    @patch("sunrise_timelapse.get_timelapse.select_video")
    def test_process_video_no_suitable_video(self, mock_select_video, mock_fetch_data):
        """Test handling when no suitable video is found."""

        def fetch_side_effect(endpoint_type):
            if endpoint_type == "timelapse":
                return MOCK_TIMELAPSE_DATA
            if endpoint_type == "thumbnails":
                return MOCK_THUMBNAIL_DATA
            return {}

        mock_fetch_data.side_effect = fetch_side_effect
        mock_select_video.return_value = (None, None, None)

        result = process_video()

        assert result == ("", "", "")

    @patch("sunrise_timelapse.get_timelapse.fetch_glacier_data")
    @patch("sunrise_timelapse.get_timelapse.select_video")
    @patch("sunrise_timelapse.get_timelapse.find_matching_thumbnail")
    def test_process_video_no_matching_thumbnail(
        self,
        mock_find_thumbnail,
        mock_select_video,
        mock_fetch_data,
    ):
        """Test handling when no matching thumbnail is found."""

        def fetch_side_effect(endpoint_type):
            if endpoint_type == "timelapse":
                return MOCK_TIMELAPSE_DATA
            if endpoint_type == "thumbnails":
                return MOCK_THUMBNAIL_DATA
            return {}

        mock_fetch_data.side_effect = fetch_side_effect
        mock_select_video.return_value = (
            "8_20_2025_sunrise_timelapse",
            "https://glacier.org/webcam-timelapse/?type=daily&id=8_20_2025_sunrise_timelapse",
            "This Morning's",
        )
        mock_find_thumbnail.return_value = None

        result = process_video()

        assert result == ("", "", "")

    @patch("sunrise_timelapse.get_timelapse.fetch_glacier_data")
    def test_process_video_exception_handling(self, mock_fetch_data):
        """Test handling unexpected exceptions."""
        mock_fetch_data.side_effect = Exception("Unexpected error")

        result = process_video()

        assert result == ("", "", "")
