"""
This module fetches weather forecasts for various locations in Glacier National Park using the Open-Meteo API.
"""

import json
import socket
from dataclasses import dataclass
from datetime import datetime
from math import floor
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlsplit

import certifi
import requests
import requests_cache
import urllib3
from requests.adapters import HTTPAdapter
from requests.exceptions import ConnectionError as RequestsConnectionError
from requests.exceptions import RequestException, Timeout
from urllib3.util.retry import Retry

from shared.datetime_utils import cross_platform_strftime
from shared.logging_config import get_logger

logger = get_logger(__name__)

# Constants
BASE_URL = "https://api.open-meteo.com/v1/forecast"
CACHE_DURATION = 3600  # Cache duration in seconds
RETRY_ATTEMPTS = 5
BACKOFF_FACTOR = 0.2
READ_RETRIES = 2  # Bound retries on read timeouts so a stalled API fails fast
REQUEST_TIMEOUT = (5, 30)  # (connect, read) timeout in seconds
API_HOST = urlsplit(BASE_URL).hostname or "api.open-meteo.com"
# Open-Meteo's DNS answers with one server at a time, and different resolver
# caches hold different answers. When the server our resolver gives us stalls,
# ask Cloudflare's DNS-over-HTTPS (a few times, since its caches disagree) for
# other servers and try those directly.
DOH_URL = "https://cloudflare-dns.com/dns-query"
DOH_TIMEOUT = (5, 10)
DOH_LOOKUPS = 6
MAX_FALLBACK_SERVERS = 2
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


def _system_ips(host: str) -> set[str]:
    """Return the IPv4 addresses the system resolver gives for host."""
    try:
        infos = socket.getaddrinfo(host, 443, socket.AF_INET, socket.SOCK_STREAM)
    except OSError:
        return set()
    return {str(info[4][0]) for info in infos}


def _doh_lookup(host: str) -> list[str]:
    """Look up host's IPv4 addresses once through Cloudflare's DNS-over-HTTPS."""
    response = requests.get(
        DOH_URL,
        params={"name": host, "type": "A"},
        headers={"accept": "application/dns-json"},
        timeout=DOH_TIMEOUT,
    )
    response.raise_for_status()
    return [
        answer["data"]
        for answer in response.json().get("Answer", [])
        if answer.get("type") == 1  # A record
    ]


def _other_server_ips(host: str, exclude: set[str]) -> list[str]:
    """Find host's IPv4 addresses that aren't in exclude, via DNS-over-HTTPS.

    Repeats the lookup up to DOH_LOOKUPS times and stops once one is found.

    Raises:
        RequestException: If every lookup failed.
    """
    found: list[str] = []
    errors = []
    for _ in range(DOH_LOOKUPS):
        try:
            ips = _doh_lookup(host)
        except (RequestException, ValueError) as e:
            errors.append(str(e))
            continue
        found += [ip for ip in ips if ip not in exclude and ip not in found]
        if found:
            break
    if errors and len(errors) == DOH_LOOKUPS:
        raise RequestException(f"DNS-over-HTTPS lookup failed: {errors[-1]}")
    return found


def _fetch_from_ip(ip: str, params: dict[str, Any]) -> Any:
    """GET the forecast from one server address, verifying TLS against API_HOST."""
    pool = urllib3.HTTPSConnectionPool(
        ip,
        port=443,
        server_hostname=API_HOST,
        assert_hostname=API_HOST,
        cert_reqs="CERT_REQUIRED",
        ca_certs=certifi.where(),
        timeout=urllib3.Timeout(connect=REQUEST_TIMEOUT[0], read=REQUEST_TIMEOUT[1]),
        retries=False,
    )
    try:
        response = pool.request(
            "GET",
            f"{urlsplit(BASE_URL).path}?{urlencode(params, doseq=True)}",
            headers={"Host": API_HOST},
        )
    except urllib3.exceptions.HTTPError as e:
        raise RequestException(f"{ip}: {e}") from e
    finally:
        pool.close()
    if response.status != 200:
        raise RequestException(f"{ip}: HTTP {response.status}")
    return json.loads(response.data)


def _fetch_from_fallback_server(params: dict[str, Any]) -> Any:
    """Fetch the forecast from an Open-Meteo server other than the system's pick.

    Raises:
        RequestException: If no other server is found or none of them answer.
    """
    tried = _system_ips(API_HOST)
    others = _other_server_ips(API_HOST, tried)[:MAX_FALLBACK_SERVERS]
    if not others:
        raise RequestException(
            f"No {API_HOST} servers found besides {sorted(tried)} "
            f"in {DOH_LOOKUPS} DNS-over-HTTPS lookups"
        )

    errors = []
    for ip in others:
        try:
            forecasts = _fetch_from_ip(ip, params)
        except RequestException as e:
            errors.append(str(e))
            continue
        logger.warning("Fetched forecast from fallback Open-Meteo server %s", ip)
        return forecasts
    raise RequestException(f"Fallback servers failed: {'; '.join(errors)}")


def _fetch_forecasts(api: WeatherAPI) -> Any:
    """Fetch forecasts, trying another Open-Meteo server if ours stalls."""
    params = api._build_params()
    try:
        response = api.session.get(BASE_URL, params=params, timeout=REQUEST_TIMEOUT)
    except (RequestsConnectionError, Timeout) as e:
        logger.warning(
            "Open-Meteo request failed (%s); trying another server", type(e).__name__
        )
        try:
            return _fetch_from_fallback_server(params)
        except RequestException as fallback_error:
            raise RequestException(f"{e!s}; {fallback_error!s}") from e
    response.raise_for_status()
    return response.json()


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
        forecasts = _fetch_forecasts(api)

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
