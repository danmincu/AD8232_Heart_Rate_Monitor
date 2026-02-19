#!/usr/bin/env python3
"""AD8232 ECG Web Monitor — real-time browser-based ECG visualization.

Usage:
    pip install aiohttp pyserial-asyncio
    python ecg_server.py [--port 8080] [--serial /dev/ttyACM0] [--baud 9600]

Open http://localhost:8080 in your browser.
"""

import argparse
import asyncio
import json
import logging
import os
import re
import statistics
import time
from collections import deque
from datetime import datetime
from pathlib import Path

from aiohttp import web

log = logging.getLogger("ecg")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BUFFER_SIZE = 50000          # ~50 s at 1 kHz
BPM_THRESHOLD = 620.0        # ADC threshold for beat detection
BPM_WINDOW = 500             # rolling-average window (beats)
CSV_FLUSH_EVERY = 1000       # flush CSV every N samples
BPM_REFRACTORY_MS = 350      # ignore threshold resets for 350ms after a beat

# Arrhythmia detection
ARR_DEVIATION_PCT = 0.30        # 30% R-R deviation from baseline = anomaly
ARR_BASELINE_WINDOW = 10        # median of last 10 R-R intervals = baseline
ARR_MIN_BEATS = 5               # minimum beats before detection activates
ARR_BEATS_BEFORE = 15           # beats of context before event
ARR_BEATS_AFTER = 15            # beats of context after event
ARR_RETURN_BEATS = 3            # consecutive normal beats to confirm event end
ARR_COOLDOWN_MS = 5000          # minimum gap between events

# ---------------------------------------------------------------------------
# Shared state
# ---------------------------------------------------------------------------


class ECGState:
    def __init__(self):
        self.samples = deque(maxlen=BUFFER_SIZE)
        self.broadcast_queue: asyncio.Queue = asyncio.Queue()
        self.ws_clients: set = set()
        # BPM
        self.beats = [0.0] * BPM_WINDOW
        self.beat_index = 0
        self.last_beat_ms = 0
        self.last_beat_rearm_ms = 0          # refractory period tracking
        self.below_threshold = True
        self.current_bpm = 0
        # Adaptive threshold
        self.recent_values = deque(maxlen=5000)  # ~5 s of ADC values
        self.adaptive_threshold = BPM_THRESHOLD  # initial guess
        self.rearm_threshold = BPM_THRESHOLD     # hysteresis re-arm level
        self.pvc_threshold = BPM_THRESHOLD       # opposite-direction PVC detect
        self.pvc_rearm = BPM_THRESHOLD           # PVC re-arm level
        self.pvc_armed = True                    # PVC detector armed
        self.peaks_go_up = True                  # auto-detected polarity
        self.thresh_counter = 0                  # recompute every 500 samples
        # CSV
        self.csv_file = None
        self.csv_count = 0
        # Arrhythmia detection
        self.rr_intervals = deque(maxlen=20)        # recent R-R intervals (ms)
        self.beat_sample_indices = deque(maxlen=50)  # monotonic sample index at each beat
        self.total_samples_appended = 0              # monotonic counter (never resets)
        self.arrhythmia_state = "normal"             # "normal" | "anomalous"
        self.anomalous_beats = []                    # [(rr_ms, deviation_pct), ...]
        self.event_trigger_ts = 0                    # cooldown tracking
        self.beats_since_return = 0                  # normal beats after anomaly ends
        self.pending_event_notifications = []        # WS notifications to broadcast
        # Event storage
        self.events_dir = None                       # Path, set at startup
        self.event_count = 0


state = ECGState()


