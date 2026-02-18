"""AD8232 ECG Serial Sender + Pico Display — Raspberry Pi Pico 2W.

Dual-core architecture:
    Core 0: ADC read + serial send at ~1kHz (never blocked by display)
    Core 1: Display rendering at ~5Hz (reads shared buffer, never touches ADC)

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
# All are simple scalars or pre-allocated arrays (no GC contention).
# ---------------------------------------------------------------------------

WAVE_COMPRESS = 4
WAVE_LEN = 240 * WAVE_COMPRESS            # 960 samples in ring buffer
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
# BPM detection  (runs on Core 0, lightweight)
# ---------------------------------------------------------------------------

THRESH_BUF_SIZE = 2000
thresh_buf = array("H", (512 for _ in range(THRESH_BUF_SIZE)))
thresh_idx = 0
thresh_fill = 0
thresh_recompute_every = 500
thresh_counter = 0
adaptive_threshold = 620
rearm_threshold = 620
peaks_go_up = True

below_threshold = True
last_beat_ms = 0
last_beat_rearm_ms = 0
REFRACTORY_MS = 350
recent_bpms = []
_bpm = 0


def _recompute_threshold():
    global adaptive_threshold, rearm_threshold, peaks_go_up
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
    else:
        peaks_go_up = False
        adaptive_threshold = p50 - int(0.80 * range_down)
        rearm_threshold = p50 - int(0.30 * range_down)


def _detect_beat(value, ts):
    global below_threshold, last_beat_ms, last_beat_rearm_ms, _bpm
    global thresh_idx, thresh_fill, thresh_counter, current_bpm

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
    else:
        beat_hit = value < thr and not below_threshold
        reset_cond = value > rearm_threshold

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
# Core 1 — Display thread  (never touches ADC, never blocks serial)
# ---------------------------------------------------------------------------


def _draw_waveform(x0, y0, w, h, widx):
    """Draw 4x-compressed ECG waveform from shared ring buffer."""
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

    display.set_pen(RED)

    # First pixel group
    best = wave_buf[widx % WAVE_LEN]
    best_dev = abs(best - baseline)
    for s in range(1, WAVE_COMPRESS):
        v = wave_buf[(widx + s) % WAVE_LEN]
        d = abs(v - baseline)
        if d > best_dev:
            best = v
            best_dev = d
    prev_y = y0 + h - ((best - baseline) * h // vrange + (h >> 1))
    if prev_y < y0:
        prev_y = y0
    elif prev_y > y0 + h:
        prev_y = y0 + h
    prev_x = x0

    for i in range(1, w):
        base_s = i * WAVE_COMPRESS
        best = wave_buf[(widx + base_s) % WAVE_LEN]
        best_dev = abs(best - baseline)
        for s in range(1, WAVE_COMPRESS):
            v = wave_buf[(widx + base_s + s) % WAVE_LEN]
            d = abs(v - baseline)
            if d > best_dev:
                best = v
                best_dev = d
        py = y0 + h - ((best - baseline) * h // vrange + (h >> 1))
        if py < y0:
            py = y0
        elif py > y0 + h:
            py = y0 + h
        px = x0 + i
        display.line(prev_x, prev_y, px, py)
        prev_x = px
        prev_y = py


def display_thread():
    """Runs on Core 1.  Renders display at ~5 Hz."""
    display_page = 0
    backlight_on = True
    btn_a_last = 0
    btn_b_last = 0

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

        # Snapshot the write index (Core 0 may advance it, that's fine)
        widx = wave_idx

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
                display.set_pen(RED)
                display.text(str(bpm), 140, DT, scale=3)
                display.set_pen(WHITE)
                display.text("BPM", 200, DT + 6, scale=1)
            else:
                display.set_pen(GREY)
                display.text("-- BPM", 140, DT + 2, scale=2)

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
# Core 0 — ADC + serial  (identical to the bare version that works)
# ---------------------------------------------------------------------------


def main():
    global current_value, leads_off_flag, wave_idx

    # Start display on Core 1
    _thread.start_new_thread(display_thread, ())

    while True:
        ts = time.ticks_ms()

        # ---- Read & send (exactly like main_bare.py) ----
        if lo_plus.value() == 1 or lo_minus.value() == 1:
            leads_off_flag = True
            current_value = 512
            sys.stdout.write("!\n")
        else:
            leads_off_flag = False
            current_value = adc.read_u16() >> 6
            sys.stdout.write(str(current_value) + "\n")
            _detect_beat(current_value, ts)

        # Store in shared ring buffer (atomic 16-bit write)
        wave_buf[wave_idx] = current_value
        wave_idx = (wave_idx + 1) % WAVE_LEN

        time.sleep_ms(1)


main()
