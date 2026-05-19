# Weather → Webhook Integration

Polls the [OpenWeatherMap](https://openweathermap.org/) API for the current temperature at a configured location. When the temperature rises by a configurable threshold between polls, it posts an alert directly to a Slack channel and fires a Zapier webhook with a structured payload. A small Flask server runs alongside the poller and exposes endpoints for live inspection and manual triggering.

---

## What it does

1. **Reads** the current temperature from OpenWeatherMap every `POLL_SECONDS` seconds.
2. **Compares** it to the previous reading.
3. **Writes** to Zapier by firing a webhook POST and alert to Slack when the temperature has risen by `INCREASE_C` °C or more.
4. **Exposes** three HTTP endpoints so the integration can be inspected and triggered externally.

---

## Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/health` | Liveness probe — returns `{"status": "ok"}` |
| `GET` | `/status` | Current state: last temp, last check time, recent webhook fires and errors |
| `POST` | `/trigger` | Run a weather check right now and fire the webhook if the threshold is met |

### Example of manual trigger 

```bash
curl -X POST https://weather-webhook-production.up.railway.app/trigger
```

Response (threshold not crossed):
```json
{
  "checked_at": "2025-06-01T10:00:00+00:00",
  "temperature_c": 32.1,
  "delta_c": 1.4,
  "action": "no_threshold_crossed"
}
```

Response (webhook fired):
```json
{
  "checked_at": "2025-06-01T10:00:00+00:00",
  "temperature_c": 38.5,
  "delta_c": 6.2,
  "action": "webhook_fired",
  "webhook_status": 200
}
```

---

## Running locally 

### 1. Clone and install dependencies

```bash
git clone <your-repo-url>
cd <repo-folder>
pip install -r requirements.txt
```

### 2. Configure environment variables

```bash
cp .env.example .env
# Edit .env with your OpenWeatherMap API key and Zapier webhook URL
```

Or export them directly:

```bash
export OWM_API_KEY=your_key_here
export ZAPIER_WEBHOOK_URL=https://hooks.zapier.com/hooks/catch/...
```

### 3. Start the server

```bash
python app.py
```

The server starts on `http://localhost:5000`. The background poller begins immediately.

### 4. Hit the endpoint

```bash
# Check status
curl http://localhost:5000/status

# Manually trigger a check
curl -X POST http://localhost:5000/trigger
```

---

## Deploying to Railway

[Railway] was used to get my project publically accessible

1. Code is pushed to a GitHub repo.
2. Go to [railway.app](https://railway.app/) → **New Project** → **Deploy from GitHub repo**.
3. Python was used for repo
4. Environment variables are added: 
   - `OWM_API_KEY`
   - `ZAPIER_WEBHOOK_URL`
   - `INCREASE_C` (optional, default `5.0`)
   - `POLL_SECONDS` (optional, default `1000`)
5. Railway sets `PORT` automatically — the app reads it.
6. Pulic URL: https://weather-webhook-production.up.railway.app/trigger

### Triggering over the web

```bash
curl -X POST (https://weather-webhook-production.up.railway.app/trigger)
```

---

## Configuration reference

| Variable | Default | Description |
|----------|---------|-------------|
| `OWM_API_KEY` | *(inputted)* | OpenWeatherMap API key |
| `LAT` | `31.5204` | Latitude of the location to monitor |
| `LON` | `74.3587` | Longitude of the location to monitor |
| `ZAPIER_WEBHOOK_URL` | *(inputted)* | Zapier catch webhook URL |
| `INCREASE_C` | `5.0` | Temperature rise (°C) needed to fire the webhook |
| `POLL_SECONDS` | `1000` | How often to poll OpenWeatherMap (~16 min) |
| `PORT` | `5000` | Port the Flask server listens on |

---

## Error handling

- **OpenWeather timeout** — logged, poller continues on the next interval.
- **OpenWeather HTTP errors** (401 bad key, 429 rate-limit, 5xx) — logged with status code, no crash.
- **Unexpected response shape** — caught as a `ValueError`, logged, poller continues.
- **Zapier POST failure** — logged separately; the temperature reading is still recorded so the next poll compares correctly.
- All errors are surfaced in `GET /status` under `recent_errors`.

---

## Assumptions

- The threshold check is one-directional: only a **rise** of `INCREASE_C` °C or more triggers the webhook. Drops are ignored.
- The first poll is treated as a baseline — no webhook is fired on startup regardless of temperature.
- State is in-memory. Restarting the server resets the baseline reading.
- OpenWeatherMap free tier is used (`/data/2.5/weather`), which allows ~60 calls/minute — well within the default polling interval.
