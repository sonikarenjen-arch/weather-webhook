"""
Weather-to-Webhook Integration
-------------------------------
Polls OpenWeatherMap every POLL_SECONDS seconds.
When the temperature rises by INCREASE_C degrees or more between polls,
it fires a Zapier webhook with a structured payload.

Exposes three HTTP endpoints:
  GET  /health   — liveness probe
  GET  /status   — current state + recent webhook history
  POST /trigger  — run a weather check right now (used during the live demo)
"""

import os
import threading
import time
from datetime import datetime, timezone

import requests
from flask import Flask, jsonify

# ---------------------------------------------------------------------------
# Configuration — override any value via environment variable
# ---------------------------------------------------------------------------
def _require(name: str) -> str:
    val = os.getenv(name)
    if not val:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return val

API_KEY      = _require("OWM_API_KEY")
WEBHOOK_URL  = _require("ZAPIER_WEBHOOK_URL")
LAT          = float(os.getenv("LAT",          "31.5204"))
LON          = float(os.getenv("LON",          "74.3587"))
INCREASE_C   = float(os.getenv("INCREASE_C",  "5.0"))
POLL_SECONDS = int(os.getenv("POLL_SECONDS",  "1000"))

WEATHER_URL = (
    "https://api.openweathermap.org/data/2.5/weather"
    f"?lat={LAT}&lon={LON}&units=metric&appid={API_KEY}"
)

# ---------------------------------------------------------------------------
# Shared in-memory state — guarded by a lock so the poller thread and Flask
# request threads can read/write safely.
# ---------------------------------------------------------------------------
_lock = threading.Lock()
_state = {
    "previous_temp": None,
    "last_temp":     None,
    "last_checked":  None,
    "webhook_history": [],  # capped at 10 entries
    "error_log":       [],  # capped at 10 entries
}

