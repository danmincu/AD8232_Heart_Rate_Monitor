# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

AD8232 Single Lead Heart Rate Monitor — an ECG/EKG visualization system using the AD8232 sensor chip. Originally by SparkFun Electronics (SEN-12650), extended with a Python web monitor and a Raspberry Pi Pico 2W embedded display.

## Architecture

Three independent visualization frontends all consume the same serial protocol:

```
[AD8232 Sensor] → analog → [MCU] → serial 9600 baud ("0-1023\n" or "!\n")
                                ↓
                 ┌──────────────┼──────────────────┐
                 ↓              ↓                   ↓
          [Processing App]  [Python Web Server]  [Pico 2W Display]
          Desktop ECG+BPM   Browser ECG+BPM+CSV  On-device ECG+BPM
          + PNG export      + arrhythmia detect   + PVC detection
```

### 1. Arduino firmware (`Software/Heart_Rate_Display_Arduino/Heart_Rate_Display_Arduino.ino`)
Reads analog pin A0, sends raw ADC values (0–1023) or `!` (leads-off) over serial at 9600 baud. Pins 10/11 are leads-off detection inputs. 1ms sampling delay.

### 2. Processing visualization (`Software/Heart_Rate_Display_Processing/Heart_Rate_Display/Heart_Rate_Display.pde`)
Desktop app (Processing/Java, not Arduino): real-time ECG waveform (red=valid, blue=leads-off), BPM via fixed 620.0 ADC threshold with rolling 500-beat average, calibration grid, PNG frame export. Serial port selected via `Serial.list()[2]`.

### 3. Python web server (`Software/ecg_server.py`)
aiohttp async server (~700 lines). The browser frontend is a separate HTML/CSS/JS SPA in `Software/static/index.html` (~800 lines of raw canvas 2D rendering — no framework), loaded at startup. To modify the web UI, edit `Software/static/index.html`.

Server-side features:
- Adaptive threshold BPM detection (Schmitt trigger with auto-polarity via percentile analysis)
- Arrhythmia detection (R-R interval deviation state machine)
- PVC detection (opposite-direction spikes)
- CSV recording to `Software/ecg_data/`
- Arrhythmia event JSON files in `Software/ecg_data/events/`
- WebSocket broadcast at 60Hz to browser clients
- REST API: `GET /api/events`, `GET /api/events/{filename}`, `POST /api/record-event`

Global mutable state lives in the `ECGState` singleton (`state`). The beat detection algorithm (`process_sample`) and threshold adaptation (`_update_adaptive_threshold`) are shared logic that the Pico 2W reimplements in MicroPython with the same percentile/Schmitt-trigger approach.

WebSocket message types (server→client): `init` (config + recent samples + BPM on connect), `d` (data batch at 60Hz), `history_resp` (response to scroll-back request), `arrhythmia_event` (real-time alert with event_type + filename). Client→server: `history` (request older samples by start index + count).

### 4. Pico 2W MicroPython display + WiFi web server (`pico2w-display/`)
Runs on Raspberry Pi Pico 2W with Pimoroni Pico Display. Dual-core via `_thread`. No asyncio — uses raw sockets for maximum compatibility with all MicroPython builds.
- **Core 0** (`main()`): WiFi STA init (one-time, blocking) + ADC read at ~1kHz + serial output (every Nth sample, configurable) + non-blocking HTTP server poll + SSE broadcast every ~50ms. Writes to `wave_buf` ring buffer.
- **Core 1** (`display_thread()`): BPM detection + display rendering at ~5Hz. Owns all display/PicoGraphics objects. No networking, no asyncio.

WiFi Station mode: connects to a configurable hotspot (constants `WIFI_SSID`/`WIFI_PASSWORD` at top of `main.py`). Device gets a DHCP-assigned IP; open `http://<ip>` from any device on the same network. Max 2 concurrent SSE clients.

Web UI (`pico2w-display/index.html`): Stripped-down version of `Software/static/index.html` — same canvas ECG rendering (grid, multi-row trace, minimap, scroll/zoom, BPM labels) but without events panel, event viewer, recording, arrhythmia markers, or history requests. Uses SSE (Server-Sent Events) via `EventSource("/events")`. SSE message format: `init` has `{type, bpm, buf}`, data messages have `{type, ts, lo, bpm, b[], v[]}` where `v` is raw ADC values and `b` is beat offset indices.

