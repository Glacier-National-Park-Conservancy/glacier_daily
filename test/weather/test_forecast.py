import json
from unittest.mock import Mock, mock_open, patch

import pytest
import urllib3
from requests.exceptions import ConnectionError as RequestsConnectionError
from requests.exceptions import RequestException

from weather import forecast
from weather.forecast import (
    API_HOST,
    DOH_LOOKUPS,
    READ_RETRIES,
    REQUEST_TIMEOUT,
    Location,
    WeatherAPI,
    get_forecast,
)

# Test data
SAMPLE_LOCATION = Location(
    name="Test Location", latitude=48.50228, longitude=-113.98202, altitude=3218
)

MOCK_WEATHER_CODES = {"0": {"day": {"description": "Sunny"}}}

MOCK_API_RESPONSE = {
    "daily": {
        "temperature_2m_min": [35.5],
        "temperature_2m_max": [75.2],
        "weather_code": [0],
        "sunrise": ["2025-01-14T07:30:00"],
        "sunset": ["2025-01-14T17:30:00"],
        "daylight_duration": [36000],  # 10 hours in seconds
    }
}


@pytest.fixture
def weather_api():
    """Create a WeatherAPI instance with mocked session."""
    with patch("requests_cache.CachedSession") as mock_session:
        api = WeatherAPI()
        api.session = mock_session
        api.locations = [SAMPLE_LOCATION]
        yield api


@pytest.fixture
def mock_weather_codes_file():
    """Mock the weather codes JSON file."""
    with patch("builtins.open", mock_open(read_data=json.dumps(MOCK_WEATHER_CODES))):
        yield


class TestWeatherAPI:
    def test_init(self):
        """Test WeatherAPI initialization."""
        api = WeatherAPI()
        assert isinstance(api.locations, list)
        assert all(isinstance(loc, Location) for loc in api.locations)
        assert len(api.locations) > 0

    def test_session_bounds_read_retries(self):
        """Test the retry strategy limits retries on read timeouts."""
        session = WeatherAPI._setup_session()
        retries = session.get_adapter("https://api.open-meteo.com").max_retries
        assert retries.read == READ_RETRIES

    def test_load_locations(self):
        """Test loading of location data."""
        locations = WeatherAPI._load_locations()
        assert isinstance(locations, list)
        assert all(isinstance(loc, Location) for loc in locations)
        assert "West Glacier" in [loc.name for loc in locations]

    def test_build_params(self, weather_api):
        """Test API parameters building."""
        params = weather_api._build_params()
        assert "latitude" in params
        assert "longitude" in params
        assert "daily" in params
        assert "temperature_unit" in params
        assert params["temperature_unit"] == "fahrenheit"
        assert len(params["latitude"]) == len(weather_api.locations)

    def test_format_daylight_info(self, weather_api):
        """Test daylight information formatting."""
        mock_data = [MOCK_API_RESPONSE]
        result = weather_api._format_daylight_info(mock_data)
        assert "sunrise is at 7:30 am" in result
        assert "sunset is at 5:30 pm" in result
        assert "10 hours" in result
        assert "0 minutes" in result

    def test_process_forecasts(self, weather_api, mock_weather_codes_file):
        """Test forecast data processing."""
        mock_forecasts = [MOCK_API_RESPONSE]
        results = weather_api._process_forecasts(mock_forecasts, MOCK_WEATHER_CODES)
        assert isinstance(results, list)
        assert all(isinstance(item, tuple) for item in results)
        assert len(results) == 1
        _location, high, low, condition = results[0]
        assert isinstance(high, int)
        assert isinstance(low, int)
        assert condition.lower() == "sunny"

    @patch("pathlib.Path.open", mock_open(read_data=json.dumps(MOCK_WEATHER_CODES)))
    def test_fetch_weather_codes(self, weather_api):
        """Test weather codes fetching."""
        codes = weather_api._fetch_weather_codes()
        assert isinstance(codes, dict)
        assert "0" in codes
        assert "description" in codes["0"]["day"]

    def test_fetch_weather_codes_file_not_found(self, weather_api):
        """Test weather codes file not found error."""
        with patch("builtins.open", side_effect=FileNotFoundError("File not found")):
            with pytest.raises(FileNotFoundError) as exc_info:
                weather_api._fetch_weather_codes()
            assert "Weather descriptions file not found" in str(exc_info.value)