app = Flask(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _log_error(msg: str) -> None:
    entry = {"time": datetime.now(timezone.utc).isoformat(), "error": msg}
    with _lock:
        _state["error_log"].append(entry)
        _state["error_log"] = _state["error_log"][-10:]


def _log_webhook(payload: dict) -> None:
    with _lock:
        _state["webhook_history"].append(payload)
        _state["webhook_history"] = _state["webhook_history"][-10:]


def fetch_temperature() -> tuple[float, dict]:
    """
    Call OpenWeatherMap and return (temp_c, raw_json).
    Raises requests.RequestException on network/HTTP errors,
    ValueError if the response shape is unexpected.
    """
    r = requests.get(WEATHER_URL, timeout=10)

    # Surface HTTP errors (401 bad key, 429 rate-limit, etc.) as exceptions
    r.raise_for_status()

    data = r.json()
    try:
        temp = data["main"]["temp"]
    except (KeyError, TypeError) as exc:
        raise ValueError(
            f"Unexpected OpenWeather response — missing 'main.temp': {exc}"
        ) from exc

    return float(temp), data


def fire_webhook(temp: float, previous_temp: float, raw: dict) -> tuple[int, dict]:
    """POST a structured payload to the Zapier webhook. Returns (status_code, payload)."""
    payload = {
        "event":                  "temperature_increased",
        "increase_threshold_c":   INCREASE_C,
        "temperature_c":          temp,
        "previous_temperature_c": previous_temp,
        "delta_c":                round(temp - previous_temp, 2),
        "location":               {"lat": LAT, "lon": LON},
        "observed_at":            datetime.now(timezone.utc).isoformat(),
        "raw":                    raw,
    }
    resp = requests.post(WEBHOOK_URL, json=payload, timeout=10)
    resp.raise_for_status()
    return resp.status_code, payload


def run_check() -> dict:
    """
    Fetch the current temperature and fire the webhook if the threshold is
    crossed.  Returns a result dict describing what happened.

    This is called both by the background poller and by POST /trigger.
    """
    result: dict = {"checked_at": datetime.now(timezone.utc).isoformat()}

    # --- Fetch temperature ---------------------------------------------------
    try:
        temp, raw = fetch_temperature()
    except requests.exceptions.Timeout:
        msg = "OpenWeather request timed out after 10 s"
        result["error"] = msg
        _log_error(msg)
        return result
    except requests.exceptions.HTTPError as exc:
        msg = f"OpenWeather HTTP {exc.response.status_code}: {exc.response.reason}"
        result["error"] = msg
        _log_error(msg)
        return result
    except requests.RequestException as exc:
        msg = f"Network error contacting OpenWeather: {exc}"
        result["error"] = msg
        _log_error(msg)
        return result
    except ValueError as exc:
        msg = str(exc)
        result["error"] = msg
        _log_error(msg)
        return result

    result["temperature_c"] = temp

    with _lock:
        previous_temp = _state["previous_temp"]
        _state["last_temp"]    = temp
        _state["last_checked"] = result["checked_at"]

    # --- First reading: just record the baseline ----------------------------
    if previous_temp is None:
        result["action"] = "baseline_recorded"
        with _lock:
            _state["previous_temp"] = temp
        return result

    # --- Subsequent readings: compare and maybe fire webhook ----------------
    delta = temp - previous_temp
    result["delta_c"] = round(delta, 2)

    if delta >= INCREASE_C:
        try:
            status_code, payload = fire_webhook(temp, previous_temp, raw)
            result["action"]         = "webhook_fired"
            result["webhook_status"] = status_code
            _log_webhook(payload)
        except requests.exceptions.Timeout:
            result["action"] = "webhook_failed"
            result["error"]  = "Zapier POST timed out"
            _log_error(result["error"])
        except requests.exceptions.HTTPError as exc:
            result["action"] = "webhook_failed"
            result["error"]  = f"Zapier HTTP {exc.response.status_code}"
            _log_error(result["error"])
        except requests.RequestException as exc:
            result["action"] = "webhook_failed"
            result["error"]  = f"Zapier network error: {exc}"
            _log_error(result["error"])
    else:
        result["action"] = "no_threshold_crossed"

    with _lock:
        _state["previous_temp"] = temp

    return result


# ---------------------------------------------------------------------------
# Background polling loop
# ---------------------------------------------------------------------------

def _polling_loop() -> None:
    print(
        f"[poller] started — interval={POLL_SECONDS}s  "
        f"threshold={INCREASE_C}°C  location=({LAT},{LON})"
    )
    while True:
        result = run_check()
        print(f"[poller] {result}")
        time.sleep(POLL_SECONDS)


# ---------------------------------------------------------------------------
# Flask routes
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    """Liveness probe — returns 200 if the server is up."""
    return jsonify({"status": "ok"})


@app.get("/status")
def status():
    """
    Return the current integration state:
      - last observed temperature
      - time of last check
      - recent webhook fires (up to 5)
      - recent errors (up to 5)
    """
    with _lock:
        return jsonify({
            "last_temp_c":           _state["last_temp"],
            "previous_temp_c":       _state["previous_temp"],
            "last_checked":          _state["last_checked"],
            "location":              {"lat": LAT, "lon": LON},
            "threshold_c":           INCREASE_C,
            "poll_interval_s":       POLL_SECONDS,
            "recent_webhook_fires":  _state["webhook_history"][-5:],
            "recent_errors":         _state["error_log"][-5:],
        })


@app.post("/trigger")
def trigger():
    """
    Manually run a weather check right now and fire the webhook if the
    threshold is met.  Used during the live onsite demo.

    Returns the full result object so the interviewer can see exactly
    what happened (temp, delta, whether the webhook fired, etc.).
    """
    result = run_check()
    http_status = 502 if "error" in result else 200
    return jsonify(result), http_status


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # Start the background poller as a daemon thread — it exits automatically
    # when the main process is killed.
    poller = threading.Thread(target=_polling_loop, daemon=True)
    poller.start()

    port = int(os.getenv("PORT", 5000))
    print(f"[server] listening on http://0.0.0.0:{port}")
    app.run(host="0.0.0.0", port=port)
