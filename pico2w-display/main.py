"""AD8232 ECG Serial Sender + Pico Display + WiFi Web Server — Pico 2W.

Dual-core architecture:
    Core 0 (main loop):
        - ADC read + serial at ~1kHz
        - Non-blocking poll for HTTP connections (raw sockets, no asyncio)
        - SSE broadcast every ~50ms
    Core 1 (display_thread):
        - BPM detection + display rendering at ~5Hz

Networking uses raw sockets polled from Core 0's main loop.
No asyncio — maximum compatibility with all MicroPython builds.

WiFi Station mode:
    Connects to phone hotspot (configure WIFI_SSID / WIFI_PASSWORD)
    Open http://<assigned-ip> from any device on the same network

Serial protocol (unchanged — compatible with ecg_server.py):
    - Integer 0-1023 + newline   — normal ADC reading
    - "!" + newline              — leads-off detected

Wiring:
    AD8232 OUTPUT  ->  GP26 (ADC0, pin 31)
    AD8232 LO+     ->  GP2  (pin 4)    — leads-off detection +
    AD8232 LO-     ->  GP3  (pin 5)    — leads-off detection -
    AD8232 3.3V    ->  3V3(OUT) (pin 36)
    AD8232 GND     ->  GND (pin 38)

    Pimoroni Pico Display sits on top (uses GP6-8, GP12-15, GP16-20).

Display pages (Button A to cycle):
    Page 0: ECG label + BPM + leads status + ECG waveform
    Page 1: Full-screen ECG waveform + minimal status

Button B: toggle backlight on/off
"""

import sys
import time
import _thread
import socket
from array import array
from machine import ADC, Pin

from picographics import PicoGraphics, DISPLAY_PICO_DISPLAY

# ---------------------------------------------------------------------------
# WiFi AP configuration
# ---------------------------------------------------------------------------

WIFI_SSID = "danix"
WIFI_PASSWORD = "12345678"
WEB_PORT = 80
MAX_SSE_CLIENTS = 2
SERIAL_PRINT_EVERY = 1       # print every Nth sample (0 = disable serial ADC output)

# ---------------------------------------------------------------------------
# Pin configuration
# ---------------------------------------------------------------------------

adc = ADC(Pin(26))
lo_plus = Pin(2, Pin.IN, Pin.PULL_DOWN)
lo_minus = Pin(3, Pin.IN, Pin.PULL_DOWN)

# ---------------------------------------------------------------------------
# Shared state — written by Core 0, read by Core 1
# ---------------------------------------------------------------------------

WAVE_COMPRESS = 3
WAVE_LEN = 240 * WAVE_COMPRESS            # 3840 samples in ring buffer (~4 s)
wave_buf = array("H", (512 for _ in range(WAVE_LEN)))
wave_idx = 0

current_value = 512
current_bpm = 0
leads_off_flag = False

# ---------------------------------------------------------------------------
# Display setup  (all display objects stay on Core 1)
# ---------------------------------------------------------------------------

display = PicoGraphics(display=DISPLAY_PICO_DISPLAY, rotate=0)
display.set_backlight(0.8)
WIDTH, HEIGHT = display.get_bounds()

btn_a = Pin(12, Pin.IN, Pin.PULL_UP)
btn_b = Pin(13, Pin.IN, Pin.PULL_UP)

BLACK = display.create_pen(0, 0, 0)
WHITE = display.create_pen(255, 255, 255)
RED = display.create_pen(255, 0, 0)
GREEN = display.create_pen(0, 200, 0)
YELLOW = display.create_pen(255, 200, 0)
GREY = display.create_pen(50, 50, 50)

DEAD_TOP = 14

# ---------------------------------------------------------------------------
# PVC display state (no file I/O — display flash only)
# ---------------------------------------------------------------------------

pvc_event_flag = False
pvc_event_ts = 0
pvc_show_until = 0

# ---------------------------------------------------------------------------
# SSE state
# ---------------------------------------------------------------------------

sse_clients = []             # [{'s': socket, 'li': last_sent_wave_idx, 'init': False}, ...]
beat_broadcast_idxs = []     # wave_buf indices where beats detected

# ---------------------------------------------------------------------------
# BPM detection  (called from Core 1 display thread)
# ---------------------------------------------------------------------------

THRESH_BUF_SIZE = 2000
thresh_buf = array("H", (512 for _ in range(THRESH_BUF_SIZE)))
thresh_idx = 0
thresh_fill = 0
thresh_recompute_every = 500
thresh_counter = 0
adaptive_threshold = 620
rearm_threshold = 620
pvc_threshold = 620
pvc_rearm = 620
peaks_go_up = True

