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

BUFFER_SIZE = 25000          # ~25 s at 1 kHz
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


async def handle_index(request):
    return web.Response(text=HTML_PAGE, content_type="text/html")


async def handle_websocket(request):
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    log.info("WebSocket client connected")

    recent = list(state.samples)[-2500:]
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
# Embedded HTML page
# ---------------------------------------------------------------------------

HTML_PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AD8232 ECG Monitor</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{background:#111;font-family:system-ui,-apple-system,sans-serif;overflow:hidden}
#bar{height:40px;background:#1a1a2e;color:#ccc;display:flex;align-items:center;
  padding:0 16px;font-size:14px;gap:20px;border-bottom:2px solid #0f3460;user-select:none}
.bpm{color:#ff4444;font-weight:bold;font-size:16px;min-width:140px;white-space:nowrap}
.val{color:#888;font-family:monospace;min-width:110px}
.st{margin-left:auto;font-size:12px}
.st.ok{color:#4caf50}.st.err{color:#f44336}
#lbtn{padding:3px 14px;background:transparent;color:#e94560;border:1px solid #e94560;
  border-radius:3px;cursor:pointer;font-size:11px;font-weight:bold;display:none}
#lbtn:hover{background:#e94560;color:#fff}
canvas{display:block;cursor:crosshair}
/* Events button & panel */
#evbtn{padding:3px 14px;background:transparent;color:#ffb347;border:1px solid #ffb347;
  border-radius:3px;cursor:pointer;font-size:11px;font-weight:bold}
#evbtn:hover{background:#ffb347;color:#111}
#recbtn{padding:3px 14px;background:transparent;color:#4caf50;border:1px solid #4caf50;
  border-radius:3px;cursor:pointer;font-size:11px;font-weight:bold}
#recbtn:hover{background:#4caf50;color:#111}
#recbeats{width:50px;padding:2px 4px;background:#222;color:#ccc;border:1px solid #555;
  border-radius:3px;font-size:11px;text-align:center;font-family:monospace}
#evpanel{display:none;position:absolute;top:42px;right:16px;width:380px;max-height:60vh;
  background:#1a1a2e;border:1px solid #0f3460;border-radius:6px;overflow-y:auto;z-index:100;
  box-shadow:0 4px 20px rgba(0,0,0,0.5)}
#evpanel.open{display:block}
#evpanel .ev-header{padding:10px 14px;border-bottom:1px solid #0f3460;font-size:13px;
  color:#888;font-weight:bold}
.ev-item{padding:10px 14px;border-bottom:1px solid rgba(255,255,255,0.05);cursor:pointer;
  display:flex;align-items:center;gap:10px}