Cross-core contract: Core 0 writes `wave_buf`, `wave_idx`, `current_value`, `current_bpm`, `leads_off_flag`. Core 1 reads these (and Core 1 writes `current_bpm` from BPM detection). No locks — relies on atomic scalar writes and tolerates occasional torn reads.

Wiring: AD8232 OUTPUT→GP26, LO+→GP2, LO-→GP3. Button A cycles display pages, Button B toggles backlight. PVC detection is display-flash only (no file I/O).

`main_simple.py` is the previous version without WiFi (display + PVC file logging only). `main_bare.py` is a minimal variant (no display, no BPM — raw ADC only).

## Build & Run

**Arduino sketch**: Open `.ino` in Arduino IDE, select "Arduino Pro 3.3V/8MHz", upload.

**Processing sketch**: Open `.pde` in Processing IDE (Java mode), run. Adjust `Serial.list()` index for your system.

**Python web server** (dependencies: `aiohttp`, `pyserial-asyncio` — no requirements.txt, installed by run.sh):
```bash
Software/run.sh                    # creates venv, installs deps, starts server
# or manually:
pip install aiohttp pyserial-asyncio
python Software/ecg_server.py --serial /dev/ttyACM0 --port 8080 --baud 9600
```
Open `http://localhost:8080` in browser. CLI args also settable via env vars: `ECG_PORT`, `ECG_SERIAL_PORT`, `ECG_SERIAL_BAUD`. Data directories (`Software/ecg_data/` and `events/` subdirectory) are created automatically on first recording.

**Pico 2W**: Copy `pico2w-display/boot.py`, `main.py`, and `index.html` to the Pico 2W filesystem (requires Pimoroni MicroPython build with `picographics` module). The `index.html` must be at `/index.html` on the Pico flash for the web server to serve it.

**No tests or linters are configured.** This is a hardware-dependent project — all components require physical AD8232 sensor hardware or a serial device to function.

## Serial Protocol

Unidirectional MCU→host. ASCII newline-terminated: integer `0`–`1023` (ADC reading) or `!` (leads-off detected). All three frontends consume this same protocol.

## Key Configuration Points

- **BPM threshold**: Fixed `620.0` in Processing; adaptive (percentile-based Schmitt trigger) in ecg_server.py and Pico 2W — both use 80% of peak range from median as trigger, 30% as re-arm
- **Serial port**: `Serial.list()[2]` in Processing; `--serial` flag or `ECG_SERIAL_PORT` env var for Python server
- **Arrhythmia detection**: 30% R-R deviation, 10-beat baseline window, 5s cooldown between events (constants at top of `ecg_server.py`)
- **Pico 2W pins**: ADC0=GP26, LO+=GP2, LO-=GP3, display on GP6-8/GP12-20
- **Pico 2W WiFi**: Station mode — connects to hotspot configured via `WIFI_SSID`/`WIFI_PASSWORD` constants at top of `main.py`. HTTP server on port 80, max 2 SSE clients, ~20Hz broadcast.
- **Pico 2W MicroPython constraints**: No asyncio, no standard library beyond `machine`/`time`/`sys`/`_thread`/`array`/`socket`/`network`/`hashlib`/`binascii`. Prefer integer math. Memory-constrained (~190KB heap, ~140KB free with WiFi active). `picographics` is from Pimoroni's custom firmware.

## Data Storage Formats

**CSV** (`Software/ecg_data/ecg_YYYYMMDD_HHMMSS.csv`): `timestamp_ms,raw_value,leads_off,bpm` — BPM column is empty except on beat-detection samples.

**Event JSON** (`Software/ecg_data/events/event_YYYYMMDD_HHMMSS_{type}.json`): Contains `version`, `type` (pause/premature/manual), `baseline_rr_ms`, `anomalous_beats` array, and raw `samples` array with format `[timestamp_ms, raw_value, leads_off, bpm_or_null]`.

**Pico PVC log** (legacy, `main_simple.py` only — `/pvc_events.jsonl` on flash): JSONL with `v`, `t_ms`, `bpm`, and `samples` array (raw ADC values only, ~2000 samples per event). The WiFi-enabled `main.py` does not write PVC events to flash.

## ECG Rendering Standards

`ECG.md` documents clinical ECG display guidelines (signal orientation, grid formatting, trace colors, calibration, strip layout). Consult this when modifying any waveform rendering in the web UI, Processing app, or Pico display.

## Licensing

Hardware: CC BY-SA 3.0. Code: Beerware license.