def _update_adaptive_threshold():
    """Recompute adaptive threshold from recent ADC values.

    Determines signal polarity (normal vs inverted) and sets the beat-detection
    threshold at 80 % of the peak range from the median baseline, with a
    30 % re-arm threshold (Schmitt trigger hysteresis).
    """
    vals = sorted(state.recent_values)
    n = len(vals)
    if n < 100:
        return  # not enough data yet
    p1 = vals[int(n * 0.01)]
    p50 = vals[int(n * 0.50)]
    p99 = vals[int(n * 0.99)]
    range_up = p99 - p50
    range_down = p50 - p1
    if range_up >= range_down:
        state.peaks_go_up = True
        state.adaptive_threshold = p50 + 0.80 * range_up
        state.rearm_threshold = p50 + 0.30 * range_up
        state.pvc_threshold = p50 - 0.15 * range_up
        state.pvc_rearm = p50 - 0.05 * range_up
    else:
        state.peaks_go_up = False
        state.adaptive_threshold = p50 - 0.80 * range_down
        state.rearm_threshold = p50 - 0.30 * range_down
        state.pvc_threshold = p50 + 0.15 * range_down
        state.pvc_rearm = p50 + 0.05 * range_down
    log.debug(
        "Adaptive threshold: %.1f  rearm: %.1f  polarity=%s  p1=%d p50=%d p99=%d",
        state.adaptive_threshold, state.rearm_threshold,
        "UP" if state.peaks_go_up else "DOWN",
        p1, p50, p99,
    )


# ---------------------------------------------------------------------------
# Arrhythmia detection
# ---------------------------------------------------------------------------


def _check_arrhythmia(ts, rr_ms):
    """State machine: detect arrhythmia events based on R-R interval deviation."""
    # Need enough beats to establish a baseline
    if len(state.rr_intervals) < ARR_MIN_BEATS:
        return

    # Compute baseline: median of last N R-R intervals (excluding current)
    history = list(state.rr_intervals)
    # Exclude the current interval (last element) for baseline
    baseline_pool = history[:-1] if len(history) > 1 else history
    baseline_pool = baseline_pool[-ARR_BASELINE_WINDOW:]
    baseline = statistics.median(baseline_pool)

    if baseline <= 0:
        return

    deviation = (rr_ms - baseline) / baseline  # signed: >0 = pause, <0 = premature

    is_anomalous = abs(deviation) > ARR_DEVIATION_PCT

    if state.arrhythmia_state == "normal":
        if is_anomalous:
            # Check cooldown
            if ts - state.event_trigger_ts < ARR_COOLDOWN_MS:
                return
            state.arrhythmia_state = "anomalous"
            state.anomalous_beats = [(rr_ms, round(deviation * 100, 1))]
            state.beats_since_return = 0
            log.info(
                "Arrhythmia onset: rr=%dms baseline=%dms dev=%.1f%%",
                rr_ms, baseline, deviation * 100,
            )
    elif state.arrhythmia_state == "anomalous":
        if is_anomalous:
            state.anomalous_beats.append((rr_ms, round(deviation * 100, 1)))
            state.beats_since_return = 0
        else:
            state.beats_since_return += 1
            if state.beats_since_return >= ARR_RETURN_BEATS:
                # Event confirmed — save it
                _save_arrhythmia_event(ts, baseline)
                state.arrhythmia_state = "normal"
                state.anomalous_beats = []
                state.beats_since_return = 0
                state.event_trigger_ts = ts


