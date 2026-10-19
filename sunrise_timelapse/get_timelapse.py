"""
Select the best thumbnail frame from the timelapse video.
"""

import requests

from shared.datetime_utils import now_mountain
from shared.logging_config import get_logger

logger = get_logger(__name__)


class TimelapseError(Exception):
    """Base exception for timelapse processing errors."""


class VideoProcessingError(TimelapseError):
    """Exception raised for errors during video processing."""


class FileOperationError(TimelapseError):
    """Exception raised for file operation errors."""


def fetch_glacier_data(endpoint_type: str) -> list:
    """
    Fetch data from glacier.org JSON endpoints.

    Args:
        endpoint_type (str): Either "timelapse" or "thumbnails"

    Returns:
        list: JSON data from the endpoint, empty list if error
    """
    endpoint_map = {
        "timelapse": "daily_timelapse_data.json",
        "thumbnails": "sunrise_thumbnails.json",
    }

    if endpoint_type not in endpoint_map:
        logger.error("Invalid timelapse endpoint type: %s", endpoint_type)
        return []

    try:
        cache_buster = str(int(now_mountain().timestamp()))
        filename = endpoint_map[endpoint_type]
        url = f"http://timelapse.glacierconservancy.org/{filename}?{cache_buster}"
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
        }

        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        logger.error("Error fetching %s data: %s", endpoint_type, e)
        return []


def select_video(
    timelapse_data: list,
) -> tuple[str | None, str | None, str | None]:
    """
    Select video based on today's date first, then fallback to latest.

    Args:
        timelapse_data (dict): JSON data from timelapse endpoint

    Returns:
        tuple[Optional[str], Optional[str], Optional[str]]: (video_id, video_url, descriptor)
    """
    if not timelapse_data:
        return None, None, None

    try:
        today = now_mountain()
        today_id = f"{today.month}_{today.day}_{today.year}_sunrise_timelapse"

        # Skip the first entry which is just the date
        video_entries = [
            entry
            for entry in timelapse_data
            if isinstance(entry, dict) and "id" in entry
        ]

        # First, look for today's video
        for entry in video_entries:
            if entry.get("id") == today_id:
                return entry.get("id"), entry.get("url"), "This Morning's"

        # Fallback to latest
        for entry in video_entries:
            if entry.get("id") == "latest":
                id_ = entry.get("vid_src").split("/")[-1].rsplit("_", 2)[0]
                logger.info(
                    "No sunrise video for today (%s); using latest (%s)", today_id, id_
                )
                return id_, entry.get("url"), "Latest"

        # If no latest found, return the first sunrise entry
        # (the feed also carries sunset entries)
        for entry in video_entries:
            if "sunrise" in entry.get("id", ""):
                logger.info(
                    "No sunrise video for today (%s) and no latest entry; using %s",
                    today_id,
                    entry.get("id"),
                )
                return entry.get("id"), entry.get("url"), "Latest"

        return None, None, None

    except Exception as e:
        logger.error("Error selecting video: %s", e)
        return None, None, None


def find_matching_thumbnail(video_id: str, thumbnail_data: list) -> str | None:
    """
    Find thumbnail that matches the selected video.

    Args:
        video_id (str): ID of the selected video
        thumbnail_data (dict): JSON data from thumbnail endpoint

    Returns:
        Optional[str]: Full URL to the thumbnail image
    """
    if not video_id or not thumbnail_data:
        return None

    try:
        # Extract date pattern from video_id (e.g., "8_20_2025" from "8_20_2025_sunrise_timelapse")
        video_date_part = video_id.replace("_sunrise_timelapse", "")
        expected_thumbnail = f"{video_date_part}_sunrise.jpg"

        # Skip the first entry which is just the date
        thumbnail_entries = [
            entry
            for entry in thumbnail_data
            if isinstance(entry, dict) and "path" in entry
        ]

        for entry in thumbnail_entries:
            thumbnail_path = entry.get("path", "")
            if expected_thumbnail in thumbnail_path:
                return f"https://glacier.org{thumbnail_path}"

        return None

    except Exception as e:
        logger.error("Error finding matching thumbnail: %s", e)
        return None


def process_video() -> tuple[str, str, str]:
    """
    Process the sunrise timelapse by fetching remote data and selecting appropriate video and thumbnail.

    Returns:
        tuple[str, str, str]: (video_url, thumbnail_url, descriptor_string)
        Returns ("", "", "") if any error occurs.
    """
    try:
        # Fetch remote data
        timelapse_data = fetch_glacier_data("timelapse")
        thumbnail_data = fetch_glacier_data("thumbnails")

        if not timelapse_data or not thumbnail_data:
            logger.warning("Failed to fetch remote timelapse data")
            return "", "", ""

        # Select video based on today's date first, then latest
        video_id, video_url, descriptor = select_video(timelapse_data)

        if not video_id or not video_url:
            logger.warning("No suitable video found")
            return "", "", ""

        # Find matching thumbnail
        thumbnail_url = find_matching_thumbnail(video_id, thumbnail_data)

        if not thumbnail_url:
            logger.warning("No matching thumbnail found for video %s", video_id)
            return "", "", ""

        return video_url, thumbnail_url, descriptor or ""

    except Exception as e:
        logger.exception("Unexpected error in process_video: %s", e)
        return "", "", ""


if __name__ == "__main__":  # pragma: no cover
    print(process_video())
