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
Desktop app: real-time ECG waveform (red=valid, blue=leads-off), BPM via fixed 620.0 ADC threshold with rolling 500-beat average, calibration grid, PNG frame export. Serial port selected via `Serial.list()[2]`.

### 3. Python web server (`Software/ecg_server.py`)
aiohttp-based server with embedded single-page HTML/JS app. Features:
- Adaptive threshold BPM detection (Schmitt trigger with auto-polarity)
- Arrhythmia detection (R-R interval deviation state machine)
- PVC detection (opposite-direction spikes)
- CSV recording to `Software/ecg_data/`
- Arrhythmia event JSON files in `Software/ecg_data/events/`
- WebSocket broadcast at 60Hz to browser clients
- REST API: `GET /api/events`, `GET /api/events/{filename}`, `POST /api/record-event`

### 4. Pico 2W MicroPython display (`pico2w-display/`)
Runs on Raspberry Pi Pico 2W with Pimoroni Pico Display. Dual-core:
- **Core 0**: ADC read + serial output at ~1kHz (same protocol as Arduino)
- **Core 1**: BPM detection + display rendering at ~5Hz

Wiring: AD8232 OUTPUT→GP26, LO+→GP2, LO-→GP3. Button A cycles display pages, Button B toggles backlight. PVC events logged to `/pvc_events.jsonl` on flash.

`main_bare.py` is a minimal variant (no display, no BPM — raw ADC only).

## Build & Run

**Arduino sketch**: Open `.ino` in Arduino IDE, select "Arduino Pro 3.3V/8MHz", upload.

**Processing sketch**: Open `.pde` in Processing IDE (Java mode), run. Adjust `Serial.list()` index for your system.

**Python web server**:
```bash
Software/run.sh                    # creates venv, installs deps, starts server
# or manually:
pip install aiohttp pyserial-asyncio
python Software/ecg_server.py --serial /dev/ttyACM0 --port 8080 --baud 9600
```
Open `http://localhost:8080` in browser. CLI args also settable via env vars: `ECG_PORT`, `ECG_SERIAL_PORT`, `ECG_SERIAL_BAUD`.

**Pico 2W**: Copy `pico2w-display/boot.py` and `main.py` to the Pico 2W filesystem (requires MicroPython with `picographics` module from Pimoroni).

## Serial Protocol

Unidirectional MCU→host. ASCII newline-terminated: integer `0`–`1023` (ADC reading) or `!` (leads-off detected). All three frontends consume this same protocol.

## Key Configuration Points

- **BPM threshold**: Fixed `620.0` in Processing; adaptive (percentile-based) in ecg_server.py and Pico 2W
- **Serial port**: `Serial.list()[2]` in Processing; `--serial` flag or `ECG_SERIAL_PORT` env var for Python server
- **Arrhythmia detection**: 30% R-R deviation, 10-beat baseline window, 5s cooldown between events (ecg_server.py constants)
- **Pico 2W pins**: ADC0=GP26, LO+=GP2, LO-=GP3, display on GP6-8/GP12-20

## Licensing

Hardware: CC BY-SA 3.0. Code: Beerware license.