def _save_arrhythmia_event(ts, baseline_rr):
    """Persist an arrhythmia event to a JSON file and queue WS notification."""
    if state.events_dir is None:
        return

    # Classify event type based on average deviation sign
    avg_dev = sum(b[1] for b in state.anomalous_beats) / len(state.anomalous_beats)
    event_type = "pause" if avg_dev > 0 else "premature"

    # Extract sample context from the deque
    # Find sample range: ARR_BEATS_BEFORE beats before first anomalous beat
    # to ARR_BEATS_AFTER beats after the last anomalous beat
    beat_indices = list(state.beat_sample_indices)
    n_beats = len(beat_indices)

    # The anomalous beats are the most recent len(anomalous_beats) + beats_since_return
    # beats (anomalous + the normal return beats)
    total_event_beats = len(state.anomalous_beats) + ARR_RETURN_BEATS
    event_end_pos = n_beats  # position in beat_indices list (exclusive)
    event_start_pos = max(0, event_end_pos - total_event_beats)

    # Context range
    ctx_start_pos = max(0, event_start_pos - ARR_BEATS_BEFORE)
    ctx_end_pos = min(n_beats, event_end_pos)  # after beats are already included

    if ctx_start_pos >= n_beats or ctx_end_pos <= 0:
        return

    # Convert monotonic indices to deque-relative
    samples_list = state.samples
    deque_base = state.total_samples_appended - len(samples_list)

    abs_start = beat_indices[ctx_start_pos]
    # End: use current total_samples_appended as the end bound
    abs_end = state.total_samples_appended

    rel_start = max(0, abs_start - deque_base)
    rel_end = min(len(samples_list), abs_end - deque_base)

    if rel_start >= rel_end:
        return

    # Extract samples
    event_samples = [list(samples_list[i]) for i in range(rel_start, rel_end)]

    now = datetime.now()
    state.event_count += 1
    filename = f"event_{now:%Y%m%d_%H%M%S}_{event_type}.json"

    event_data = {
        "version": 1,
        "type": event_type,
        "detected_at_iso": now.isoformat(timespec="seconds"),
        "baseline_rr_ms": round(baseline_rr),
        "anomalous_beats": [
            {"rr_ms": b[0], "deviation_pct": b[1]} for b in state.anomalous_beats
        ],
        "samples": event_samples,
        "sample_format": ["timestamp_ms", "raw_value", "leads_off", "bpm_or_null"],
    }

    filepath = state.events_dir / filename
    try:
        with open(filepath, "w") as f:
            json.dump(event_data, f)
        log.info("Arrhythmia event #%d saved: %s (%s)", state.event_count, filename, event_type)
    except OSError as e:
        log.error("Failed to save arrhythmia event: %s", e)
        return

    # Queue notification for WebSocket broadcast
    notification = {
        "type": "arrhythmia_event",
        "event_type": event_type,
        "filename": filename,
        "detected_at": now.isoformat(timespec="seconds"),
        "baseline_rr_ms": round(baseline_rr),
        "anomalous_beat_count": len(state.anomalous_beats),
    }
    state.pending_event_notifications.append(notification)


# ---------------------------------------------------------------------------
# Sample processing
# ---------------------------------------------------------------------------


