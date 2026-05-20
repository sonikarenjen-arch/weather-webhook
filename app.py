"""
Weather → Slack + Webhook Integration
--------------------------------------
Polls OpenWeatherMap every POLL_SECONDS seconds.
When the temperature rises by INCREASE_C degrees or more between polls:
  1. Posts an alert directly to a Slack channel
  2. Fires a Zapier webhook with a structured payload

Exposes:
  GET  /          — live dashboard (auto-refreshes every 30s)
  GET  /health    — liveness probe
  GET  /status    — current state as JSON
  GET  /history   — last 20 temperature readings as JSON
  POST /trigger   — run a weather check right now (used during live demo)
"""

import os
import threading
import time
from datetime import datetime, timezone

import requests
from flask import Flask, jsonify

# ---------------------------------------------------------------------------
# Configuration ( tells you if an env var is there and gives alert if it is missing)
# ---------------------------------------------------------------------------

def _require(name: str) -> str:
    val = os.getenv(name)
    if not val:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return val

API_KEY          = _require("OWM_API_KEY")
WEBHOOK_URL      = _require("ZAPIER_WEBHOOK_URL")
SLACK_WEBHOOK    = os.getenv("SLACK_WEBHOOK_URL")   # optional — Slack alert disabled if not set
LAT              = float(os.getenv("LAT",           "31.5204"))
LON              = float(os.getenv("LON",           "74.3587"))
INCREASE_C       = float(os.getenv("INCREASE_C",   "5.0"))
POLL_SECONDS     = int(os.getenv("POLL_SECONDS",   "1000"))

WEATHER_URL = (
    "https://api.openweathermap.org/data/2.5/weather"
    f"?lat={LAT}&lon={LON}&units=metric&appid={API_KEY}"
)

# ---------------------------------------------------------------------------
# Shared in-memory state 
# lock needed because poller and web server are running simultaneously
# ---------------------------------------------------------------------------
_lock = threading.Lock()
_state = {
    "previous_temp":   None,
    "last_temp":       None,
    "last_checked":    None,
    "history":         [],   # last 20 readings: [{temp, time}]
    "webhook_history": [],   # last 10 webhook fires
    "slack_history":   [],   # last 10 Slack posts
    "error_log":       [],   # last 10 errors
}

app = Flask(__name__)


# ---------------------------------------------------------------------------
# Helpers (add entries to error log, webhook history, and Slack history)
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


def _log_slack(msg: str) -> None:
    entry = {"time": datetime.now(timezone.utc).isoformat(), "message": msg}
    with _lock:
        _state["slack_history"].append(entry)
        _state["slack_history"] = _state["slack_history"][-10:]

# get temperature from weather url or return error message 

def fetch_temperature() -> tuple[float, dict]:
    r = requests.get(WEATHER_URL, timeout=10)
    r.raise_for_status()
    data = r.json()
    try:
        temp = data["main"]["temp"]
    except (KeyError, TypeError) as exc:
        raise ValueError(f"Unexpected OpenWeather response — missing 'main.temp': {exc}") from exc
    return float(temp), data


def fire_webhook(temp: float, previous_temp: float, raw: dict) -> tuple[int, dict]:
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


def post_to_slack(temp: float, previous_temp: float) -> None:
    """Post a formatted alert to Slack using an incoming webhook."""
    if not SLACK_WEBHOOK:
        return

    delta = round(temp - previous_temp, 2)
    text = (
        f":thermometer: *Temperature Alert* — Lahore ({LAT}, {LON})\n"
        f"Temp rose by *{delta}°C* (from {previous_temp}°C → {temp}°C)\n"
        f"Threshold: {INCREASE_C}°C  |  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
    )
    payload = {"text": text}
    resp = requests.post(SLACK_WEBHOOK, json=payload, timeout=10)
    resp.raise_for_status()
    _log_slack(text)