@patch("requests_cache.CachedSession")
class TestGetForecast:
    """Test the main get_forecast function."""

    def test_successful_forecast(self, mock_session, mock_weather_codes_file):
        """Test successful forecast retrieval."""
        mock_response = Mock()
        mock_response.json.return_value = [MOCK_API_RESPONSE]
        mock_session.return_value.get.return_value = mock_response

        with patch.object(
            WeatherAPI, "_load_locations", return_value=[SAMPLE_LOCATION]
        ):
            results, length_str = get_forecast()
        assert isinstance(results, list)
        assert isinstance(length_str, str)
        assert all(isinstance(item, tuple) for item in results)
        assert "sunrise" in length_str
        assert "sunset" in length_str

    def test_request_uses_timeout(self, mock_session, mock_weather_codes_file):
        """Test the API request sets a timeout so a stalled server can't hang."""
        mock_response = Mock()
        mock_response.json.return_value = [MOCK_API_RESPONSE]
        mock_session.return_value.get.return_value = mock_response

        with patch.object(
            WeatherAPI, "_load_locations", return_value=[SAMPLE_LOCATION]
        ):
            get_forecast()
        assert mock_session.return_value.get.call_args.kwargs["timeout"] == (
            REQUEST_TIMEOUT
        )

    def test_api_error(self, mock_session):
        """Test API error handling."""
        mock_session.return_value.get.side_effect = RequestException("API Error")

        with pytest.raises(RequestException):
            get_forecast()

    def test_invalid_json_response(self, mock_session):
        """Test invalid JSON response handling."""
        mock_response = Mock()
        mock_response.json.side_effect = json.JSONDecodeError("Invalid JSON", "", 0)
        mock_session.return_value.get.return_value = mock_response

        with pytest.raises(json.JSONDecodeError):
            get_forecast()


@patch("requests_cache.CachedSession")
class TestFallbackServer:
    """get_forecast tries another Open-Meteo server when ours stalls."""

    @pytest.fixture(autouse=True)
    def one_location(self):
        with patch.object(
            WeatherAPI, "_load_locations", return_value=[SAMPLE_LOCATION]
        ):
            yield

    def test_uses_other_server_after_connection_error(
        self, mock_session, mock_weather_codes_file
    ):
        mock_session.return_value.get.side_effect = RequestsConnectionError("stall")
        with (
            patch.object(forecast, "_system_ips", return_value={"1.1.1.1"}),
            patch.object(
                forecast, "_doh_lookup", side_effect=[["1.1.1.1"], ["2.2.2.2"]]
            ) as lookup,
            patch.object(
                forecast, "_fetch_from_ip", return_value=[MOCK_API_RESPONSE]
            ) as fetch,
        ):
            results, length_str = get_forecast()

        assert results == [("Test Location", 75, 36, "sunny")]
        assert "sunrise" in length_str
        assert lookup.call_count == 2  # stops once another server turns up
        assert fetch.call_args.args[0] == "2.2.2.2"

    def test_raises_when_no_other_server_found(self, mock_session):
        mock_session.return_value.get.side_effect = RequestsConnectionError("stall")
        with (
            patch.object(forecast, "_system_ips", return_value={"1.1.1.1"}),
            patch.object(forecast, "_doh_lookup", return_value=["1.1.1.1"]) as lookup,
            patch.object(forecast, "_fetch_from_ip") as fetch,
            pytest.raises(RequestException, match=r"No api\.open-meteo\.com servers"),
        ):
            get_forecast()
        assert lookup.call_count == DOH_LOOKUPS
        fetch.assert_not_called()

    def test_raises_when_fallback_servers_fail(self, mock_session):
        mock_session.return_value.get.side_effect = RequestsConnectionError("stall")
        with (
            patch.object(forecast, "_system_ips", return_value=set()),
            patch.object(forecast, "_doh_lookup", return_value=["2.2.2.2", "3.3.3.3"]),
            patch.object(
                forecast, "_fetch_from_ip", side_effect=RequestException("down")
            ) as fetch,
            pytest.raises(RequestException, match="Fallback servers failed"),
        ):
            get_forecast()
        assert fetch.call_count == 2

    def test_raises_when_doh_lookups_all_fail(self, mock_session):
        mock_session.return_value.get.side_effect = RequestsConnectionError("stall")
        with (
            patch.object(forecast, "_system_ips", return_value=set()),
            patch.object(
                forecast, "_doh_lookup", side_effect=RequestException("no dns")
            ),
            pytest.raises(RequestException, match="DNS-over-HTTPS lookup failed"),
        ):
            get_forecast()

    def test_http_error_does_not_use_fallback(self, mock_session):
        """A server that answers with an error isn't stalled; don't go around it."""
        response = Mock()
        response.raise_for_status.side_effect = RequestException("503")
        mock_session.return_value.get.return_value = response
        with (
            patch.object(forecast, "_doh_lookup") as lookup,
            pytest.raises(RequestException, match="503"),
        ):
            get_forecast()
        lookup.assert_not_called()


