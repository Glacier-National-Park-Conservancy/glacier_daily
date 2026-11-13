#!/home/pi/.local/bin/uv run python
"""
Generate all of the data in parallel threads, then upload it to the glacier.org
server with FTP.
"""

import argparse
import concurrent.futures
import json
import os
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from dataclasses import fields as dataclass_fields
from time import sleep
from typing import Any

import requests

from activities.events import events_today
from activities.gnpc_events import get_gnpc_events
from image_otd.image_otd import get_image_otd, prepare_pic_otd
from notices.notices import get_notices
from peak.peak import peak
from peak.sat import prepare_peak_upload
from product_otd.product import get_product, prepare_potd_upload
from roads.hiker_biker import get_hiker_biker_status
from roads.roads import get_road_status
from shared.data_types import (
    AlertBullet,
    CampgroundsResult,
    Event,
    EventsResult,
    HikerBikerResult,
    NoticesResult,
    RoadsResult,
    TrailsResult,
    WeatherResult,
)
from shared.datetime_utils import (
    cross_platform_strftime,
    format_date_readable,
    now_mountain,
)
from shared.ftp import FTPSession
from shared.lkg_cache import LKGCache
from shared.logging_config import get_logger, setup_logging
from shared.run_context import start_run
from shared.run_report import complete_run
from shared.settings import get_settings
from shared.timing import get_timing, timed
from sunrise_timelapse.get_timelapse import process_video
from sunset_timelapse.get_timelapse import process_sunset_video
from trails_and_cgs.frontcountry_cgs import get_campground_status
from trails_and_cgs.trails import get_closed_trails
from weather.weather import weather_data
from weather.weather_img import prepare_weather_upload, weather_image
from web_version import web_version

logger = get_logger(__name__)


@dataclass(frozen=True)
class ModuleSpec:
    """Declares how one data module plugs into gen_data().

    Adding a module means adding one entry to MODULES — the fetch,
    timing, LKG caching, and template-field assembly are all driven
    from this spec.
    """

    name: str  # LKG module name and timing label
    func: Callable[..., Any]  # module entrypoint; re-resolved by name at submit time
    fields: tuple[str, ...]  # template field keys, in result-tuple order
    default_factory: Callable[[], Any]  # result when fetch and LKG both fail
    # Date-deterministic modules treat LKG as a primary cache: if today's
    # data exists, the fetch is skipped entirely. Dynamic modules always
    # fetch fresh and use LKG only as a fallback (lkg_fallback).
    date_deterministic: bool = False
    lkg_fallback: bool = True
    # Extra template fields saved under this module's LKG entry that the
    # module callable itself doesn't produce (e.g. weather_image)
    extra_lkg_keys: tuple[str, ...] = ()


MODULES: tuple[ModuleSpec, ...] = (
    # Weather's LKG fallback is handled after the pool drains (not via
    # lkg_fallback) because the derived weather_image field and the
    # pending-uploads list must stay in sync with it.
    ModuleSpec(
        "weather",
        weather_data,
        ("weather",),
        WeatherResult,
        lkg_fallback=False,
        extra_lkg_keys=("weather_image",),
    ),
    ModuleSpec("trails", get_closed_trails, ("trails",), TrailsResult),
    ModuleSpec(
        "campgrounds", get_campground_status, ("campgrounds",), CampgroundsResult
    ),
    ModuleSpec("roads", get_road_status, ("roads",), RoadsResult),
    ModuleSpec(
        "hiker_biker", get_hiker_biker_status, ("hikerbiker",), HikerBikerResult
    ),
    ModuleSpec("events", events_today, ("events",), EventsResult),
    ModuleSpec(
        "sunrise",
        process_video,
        ("sunrise_vid", "sunrise_still", "sunrise_str"),
        lambda: ("", "", ""),
    ),
    # Sunset fields are blank until the timelapse system publishes
    # tonight's video (evenings) — template guards hide the section
    ModuleSpec(
        "sunset",
        process_sunset_video,
        ("sunset_vid", "sunset_still", "sunset_str"),
        lambda: ("", "", ""),
    ),
    ModuleSpec("notices", get_notices, ("notices",), NoticesResult),
    # Each module's caching behavior (primary cache vs. fallback vs. none)
    # is deliberate — don't change these flags without checking first
    ModuleSpec(
        "gnpc_events", get_gnpc_events, ("gnpc-events",), list, lkg_fallback=False
    ),
    # Date-deterministic modules use LKG as a primary cache (checked before
    # the fetch), not as a failure fallback — on a cache miss + fetch failure
    # they return their empty defaults
    ModuleSpec(
        "image_otd",
        get_image_otd,
        ("image_otd", "image_otd_title", "image_otd_link"),
        lambda: ("", "", ""),
        date_deterministic=True,
        lkg_fallback=False,
    ),
    ModuleSpec(
        "peak",
        peak,
        ("peak", "peak_image", "peak_map"),
        lambda: ("", None, ""),
        date_deterministic=True,
        lkg_fallback=False,
    ),
    ModuleSpec(
        "product",
        get_product,
        ("product_title", "product_image", "product_link", "product_desc"),
        lambda: ("", None, "", ""),
        date_deterministic=True,
        lkg_fallback=False,
    ),
)

