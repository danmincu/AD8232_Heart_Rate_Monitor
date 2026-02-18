# ECG Rendering — Standard Clinical Display Guidelines

This document describes modifications needed to transform the current device-specific ECG rendering into a standard clinical-style display. The grey BPM annotations at the top of the strip should remain unchanged.

---

## 1. Signal Orientation (Critical)

The current rendering appears **inverted** — the QRS complexes (R waves) are deflecting **downward** instead of upward. In a standard Lead II (or most limb leads) display, the R wave should be the tallest **upward** spike.

**Fix:** Invert the Y-axis of the signal data. Multiply all amplitude values by `-1`, or flip the coordinate system so that positive voltage deflects **upward** on screen.

```
// Pseudocode
y_display = -y_raw   // or: y_display = canvas_midline - (y_raw - baseline)
```

> **Note:** Verify the device's lead configuration. If the device records in an inverted lead orientation by design, the software inversion is the correct fix. If the device supports multiple leads, ensure Lead II is the default display (most common for rhythm strips).

---

## 2. Background Grid (ECG Paper)

Standard ECG paper uses a specific grid:

| Element | Size | Colour |
|---------|------|--------|
| Small squares | 1 mm × 1 mm | Light pink/salmon (`#FCC` or `rgba(255, 180, 180, 0.4)`) |
| Large squares | 5 mm × 5 mm (bold lines every 5 small squares) | Darker pink/red (`#E88` or `rgba(200, 80, 80, 0.3)`) |

**Current state:** Grey background with sparse grey gridlines.

**Fix:** Replace the background with the standard two-tier grid:

```
// Draw small grid (1mm squares)
strokeColour = "rgba(255, 150, 150, 0.3)"
lineWidth = 0.5
spacing = 1mm_in_pixels  // see calibration section below

// Draw large grid (5mm squares)
strokeColour = "rgba(220, 80, 80, 0.25)"
lineWidth = 1.0
spacing = 5 * 1mm_in_pixels
```

Background fill should be **white** (`#FFFFFF`) or very faint pink (`#FFF5F5`).

---

## 3. Trace Colour and Line Weight

| Property | Current | Standard |
|----------|---------|----------|
| Colour | Red | **Black** (`#000000`) or very dark blue (`#1A1A2E`) |
| Line width | Thin | ~1.5–2.0 px at screen resolution (should appear ~0.5 mm on paper) |
| Anti-aliasing | Varies | Enabled, for smooth curves |

**Fix:**
```
traceColour = "#000000"
traceLineWidth = 1.5  // px, adjust for DPI
```

---

## 4. Standard Speed and Gain (Calibration)

Standard ECG display uses fixed scales:

| Parameter | Standard Value | Meaning |
|-----------|---------------|---------|
| Paper speed | **25 mm/s** | 1 small square = 0.04 s (40 ms) |
| Voltage gain | **10 mm/mV** | 1 small square = 0.1 mV |

**Fix:**
- Calculate `pixels_per_mm` based on intended display size or DPI.
- Map the time axis so that 25 mm of screen width = 1 second of signal.
- Map the amplitude axis so that 10 mm of screen height = 1 mV of signal.

```
pixels_per_mm = display_dpi / 25.4   // or define a fixed zoom level
pixels_per_second = 25 * pixels_per_mm
pixels_per_mV = 10 * pixels_per_mm
```

If the raw device data is in ADC units rather than millivolts, you'll need the device's conversion factor (check the device SDK/documentation for the µV-per-LSB value).

---

## 5. Calibration Pulse

Standard ECG strips begin with a **1 mV calibration pulse** — a rectangular wave at the left edge of the strip.

**Fix:** Before the first beat, draw a rectangular pulse:
- Width: 200 ms (5 small squares at 25 mm/s)
- Height: 1 mV (10 mm at standard gain)

```
// Draw calibration pulse at strip start
drawRect(x_start, baseline_y - pixels_per_mV, 5 * mm_pixels, pixels_per_mV)
```

---

## 6. Time and Lead Markings

Add the following annotations:

| Annotation | Location | Content |
|------------|----------|---------|
| Lead label | Top-left of strip | e.g., `"II"` or `"Lead II"` |
| Speed label | Bottom-left or bottom-right | `"25 mm/s"` |
| Gain label | Below speed label | `"10 mm/mV"` |
| Time markers | Bottom edge, every 1 s or 3 s | Tick marks or second labels |

Font: clean sans-serif (e.g., Arial, Helvetica), ~8–10 pt, dark grey or black.

---

## 7. Baseline Rendering

If the signal has significant **baseline wander** (the overall trace drifts up and down), consider applying a high-pass filter:

- Standard clinical monitors use a **0.05 Hz high-pass filter** for diagnostic mode or **0.5 Hz** for monitor mode.
- Apply before rendering but **after** storing the raw data. Never discard the unfiltered data.

```
// Simple approach: subtract a moving average
baseline = movingAverage(signal, window=2*sampleRate)  // ~2 second window
filtered = signal - baseline
```

---

## 8. Strip Layout

For long recordings, render in **stacked horizontal strips** rather than one continuous scrolling line:

- Each strip: **10 seconds** wide (standard 12-lead page) or **shorter strips** for rhythm monitoring.
- Strips stack vertically, reading top-to-bottom like lines of text.
- Add a **small time label** at the start of each strip (e.g., `0:00`, `0:10`, `0:20`).

---

## 9. Summary of Changes

```
BEFORE (current)                    AFTER (standard)
─────────────────                   ─────────────────
Signal inverted (R waves down)  →   R waves point UP
Grey background + sparse grid   →   White/pink bg + 1mm/5mm pink grid
Red trace                       →   Black trace
No calibration reference        →   1 mV calibration pulse at start
No axis labels                  →   Lead, speed, gain labels
Unknown/variable scale          →   25 mm/s, 10 mm/mV
Continuous horizontal scroll    →   Stacked 10-second strips (optional)
Grey BPM labels (top)           →   KEEP AS-IS
```

---

## 10. Reference

For a visual reference of standard ECG rendering, see any clinical ECG textbook or the AHA/ACC recommendations for standardisation of electrocardiographic interpretation (Circulation, 2007). The key visual signature is: **black trace, pink grid, R waves up, calibrated axes**.