def process_sample(raw_line: str):
    """Parse one serial line, compute BPM, store, enqueue for broadcast."""
    raw_line = raw_line.strip()
    if not raw_line:
        return

    ts = int(time.time() * 1000)
    leads_off = 0
    bpm = None

    if raw_line == "!":
        value = 512
        leads_off = 1
    else:
        try:
            value = int(raw_line)
        except ValueError:
            return

        # Track recent values for adaptive threshold
        state.recent_values.append(value)
        state.thresh_counter += 1
        if state.thresh_counter >= 500:
            _update_adaptive_threshold()
            state.thresh_counter = 0

        # Adaptive threshold-crossing beat detection (Schmitt trigger)
        thresh = state.adaptive_threshold
        if state.peaks_go_up:
            beat_detected = value > thresh and state.below_threshold
            reset_condition = value < state.rearm_threshold
            pvc_hit = value < state.pvc_threshold and state.pvc_armed
            pvc_reset = value > state.pvc_rearm
        else:
            beat_detected = value < thresh and not state.below_threshold
            reset_condition = value > state.rearm_threshold
            pvc_hit = value > state.pvc_threshold and state.pvc_armed
            pvc_reset = value < state.pvc_rearm

        # PVC detection (opposite-direction spike — not counted as beat)
        if pvc_hit:
            state.pvc_armed = False
            bpm = -1  # PVC marker
        elif pvc_reset:
            state.pvc_armed = True

        if beat_detected:
            if state.peaks_go_up:
                state.below_threshold = False
            else:
                state.below_threshold = True
            state.last_beat_rearm_ms = ts     # start refractory period
            if state.last_beat_ms > 0:
                diff = ts - state.last_beat_ms
                if 200 < diff < 3000:                    # 20-300 BPM
                    instant = 60000.0 / diff
                    state.beats[state.beat_index] = instant
                    state.beat_index = (state.beat_index + 1) % BPM_WINDOW
                    non_zero = [b for b in state.beats if b > 0]
                    if non_zero:
                        state.current_bpm = int(sum(non_zero) / len(non_zero))
                    bpm = int(instant)
                    state.beat_sample_indices.append(state.total_samples_appended)
                    state.rr_intervals.append(diff)
                    _check_arrhythmia(ts, diff)
            state.last_beat_ms = ts
        elif reset_condition:
            # Only re-arm after refractory period to prevent T-wave false triggers
            if ts - state.last_beat_rearm_ms > BPM_REFRACTORY_MS:
                if state.peaks_go_up:
                    state.below_threshold = True
                else:
                    state.below_threshold = False

    sample = (ts, value, leads_off, bpm)
    state.samples.append(sample)
    state.total_samples_appended += 1

    # CSV
    if state.csv_file:
        bpm_s = str(bpm) if bpm is not None else ""
        state.csv_file.write(f"{ts},{value},{leads_off},{bpm_s}\n")
        state.csv_count += 1
        if state.csv_count % CSV_FLUSH_EVERY == 0:
            state.csv_file.flush()

    try:
        state.broadcast_queue.put_nowait(sample)
    except asyncio.QueueFull:
        pass

# ---------------------------------------------------------------------------
# Serial reader
# ---------------------------------------------------------------------------


async def serial_reader_task(port: str, baud: int):
    data_dir = Path(__file__).parent / "ecg_data"
    data_dir.mkdir(exist_ok=True)
    events_dir = data_dir / "events"
    events_dir.mkdir(exist_ok=True)
    state.events_dir = events_dir
    csv_path = data_dir / f"ecg_{datetime.now():%Y%m%d_%H%M%S}.csv"
    state.csv_file = open(csv_path, "w")
    state.csv_file.write("timestamp_ms,raw_value,leads_off,bpm\n")
    log.info("Recording to %s", csv_path)

    while True:
        try:
            try:
                import serial_asyncio
                log.info("Connecting via pyserial-asyncio: %s @ %d", port, baud)
                reader, _ = await serial_asyncio.open_serial_connection(
                    url=port, baudrate=baud,
                )
                while True:
                    line = await reader.readline()
                    process_sample(line.decode(errors="replace"))
            except ImportError:
                import serial as pyserial
                log.info("Connecting via threaded pyserial: %s @ %d", port, baud)
                loop = asyncio.get_event_loop()
                ser = await loop.run_in_executor(
                    None, lambda: pyserial.Serial(port, baud, timeout=1),
                )
                try:
                    while True:
                        line = await loop.run_in_executor(None, ser.readline)
                        if line:
                            process_sample(line.decode(errors="replace"))
                finally:
                    ser.close()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.error("Serial error: %s — retrying in 2 s", e)
            await asyncio.sleep(2)

# ---------------------------------------------------------------------------
# WebSocket broadcaster
# ---------------------------------------------------------------------------


async def broadcaster_task():
    interval = 1.0 / 60
    while True:
        await asyncio.sleep(interval)

        if not state.ws_clients:
            # Drain queue to prevent unbounded growth
            while not state.broadcast_queue.empty():
                try:
                    state.broadcast_queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
            continue

        batch = []
        for _ in range(100):
            try:
                batch.append(state.broadcast_queue.get_nowait())
            except asyncio.QueueEmpty:
                break

        if not batch:
            continue

        msg = json.dumps({"type": "d", "s": [list(s) for s in batch]})
        dead = set()
        for ws in state.ws_clients:
            try:
                await ws.send_str(msg)
            except Exception:
                dead.add(ws)
        state.ws_clients -= dead

        # Broadcast arrhythmia event notifications
        while state.pending_event_notifications:
            notif = state.pending_event_notifications.pop(0)
            notif_msg = json.dumps(notif)
            dead = set()
            for ws in state.ws_clients:
                try:
                    await ws.send_str(notif_msg)
                except Exception:
                    dead.add(ws)
            state.ws_clients -= dead