# Maps every template field key to the LKG module that owns it
# (serve_api uses this to save resolved image URLs after upload)
_FIELD_TO_MODULE = {
    key: spec.name for spec in MODULES for key in (*spec.fields, *spec.extra_lkg_keys)
}

# Template fields holding dataclasses, which LKG stores as JSON text
_FIELD_DATACLASSES: dict[str, type] = {
    "weather": WeatherResult,
    "events": EventsResult,
    "trails": TrailsResult,
    "campgrounds": CampgroundsResult,
    "roads": RoadsResult,
    "hikerbiker": HikerBikerResult,
    "notices": NoticesResult,
}

_CACHE_PURGE_PROPAGATION_SECS = 3

# Wall-clock budget for collecting every module's data. A module still
# running at the deadline is abandoned and falls back like a failure
# (today's LKG, else its empty default).
MODULE_TIMEOUT_SECS = 300


def _is_substantive(value) -> bool:
    """Check if a value has meaningful content worth caching.

    Empty dataclass instances (all fields falsy) are considered non-substantive
    to prevent failed module defaults from overwriting good cached data.
    """
    if not value:
        return False
    if hasattr(value, "__dataclass_fields__"):
        return any(getattr(value, f) for f in value.__dataclass_fields__)
    return True


def _save_module_lkg(module_name, data):
    """Save successful module output to LKG cache.

    Dataclass and list values are serialized to JSON strings for storage.
    """
    try:
        cache = LKGCache.get_cache()
        serialized = {}
        for k, v in data.items():
            if not v:
                continue
            if hasattr(v, "__dataclass_fields__"):
                serialized[k] = json.dumps(_serialize_value(v))
            elif isinstance(v, str):
                serialized[k] = v
            elif isinstance(v, list | dict):
                serialized[k] = json.dumps(v, default=str)
            else:
                serialized[k] = str(v)
        if serialized:
            cache.save(module_name, serialized)
    except Exception:
        # A broken cache means fallbacks silently stop working — make it visible
        logger.warning("Failed to save LKG for %s", module_name, exc_info=True)


def _load_module_lkg(module_name, keys):
    """Load today's LKG data for a module, or None."""
    try:
        return LKGCache.get_cache().load(module_name, keys)
    except Exception:
        logger.warning("Failed to load LKG for %s", module_name, exc_info=True)
        return None


def _dataclass_from_dict(cls: type, payload: dict) -> Any:
    """Rebuild a result dataclass from its asdict() representation."""
    if cls is EventsResult:
        payload["events"] = [Event(**e) for e in payload.get("events", [])]
    elif cls is WeatherResult:
        payload["forecasts"] = [tuple(f) for f in payload.get("forecasts", [])]
        payload["alerts"] = [AlertBullet(**a) for a in payload.get("alerts", [])]
    names = {f.name for f in dataclass_fields(cls)}
    return cls(**{k: v for k, v in payload.items() if k in names})