# main function 
def run_check() -> dict:
    """
    Fetch temperature, compare to previous reading, and fire alerts if
    the threshold is crossed. Returns a dict describing what happened.
    """
    result: dict = {"checked_at": datetime.now(timezone.utc).isoformat()}

    # --- Fetch temperature ---------------------------------------------------
    try:
        temp, raw = fetch_temperature()
    except requests.exceptions.Timeout:
        msg = "OpenWeather request timed out after 10s"
        result["error"] = msg
        _log_error(msg)
        return result
    except requests.exceptions.HTTPError as exc:
        msg = f"OpenWeather HTTP {exc.response.status_code}: {exc.response.reason}"
        result["error"] = msg
        _log_error(msg)
        return result
    except requests.RequestException as exc:
        msg = f"Network error: {exc}"
        result["error"] = msg
        _log_error(msg)
        return result
    except ValueError as exc:
        msg = str(exc)
        result["error"] = msg
        _log_error(msg)
        return result

    result["temperature_c"] = temp

    # Record history
    with _lock:
        previous_temp = _state["previous_temp"]
        _state["last_temp"]    = temp
        _state["last_checked"] = result["checked_at"]
        _state["history"].append({"temp_c": temp, "time": result["checked_at"]})
        _state["history"] = _state["history"][-20:]

    # --- First reading -------------------------------------------------------
    if previous_temp is None:
        result["action"] = "baseline_recorded"
        with _lock:
            _state["previous_temp"] = temp
        return result

    # --- Compare and alert ---------------------------------------------------
    delta = temp - previous_temp
    result["delta_c"] = round(delta, 2)

    if delta >= INCREASE_C:
        # 1. Fire Zapier webhook
        try:
            status_code, payload = fire_webhook(temp, previous_temp, raw)
            result["webhook_status"] = status_code
            _log_webhook(payload)
        except requests.RequestException as exc:
            result["webhook_error"] = f"Zapier POST failed: {exc}"
            _log_error(result["webhook_error"])

        # 2. Post to Slack
        try:
            post_to_slack(temp, previous_temp)
            result["slack"] = "posted"
        except requests.RequestException as exc:
            result["slack_error"] = f"Slack POST failed: {exc}"
            _log_error(result["slack_error"])

        result["action"] = "threshold_crossed"
    else:
        result["action"] = "no_threshold_crossed"

    with _lock:
        _state["previous_temp"] = temp

    return result


# ---------------------------------------------------------------------------
# Background polling loop (check, log, wait, repeat)
# ---------------------------------------------------------------------------

def _polling_loop() -> None:
    print(f"[poller] started — interval={POLL_SECONDS}s  threshold={INCREASE_C}°C")
    while True:
        result = run_check()
        print(f"[poller] {result}")
        time.sleep(POLL_SECONDS)