below_threshold = True
pvc_armed = True
last_beat_ms = 0
last_beat_rearm_ms = 0
REFRACTORY_MS = 350
recent_bpms = []
_bpm = 0


def _recompute_threshold():
    global adaptive_threshold, rearm_threshold, peaks_go_up
    global pvc_threshold, pvc_rearm
    n = min(thresh_fill, THRESH_BUF_SIZE)
    if n < 100:
        return
    vals = sorted(thresh_buf[:n])
    p1 = vals[int(n * 0.01)]
    p50 = vals[n >> 1]
    p99 = vals[int(n * 0.99)]
    range_up = p99 - p50
    range_down = p50 - p1
    if range_up >= range_down:
        peaks_go_up = True
        adaptive_threshold = p50 + int(0.80 * range_up)
        rearm_threshold = p50 + int(0.30 * range_up)
        pvc_threshold = p50 - int(0.15 * range_up)
        pvc_rearm = p50 - int(0.05 * range_up)
    else:
        peaks_go_up = False
        adaptive_threshold = p50 - int(0.80 * range_down)
        rearm_threshold = p50 - int(0.30 * range_down)
        pvc_threshold = p50 + int(0.15 * range_down)
        pvc_rearm = p50 + int(0.05 * range_down)


def _detect_beat(value, ts, buf_idx):
    global below_threshold, last_beat_ms, last_beat_rearm_ms, _bpm
    global thresh_idx, thresh_fill, thresh_counter, current_bpm
    global pvc_armed, pvc_event_flag, pvc_event_ts

    thresh_buf[thresh_idx] = value
    thresh_idx = (thresh_idx + 1) % THRESH_BUF_SIZE
    if thresh_fill < THRESH_BUF_SIZE:
        thresh_fill += 1
    thresh_counter += 1
    if thresh_counter >= thresh_recompute_every:
        _recompute_threshold()
        thresh_counter = 0

    thr = adaptive_threshold
    if peaks_go_up:
        beat_hit = value > thr and below_threshold
        reset_cond = value < rearm_threshold
        pvc_hit = value < pvc_threshold and pvc_armed
        pvc_reset_cond = value > pvc_rearm
    else:
        beat_hit = value < thr and not below_threshold
        reset_cond = value > rearm_threshold
        pvc_hit = value > pvc_threshold and pvc_armed
        pvc_reset_cond = value < pvc_rearm

    if pvc_hit:
        pvc_armed = False
        pvc_event_flag = True
        pvc_event_ts = ts
    elif pvc_reset_cond:
        pvc_armed = True

    if beat_hit:
        below_threshold = not peaks_go_up
        last_beat_rearm_ms = ts
        if last_beat_ms > 0:
            diff = time.ticks_diff(ts, last_beat_ms)
            if 200 < diff < 3000:
                instant = 60000.0 / diff
                recent_bpms.append(int(instant))
                if len(recent_bpms) > 10:
                    recent_bpms.pop(0)
                current_bpm = sum(recent_bpms) // len(recent_bpms)
        last_beat_ms = ts
        if len(beat_broadcast_idxs) < 10:
            beat_broadcast_idxs.append(buf_idx)
    elif reset_cond:
        if time.ticks_diff(ts, last_beat_rearm_ms) > REFRACTORY_MS:
            below_threshold = peaks_go_up


# ---------------------------------------------------------------------------
# Core 1 — Display + BPM  (blocking loop, NO networking)
# ---------------------------------------------------------------------------