def _deserialize_field(key: str, value: Any) -> Any:
    """Convert an LKG-stored string back to the field's runtime type.

    The LKG cache stores everything as TEXT: dataclasses and lists as
    JSON, plain string fields verbatim. Templates need the structured
    types back — handing them a JSON string renders as an empty section.
    """
    if not isinstance(value, str):
        return value
    cls = _FIELD_DATACLASSES.get(key)
    if cls is not None:
        try:
            return _dataclass_from_dict(cls, json.loads(value))
        except (ValueError, TypeError):
            logger.warning("Could not deserialize LKG value for '%s'", key)
            return cls()
    if key == "gnpc-events":
        try:
            return json.loads(value)
        except ValueError:
            logger.warning("Could not deserialize LKG value for '%s'", key)
            return []
    return value


def _start_module(name, func, *args, **kwargs) -> concurrent.futures.Future:
    """Run a module in a daemon thread, with timing, and return its Future.

    Daemon threads rather than a ThreadPoolExecutor so a module that
    hangs past MODULE_TIMEOUT_SECS can be abandoned: an executor's
    shutdown (and interpreter exit) joins its worker threads, so one
    stuck request would hold the run — and the email — until it returned.
    Python can't kill a thread; an abandoned daemon thread just stops
    when the process exits. Modules only fetch data (uploads happen
    later, in the main thread), so a late finish has no side effects.
    """
    future: concurrent.futures.Future = concurrent.futures.Future()
    timed_func = timed(name)(func)

    def run() -> None:
        # Nothing cancels these futures, so this always marks it running
        future.set_running_or_notify_cancel()
        try:
            result = timed_func(*args, **kwargs)
        except BaseException as e:
            future.set_exception(e)
        else:
            future.set_result(result)

    threading.Thread(target=run, name=f"module-{name}", daemon=True).start()
    return future


def _safe_result(future, name, default, *, deadline, lkg_keys=None):
    """Safely get a future's result, falling back to LKG then default.

    ``deadline`` is a time.monotonic() value shared by every module in
    the run, so the whole collection step is bounded by
    MODULE_TIMEOUT_SECS rather than each wait getting a fresh budget.
    """
    try:
        return future.result(timeout=max(0.0, deadline - time.monotonic()))
    except Exception as e:
        if not future.done():
            # Abandon it: the run continues on LKG/defaults, and its
            # timing entry stays an error even if it finishes later
            logger.error("Module '%s' timed out after %ds", name, MODULE_TIMEOUT_SECS)
            get_timing().abandon(
                name,
                MODULE_TIMEOUT_SECS,
                f"timed out after {MODULE_TIMEOUT_SECS}s",
            )
        else:
            logger.exception("Module '%s' failed: %s", name, e)
        if lkg_keys:
            lkg_data = _load_module_lkg(name, lkg_keys)
            if lkg_data:
                logger.info("Using LKG fallback for '%s'", name)
                values = tuple(
                    _deserialize_field(k, lkg_data.get(k, "")) for k in lkg_keys
                )
                return values[0] if len(values) == 1 else values
        return default


