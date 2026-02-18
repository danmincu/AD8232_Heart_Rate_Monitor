"""AD8232 ECG Serial Sender + Pico Display — Raspberry Pi Pico 2W.

Dual-core architecture:
    Core 0: ADC read + serial send at ~1kHz (lean — no computation)
    Core 1: BPM detection + display rendering at ~5Hz (reads shared buffer)

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
from array import array
from machine import ADC, Pin

from picographics import PicoGraphics, DISPLAY_PICO_DISPLAY

# ---------------------------------------------------------------------------
# Pin configuration  (Core 0 owns the ADC — Core 1 never touches it)
# ---------------------------------------------------------------------------

adc = ADC(Pin(26))                         # AD8232 analog output
lo_plus = Pin(2, Pin.IN, Pin.PULL_DOWN)    # AD8232 LO+
lo_minus = Pin(3, Pin.IN, Pin.PULL_DOWN)   # AD8232 LO-

# ---------------------------------------------------------------------------
# Shared state — written by Core 0, read by Core 1
# All are simple scalars or pre-allocated arrays (no cross-core contention).
# ---------------------------------------------------------------------------

WAVE_COMPRESS = 16
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
WIDTH, HEIGHT = display.get_bounds()       # 240 x 135

btn_a = Pin(12, Pin.IN, Pin.PULL_UP)
btn_b = Pin(13, Pin.IN, Pin.PULL_UP)

BLACK = display.create_pen(0, 0, 0)
WHITE = display.create_pen(255, 255, 255)
RED = display.create_pen(255, 0, 0)
GREEN = display.create_pen(0, 200, 0)
YELLOW = display.create_pen(255, 200, 0)
GREY = display.create_pen(50, 50, 50)

DEAD_TOP = 14                              # skip defective top 10%

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


def _detect_beat(value, ts):
    global below_threshold, last_beat_ms, last_beat_rearm_ms, _bpm
    global thresh_idx, thresh_fill, thresh_counter, current_bpm
    global pvc_armed

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

    # PVC detection (opposite-direction spike — not counted as beat)
    if pvc_hit:
        pvc_armed = False
    elif pvc_reset_cond:
        pvc_armed = True

    # Normal beat detection
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
    elif reset_cond:
        if time.ticks_diff(ts, last_beat_rearm_ms) > REFRACTORY_MS:
            below_threshold = peaks_go_up


# ---------------------------------------------------------------------------
# Core 1 — Display + BPM thread  (never touches ADC, never blocks serial)
# ---------------------------------------------------------------------------


def _draw_waveform(x0, y0, w, h, widx):
    """Draw min-max compressed ECG waveform from shared ring buffer.

    For each pixel column, tracks both the min and max sample values in
    the group and draws a vertical line spanning both.  This guarantees
    deep QRS spikes (low peaks) are always visible regardless of
    compression level.
    """
    display.set_pen(GREY)
    mid = y0 + (h >> 1)
    display.line(x0, mid, x0 + w - 1, mid)

    # Compute baseline (midpoint of min/max) and range
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
    vrange = vrange + (vrange * 2 // 5)   # 40% padding

    half_h = h >> 1

    def _v2y(v):
        py = y0 + h - ((v - baseline) * h // vrange + half_h)
        if py < y0:
            return y0
        if py > y0 + h:
            return y0 + h
        return py

    display.set_pen(RED)

    # First pixel group — find min/max
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
    # Draw vertical span for first column
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
        # Connect to previous column
        display.line(prev_x, prev_ylo, px, ylo)
        display.line(prev_x, prev_yhi, px, yhi)
        # Draw vertical span for this column (fills in the spike)
        if ylo != yhi:
            display.line(px, yhi, px, ylo)
        prev_x = px
        prev_ylo = ylo
        prev_yhi = yhi


def display_thread():
    """Runs on Core 1.  Renders display at ~5 Hz + BPM detection."""
    display_page = 0
    backlight_on = True
    btn_a_last = 0
    btn_b_last = 0
    bpm_read_idx = 0       # Core 1's read position in wave_buf

    # Splash
    display.set_pen(BLACK)
    display.clear()
    display.set_pen(RED)
    display.text("AD8232 ECG", 30, DEAD_TOP + 20, scale=3)
    display.set_pen(GREY)
    display.text("Waiting...", 50, DEAD_TOP + 56, scale=2)
    display.update()

    while True:
        ts = time.ticks_ms()

        # Buttons
        if btn_a.value() == 0 and time.ticks_diff(ts, btn_a_last) > 250:
            btn_a_last = ts
            display_page = (display_page + 1) % 2
        if btn_b.value() == 0 and time.ticks_diff(ts, btn_b_last) > 250:
            btn_b_last = ts
            backlight_on = not backlight_on
            display.set_backlight(0.8 if backlight_on else 0.0)

        # Screen off — skip all work, let Core 0 have maximum CPU
        if not backlight_on:
            bpm_read_idx = wave_idx   # stay caught up so we don't process stale data on wake
            time.sleep_ms(200)
            continue

        # Snapshot the write index (Core 0 may advance it, that's fine)
        widx = wave_idx

        # ---- Process new samples for BPM (moved from Core 0) ----
        if not leads_off_flag:
            if widx >= bpm_read_idx:
                new_count = widx - bpm_read_idx
            else:
                new_count = WAVE_LEN - bpm_read_idx + widx

            if new_count > WAVE_LEN:
                new_count = WAVE_LEN  # overflow guard

            if new_count > 1000:
                # Stale data (leads were off or major gap) — skip
                bpm_read_idx = widx
            else:
                for i in range(new_count):
                    idx = (bpm_read_idx + i) % WAVE_LEN
                    val = wave_buf[idx]
                    sample_ts = ts - (new_count - 1 - i)  # ~1ms per sample
                    _detect_beat(val, sample_ts)
                bpm_read_idx = widx
        else:
            # Leads off — skip over these samples, don't pollute threshold
            bpm_read_idx = widx

        display.set_pen(BLACK)
        display.clear()

        DT = DEAD_TOP

        if display_page == 0:
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

        display.update()
        time.sleep_ms(200)         # 5 Hz is plenty for the display


# ---------------------------------------------------------------------------
# Core 0 — ADC + serial  (lean — identical to main_bare.py + ring buffer)
# ---------------------------------------------------------------------------


def main():
    global current_value, leads_off_flag, wave_idx

    # Start display on Core 1
    _thread.start_new_thread(display_thread, ())

    while True:
        if lo_plus.value() == 1 or lo_minus.value() == 1:
            leads_off_flag = True
            current_value = 512
            sys.stdout.write("!\n")
        else:
            leads_off_flag = False
            current_value = adc.read_u16() >> 6
            sys.stdout.write(str(current_value) + "\n")

        wave_buf[wave_idx] = current_value
        wave_idx = (wave_idx + 1) % WAVE_LEN

        time.sleep_ms(1)


main()