# ---------------------------------------------------------------------------
# HTTP / WebSocket handlers
# ---------------------------------------------------------------------------


_index_html = (Path(__file__).parent / "static" / "index.html").read_text()


async def handle_index(request):
    return web.Response(text=_index_html, content_type="text/html")


async def handle_websocket(request):
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    log.info("WebSocket client connected")

    recent = list(state.samples)[-5000:]
    await ws.send_str(json.dumps({
        "type": "init",
        "config": {"bufferSize": BUFFER_SIZE, "bpmThreshold": BPM_THRESHOLD},
        "samples": [list(s) for s in recent],
        "bpm": state.current_bpm,
    }))
    state.ws_clients.add(ws)

    try:
        async for msg in ws:
            if msg.type == web.WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                    if data.get("type") == "history":
                        start = max(0, int(data.get("start", 0)))
                        count = min(int(data.get("count", 1000)), 5000)
                        all_samples = list(state.samples)
                        slc = all_samples[start:start + count]
                        await ws.send_str(json.dumps({
                            "type": "history_resp",
                            "samples": [list(s) for s in slc],
                            "start": start,
                            "totalAvailable": len(all_samples),
                        }))
                except (json.JSONDecodeError, ValueError, TypeError):
                    pass
            elif msg.type == web.WSMsgType.ERROR:
                break
    finally:
        state.ws_clients.discard(ws)
        log.info("WebSocket client disconnected")

    return ws

async def handle_events_list(request):
    """GET /api/events — list all saved arrhythmia events."""
    if state.events_dir is None or not state.events_dir.exists():
        return web.json_response([])

    events = []
    for fp in sorted(state.events_dir.glob("event_*.json"), reverse=True):
        try:
            with open(fp) as f:
                data = json.load(f)
            events.append({
                "filename": fp.name,
                "type": data.get("type", "unknown"),
                "detected_at": data.get("detected_at_iso", ""),
                "baseline_rr_ms": data.get("baseline_rr_ms", 0),
                "anomalous_beat_count": len(data.get("anomalous_beats", [])),
                "sample_count": len(data.get("samples", [])),
            })
        except (OSError, json.JSONDecodeError):
            continue
    return web.json_response(events)


async def handle_event_detail(request):
    """GET /api/events/{filename} — return full event JSON."""
    filename = request.match_info["filename"]
    # Validate filename: must match expected pattern, no path traversal
    if not re.match(r'^event_\d{8}_\d{6}_\w+\.json$', filename):
        return web.json_response({"error": "invalid filename"}, status=400)

    if state.events_dir is None:
        return web.json_response({"error": "not found"}, status=404)

    filepath = state.events_dir / filename
    if not filepath.exists():
        return web.json_response({"error": "not found"}, status=404)

    try:
        with open(filepath) as f:
            data = json.load(f)
        return web.json_response(data)
    except (OSError, json.JSONDecodeError) as e:
        return web.json_response({"error": str(e)}, status=500)


