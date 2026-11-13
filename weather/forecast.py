"""
This module fetches weather forecasts for various locations in Glacier National Park using the Open-Meteo API.
"""

import json
from dataclasses import dataclass
from datetime import datetime
from math import floor
from pathlib import Path
from typing import Any

import requests_cache
from requests.adapters import HTTPAdapter
from requests.exceptions import RequestException
from urllib3.util.retry import Retry

from shared.datetime_utils import cross_platform_strftime

# Constants
BASE_URL = "https://api.open-meteo.com/v1/forecast"
CACHE_DURATION = 3600  # Cache duration in seconds
RETRY_ATTEMPTS = 5
BACKOFF_FACTOR = 0.2
READ_RETRIES = 2  # Bound retries on read timeouts so a stalled API fails fast
REQUEST_TIMEOUT = (5, 30)  # (connect, read) timeout in seconds
FEET_TO_METERS = 3.281
TIME_FORMAT = "%-I:%M %p"


@dataclass
class Location:
    """Represents a location in Glacier National Park."""

    name: str
    latitude: float
    longitude: float
    altitude: float


class WeatherAPI:
    """Handles interactions with the Open-Meteo API."""

    def __init__(self) -> None:
        """Set up a cached HTTP session and load park locations."""
        self.session = self._setup_session()
        self.locations = self._load_locations()

    @staticmethod
    def _setup_session() -> requests_cache.CachedSession:
        """Set up a cached session with retry capabilities."""
        cache_session = requests_cache.CachedSession(
            ".cache", expire_after=CACHE_DURATION
        )
        retry_strategy = Retry(
            total=RETRY_ATTEMPTS, read=READ_RETRIES, backoff_factor=BACKOFF_FACTOR
        )
        retry_adapter = HTTPAdapter(max_retries=retry_strategy)

        cache_session.mount("http://", retry_adapter)
        cache_session.mount("https://", retry_adapter)
        return cache_session

    @staticmethod
    def _load_locations() -> list[Location]:
        """Load location data."""
        locations_data = {
            "West Glacier": {
                "latitude": 48.50228,
                "longitude": -113.98202,
                "altitude": 3218,
            },
            "Logan Pass": {
                "latitude": 48.69567,
                "longitude": -113.71656,
                "altitude": 6626,
            },
            "St. Mary": {
                "latitude": 48.74746,
                "longitude": -113.43877,
                "altitude": 4500,
            },
            "Two Medicine": {
                "latitude": 48.48480,
                "longitude": -113.36981,
                "altitude": 5174,
            },
            "Many Glacier": {
                "latitude": 48.79664,
                "longitude": -113.65773,
                "altitude": 4895,
            },
            "Polebridge": {
                "latitude": 48.78348,
                "longitude": -114.28056,
                "altitude": 3560,
            },
        }

        return [Location(name=name, **data) for name, data in locations_data.items()]

    def _build_params(self) -> dict[str, Any]:
        """Build parameters for the API request."""
        return {
            "latitude": [loc.latitude for loc in self.locations],
            "longitude": [loc.longitude for loc in self.locations],
            "daily": [
                "weather_code",
                "temperature_2m_max",
                "temperature_2m_min",
                "sunrise",
                "sunset",
                "daylight_duration",
            ],
            "temperature_unit": "fahrenheit",
            "wind_speed_unit": "mph",
            "timezone": ["America/Denver"] * len(self.locations),
            "forecast_days": 1,
            "elevation": [loc.altitude / FEET_TO_METERS for loc in self.locations],
        }

    def _fetch_weather_codes(self) -> dict[str, dict[str, dict[str, str]]]:
        """Load weather code descriptions."""
        try:
            with open(Path("weather/descriptions.json"), encoding="utf8") as desc:
                return json.load(desc)
        except FileNotFoundError as e:
            raise FileNotFoundError(
                "Weather descriptions file not found. Ensure 'weather/descriptions.json' exists."
            ) from e

    def _format_daylight_info(self, forecast_data: list) -> str:
        """Format daylight information string."""
        wg = forecast_data[0]["daily"]

        sunrise = cross_platform_strftime(
            datetime.fromisoformat(wg["sunrise"][0]), TIME_FORMAT
        ).lower()
        sunset = cross_platform_strftime(
            datetime.fromisoformat(wg["sunset"][0]), TIME_FORMAT
        ).lower()

        duration_hours = floor(wg["daylight_duration"][0] / (60 * 60))
        duration_minutes = floor(
            (wg["daylight_duration"][0] - (duration_hours * 60 * 60)) / 60
        )

        return (
            f"Today, sunrise is at {sunrise} and sunset is at {sunset}. "
            f"There will be {duration_hours} hours and {duration_minutes} minutes of daylight.\n"
        )

    def _process_forecasts(
        self,
        forecasts: list[dict[str, Any]],
        weather_codes: dict[str, dict[str, dict[str, str]]],
    ) -> list[tuple[str, int, int, str]]:
        """Process forecast data into required format."""

        results = []
        for location, forecast in zip(self.locations, forecasts, strict=True):
            daily = forecast["daily"]
            low = round(daily["temperature_2m_min"][0])
            high = round(daily["temperature_2m_max"][0])
            code = daily["weather_code"][0]
            condition = weather_codes[str(code)]["day"]["description"].lower()
            results.append((location.name, high, low, condition))
        return results


def get_forecast() -> tuple[list[tuple[str, int, int, str]], str]:
    """
    Fetch weather forecasts for various locations in Glacier National Park.

    Returns:
        Tuple[List[Tuple[str, int, int, str]], str]: A tuple containing:
            - List of tuples with (location_name, high_temp, low_temp, condition)
            - String describing the daylight duration

    Raises:
        FileNotFoundError: If weather descriptions file is not found
        requests.RequestException: If API request fails
        json.JSONDecodeError: If API response is invalid
    """
    api = WeatherAPI()

    try:
        response = api.session.get(
            BASE_URL, params=api._build_params(), timeout=REQUEST_TIMEOUT
        )
        response.raise_for_status()
        forecasts = response.json()

        weather_codes = api._fetch_weather_codes()
        results = api._process_forecasts(forecasts, weather_codes)
        length_str = api._format_daylight_info(forecasts)

        return results, length_str
    except RequestException as e:
        raise RequestException(f"Failed to fetch weather data: {e!s}") from e
    except json.JSONDecodeError as e:
        raise json.JSONDecodeError(
            f"Failed to parse API response: {e!s}", e.doc, e.pos
        ) from e
    except (KeyError, IndexError, TypeError) as e:
        raise type(e)(f"Unexpected forecast response structure: {e}") from e


if __name__ == "__main__":  # pragma: no cover
    results, length_str = get_forecast()
    print(results, length_str)
