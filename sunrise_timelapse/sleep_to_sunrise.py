"""
Calculate when the sunrise timelapse will be finished and sleep until that time.
"""

from datetime import date, datetime, timedelta
from time import sleep
from zoneinfo import ZoneInfo

from astral import LocationInfo, sun

from shared.logging_config import get_logger
from sunrise_timelapse.get_timelapse import (
    fetch_glacier_data,
    find_matching_thumbnail,
    select_video,
)

logger = get_logger(__name__)

MAX_WAIT_SECONDS = 3 * 60 * 60  # 3 hours
SUNRISE_BUFFER_MINUTES = 52
VIDEO_WAIT_MAX_SECONDS = 15 * 60  # Extra wait if this morning's video is late
VIDEO_POLL_SECONDS = 60


def sunrise_timelapse_complete_time():
    """
    Calculate the sunrise time and add SUNRISE_BUFFER_MINUTES minutes.
    """
    # Create West Glacier LI object
    wg = LocationInfo(
        name="west glacier",
        region="MT",
        timezone="US/Mountain",
        latitude=48.4950,
        longitude=-113.9811,
    )

    # Create West Glacier sun object
    s = sun.sun(wg.observer, date=date.today(), tzinfo=wg.timezone)

    now = datetime.now(tz=ZoneInfo("America/Denver"))

    # Sunrise time minus now plus buffer for timelapse completion
    timelapse_ready_in = s["sunrise"] - now + timedelta(minutes=SUNRISE_BUFFER_MINUTES)

    return timelapse_ready_in.total_seconds()


def todays_video_published() -> bool:
    """
    Check whether this morning's sunrise video and its thumbnail are both
    listed in the published metadata.
    """
    video_id, _, descriptor = select_video(fetch_glacier_data("timelapse"))
    if not video_id or descriptor != "This Morning's":
        return False
    return (
        find_matching_thumbnail(video_id, fetch_glacier_data("thumbnails")) is not None
    )


def wait_for_todays_video():
    """
    Poll until this morning's video is published, giving up after
    VIDEO_WAIT_MAX_SECONDS so a missing video can't hold up the email.
    """
    waited = 0
    while not todays_video_published():
        if waited >= VIDEO_WAIT_MAX_SECONDS:
            logger.warning(
                "This morning's sunrise video not published after %d minutes; "
                "continuing with the latest available.",
                waited // 60,
            )
            return
        if waited == 0:
            logger.info("This morning's sunrise video not published yet; waiting.")
        sleep(VIDEO_POLL_SECONDS)
        waited += VIDEO_POLL_SECONDS
    if waited:
        logger.info(
            "This morning's sunrise video published after %d minutes.", waited // 60
        )


def sleep_time():
    """
    Sleep the program until sunrise timelapse is complete.
    """

    timelapse_ready_in = sunrise_timelapse_complete_time()

    if timelapse_ready_in > MAX_WAIT_SECONDS:
        logger.warning(
            "Computed sleep time (%d minutes) exceeds maximum wait of %d hours. Skipping sleep.",
            round(timelapse_ready_in / 60),
            MAX_WAIT_SECONDS // 3600,
        )
        return
    if timelapse_ready_in > 0:
        logger.info(
            "Waiting %d minutes for timelapse to finish.",
            round(timelapse_ready_in / 60),
        )
        sleep(timelapse_ready_in)

    wait_for_todays_video()


if __name__ == "__main__":  # pragma: no cover
    sleep_time()