class TestFetchFromIp:
    @pytest.fixture
    def pool(self):
        with patch.object(urllib3, "HTTPSConnectionPool") as pool_cls:
            yield pool_cls

    def test_verifies_tls_against_api_host(self, pool):
        pool.return_value.request.return_value = Mock(
            status=200, data=json.dumps([MOCK_API_RESPONSE]).encode()
        )
        result = forecast._fetch_from_ip("2.2.2.2", {"latitude": [1, 2]})

        assert result == [MOCK_API_RESPONSE]
        args, kwargs = pool.call_args
        assert args[0] == "2.2.2.2"
        assert kwargs["server_hostname"] == API_HOST
        assert kwargs["assert_hostname"] == API_HOST
        assert kwargs["cert_reqs"] == "CERT_REQUIRED"
        _, url = pool.return_value.request.call_args.args
        assert url == "/v1/forecast?latitude=1&latitude=2"
        assert pool.return_value.request.call_args.kwargs["headers"] == {
            "Host": API_HOST
        }
        pool.return_value.close.assert_called_once()

    def test_non_200_raises(self, pool):
        pool.return_value.request.return_value = Mock(status=503, data=b"")
        with pytest.raises(RequestException, match="HTTP 503"):
            forecast._fetch_from_ip("2.2.2.2", {})

    def test_urllib3_error_raises_request_exception(self, pool):
        pool.return_value.request.side_effect = urllib3.exceptions.ReadTimeoutError(
            None, "/", "timed out"
        )
        with pytest.raises(RequestException, match=r"2\.2\.2\.2"):
            forecast._fetch_from_ip("2.2.2.2", {})


def test_doh_lookup_returns_a_records():
    response = Mock()
    response.json.return_value = {
        "Answer": [
            {"type": 5, "data": "alias.example."},
            {"type": 1, "data": "2.2.2.2"},
        ]
    }
    with patch.object(forecast.requests, "get", return_value=response) as get:
        assert forecast._doh_lookup(API_HOST) == ["2.2.2.2"]
    assert get.call_args.kwargs["params"] == {"name": API_HOST, "type": "A"}


def test_system_ips_handles_resolution_failure():
    with patch.object(forecast.socket, "getaddrinfo", side_effect=OSError):
        assert forecast._system_ips(API_HOST) == set()


def test_location_dataclass():
    """Test Location dataclass."""
    location = Location("Test", 48.0, -113.0, 3000)
    assert location.name == "Test"
    assert location.latitude == 48.0
    assert location.longitude == -113.0
    assert location.altitude == 3000


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__])