def _draw_waveform(x0, y0, w, h, widx):
    display.set_pen(GREY)
    mid = y0 + (h >> 1)
    display.line(x0, mid, x0 + w - 1, mid)

    vmin = 65535
    vmax = 0
    for k in range(WAVE_LEN):
        v = wave_buf[(widx + k) % WAVE_LEN]
        if v < vmin:
            vmin = v
        if v > vmax:
            vmax = v
    baseline = (vmin + vmax) >> 1
    vrange = vmax - vmin
    if vrange < 30:
        vrange = 30
    vrange = vrange + (vrange * 2 // 5)

    half_h = h >> 1

    def _v2y(v):
        py = y0 + (v - baseline) * h // vrange + half_h
        if py < y0:
            return y0
        if py > y0 + h:
            return y0 + h
        return py

    display.set_pen(RED)

    v0 = wave_buf[widx % WAVE_LEN]
    gmin = v0
    gmax = v0
    for s in range(1, WAVE_COMPRESS):
        v = wave_buf[(widx + s) % WAVE_LEN]
        if v < gmin:
            gmin = v
        if v > gmax:
            gmax = v
    prev_ylo = _v2y(gmin)
    prev_yhi = _v2y(gmax)
    prev_x = x0
    if prev_ylo != prev_yhi:
        display.line(prev_x, prev_yhi, prev_x, prev_ylo)

    for i in range(1, w):
        base_s = i * WAVE_COMPRESS
        v0 = wave_buf[(widx + base_s) % WAVE_LEN]
        gmin = v0
        gmax = v0
        for s in range(1, WAVE_COMPRESS):
            v = wave_buf[(widx + base_s + s) % WAVE_LEN]
            if v < gmin:
                gmin = v
            if v > gmax:
                gmax = v
        ylo = _v2y(gmin)
        yhi = _v2y(gmax)
        px = x0 + i
        display.line(prev_x, prev_ylo, px, ylo)
        display.line(prev_x, prev_yhi, px, yhi)
        if ylo != yhi:
            display.line(px, yhi, px, ylo)
        prev_x = px
        prev_ylo = ylo
        prev_yhi = yhi


def display_thread():
    global pvc_show_until, pvc_event_flag
    display_page = 0
    backlight_on = True
    btn_a_last = 0
    btn_b_last = 0
    bpm_read_idx = 0

    while True:
        ts = time.ticks_ms()

        if btn_a.value() == 0 and time.ticks_diff(ts, btn_a_last) > 250:
            btn_a_last = ts
            display_page = (display_page + 1) % 2
        if btn_b.value() == 0 and time.ticks_diff(ts, btn_b_last) > 250:
            btn_b_last = ts
            backlight_on = not backlight_on
            display.set_backlight(0.8 if backlight_on else 0.0)

        if not backlight_on:
            bpm_read_idx = wave_idx
            time.sleep_ms(200)
            continue

        widx = wave_idx

        if not leads_off_flag:
            if widx >= bpm_read_idx:
                new_count = widx - bpm_read_idx
            else:
                new_count = WAVE_LEN - bpm_read_idx + widx
            if new_count > WAVE_LEN:
                new_count = WAVE_LEN
            if new_count > 1000:
                bpm_read_idx = widx
            else:
                for i in range(new_count):
                    idx = (bpm_read_idx + i) % WAVE_LEN
                    val = wave_buf[idx]
                    sample_ts = ts - (new_count - 1 - i)
                    _detect_beat(val, sample_ts, idx)
                bpm_read_idx = widx
        else:
            bpm_read_idx = widx

        display.set_pen(BLACK)
        display.clear()

        DT = DEAD_TOP
        show_pvc = time.ticks_diff(pvc_show_until, ts) > 0

        if display_page == 0:
            if show_pvc:
                display.set_pen(YELLOW)
                display.text("PVC", 2, 2, scale=2)

            wave_top = DT + 22
            wave_h = HEIGHT - wave_top - 2

            display.set_pen(WHITE)
            display.text("ECG", 2, DT + 2, scale=2)
            bpm = current_bpm
            if bpm > 0:
                display.set_pen(WHITE)
                display.text(str(bpm), 120, DT - 2, scale=4)
                display.set_pen(GREY)
                display.text("BPM", 210, DT + 8, scale=1)
            else:
                display.set_pen(GREY)
                display.text("-- BPM", 120, DT + 2, scale=2)

            display.set_pen(GREY)
            display.line(0, wave_top - 2, WIDTH - 1, wave_top - 2)
            if leads_off_flag:
                display.set_pen(YELLOW)
                display.text("LO!", 104, wave_top - 10, scale=1)
            else:
                display.set_pen(GREEN)
                display.text("OK", 112, wave_top - 10, scale=1)
            _draw_waveform(0, wave_top, WIDTH, wave_h, widx)

        else:
            bar_h = 14
            _draw_waveform(0, DT, WIDTH, HEIGHT - DT - bar_h - 1, widx)

            display.set_pen(GREY)
            display.line(0, HEIGHT - bar_h - 1, WIDTH - 1, HEIGHT - bar_h - 1)
            bpm_s = str(current_bpm) + " BPM" if current_bpm > 0 else "-- BPM"
            if leads_off_flag:
                display.set_pen(YELLOW)
                bpm_s += "  LO!"
            else:
                display.set_pen(GREEN)
            display.text(bpm_s, 2, HEIGHT - bar_h + 1, scale=1)
            if show_pvc:
                display.set_pen(YELLOW)
                display.text("PVC", 180, HEIGHT - bar_h + 1, scale=1)

        display.update()

        if pvc_event_flag:
            pvc_event_flag = False
            pvc_show_until = ts + 2000

        time.sleep_ms(200)


# ---------------------------------------------------------------------------
# HTTP + SSE helpers  (synchronous raw sockets — no asyncio)
# ---------------------------------------------------------------------------


def _handle_http(cl):
    """Handle one incoming HTTP connection synchronously.

    For GET / — serve index.html and close.
    For GET /events — start SSE stream (socket stays open for broadcast).
    """
    cl.settimeout(5)
    try:
        raw = cl.recv(1024)
        if not raw:
            cl.close()
            return

        # Parse request line
        first_line_end = raw.find(b"\r\n")
        if first_line_end < 0:
            cl.close()
            return
        req_line = raw[:first_line_end]
        parts = req_line.split(b" ")
        path = parts[1] if len(parts) >= 2 else b"/"

        sys.stdout.write("HTTP %s\n" % path.decode())

        # --- SSE endpoint ---
        if path == b"/events":
            if len(sse_clients) >= MAX_SSE_CLIENTS:
                cl.send(b"HTTP/1.1 503 Too Many Clients\r\nConnection: close\r\n\r\n")
                cl.close()
                return

            cl.sendall(
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: text/event-stream\r\n"
                b"Cache-Control: no-cache\r\n"
                b"Connection: keep-alive\r\n"
                b"Access-Control-Allow-Origin: *\r\n\r\n"
            )
            cl.settimeout(0.1)
            sse_clients.append({"s": cl, "li": wave_idx, "init": True})
            sys.stdout.write("SSE+ (%d)\n" % len(sse_clients))
            return  # socket stays open

        # --- Serve index.html ---
        if path == b"/" or path == b"/index.html":
            try:
                cl.sendall(
                    b"HTTP/1.0 200 OK\r\n"
                    b"Content-Type: text/html; charset=utf-8\r\n"
                    b"Connection: close\r\n\r\n"
                )
                with open("/index.html", "rb") as f:
                    while True:
                        chunk = f.read(512)
                        if not chunk:
                            break
                        cl.sendall(chunk)
            except OSError:
                cl.send(b"HTTP/1.0 404 Not Found\r\n\r\nindex.html not found")
            cl.close()
            return

        # --- Favicon (browsers request this) ---
        if path == b"/favicon.ico":
            cl.send(b"HTTP/1.1 204 No Content\r\nConnection: close\r\n\r\n")
            cl.close()
            return

        # --- 404 ---
        cl.send(b"HTTP/1.1 404 Not Found\r\nConnection: close\r\n\r\n404")
        cl.close()

    except Exception as e:
        sys.stdout.write("HTTP err: %s\n" % str(e))
        try:
            cl.close()
        except Exception:
            pass


def _sse_broadcast():
    """Send new ECG samples to all SSE clients. Called every ~50ms."""
    if not sse_clients:
        beat_broadcast_idxs.clear()
        return

    widx = wave_idx
    ts = time.ticks_ms()
    lo = 1 if leads_off_flag else 0
    bpm = current_bpm

    dead = []
    for client in sse_clients:
        # Send init event on first broadcast
        if client.get("init"):
            client["init"] = False
            init_msg = 'data: {"type":"init","bpm":%d,"buf":%d}\n\n' % (bpm, WAVE_LEN)
            try:
                client["s"].sendall(init_msg.encode())
            except Exception:
                dead.append(client)
                continue

        li = client["li"]
        if widx >= li:
            count = widx - li
        else:
            count = WAVE_LEN - li + widx

        if count > WAVE_LEN:
            count = WAVE_LEN
        if count == 0:
            continue
        if count > 500:
            client["li"] = widx
            continue

        vals = []
        for i in range(count):
            vals.append(str(wave_buf[(li + i) % WAVE_LEN]))
        client["li"] = widx

        beats = []
        for bidx in beat_broadcast_idxs:
            if bidx >= li:
                offset = bidx - li
            else:
                offset = WAVE_LEN - li + bidx
            if 0 <= offset < count:
                beats.append(str(offset))

        msg = 'data: {"type":"d","ts":%d,"lo":%d,"bpm":%d,"b":[%s],"v":[%s]}\n\n' % (
            ts, lo, bpm, ",".join(beats), ",".join(vals)
        )

        try:
            client["s"].sendall(msg.encode())
        except Exception:
            dead.append(client)

    for d in dead:
        if d in sse_clients:
            sse_clients.remove(d)
            sys.stdout.write("SSE- (%d)\n" % len(sse_clients))
            try:
                d["s"].close()
            except Exception:
                pass

    beat_broadcast_idxs.clear()


# ---------------------------------------------------------------------------
# Core 0 — main loop: ADC + serial + HTTP poll + SSE broadcast
# ---------------------------------------------------------------------------


def main():
    global current_value, leads_off_flag, wave_idx

    # ---- WiFi STA setup — connect to phone hotspot ----
    global _wlan
    wifi_ok = False
    wifi_ip = "0.0.0.0"
    try:
        import network
        wlan = network.WLAN(network.STA_IF)
        wlan.active(True)
        wlan.connect(WIFI_SSID, WIFI_PASSWORD)
        sys.stdout.write("Connecting to %s...\n" % WIFI_SSID)

        # Show connecting status on display
        display.set_pen(BLACK)
        display.clear()
        display.set_pen(RED)
        display.text("AD8232 ECG", 30, DEAD_TOP + 10, scale=3)
        display.set_pen(YELLOW)
        display.text("WiFi: " + WIFI_SSID, 10, DEAD_TOP + 46, scale=2)
        display.set_pen(WHITE)
        display.text("Connecting...", 30, DEAD_TOP + 72, scale=2)
        display.update()

        for attempt in range(30):
            if wlan.isconnected():
                break
            time.sleep(1)
            # Update dots on display
            display.set_pen(BLACK)
            display.rectangle(30, DEAD_TOP + 72, 200, 20)
            display.set_pen(WHITE)
            display.text("Connecting" + "." * ((attempt % 3) + 1), 30, DEAD_TOP + 72, scale=2)
            display.update()
        wifi_ok = wlan.isconnected()
        if wifi_ok:
            ifc = wlan.ifconfig()
            wifi_ip = ifc[0]
            sys.stdout.write("Connected! IP: %s\n" % wifi_ip)
            _wlan = wlan  # prevent GC
        else:
            sys.stdout.write("WiFi FAILED status: %d\n" % wlan.status())
    except Exception as e:
        sys.stdout.write("WiFi err: %s\n" % str(e))
        wifi_ok = False

    # Splash screen
    display.set_pen(BLACK)
    display.clear()
    display.set_pen(RED)
    display.text("AD8232 ECG", 30, DEAD_TOP + 10, scale=3)
    if wifi_ok:
        display.set_pen(GREEN)
        display.text("on " + WIFI_SSID, 10, DEAD_TOP + 46, scale=2)
        display.set_pen(WHITE)
        display.text("http://" + wifi_ip, 4, DEAD_TOP + 70, scale=2)
    else:
        display.set_pen(YELLOW)
        display.text("WiFi FAILED", 30, DEAD_TOP + 46, scale=2)
        display.set_pen(GREY)
        display.text("Display only", 40, DEAD_TOP + 72, scale=1)
    display.update()
    time.sleep_ms(2000)

    # ---- HTTP server socket ----
    srv = None
    if wifi_ok:
        try:
            srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            srv.bind(("0.0.0.0", WEB_PORT))
            srv.listen(2)
            # Use short timeout instead of non-blocking — gives WiFi stack
            # processing time inside accept() when no clients are connecting
            srv.settimeout(0.01)
            sys.stdout.write("HTTP server on :%d\n" % WEB_PORT)
        except Exception as e:
            sys.stdout.write("Server err: %s\n" % str(e))
            srv = None

    # Start display + BPM on Core 1
    _thread.start_new_thread(display_thread, ())

    # ---- Core 0 main loop ----
    sse_counter = 0
    serial_counter = 0

    while True:
        # ADC read
        if lo_plus.value() == 1 or lo_minus.value() == 1:
            leads_off_flag = True
            current_value = 512
        else:
            leads_off_flag = False
            current_value = adc.read_u16() >> 6

        # Serial output (reduced frequency to give WiFi more CPU time)
        serial_counter += 1
        if SERIAL_PRINT_EVERY > 0 and serial_counter >= SERIAL_PRINT_EVERY:
            serial_counter = 0
            if leads_off_flag:
                sys.stdout.write("!\n")
            else:
                sys.stdout.write(str(current_value) + "\n")

        wave_buf[wave_idx] = current_value
        wave_idx = (wave_idx + 1) % WAVE_LEN

        # Accept new HTTP connections (blocking with 10ms timeout —
        # this is where CYW43 WiFi driver gets most of its processing time)
        if srv:
            try:
                cl, addr = srv.accept()
                _handle_http(cl)
            except OSError:
                pass  # timeout — no pending connection

        # SSE broadcast every ~50 iterations (~50ms)
        sse_counter += 1
        if sse_counter >= 50:
            sse_counter = 0
            _sse_broadcast()


main()
