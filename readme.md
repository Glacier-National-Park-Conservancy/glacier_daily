# Glacier Daily Update Emails

[![codecov](https://codecov.io/github/amerorchis/glacier_daily/graph/badge.svg?token=JS85YV7E68)](https://codecov.io/github/amerorchis/glacier_daily) [![Tests](https://github.com/amerorchis/glacier_daily/actions/workflows/tests.yml/badge.svg)](https://github.com/amerorchis/glacier_daily/actions/workflows/tests.yml) [![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)

An automated system that generates and distributes daily email updates about conditions at Glacier National Park. The system aggregates data from multiple sources and delivers personalized updates to subscribers through the Drip email platform. A web version is also generated and updated throughout the day.

**View the live emails [here](https://glacier.org/glacier-daily-updates-signup/#iFrame1).**

## Features

- Real-time park conditions and updates
- Weather forecasts, alerts, air quality, and sunset quality predictions
- Trail and road status tracking
- Daily featured peak with satellite imagery
- Sunrise timelapse videos
- Evening sunset timelapse email
- Ranger-led activity schedules
- Featured product and image of the day
- Aurora forecast
- Automated retry on failed email runs

## Quick Start (For Users)

### Prerequisites
- Python 3.11
- [uv](https://docs.astral.sh/uv/) package manager
- API keys and FTP access (see Configuration)

### Installation
```bash
# Clone and setup
git clone [repository-url]
cd glacier_daily
uv sync

# Create required directory for daily images
mkdir -p email_images/today/

# Configure environment variables (see Configuration section)
```

### Usage
```bash
# Run daily update (production)
uv run python main.py

# Run in test mode (test subscribers only)
uv run python main.py --tag "Test Glacier Daily Update"

# Force re-fetch all data (clear cache)
uv run python main.py --force

# Send the sunset timelapse email (invoked by the timelapse system each
# evening after it uploads the sunset video — not run by cron here)
uv run python main.py --sunset

# Sunset test mode (test subscribers only)
uv run python main.py --sunset --tag "Test Sunset Timelapse"

# Generate and upload data only (no email sending)
uv run python generate_and_upload.py

# Generate data with health check (validate data + FTP connectivity)
uv run python generate_and_upload.py --check

# Check if today's email succeeded; retry if not
uv run python retry_check.py
uv run python retry_check.py --dry-run  # log what would happen without acting
```

### Configuration

The project uses two configuration files:

**`email.env`** — API keys and secrets (required). Copy from the template and fill in credentials:

```bash
cp email.template.env email.env
# Edit email.env with your actual API keys and credentials
```

**`config.env`** — Machine-local settings (optional, sensible defaults provided):

```bash
cp config.template.env config.env
# Edit if needed (ENVIRONMENT, FTP_SERVER, etc.)
```

See `email.template.env` and `config.template.env` for all available variables. Secrets are managed via SOPS + age — see `.sops.yaml` for setup.

---

## Contributing & Development

### Development Setup

1. **Install development dependencies:**
   ```bash
   uv sync --extra dev
   ```

2. **Install pre-commit hooks:**
   ```bash
   uv run pre-commit install
   ```

3. **Set up Google Sheets API:**
   - Create Google service account JSON and update file location.

### Code Quality

This project uses automated code formatting and quality checks:

- **Ruff** for Python code formatting, linting, and import sorting
- **ty** for type checking
- **Pre-commit hooks** for automated checks

```bash
# Format code manually
uv run ruff format .

# Run all quality checks
uv run pre-commit run --all-files
```

### Testing

Tests use a pytest fixture (`_reset_settings` in `conftest.py`) that monkeypatches all settings to empty strings, so no real credentials are needed:

```bash
# Run full test suite with coverage
uv run pytest test/ --cov=. --cov-report xml

# Run specific tests
uv run pytest test/weather/test_weather.py
uv run pytest test/weather/
```

### Running Individual Modules

With the package structure, run modules as:
```bash
# Run specific module
uv run python -m peak.peak
uv run python -m weather.weather

# Suppress urllib3 SSL warnings on macOS (harmless)
uv run python -W ignore -m peak.peak
```

### Architecture Overview

**Data Collection:** Runs each module in its own daemon thread to gather data from multiple APIs concurrently. Collection has a shared 5-minute deadline (`MODULE_TIMEOUT_SECS`): a module still running then is abandoned and falls back to today's LKG data or its empty default, so one hung request can't delay the email.

**Entry Points:**
- `main.py` - Full pipeline: data collection, FTP upload, email delivery
- `main.py --sunset` - Sunset email pipeline: regenerates/uploads email.json and fires the "Sunset Timelapse trigger" Drip event to subscribers tagged "Sunset Timelapse". Invoked by the separate timelapse system each evening after the sunset video uploads. Today-or-nothing: if tonight's sunset video isn't in the timelapse feed, no email is sent. Tag and event names live in `shared/constants.py`; the Drip workflow email uses `email_html/sunset_template.html`
- `generate_and_upload.py` - Data collection and FTP upload only (used for web version updates)
- `retry_check.py` - Cron-driven retry checker: retriggers `main.py` if today's email hasn't gone out (a partial run that already delivered emails does not retrigger)
- `web_version.py` - Generates the web version of the daily update via Liquid-to-Jinja2 template rendering

**Data Source Modules:**
- `weather/` - Weather forecasts, alerts, air quality, aurora predictions, sunset quality
- `activities/` - Park ranger events from NPS and GNPC
- `roads/` - Road closures and hiker/biker access
- `trails_and_cgs/` - Trail closures and campground availability
- `peak/` - Daily featured peak with satellite imagery
- `image_otd/` - Daily image selection from Flickr
- `product_otd/` - Featured product from Shopify
- `sunrise_timelapse/` - Video processing and timelapse compilation
- `sunset_timelapse/` - Tonight's sunset video/thumbnail selection for the sunset email
- `notices/` - Administrative notices from Google Sheets

**Infrastructure:**
- `drip/` - Email delivery, subscriber management, and canary delivery verification
- `shared/` - FTP operations, retry mechanisms, centralized settings, config validation, logging, datetime utilities, LKG fallback cache, timing instrumentation, run reporting, process locking
- `email_html/` - Email templates (Liquid syntax, with Jinja2 compatibility layer)
- `server/` - Generated HTML files for web display and status dashboard
- `frontend_files/` - JavaScript and PHP for the web interface

**Data Flow:**
1. **Collection**: Concurrent API calls gather real-time data
2. **Processing**: Format data for HTML email templates
3. **Distribution**: Upload to glacier.org via FTP, purge Cloudflare cache, and trigger emails through Drip

### Error Handling & Data Reliability

The system uses a custom retry decorator (`shared/retry.py`) with exponential backoff for API calls. When a module fails despite retries, a Last Known Good (LKG) cache (`shared/lkg_cache.py`) provides today's most recent successful output as a fallback, so subscribers receive an email with stale-but-valid data rather than missing sections. Date-deterministic modules (peak, image of the day, product) are cached after first successful fetch each day; dynamic modules (weather, roads, trails, etc.) always attempt a fresh fetch with LKG as a safety net.

Process-level locking (`shared/lock.py`) prevents concurrent runs. A status dashboard (`server/status.html`) tracks run history, per-module timing, and error details.

---

## Credits

Made by Andrew Smith for the Glacier National Park Conservancy, 2023.