def gen_data() -> tuple[dict, list]:
    """
    Use threads to gather the data from every module, then store it in a dictionary.
    Make sure text is all HTML safe, then return it.

    Date-deterministic modules (peak, image_otd, product) are checked in
    the LKG cache first and skipped if today's data already exists.
    Dynamic modules always fetch fresh data, with LKG as a fallback on
    failure.

    Image uploads are always deferred — callers handle uploading via the
    returned pending_uploads list so the FTP connection is only opened
    after all data collection is complete.

    Returns:
        tuple: (drip_template_fields dict, pending_uploads list)
            pending_uploads contains (field_key, upload_args) tuples for
            images that still need to be uploaded via FTPSession.upload().
    """

    # Date-deterministic modules: LKG is a primary cache, skip fetch on a hit
    cached: dict[str, dict[str, str]] = {}
    for spec in MODULES:
        if not spec.date_deterministic:
            continue
        lkg_data = _load_module_lkg(spec.name, list(spec.fields))
        if lkg_data:
            cached[spec.name] = lkg_data
            logger.info("Using cached data for '%s' (date-deterministic)", spec.name)

    fields: dict[str, Any] = {}
    futures = {}
    module_globals = globals()
    for spec in MODULES:
        if spec.name in cached:
            continue
        # Re-resolved by name at submit time (not registry-build time)
        # so tests can monkeypatch the module-level callables
        func_name = getattr(spec.func, "__name__", "")
        func = module_globals.get(func_name, spec.func)
        kwargs = {"skip_upload": True} if spec.date_deterministic else {}
        futures[spec.name] = _start_module(spec.name, func, **kwargs)

    deadline = time.monotonic() + MODULE_TIMEOUT_SECS
    for spec in MODULES:
        if spec.name in cached:
            values = tuple(cached[spec.name].get(k, "") for k in spec.fields)
        else:
            lkg_keys = list(spec.fields) if spec.lkg_fallback else None
            result = _safe_result(
                futures[spec.name],
                spec.name,
                spec.default_factory(),
                lkg_keys=lkg_keys,
                deadline=deadline,
            )
            values = result if len(spec.fields) > 1 else (result,)
        fields.update(zip(spec.fields, values, strict=True))

    weather = fields["weather"]
    fields["weather_image"] = (
        weather_image(weather.forecasts, skip_upload=True)
        if weather.forecasts
        else None
    )

    # Collect deferred image uploads for the caller to process
    pending_uploads = []
    if fields["image_otd"] is None:
        pending_uploads.append(("image_otd", prepare_pic_otd()))
    if fields["peak_image"] is None:
        pending_uploads.append(("peak_image", prepare_peak_upload()))
    if fields["product_image"] is None:
        pending_uploads.append(("product_image", prepare_potd_upload()))
    if fields["weather_image"] is None:
        pending_uploads.append(("weather_image", prepare_weather_upload()))

    # LKG: Apply weather fallback if weather module failed. Handled here
    # rather than via lkg_fallback so the cached weather_image URL can
    # replace the pending weather-image upload.
    if not fields["weather"].daylight_message:
        lkg = _load_module_lkg("weather", ["weather", "weather_image"])
        if lkg:
            logger.info("Using LKG fallback for 'weather'")
            fields["weather"] = _deserialize_field("weather", lkg["weather"])
            if lkg.get("weather_image"):
                fields["weather_image"] = lkg["weather_image"]
                pending_uploads[:] = [
                    (k, v) for k, v in pending_uploads if k != "weather_image"
                ]

    # LKG: Save successful module data
    for spec in MODULES:
        module_data = {
            k: fields[k]
            for k in (*spec.fields, *spec.extra_lkg_keys)
            if _is_substantive(fields.get(k))
        }
        if module_data:
            _save_module_lkg(spec.name, module_data)

    drip_template_fields = {
        "date": now_mountain().strftime("%Y-%m-%d"),
        "today": format_date_readable(now_mountain()),
        **fields,
    }

    # Replace None with "" for simple string fields
    for key, value in drip_template_fields.items():
        if value is None:
            drip_template_fields[key] = ""

    return drip_template_fields, pending_uploads


def _serialize_value(value):
    """Convert dataclass instances to dicts for JSON serialization."""
    if hasattr(value, "__dataclass_fields__"):
        d = asdict(value)
        # Remove Event.sortable (datetime, not JSON-serializable, not needed in API)
        if "events" in d and isinstance(d["events"], list):
            for event in d["events"]:
                event.pop("sortable", None)
        return d
    return value


def write_data_to_json(data: dict, doctype: str) -> str:
    """
    Serialize structured data to clean JSON for API.
    """
    serializable = {key: _serialize_value(value) for key, value in data.items()}

    serializable["date"] = now_mountain().strftime("%Y-%m-%d")
    serializable["time_generated"] = cross_platform_strftime(
        now_mountain(), "%-I:%M %p"
    ).lower()

    filepath = f"server/{doctype}"
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(serializable, f)

    return filepath


