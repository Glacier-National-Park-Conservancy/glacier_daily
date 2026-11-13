import json
from unittest.mock import Mock, mock_open, patch

import pytest
from requests.exceptions import RequestException

from weather.forecast import (
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


def test_location_dataclass():
    """Test Location dataclass."""
    location = Location("Test", 48.0, -113.0, 3000)
    assert location.name == "Test"
    assert location.latitude == 48.0
    assert location.longitude == -113.0
    assert location.altitude == 3000


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__])