.ev-item:hover{background:rgba(255,255,255,0.05)}
.ev-type{padding:2px 8px;border-radius:3px;font-size:10px;font-weight:bold;text-transform:uppercase}
.ev-pause{background:#ff8f00;color:#111}
.ev-premature{background:#ff5722;color:#fff}
.ev-manual{background:#4caf50;color:#fff}
.ev-info{flex:1;font-size:12px;color:#aaa}
.ev-info .ev-time{color:#ccc;font-weight:bold}
.ev-empty{padding:20px;text-align:center;color:#555;font-size:13px}
/* Event viewer overlay */
#evoverlay{display:none;position:absolute;top:50px;left:16px;background:rgba(26,26,46,0.92);
  border:1px solid #0f3460;border-radius:6px;padding:12px 16px;z-index:90;font-size:12px;
  color:#ccc;max-width:340px}
#evoverlay.open{display:block}
#evoverlay .evo-title{font-size:15px;font-weight:bold;margin-bottom:6px}
#evoverlay .evo-row{margin:3px 0;color:#aaa}
#evoverlay .evo-row b{color:#ccc}
</style>
</head>
<body>
<div id="bar">
  <span class="bpm" id="bpm">&#9829; -- BPM</span>
  <span class="val" id="val">data: ---</span>
  <button id="lbtn" onclick="window._goLive()">&#9654; LIVE</button>
  <input id="recbeats" type="number" value="100" min="5" max="500" title="Beats to record">
  <button id="recbtn" onclick="window._recordEvent()">REC</button>
  <button id="evbtn" onclick="window._toggleEvents()">Events (0)</button>
  <span class="st err" id="st">&#9679; Connecting</span>
</div>
<div id="evpanel"><div class="ev-header">Arrhythmia Events</div><div id="evlist"></div></div>
<div id="evoverlay"><div class="evo-title" id="evo-title"></div><div id="evo-body"></div></div>
<canvas id="c"></canvas>
<script>
(function(){
"use strict";

var canvas = document.getElementById("c");
var ctx = canvas.getContext("2d");
var gridCv = document.createElement("canvas");
var gridCtx = gridCv.getContext("2d");

var samples = [];       // [[ts, value, leadsOff(0|1), bpm|null], ...]
var viewOffset = 0;     // 0 = live
var isLive = true;
var needsRedraw = true;
var signalBaseline = -1;
var signalRange = -1;          // adaptive vertical range (auto-scaled)
var lastBPMs = [];
var COMPRESS = 5;              // samples per pixel (2 = show 2x more data)

/* ---- arrhythmia state ---- */
var arrhythmiaMarkers = [];   // [{sampleIdx, type, filename}]
var eventViewerMode = false;
var eventViewerData = null;   // full event JSON when viewing
var eventCount = 0;
var liveSamplesBackup = null;

/* ---- resize ---- */
function resize(){
  var h = window.innerHeight - 40;
  canvas.width = window.innerWidth;
  canvas.height = Math.max(h, 200);
  gridCv.width = canvas.width;
  gridCv.height = canvas.height;
  drawGrid();
  needsRedraw = true;
}
window.addEventListener("resize", resize);
resize();

/* ---- grid (offscreen, redrawn on resize) ---- */
function drawGrid(){
  var w = gridCv.width, h = gridCv.height, g = gridCtx;
  g.fillStyle = "#ffffff";
  g.fillRect(0, 0, w, h);

  var midY = Math.round(h / 2);

  /* vertical lines */
  var x;
  for(x = 0; x < w; x += 40){
    var vmaj = (x % 200 === 0);
    g.strokeStyle = vmaj ? "rgba(220,80,80,0.40)" : "rgba(255,150,150,0.35)";
    g.lineWidth   = vmaj ? 1.0 : 0.5;
    g.beginPath(); g.moveTo(x + 0.5, 0); g.lineTo(x + 0.5, h); g.stroke();
  }

  /* horizontal lines centred on midY */
  var i, y1, y2, hmaj;
  for(i = 0; i * 40 <= h; i++){
    hmaj = (i % 5 === 0);
    var offsets = (i === 0) ? [0] : [-i * 40, i * 40];
    for(var oi = 0; oi < offsets.length; oi++){
      var y = midY + offsets[oi];
      if(y < 0 || y > h) continue;
      g.strokeStyle = hmaj ? "rgba(220,80,80,0.40)" : "rgba(255,150,150,0.35)";
      g.lineWidth   = hmaj ? 1.0 : 0.5;
      g.beginPath(); g.moveTo(0, Math.round(y)+0.5); g.lineTo(w, Math.round(y)+0.5); g.stroke();
    }
  }
}

/* ---- helpers ---- */
/* v2y uses adaptive signalRange instead of hardcoded 1023 */
function v2y(v){
  var range = (signalRange > 0) ? signalRange : 1023;
  return canvas.height / 2 + (v - signalBaseline) * (canvas.height / range);
}

/* ---- render ---- */
function render(){
  if(!needsRedraw){ requestAnimationFrame(render); return; }
  needsRedraw = false;

  var w = canvas.width, h = canvas.height, n = samples.length;
  ctx.drawImage(gridCv, 0, 0);

  if(n < 2){ requestAnimationFrame(render); return; }

  var endIdx   = Math.max(1, n - viewOffset);
  var startIdx = Math.max(0, endIdx - w * COMPRESS);
  var count    = endIdx - startIdx;
  var xOff     = w - Math.ceil(count / COMPRESS);  /* right-align trace */

  /* auto-centre + auto-scale from visible data */
  var vmin = 99999, vmax = -99999;
  for(var j = startIdx; j < endIdx; j++){
    var sv = samples[j][1];
    if(sv < vmin) vmin = sv;
    if(sv > vmax) vmax = sv;
  }

  /* Centre on midpoint of min/max (not the mean — the mean is biased toward
     baseline and causes asymmetric peaks to clip off-screen) */
  var visMid = (vmin + vmax) / 2;
  if(signalBaseline < 0) signalBaseline = visMid;
  else if(!eventViewerMode) signalBaseline = signalBaseline * 0.90 + visMid * 0.10;
  else signalBaseline = visMid;

  /* Adaptive range: grow FAST to catch peaks, shrink slowly to stay stable.
     30 % padding ensures peaks don't touch the very edge of the canvas. */
  var visRange = Math.max(vmax - vmin, 30) * 1.3;
  if(signalRange < 0){
    signalRange = visRange;
  } else if(!eventViewerMode){
    if(visRange > signalRange) signalRange = signalRange * 0.3 + visRange * 0.7;
    else                       signalRange = signalRange * 0.98 + visRange * 0.02;
  } else {
    signalRange = visRange;
  }

  /* Arrhythmia markers — vertical bands on live trace */
  if(!eventViewerMode){
    for(var mi = 0; mi < arrhythmiaMarkers.length; mi++){
      var marker = arrhythmiaMarkers[mi];
      var mx = Math.round((marker.sampleIdx - startIdx) / COMPRESS) + xOff;
      if(mx >= 0 && mx < w){
        var mcolor = marker.type === "pause" ? "rgba(255,143,0,0.25)"
                   : marker.type === "premature" ? "rgba(255,87,34,0.25)"
                   : "rgba(76,175,80,0.25)";
        ctx.fillStyle = mcolor;
        ctx.fillRect(mx - 15, 0, 30, h);
        ctx.font = "bold 11px sans-serif";
        ctx.fillStyle = marker.type === "pause" ? "#ff8f00"
                      : marker.type === "premature" ? "#ff5722" : "#4caf50";
        var label = marker.type === "pause" ? "PAUSE"
                  : marker.type === "premature" ? "PVC" : "REC";
        ctx.fillText(label, mx - 12, 16);
      }
    }
  }

  /* Event viewer: highlight anomalous beat regions */
  if(eventViewerMode && eventViewerData && eventViewerData.anomalous_beats && eventViewerData.anomalous_beats.length > 0){
    /* find beat positions (samples with bpm != null) */
    var beatPositions = [];
    for(var bi = startIdx; bi < endIdx; bi++){
      if(samples[bi][3] !== null && samples[bi][3] !== undefined){
        beatPositions.push(bi);
      }
    }
    /* highlight the last N beats where N = anomalous count + some context */
    var anomCount = eventViewerData.anomalous_beats.length;
    if(beatPositions.length > 0){
      /* The anomalous beats are roughly in the middle-to-end of the recording */
      var hlStart = Math.max(0, beatPositions.length - anomCount - 5);
      var hlEnd = Math.min(beatPositions.length, hlStart + anomCount + 2);
      if(hlStart < beatPositions.length && hlEnd > 0){
        var hlX1 = Math.round((beatPositions[hlStart] - startIdx) / COMPRESS) + xOff;
        var hlX2 = Math.round((beatPositions[Math.min(hlEnd, beatPositions.length - 1)] - startIdx) / COMPRESS) + xOff;
        var hlColor = eventViewerData.type === "pause" ? "rgba(255,143,0,0.15)"
                    : eventViewerData.type === "premature" ? "rgba(255,87,34,0.15)"
                    : "rgba(76,175,80,0.15)";
        ctx.fillStyle = hlColor;
        ctx.fillRect(hlX1, 0, Math.max(hlX2 - hlX1, 20), h);
      }
    }
  }

  /* ECG trace — batch consecutive same-colour segments */
  ctx.lineWidth = 1.8;
  ctx.lineJoin = "round";
  ctx.lineCap  = "round";

  var curLO = samples[startIdx][2];
  ctx.strokeStyle = curLO ? "#0000ff" : "#000000";
  ctx.beginPath();
  ctx.moveTo(xOff, v2y(samples[startIdx][1]));

  var bpmLabels = [];
  var i, s, px, py;

  for(i = startIdx + 1; i < endIdx; i++){
    s  = samples[i];
    px = Math.round((i - startIdx) / COMPRESS) + xOff;
    py = v2y(s[1]);

    if(s[2] !== curLO){
      ctx.lineTo(px, py);
      ctx.stroke();
      curLO = s[2];
      ctx.strokeStyle = curLO ? "#0000ff" : "#000000";
      ctx.beginPath();
      ctx.moveTo(px, py);
    } else {
      ctx.lineTo(px, py);
    }

    if(s[3] !== null && s[3] !== undefined){
      bpmLabels.push([px, s[3]]);
    }
  }
  ctx.stroke();

  /* BPM beat labels — faint, above the peaks */
  ctx.font = "bold 14px sans-serif";
  for(i = 0; i < bpmLabels.length; i++){
    var lx = bpmLabels[i][0], lb = bpmLabels[i][1];
    var ly = Math.round(h * 0.08);
    if(lb < 0){
      ctx.fillStyle = "rgba(255,100,0,0.60)";
      ctx.fillText("PVC", lx - 10, ly);
    } else {
      ctx.fillStyle = "rgba(0,0,0,0.30)";
      ctx.fillText("\u2665 " + lb, lx - 10, ly);
    }
  }

  /* Clinical annotations */
  ctx.font = "bold 13px sans-serif";
  ctx.fillStyle = "rgba(0,0,0,0.6)";
  ctx.fillText("II", 10, 24);
  ctx.font = "11px sans-serif";
  ctx.fillStyle = "rgba(0,0,0,0.45)";
  ctx.fillText("25 mm/s", 10, h - 22);
  ctx.fillText("10 mm/mV", 10, h - 8);

  /* scroll-position indicator when not live */
  if(!isLive && n > w){
    var totalW = n;
    var visFrac = Math.min(1, w * COMPRESS / totalW);
    var scrollFrac = viewOffset / Math.max(1, n - w * COMPRESS);
    var barW = Math.max(30, w * visFrac);
    var barX = (w - barW) * (1 - scrollFrac);
    ctx.fillStyle = "rgba(0,0,0,0.25)";
    ctx.fillRect(0, h - 6, w, 6);
    ctx.fillStyle = "rgba(233,69,96,0.70)";
    ctx.fillRect(barX, h - 6, barW, 6);
  }

  /* Event viewer mode label */
  if(eventViewerMode){
    ctx.font = "bold 16px sans-serif";
    ctx.fillStyle = "rgba(233,69,96,0.8)";
    ctx.fillText("EVENT VIEWER", w - 160, 26);
  }

  requestAnimationFrame(render);
}
requestAnimationFrame(render);

/* ---- scroll ---- */
canvas.addEventListener("wheel", function(e){
  e.preventDefault();
  var delta = (e.deltaMode === 1) ? e.deltaY * 40 : e.deltaY;
  viewOffset = Math.round(Math.max(0, Math.min(
    viewOffset - delta,
    Math.max(0, samples.length - canvas.width * COMPRESS)
  )));
  isLive = (viewOffset === 0) && !eventViewerMode;
  document.getElementById("lbtn").style.display = (isLive && !eventViewerMode) ? "none" : "inline-block";
  if(eventViewerMode) document.getElementById("lbtn").textContent = "EXIT VIEWER";
  needsRedraw = true;
}, {passive: false});

window._goLive = function(){
  if(eventViewerMode){
    _exitViewer();
    return;
  }
  viewOffset = 0; isLive = true;
  document.getElementById("lbtn").style.display = "none";
  needsRedraw = true;
};

/* double-click to go live */
canvas.addEventListener("dblclick", function(){ window._goLive(); });

/* ---- BPM status bar ---- */
function updateBPMBar(){
  if(lastBPMs.length === 0){
    document.getElementById("bpm").textContent = "\u2665 -- BPM";
    return;
  }
  var sum = 0;
  for(var i = 0; i < lastBPMs.length; i++) sum += lastBPMs[i];
  var avg = Math.round(sum / lastBPMs.length);
  var display = "\u2665 " + avg + " avg | " + lastBPMs.join(", ");
  document.getElementById("bpm").textContent = display;
}

/* ---- events panel ---- */
window._toggleEvents = function(){
  var panel = document.getElementById("evpanel");
  panel.classList.toggle("open");
  if(panel.classList.contains("open")) _fetchEvents();
};

function _fetchEvents(){
  fetch("/api/events").then(function(r){ return r.json(); }).then(function(evts){
    var list = document.getElementById("evlist");
    if(evts.length === 0){
      list.innerHTML = '<div class="ev-empty">No events recorded yet</div>';
      return;
    }
    var html = "";
    for(var i = 0; i < evts.length; i++){
      var ev = evts[i];
      var cls = ev.type === "pause" ? "ev-pause"
              : ev.type === "premature" ? "ev-premature" : "ev-manual";
      html += '<div class="ev-item" onclick="window._viewEvent(\'' + ev.filename + '\')">'
            + '<span class="ev-type ' + cls + '">' + ev.type + '</span>'
            + '<div class="ev-info"><div class="ev-time">' + ev.detected_at + '</div>'
            + ev.anomalous_beat_count + ' anom. beats | ' + ev.sample_count + ' samples</div></div>';
    }
    list.innerHTML = html;
  }).catch(function(){});
}

/* ---- event viewer ---- */
window._viewEvent = function(filename){
  fetch("/api/events/" + filename).then(function(r){ return r.json(); }).then(function(data){
    if(data.error) return;
    /* Close events panel */
    document.getElementById("evpanel").classList.remove("open");
    /* Backup live state */
    if(!eventViewerMode){
      liveSamplesBackup = samples.slice();
    }
    eventViewerMode = true;
    eventViewerData = data;
    samples = data.samples || [];
    viewOffset = 0;
    isLive = false;
    signalBaseline = -1;
    signalRange = -1;
    needsRedraw = true;

    /* Show exit button */
    var lbtn = document.getElementById("lbtn");
    lbtn.textContent = "EXIT VIEWER";
    lbtn.style.display = "inline-block";

    /* Show info overlay */
    var overlay = document.getElementById("evoverlay");
    overlay.classList.add("open");
    document.getElementById("evo-title").textContent =
      (data.type === "pause" ? "Pause Event" : data.type === "premature" ? "PVC Event" : "Manual Recording");
    var bodyHtml = '<div class="ovo-row"><b>Time:</b> ' + (data.detected_at_iso || "") + '</div>'
      + '<div class="ovo-row"><b>Baseline R-R:</b> ' + data.baseline_rr_ms + ' ms</div>'
      + '<div class="ovo-row"><b>Anomalous beats:</b> ' + (data.anomalous_beats ? data.anomalous_beats.length : 0) + '</div>';
    if(data.anomalous_beats){
      for(var ab = 0; ab < data.anomalous_beats.length; ab++){
        bodyHtml += '<div class="ovo-row">  R-R ' + data.anomalous_beats[ab].rr_ms
          + 'ms (' + (data.anomalous_beats[ab].deviation_pct > 0 ? '+' : '')
          + data.anomalous_beats[ab].deviation_pct + '%)</div>';
      }
    }
    document.getElementById("evo-body").innerHTML = bodyHtml;
  }).catch(function(){});
};

function _exitViewer(){
  eventViewerMode = false;
  eventViewerData = null;
  document.getElementById("evoverlay").classList.remove("open");
  if(liveSamplesBackup){
    samples = liveSamplesBackup;
    liveSamplesBackup = null;
  }
  viewOffset = 0;
  isLive = true;
  signalBaseline = -1;
  signalRange = -1;
  var lbtn = document.getElementById("lbtn");
  lbtn.textContent = "\u25b6 LIVE";
  lbtn.style.display = "none";
  needsRedraw = true;
}

/* ---- manual record ---- */
window._recordEvent = function(){
  var beats = parseInt(document.getElementById("recbeats").value) || 100;
  fetch("/api/record-event", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({beats: beats})
  }).then(function(r){ return r.json(); }).then(function(data){
    if(data.ok){
      var btn = document.getElementById("recbtn");
      btn.textContent = "SAVED!";
      setTimeout(function(){ btn.textContent = "REC"; }, 1500);
    }
  }).catch(function(){});
};

/* ---- check URL param for direct event view ---- */
function checkEventParam(){
  var params = new URLSearchParams(window.location.search);
  var evFile = params.get("event");
  if(evFile) window._viewEvent(evFile);
}

/* ---- websocket ---- */
function connect(){
  var proto = (location.protocol === "https:") ? "wss:" : "ws:";
  var ws = new WebSocket(proto + "//" + location.host + "/ws");

  ws.onopen = function(){
    document.getElementById("st").textContent = "\u25cf Connected";
    document.getElementById("st").className = "st ok";
  };

  ws.onclose = function(){
    document.getElementById("st").textContent = "\u25cf Disconnected";
    document.getElementById("st").className = "st err";
    setTimeout(connect, 2000);
  };

  ws.onerror = function(){ ws.close(); };

  ws.onmessage = function(e){
    var msg = JSON.parse(e.data);

    if(msg.type === "init"){
      if(!eventViewerMode){
        samples = msg.samples || [];
        signalBaseline = -1;
        signalRange = -1;
      }
      lastBPMs = [];
      var initSamples = msg.samples || [];
      for(var k = 0; k < initSamples.length; k++){
        if(initSamples[k][3] !== null && initSamples[k][3] !== undefined && initSamples[k][3] > 0){
          lastBPMs.push(initSamples[k][3]);
        }
      }
      if(lastBPMs.length > 10) lastBPMs = lastBPMs.slice(lastBPMs.length - 10);
      updateBPMBar();
      if(initSamples.length > 0){
        document.getElementById("val").textContent = "data: " + initSamples[initSamples.length-1][1];
      }
      needsRedraw = true;
      /* Check URL param after init */
      checkEventParam();
      return;
    }

    if(msg.type === "d"){
      if(eventViewerMode) return; /* ignore live data in viewer mode */
      var batch = msg.s;
      var added = batch.length;
      for(var i = 0; i < added; i++){
        samples.push(batch[i]);
        if(batch[i][3] !== null && batch[i][3] !== undefined && batch[i][3] > 0){
          lastBPMs.push(batch[i][3]);
          if(lastBPMs.length > 10) lastBPMs.shift();
          updateBPMBar();
        }
      }
      if(added > 0){
        document.getElementById("val").textContent = "data: " + batch[added-1][1];
      }
      /* keep view at same position when scrolled back */
      if(!isLive) viewOffset += added;
      /* trim front */
      if(samples.length > 25000){
        var excess = samples.length - 25000;
        samples.splice(0, excess);
        viewOffset = Math.max(0, viewOffset - excess);
        /* adjust marker indices */
        for(var mi = arrhythmiaMarkers.length - 1; mi >= 0; mi--){
          arrhythmiaMarkers[mi].sampleIdx -= excess;
          if(arrhythmiaMarkers[mi].sampleIdx < 0) arrhythmiaMarkers.splice(mi, 1);
        }
      }
      /* clamp */
      viewOffset = Math.min(viewOffset, Math.max(0, samples.length - canvas.width * COMPRESS));
      if(isLive) needsRedraw = true;
      return;
    }

    if(msg.type === "history_resp"){
      if(!eventViewerMode){
        samples = msg.samples.concat(samples);
        if(samples.length > 25000) samples.splice(0, samples.length - 25000);
        needsRedraw = true;
      }
      return;
    }

    if(msg.type === "arrhythmia_event"){
      eventCount++;
      document.getElementById("evbtn").textContent = "Events (" + eventCount + ")";
      /* Place marker at current sample position */
      arrhythmiaMarkers.push({
        sampleIdx: samples.length - 1,
        type: msg.event_type,
        filename: msg.filename
      });
      needsRedraw = true;
      return;
    }
  };
}
connect();

})();
</script>
</body>
</html>
"""

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