# ---------------------------------------------------------------------------
# Flask routes
# ---------------------------------------------------------------------------

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <meta http-equiv="refresh" content="30">
  <title>Weather Monitor</title>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: #0f172a; color: #e2e8f0; min-height: 100vh; padding: 2rem;
    }
    h1 { font-size: 1.4rem; font-weight: 600; color: #94a3b8; margin-bottom: 2rem; }
    .cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 1rem; margin-bottom: 2rem; }
    .card {
      background: #1e293b; border-radius: 12px; padding: 1.5rem;
      border: 1px solid #334155;
    }
    .card .label { font-size: 0.75rem; color: #64748b; text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 0.5rem; }
    .card .value { font-size: 2rem; font-weight: 700; color: #f1f5f9; }
    .card .value.temp { color: #fb923c; }
    .card .value.ok   { color: #34d399; font-size: 1.1rem; }
    .card .sub { font-size: 0.8rem; color: #64748b; margin-top: 0.4rem; }
    .section { background: #1e293b; border-radius: 12px; padding: 1.5rem; margin-bottom: 1rem; border: 1px solid #334155; }
    .section h2 { font-size: 0.85rem; color: #64748b; text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 1rem; }
    table { width: 100%; border-collapse: collapse; font-size: 0.85rem; }
    th { text-align: left; color: #64748b; font-weight: 500; padding: 0.4rem 0.75rem; border-bottom: 1px solid #334155; }
    td { padding: 0.5rem 0.75rem; border-bottom: 1px solid #1e293b; color: #cbd5e1; }
    tr:last-child td { border-bottom: none; }
    .badge {
      display: inline-block; padding: 0.2rem 0.6rem; border-radius: 9999px;
      font-size: 0.75rem; font-weight: 600;
    }
    .badge.fired  { background: #fef08a22; color: #fde047; }
    .badge.normal { background: #d1fae522; color: #6ee7b7; }
    .badge.error  { background: #fca5a522; color: #f87171; }
    .footer { text-align: center; color: #334155; font-size: 0.75rem; margin-top: 2rem; }
  </style>
</head>
<body>
  <h1>🌡️ Weather Monitor — Lahore (31.52°N, 74.36°E)</h1>

  <div class="cards">
    <div class="card">
      <div class="label">Current Temperature</div>
      <div class="value temp" id="temp">--</div>
      <div class="sub" id="last-checked">Loading...</div>
    </div>
    <div class="card">
      <div class="label">Alert Threshold</div>
      <div class="value" id="threshold">--</div>
      <div class="sub">rise between polls</div>
    </div>
    <div class="card">
      <div class="label">Poll Interval</div>
      <div class="value" id="interval">--</div>
      <div class="sub">seconds</div>
    </div>
    <div class="card">
      <div class="label">Server Status</div>
      <div class="value ok">● Live</div>
      <div class="sub">auto-refreshes every 30s</div>
    </div>
  </div>

  <div class="section">
    <h2>Temperature History (last 20 readings)</h2>
    <table>
      <thead><tr><th>#</th><th>Temperature</th><th>Time (UTC)</th></tr></thead>
      <tbody id="history-body"><tr><td colspan="3">Loading...</td></tr></tbody>
    </table>
  </div>

  <div class="section">
    <h2>Recent Alerts</h2>
    <table>
      <thead><tr><th>Time</th><th>Temp</th><th>Delta</th><th>Webhook</th><th>Slack</th></tr></thead>
      <tbody id="alerts-body"><tr><td colspan="5">No alerts yet</td></tr></tbody>
    </table>
  </div>

  <div class="section">
    <h2>Recent Errors</h2>
    <table>
      <thead><tr><th>Time</th><th>Error</th></tr></thead>
      <tbody id="errors-body"><tr><td colspan="2">None</td></tr></tbody>
    </table>
  </div>

  <div class="footer">Page auto-refreshes every 30 seconds</div>

  <script>
    async function load() {
      const [status, history] = await Promise.all([
        fetch('/status').then(r => r.json()),
        fetch('/history').then(r => r.json()),
      ]);

      document.getElementById('temp').textContent =
        status.last_temp_c !== null ? status.last_temp_c + '°C' : '--';
      document.getElementById('threshold').textContent = status.threshold_c + '°C';
      document.getElementById('interval').textContent  = status.poll_interval_s;
      document.getElementById('last-checked').textContent =
        status.last_checked ? 'Last checked: ' + new Date(status.last_checked).toLocaleTimeString() : 'No data yet';

      // History table
      const hBody = document.getElementById('history-body');
      if (history.length === 0) {
        hBody.innerHTML = '<tr><td colspan="3">No readings yet</td></tr>';
      } else {
        hBody.innerHTML = [...history].reverse().map((r, i) =>
          `<tr><td>${history.length - i}</td><td>${r.temp_c}°C</td><td>${new Date(r.time).toLocaleString()}</td></tr>`
        ).join('');
      }

      // Alerts table — combine webhook fires and Slack posts
      const aBody = document.getElementById('alerts-body');
      const webhookAlerts = (status.recent_webhook_fires || []).map(a => ({
        time: a.observed_at,
        temp: a.temperature_c,
        delta: a.delta_c,
        webhook: true,
        slack: false,
      }));
      const slackAlerts = (status.recent_slack_posts || []).map(s => ({
        time: s.time,
        temp: null,
        delta: null,
        webhook: false,
        slack: true,
        message: s.message,
      }));
      const allAlerts = [...webhookAlerts, ...slackAlerts]
        .sort((a, b) => new Date(b.time) - new Date(a.time))
        .slice(0, 10);

      if (allAlerts.length === 0) {
        aBody.innerHTML = '<tr><td colspan="5">No alerts yet</td></tr>';
      } else {
        aBody.innerHTML = allAlerts.map(a =>
          `<tr>
            <td>${new Date(a.time).toLocaleString()}</td>
            <td>${a.temp !== null ? a.temp + '°C' : '—'}</td>
            <td>${a.delta !== null ? '+' + a.delta + '°C' : '—'}</td>
            <td>${a.webhook ? '<span class="badge fired">fired</span>' : '<span class="badge normal">—</span>'}</td>
            <td>${a.slack ? '<span class="badge fired">posted</span>' : '<span class="badge normal">—</span>'}</td>
          </tr>`
        ).join('');
      }

      // Errors table
      const eBody = document.getElementById('errors-body');
      const errors = status.recent_errors || [];
      if (errors.length === 0) {
        eBody.innerHTML = '<tr><td colspan="2">None</td></tr>';
      } else {
        eBody.innerHTML = [...errors].reverse().map(e =>
          `<tr><td>${new Date(e.time).toLocaleString()}</td><td>${e.error}</td></tr>`
        ).join('');
      }
    }

    load();
  </script>
</body>
</html>"""

 # flask endpoints
@app.get("/")
def dashboard():
    """Live dashboard — auto-refreshes every 30 seconds."""
    return DASHBOARD_HTML, 200, {"Content-Type": "text/html"}


@app.get("/health")
def health():
    return jsonify({"status": "ok"})


@app.get("/status")
def status():
    with _lock:
        return jsonify({
            "last_temp_c":          _state["last_temp"],
            "previous_temp_c":      _state["previous_temp"],
            "last_checked":         _state["last_checked"],
            "location":             {"lat": LAT, "lon": LON},
            "threshold_c":          INCREASE_C,
            "poll_interval_s":      POLL_SECONDS,
            "slack_enabled":        bool(SLACK_WEBHOOK),
            "recent_webhook_fires": _state["webhook_history"][-5:],
            "recent_slack_posts":   _state["slack_history"][-5:],
            "recent_errors":        _state["error_log"][-5:],
        })


@app.get("/history")
def history():
    """Return the last 20 temperature readings."""
    with _lock:
        return jsonify(_state["history"])


@app.post("/trigger")
def trigger():
    """
    Manually run a weather check right now.
    Used during the live onsite demo.
    """
    result = run_check()
    http_status = 502 if "error" in result else 200
    return jsonify(result), http_status


# ---------------------------------------------------------------------------
# Entry point (start the poller in the background, then start the web server)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    poller = threading.Thread(target=_polling_loop, daemon=True)
    poller.start()

    port = int(os.getenv("PORT", 5000))
    print(f"[server] listening on http://0.0.0.0:{port}")
    app.run(host="0.0.0.0", port=port)