async def handle_record_event(request):
    """POST /api/record-event — manually save a snapshot of recent beats."""
    try:
        body = await request.json()
    except (json.JSONDecodeError, Exception):
        body = {}

    num_beats = int(body.get("beats", 100))
    num_beats = max(5, min(num_beats, 500))  # clamp to reasonable range

    if state.events_dir is None:
        return web.json_response({"error": "events dir not ready"}, status=503)

    beat_indices = list(state.beat_sample_indices)
    n_beats = len(beat_indices)

    if n_beats < 2:
        return web.json_response({"error": "not enough beats recorded yet"}, status=400)

    # Take last num_beats beats worth of samples
    ctx_start_pos = max(0, n_beats - num_beats)
    samples_list = state.samples
    deque_base = state.total_samples_appended - len(samples_list)

    abs_start = beat_indices[ctx_start_pos]
    abs_end = state.total_samples_appended

    rel_start = max(0, abs_start - deque_base)
    rel_end = min(len(samples_list), abs_end - deque_base)

    if rel_start >= rel_end:
        return web.json_response({"error": "no samples in range"}, status=400)

    event_samples = [list(samples_list[i]) for i in range(rel_start, rel_end)]

    # Compute baseline R-R from recent intervals
    rr_list = list(state.rr_intervals)
    baseline_rr = round(statistics.median(rr_list)) if rr_list else 0

    now = datetime.now()
    state.event_count += 1
    filename = f"event_{now:%Y%m%d_%H%M%S}_manual.json"

    event_data = {
        "version": 1,
        "type": "manual",
        "detected_at_iso": now.isoformat(timespec="seconds"),
        "baseline_rr_ms": baseline_rr,
        "anomalous_beats": [],
        "samples": event_samples,
        "sample_format": ["timestamp_ms", "raw_value", "leads_off", "bpm_or_null"],
    }

    filepath = state.events_dir / filename
    try:
        with open(filepath, "w") as f:
            json.dump(event_data, f)
        log.info("Manual event #%d saved: %s (%d beats)", state.event_count, filename, num_beats)
    except OSError as e:
        return web.json_response({"error": str(e)}, status=500)

    notification = {
        "type": "arrhythmia_event",
        "event_type": "manual",
        "filename": filename,
        "detected_at": now.isoformat(timespec="seconds"),
        "baseline_rr_ms": baseline_rr,
        "anomalous_beat_count": 0,
    }
    state.pending_event_notifications.append(notification)

    return web.json_response({"ok": True, "filename": filename})


# ---------------------------------------------------------------------------
# App lifecycle
# ---------------------------------------------------------------------------


async def on_startup(app):
    app["serial_task"] = asyncio.create_task(
        serial_reader_task(app["serial_port"], app["serial_baud"]),
    )
    app["broadcast_task"] = asyncio.create_task(broadcaster_task())
    log.info("Background tasks started")


async def on_shutdown(app):
    for name in ("serial_task", "broadcast_task"):
        task = app.get(name)
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
    if state.csv_file:
        state.csv_file.flush()
        state.csv_file.close()
        log.info("CSV file closed")
    for ws in set(state.ws_clients):
        await ws.close()

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(description="AD8232 ECG Web Monitor")
    parser.add_argument(
        "--port", type=int,
        default=int(os.environ.get("ECG_PORT", "8080")),
        help="HTTP port (default: 8080)",
    )
    parser.add_argument(
        "--serial",
        default=os.environ.get("ECG_SERIAL_PORT", "/dev/ttyACM0"),
        help="Serial port (default: /dev/ttyACM0)",
    )
    parser.add_argument(
        "--baud", type=int,
        default=int(os.environ.get("ECG_SERIAL_BAUD", "9600")),
        help="Baud rate (default: 9600)",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    app = web.Application()
    app["serial_port"] = args.serial
    app["serial_baud"] = args.baud
    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)
    app.router.add_get("/", handle_index)
    app.router.add_get("/ws", handle_websocket)
    app.router.add_get("/api/events", handle_events_list)
    app.router.add_get("/api/events/{filename}", handle_event_detail)
    app.router.add_post("/api/record-event", handle_record_event)

    log.info("Starting ECG server on http://0.0.0.0:%d", args.port)
    log.info("Serial: %s @ %d baud", args.serial, args.baud)
    web.run_app(app, host="0.0.0.0", port=args.port)


if __name__ == "__main__":
    main()