def purge_cache() -> bool:
    """
    Purge the Cloudflare cache for the site.

    Returns True on success, False otherwise.
    """
    settings = get_settings()
    purge_key = settings.CACHE_PURGE
    zone_id = settings.ZONE_ID
    if not purge_key or not zone_id:
        logger.warning("No CACHE_PURGE key or ZONE_ID set, skipping cache purge.")
        return False

    url = f"https://api.cloudflare.com/client/v4/zones/{zone_id}/purge_cache"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {purge_key}",
    }
    data = {"purge_everything": True}

    try:
        response = requests.post(url, headers=headers, json=data, timeout=30)
    except requests.RequestException as e:
        logger.error("Error purging cache: %s", e)
        return False

    if response.status_code == 200:
        logger.info("Cache purged successfully.")
        return True

    logger.error("Failed to purge cache: %s - %s", response.status_code, response.text)
    return False


def refresh_cache():
    """
    Refresh the Drip cache by hitting the endpoint.
    """
    url = "https://api.glacierconservancy.org/email.json"
    try:
        response = requests.get(url, timeout=30)
        if response.status_code == 200:
            logger.info("Cache refreshed successfully.")
        else:
            logger.error(
                "Failed to refresh cache: %s - %s",
                response.status_code,
                response.text,
            )
    except requests.RequestException as e:
        logger.error("Error refreshing cache: %s", e)


def clear_cache():
    """Remove cached email.json and date-deterministic LKG data."""
    cache_file = "server/email.json"
    if os.path.exists(cache_file):
        os.remove(cache_file)
    try:
        LKGCache.get_cache().clear_modules(
            [spec.name for spec in MODULES if spec.date_deterministic]
        )
    except Exception:
        logger.warning("Failed to clear LKG cache", exc_info=True)


def serve_api(force: bool = False) -> dict:
    """
    Get the data, then upload it to server for API.
    FTP session is created after data collection to avoid idle timeouts.

    Returns the generated template-field dict so callers (main.py) can
    sanity-check content before sending the email.
    """
    if force:
        clear_cache()

    data, pending_uploads = gen_data()

    with FTPSession() as ftp:
        for field_key, upload_args in pending_uploads:
            url, _ = ftp.upload(*upload_args)
            data[field_key] = url if url else ""
            # Save resolved image URL to LKG for future cache hits
            module = _FIELD_TO_MODULE.get(field_key)
            if module and url:
                _save_module_lkg(module, {field_key: url})

        web = web_version(data)
        printable = web_version(data, "server/printable.html", "printable.html")
        json_file = write_data_to_json(data, "email.json")
        ftp.upload("api", json_file.split("/")[-1], json_file)
        ftp.upload("email", web.split("/")[-1], web)
        ftp.upload("printable", printable.split("/")[-1], printable)

    if purge_cache():
        # Allow Cloudflare edge nodes time to propagate the purge
        sleep(_CACHE_PURGE_PROPAGATION_SECS)
        refresh_cache()

    return data


def run_health_check() -> int:
    """Validate data generation and FTP connectivity without writing anything.

    Used by the deploy pipeline (``--check``) as its rollback gate.
    Exceptions propagate so the caller exits nonzero on any failure.
    """
    logger.info("Running health check (no data will be written)")
    gen_data()
    logger.info("Data generation OK, testing FTP connectivity")
    with FTPSession():
        pass  # Login + quit validates credentials and connectivity
    logger.info("Health check passed")
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for the hourly web-update run."""
    parser = argparse.ArgumentParser(description="Generate and upload data")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Clear cached data and re-fetch everything fresh",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Health check only: validate data generation and FTP connectivity without writing",
    )
    args = parser.parse_args(argv)

    settings = get_settings()  # Load email.env so ENVIRONMENT is available
    run = start_run("web_update")
    setup_logging()
    logger.info("Starting run %s (type=%s)", run.run_id, run.run_type)

    if args.check:
        return run_health_check()

    if args.force:
        clear_cache()

    run_error = False
    try:
        if settings.ENVIRONMENT == "development":
            gen_data()  # Pending uploads ignored in development
        elif settings.ENVIRONMENT == "production":
            serve_api(force=args.force)
    except Exception:
        logger.exception("generate_and_upload failed")
        run_error = True
    finally:
        complete_run(settings.ENVIRONMENT, failed=run_error)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
