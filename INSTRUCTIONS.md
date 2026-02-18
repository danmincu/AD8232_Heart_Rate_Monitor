# SparkFun AD8232 Heart Rate Monitor — Raspberry Pi Pico W / Pico 2 W Setup

## Overview

This guide shows how to connect the SparkFun AD8232 Single Lead Heart Rate Monitor
(SEN-12650) to a **Raspberry Pi Pico W** or **Raspberry Pi Pico 2 W**, replacing the
Arduino Uno. The AD8232 is a 3.3V analog signal conditioning board — it amplifies and
filters the tiny electrical signals from your skin electrodes, then outputs a clean
analog voltage. The Pico's built-in ADC converts that analog signal to digital values
your code can use.

Both Pico boards run at 3.3V logic, making them a natural match — no level shifting needed.
The wiring and code are identical for both boards.

---

## Board Comparison

| Feature              | Pico W (RP2040)       | Pico 2 W (RP2350)         |
|----------------------|-----------------------|----------------------------|
| **CPU**              | Dual Cortex-M0+ 133MHz | Dual Cortex-M33 150MHz   |
| **SRAM**             | 264 KB                | 520 KB                     |
| **Flash memory**     | 2 MB (2,048 KB)       | 4 MB (4,096 KB)           |
| **ADC**              | 12-bit, 3 channels    | 12-bit, 3 channels        |
| **Wi-Fi**            | 2.4 GHz 802.11n      | 2.4 GHz 802.11n           |
| **Bluetooth**        | No                    | Bluetooth 5.2              |
| **Pin compatible**   | —                     | Yes, drop-in replacement   |
| **MicroPython .uf2** | RPI_PICO_W            | RPI_PICO2_W (see note below)  |

### Storage Capacity for ECG Recording

At the recommended 125 Hz sample rate, binary format (2 bytes per sample):

| | Pico W | Pico 2 W |
|---|---|---|
| **Total flash** | 2,048 KB | 4,096 KB |
| **MicroPython firmware** | ~650 KB | ~700 KB |
| **Script + overhead** | ~50 KB | ~50 KB |
| **Available for data** | ~1,350 KB | ~3,350 KB |
| **Data per hour** (125 Hz binary) | 879 KB | 879 KB |
| **Max recording time** | **~1.5 hours** | **~3.8 hours** |

> **Recommendation:** Use the **Pico 2 W** if you want to record for more than
> 1.5 hours on the internal flash without external storage.

---

## Parts Needed

- Raspberry Pi Pico W **or** Pico 2 W (with headers soldered)
- SparkFun AD8232 Heart Rate Monitor (SEN-12650)
- Biomedical sensor pads (3x)
- Sensor cable (3-lead, included with AD8232 board)
- Breadboard
- Jumper wires (5–6x)
- Micro-USB cable (for power and data)

---

## Wiring

The wiring is **identical** for Pico W and Pico 2 W — they are pin-compatible.

### Pin Connections

| AD8232 Pin | Pico Pin           | Physical Pin | Wire Color | Required? |
|------------|--------------------|--------------|------------|-----------|
| **3.3V**   | 3V3 (OUT)          | Pin 36       | Red        | Yes       |
| **GND**    | GND                | Pin 38       | Black      | Yes       |
| **OUTPUT** | GP26 (ADC0)        | Pin 31       | Yellow     | Yes       |
| **LO+**    | GP10               | Pin 14       | Green      | Yes       |
| **LO-**    | GP11               | Pin 15       | Blue       | Yes       |
| **SDN**    | GP22 *(optional)*  | Pin 29       | White      | No        |

### Wiring Diagram

```
   Pico W / Pico 2 W                AD8232
   ──────────────────                ──────
   3V3 (pin 36)  ──── Red ────►    3.3V
   GND (pin 38)  ──── Black ──►    GND
   GP26 (pin 31) ──── Yellow ──►    OUTPUT
   GP10 (pin 14) ──── Green ──►    LO+
   GP11 (pin 15) ──── Blue ───►    LO-
   GP22 (pin 29) ──── White ──►    SDN      (optional — see below)
```

### What Each AD8232 Pin Does

| Pin        | Role |
|------------|------|
| **3.3V**   | Power supply input. Must be 3.3V (provided by Pico's 3V3 pin). |
| **GND**    | Ground reference. Keep this wire short and direct for low noise. |
| **OUTPUT** | Analog ECG signal (0–3.3V). This is what the Pico's ADC reads. The AD8232 amplifies and filters the raw electrode signal but does NOT convert it to digital — the Pico's ADC does that. |
| **LO+**   | Leads-off detection, positive. Goes HIGH when the R or L electrode is disconnected. |
| **LO-**   | Leads-off detection, negative. Goes HIGH when the Common electrode is disconnected. |
| **SDN**    | Shutdown control. LOW = chip sleeps (near-zero power). HIGH or floating = chip active. The SparkFun board has a pull-up resistor so the chip is ON by default. Only connect this if you need software sleep/wake control. |

---

## Pimoroni Pico Display Pack Compatibility

The Pimoroni Pico Display Pack (PIM543) mounts directly onto all of the Pico's
header pins like a backpack. It uses the following GPIOs:

| Display Function | GPIO |
|------------------|------|
| LCD_DC           | GP16 |
| LCD_CS           | GP17 |
| LCD_SCLK         | GP18 |
| LCD_MOSI         | GP19 |
| LCD Backlight    | GP20 |
| Button A         | GP12 |
| Button B         | GP13 |
| Button X         | GP14 |
| Button Y         | GP15 |
| LED Red          | GP6  |
| LED Green        | GP7  |
| LED Blue         | GP8  |

**No conflicts with our AD8232 wiring.** Our pins (GP26, GP10, GP11, GP22)
are all free. The SDN pin was originally assigned to GP15 but was moved to
GP22 specifically to avoid the Display Pack's Button Y.

### Physical Mounting

Since the display covers all the Pico's header pins on the top side, you cannot
use a breadboard in the normal way. The solution is to **solder the AD8232 wires
directly to the underside of the Pico** (the bottom of the header pins) before
mounting the display on top.

```
   ┌──────────────────────────────────┐
   │      Pimoroni Display Pack       │  ← Top (display facing up)
   │      (plugged onto headers)      │
   ├──────────────────────────────────┤
   │       Raspberry Pi Pico 2 W      │
   ├──────────────────────────────────┤
   │   Solder AD8232 wires here       │  ← Bottom (underside of pins)
   │   (GP26, GP10, GP11, 3V3, GND,  │
   │    and optionally GP22 for SDN)  │
   └──────────────┬───────────────────┘
                  │ wires
            ┌─────┴─────┐
            │  AD8232   │
            │  Board    │
            └───────────┘
```

**Tip:** Use thin silicone-insulated wire (e.g. 26–30 AWG) for the connections.
Keep the GND wire as short as possible to minimize noise in the ECG signal.

---

## Electrode Placement (3-Lead)

Connect the sensor cable to the AD8232 board's 3.5mm jack. Attach the
electrode pads to your skin as follows:

| Cable Label | Full Name   | Placement                                         |
|-------------|-------------|----------------------------------------------------|
| **R** (RA)  | Right Arm   | Upper right chest, just below the collarbone       |
| **L** (LA)  | Left Arm    | Upper left chest, just below the collarbone        |
| **Common**  | Right Leg   | Lower left rib cage or lower left abdomen (ground) |

R and L form the measurement pair — the voltage difference between them is your
ECG signal. Common is the reference ground that helps the chip reject noise.

**Tips for good signal quality:**

- Clean skin with alcohol wipe before attaching pads
- Press pads firmly for full contact
- Stay still during readings — muscle movement creates noise
- Keep wires short and direct, especially the ground wire
- If the signal looks inverted, swap R and L pads

---

## Software Setup

### Step 1: Install MicroPython

1. Download the correct `.uf2` firmware:
   - **Pico W (vanilla MicroPython):**
     [micropython.org/download/RPI_PICO_W](https://micropython.org/download/RPI_PICO_W/)
   - **Pico 2 W (vanilla MicroPython):**
     [micropython.org/download/RPI_PICO2_W](https://micropython.org/download/RPI_PICO2_W/)
   - **If using the Pimoroni Display Pack:** use Pimoroni's custom firmware
     instead, which bundles PicoGraphics and display drivers.
     ⚠️ The RP2040 and RP2350 builds live in **different repos**:
     - **Pico W (RP2040):**
       [github.com/pimoroni/pimoroni-pico/releases](https://github.com/pimoroni/pimoroni-pico/releases)
     - **Pico 2 W (RP2350):**
       [github.com/pimoroni/pimoroni-pico-rp2350/releases](https://github.com/pimoroni/pimoroni-pico-rp2350/releases)
       → Download **`rpi_pico2_w-v1.26.1-micropython.uf2`** (or the latest version)
       *(Do NOT use `pico_plus2_rp2350-...` — that is for Pimoroni's own Plus 2 board)*
2. Hold the **BOOTSEL** button while plugging in the Pico via USB
3. Drag the `.uf2` file to the `RPI-RP2` drive that appears
4. The Pico will reboot with MicroPython installed

> **Tip:** After flashing, if Thonny shows "Device is busy" or "Connection lost",
> check that no other program is holding the serial port open:
> `sudo fuser /dev/ttyACM0` — kill any listed PID, then reconnect.

### Step 2: Install Thonny IDE

Download [Thonny](https://thonny.org/) — it connects to the Pico over USB and
lets you edit, run, and save files directly to the board.

---

## Code

### Mode 1: Live Serial Output (for visualization)

This is the direct equivalent of the original SparkFun Arduino sketch.
It prints ECG values to the serial console for use with Thonny's plotter,
Processing, or any serial monitor.

Save as **`serial_ecg.py`** on the Pico:

```python
"""
serial_ecg.py

Live serial output of AD8232 ECG data.
Direct MicroPython equivalent of SparkFun's Heart_Rate_Display.ino.

Wiring:
    AD8232 OUTPUT  -> GP26 (ADC0)
    AD8232 LO+     -> GP10
    AD8232 LO-     -> GP11
    AD8232 3.3V    -> 3V3 (pin 36)
    AD8232 GND     -> GND (pin 38)
    AD8232 SDN     -> GP22 (optional)
"""

from machine import Pin, ADC
import time

# ── Pin Setup ─────────────────────────────────────────
adc_pin = Pin(26, Pin.IN)
adc = ADC(adc_pin)
lo_plus = Pin(10, Pin.IN)
lo_minus = Pin(11, Pin.IN)

# ── Configuration ─────────────────────────────────────
SAMPLE_RATE_HZ = 125          # 125 Hz = minimum for clean ECG
SAMPLE_INTERVAL_MS = 1000 // SAMPLE_RATE_HZ   # 8 ms

# ── Main Loop ─────────────────────────────────────────
print("AD8232 ECG — Serial Output @ {} Hz".format(SAMPLE_RATE_HZ))

while True:
    if lo_plus.value() == 1 or lo_minus.value() == 1:
        print("!")
    else:
        # read_u16() returns 0–65535 (16-bit)
        # Use >> 6 if your software expects Arduino's 0–1023 range
        print(adc.read_u16())

    time.sleep_ms(SAMPLE_INTERVAL_MS)
```

### Mode 2: Binary Recording to Flash (for long-term capture)

This is the efficient recording mode. It stores raw 16-bit ADC samples in
binary files on the Pico's internal flash. Each sample is 2 bytes, and at
125 Hz that works out to **879 KB per hour**.

Save as **`record_ecg.py`** on the Pico:

```python
"""
record_ecg.py

Records AD8232 ECG data to the Pico's internal flash as binary files.
Optimized for minimum storage: 2 bytes per sample at 125 Hz.

Storage requirements:
    125 samples/sec x 2 bytes x 3600 sec = 900,000 bytes ~ 879 KB/hour

    Pico W  (~1,350 KB free) -> ~1.5 hours max
    Pico 2 W (~3,350 KB free) -> ~3.8 hours max

Binary format per file:
    - Header (16 bytes):
        Bytes 0-1:   Sample rate in Hz (uint16, little-endian)
        Bytes 2-5:   Start timestamp in seconds since boot (uint32, LE)
        Bytes 6-9:   Number of samples in this file (uint32, LE)
        Bytes 10-15: Reserved (zeroed)
    - Data:
        Each sample is a uint16 (2 bytes, little-endian)
        A value of 0xFFFF (65535) indicates leads-off at that sample

File naming: ecg_NNNN.bin (auto-incrementing)
"""

from machine import Pin, ADC
import time
import struct
import os

# ── Pin Setup ─────────────────────────────────────────
adc_pin = Pin(26, Pin.IN)
adc = ADC(adc_pin)
lo_plus = Pin(10, Pin.IN)
lo_minus = Pin(11, Pin.IN)

# Optional: SDN pin for sleep/wake control (uncomment if wired)
# sdn = Pin(22, Pin.OUT)
# sdn.value(1)  # Ensure chip is ON

# ── Configuration ─────────────────────────────────────
SAMPLE_RATE_HZ = 125
SAMPLE_INTERVAL_MS = 1000 // SAMPLE_RATE_HZ   # 8 ms
LEADS_OFF_MARKER = 0xFFFF                       # Sentinel value
SAMPLES_PER_FILE = SAMPLE_RATE_HZ * 60 * 10    # 10 minutes per file
HEADER_SIZE = 16                                 # 16-byte file header
DATA_DIR = "/ecg_data"
MIN_FREE_KB = 100  # Stop recording if less than this much space remains

# ── Helper Functions ──────────────────────────────────

def get_free_space_kb():
    """Return approximate free space on the filesystem in KB."""
    stat = os.statvfs("/")
    return (stat[0] * stat[3]) // 1024

def get_next_filename():
    """Find the next available ecg_NNNN.bin filename."""
    try:
        os.mkdir(DATA_DIR)
    except OSError:
        pass  # Directory already exists

    existing = [f for f in os.listdir(DATA_DIR) if f.startswith("ecg_") and f.endswith(".bin")]
    if not existing:
        return "{}/ecg_0000.bin".format(DATA_DIR)

    numbers = []
    for f in existing:
        try:
            numbers.append(int(f[4:8]))
        except ValueError:
            pass

    next_num = max(numbers) + 1 if numbers else 0
    return "{}/ecg_{:04d}.bin".format(DATA_DIR, next_num)

def write_header(f, sample_rate, start_time, num_samples):
    """Write or rewrite the 16-byte file header."""
    f.seek(0)
    header = struct.pack("<HII6s",
        sample_rate,
        int(start_time),
        num_samples,
        b'\x00' * 6
    )
    f.write(header)

def list_recordings():
    """Print a summary of all recorded files."""
    try:
        files = sorted(os.listdir(DATA_DIR))
    except OSError:
        print("No recordings found.")
        return

    print("\n-- Recorded Files --")
    total_samples = 0
    total_bytes = 0

    for filename in files:
        if not filename.endswith(".bin"):
            continue
        filepath = "{}/{}".format(DATA_DIR, filename)
        stat = os.stat(filepath)
        size = stat[6]
        samples = (size - HEADER_SIZE) // 2

        # Read header to get metadata
        with open(filepath, "rb") as f:
            hdr = f.read(HEADER_SIZE)
            rate, start, count = struct.unpack("<HII", hdr[:10])

        duration_sec = samples / rate if rate > 0 else 0
        minutes = int(duration_sec // 60)
        seconds = int(duration_sec % 60)

        print("  {} | {:,} samples | {}m {}s | {} KB".format(
            filename, samples, minutes, seconds, size // 1024))

        total_samples += samples
        total_bytes += size

    total_duration = total_samples / SAMPLE_RATE_HZ if total_samples > 0 else 0
    total_min = int(total_duration // 60)
    total_sec = int(total_duration % 60)

    print("  -----------------------------------------")
    print("  Total: {:,} samples | {}m {}s | {} KB".format(
        total_samples, total_min, total_sec, total_bytes // 1024))
    print("  Free space: {} KB".format(get_free_space_kb()))
    print()

# ── Recording ─────────────────────────────────────────

def record():
    """Main recording loop. Creates binary files on flash."""

    free_kb = get_free_space_kb()
    print("AD8232 ECG Recorder")
    print("  Sample rate:  {} Hz".format(SAMPLE_RATE_HZ))
    print("  Format:       Binary (2 bytes/sample)")
    print("  Data rate:    {} KB/hour".format(
        (SAMPLE_RATE_HZ * 2 * 3600) // 1024))
    print("  Free space:   {} KB".format(free_kb))
    print("  Est. capacity: {:.1f} hours".format(
        (free_kb - MIN_FREE_KB) / ((SAMPLE_RATE_HZ * 2 * 3600) / 1024)))
    print()

    if free_kb < MIN_FREE_KB:
        print("ERROR: Not enough free space to record.")
        print("Delete old files in {} or use delete_all().".format(DATA_DIR))
        return

    filepath = get_next_filename()
    print("Recording to: {}".format(filepath))
    print("Press Ctrl+C to stop.\n")

    start_time = time.ticks_ms() / 1000
    sample_count = 0

    f = open(filepath, "wb")
    # Write placeholder header (will update on close)
    write_header(f, SAMPLE_RATE_HZ, start_time, 0)

    try:
        while True:
            loop_start = time.ticks_ms()

            # Check leads
            if lo_plus.value() == 1 or lo_minus.value() == 1:
                sample = LEADS_OFF_MARKER
            else:
                sample = adc.read_u16()

            # Write 2-byte sample (little-endian unsigned 16-bit)
            f.write(struct.pack("<H", sample))
            sample_count += 1

            # Progress indicator every 5 seconds
            if sample_count % (SAMPLE_RATE_HZ * 5) == 0:
                elapsed = time.ticks_diff(time.ticks_ms(), int(start_time * 1000)) // 1000
                minutes = elapsed // 60
                seconds = elapsed % 60
                leads = "OK" if sample != LEADS_OFF_MARKER else "OFF"
                print("  {:02d}:{:02d} | {:,} samples | Leads: {} | Free: {} KB".format(
                    minutes, seconds, sample_count, leads, get_free_space_kb()))

            # Rotate to new file after SAMPLES_PER_FILE
            if sample_count >= SAMPLES_PER_FILE:
                write_header(f, SAMPLE_RATE_HZ, start_time, sample_count)
                f.close()

                if get_free_space_kb() < MIN_FREE_KB:
                    print("\nStopping: low disk space ({} KB free)".format(
                        get_free_space_kb()))
                    break

                filepath = get_next_filename()
                print("\n  New file: {}".format(filepath))
                f = open(filepath, "wb")
                start_time = time.ticks_ms() / 1000
                sample_count = 0
                write_header(f, SAMPLE_RATE_HZ, start_time, 0)

            # Maintain precise sample rate
            elapsed_ms = time.ticks_diff(time.ticks_ms(), loop_start)
            sleep_ms = SAMPLE_INTERVAL_MS - elapsed_ms
            if sleep_ms > 0:
                time.sleep_ms(sleep_ms)

    except KeyboardInterrupt:
        print("\n\nRecording stopped by user.")

    finally:
        # Update header with final sample count and close
        write_header(f, SAMPLE_RATE_HZ, start_time, sample_count)
        f.close()
        print("File saved: {:,} samples".format(sample_count))
        list_recordings()

def delete_all():
    """Delete all recorded ECG files to free up space."""
    try:
        files = os.listdir(DATA_DIR)
    except OSError:
        print("No data directory found.")
        return

    count = 0
    for filename in files:
        filepath = "{}/{}".format(DATA_DIR, filename)
        os.remove(filepath)
        count += 1

    try:
        os.rmdir(DATA_DIR)
    except OSError:
        pass

    print("Deleted {} files. Free space: {} KB".format(count, get_free_space_kb()))

# ── Entry Point ───────────────────────────────────────
# Run directly:       record()
# List files:         list_recordings()
# Clear all data:     delete_all()
#
# To auto-start recording on power-up, rename this file to main.py
# and uncomment the line below:
# record()
```

### Mode 3: Record with Wi-Fi Download

This extends Mode 2 by adding a simple web server so you can download
recorded `.bin` files from the Pico over Wi-Fi without unplugging it.

Save as **`wifi_record_ecg.py`** on the Pico:

```python
"""
wifi_record_ecg.py

Records ECG to flash (same binary format as record_ecg.py) and provides
a Wi-Fi web server to list and download recorded files.

Usage:
    1. Run this script
    2. It connects to Wi-Fi and prints an IP address
    3. Open http://<pico-ip>/ in a browser to manage recordings
    4. Press the record button on the web page or call record() in REPL
"""

import network
import socket
from machine import Pin, ADC
import struct
import time
import os

# ── Wi-Fi Config ──────────────────────────────────────
SSID = "YOUR_WIFI_SSID"
PASSWORD = "YOUR_WIFI_PASSWORD"

# ── Pin Setup ─────────────────────────────────────────
adc_pin = Pin(26, Pin.IN)
adc = ADC(adc_pin)
lo_plus = Pin(10, Pin.IN)
lo_minus = Pin(11, Pin.IN)

# ── Constants ─────────────────────────────────────────
SAMPLE_RATE_HZ = 125
SAMPLE_INTERVAL_MS = 1000 // SAMPLE_RATE_HZ
LEADS_OFF_MARKER = 0xFFFF
SAMPLES_PER_FILE = SAMPLE_RATE_HZ * 60 * 10
HEADER_SIZE = 16
DATA_DIR = "/ecg_data"
MIN_FREE_KB = 100

# ── Wi-Fi Connect ─────────────────────────────────────
def connect_wifi():
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    wlan.connect(SSID, PASSWORD)

    print("Connecting to Wi-Fi", end="")
    timeout = 20
    while not wlan.isconnected() and timeout > 0:
        print(".", end="")
        time.sleep(0.5)
        timeout -= 0.5

    if wlan.isconnected():
        ip = wlan.ifconfig()[0]
        print("\nConnected! Open http://{}/".format(ip))
        return ip
    else:
        print("\nWi-Fi connection failed.")
        return None

# ── Web Server ────────────────────────────────────────
def serve_file_list():
    """Generate HTML page listing recorded files with download links."""
    try:
        files = sorted(os.listdir(DATA_DIR))
    except OSError:
        files = []

    rows = ""
    for f in files:
        if not f.endswith(".bin"):
            continue
        path = "{}/{}".format(DATA_DIR, f)
        size = os.stat(path)[6]
        samples = (size - HEADER_SIZE) // 2
        duration = samples / SAMPLE_RATE_HZ
        mins = int(duration // 60)
        secs = int(duration % 60)
        rows += "<tr><td><a href='/download/{}'>{}</a></td>".format(f, f)
        rows += "<td>{}m {}s</td><td>{} KB</td></tr>\n".format(
            mins, secs, size // 1024)

    stat = os.statvfs("/")
    free_kb = (stat[0] * stat[3]) // 1024

    html = """HTTP/1.0 200 OK\r\nContent-Type: text/html\r\n\r\n
    <html><head><title>Pico ECG Recorder</title></head>
    <body>
    <h1>ECG Recordings</h1>
    <p>Free space: {} KB | Sample rate: {} Hz | Format: binary 16-bit LE</p>
    <table border="1" cellpadding="5">
    <tr><th>File</th><th>Duration</th><th>Size</th></tr>
    {}
    </table>
    <br><p><a href='/delete_all' onclick="return confirm('Delete all?')">
    Delete All Recordings</a></p>
    </body></html>""".format(free_kb, SAMPLE_RATE_HZ, rows)
    return html

def serve_download(filename):
    """Serve a binary file for download."""
    path = "{}/{}".format(DATA_DIR, filename)
    try:
        size = os.stat(path)[6]
    except OSError:
        return "HTTP/1.0 404 Not Found\r\n\r\nFile not found"

    header = "HTTP/1.0 200 OK\r\n"
    header += "Content-Type: application/octet-stream\r\n"
    header += "Content-Disposition: attachment; filename={}\r\n".format(filename)
    header += "Content-Length: {}\r\n\r\n".format(size)
    return header

def start_web_server(ip):
    """Run the file management web server."""
    addr = socket.getaddrinfo(ip, 80)[0][-1]
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(addr)
    s.listen(2)

    while True:
        cl, remote = s.accept()
        try:
            request = cl.recv(1024).decode()
            first_line = request.split("\r\n")[0]
            path = first_line.split(" ")[1] if len(first_line.split(" ")) > 1 else "/"

            if path.startswith("/download/"):
                filename = path.split("/download/")[1]
                header = serve_download(filename)
                cl.send(header.encode())
                # Stream file in chunks
                filepath = "{}/{}".format(DATA_DIR, filename)
                with open(filepath, "rb") as f:
                    while True:
                        chunk = f.read(1024)
                        if not chunk:
                            break
                        cl.send(chunk)
            elif path == "/delete_all":
                try:
                    for f in os.listdir(DATA_DIR):
                        os.remove("{}/{}".format(DATA_DIR, f))
                except OSError:
                    pass
                cl.send("HTTP/1.0 302 Found\r\nLocation: /\r\n\r\n".encode())
            else:
                cl.send(serve_file_list().encode())
        except Exception as e:
            print("Server error:", e)
        finally:
            cl.close()

# ── Entry Point ───────────────────────────────────────
# Call connect_wifi() then start_web_server(ip) to browse files,
# or import record() from record_ecg.py to capture data.
```

---

## Key Differences from the Arduino Version

| Feature              | Arduino Uno                  | Pico W / Pico 2 W              |
|----------------------|------------------------------|---------------------------------|
| **Logic voltage**    | 5V (needs 3.3V Pro Mini)    | 3.3V (native match)            |
| **ADC resolution**   | 10-bit (0–1023)             | 12-bit hardware, 16-bit API    |
| **ADC range**        | 0–5V (or 0–3.3V on 3.3V)   | 0–3.3V                         |
| **Serial output**    | `Serial.println()`          | `print()`                      |
| **Analog read**      | `analogRead(A0)` → 0–1023  | `adc.read_u16()` → 0–65535    |
| **Digital read**     | `digitalRead(10)`           | `lo_plus.value()`              |
| **Delay**            | `delay(1)`                  | `time.sleep_ms(1)`             |
| **Wireless**         | None (without shield)       | Wi-Fi (both) + BLE (Pico 2 W)  |
| **On-board storage** | None                        | Flash filesystem                |

### ADC Value Scaling

The Pico's `adc.read_u16()` returns 0–65535. If your software expects
Arduino's 0–1023 range:

```python
reading = adc.read_u16() >> 6   # Shift 16-bit down to 10-bit (0–1023)
```

### Sample Rate Rationale

The AD8232 board has a bandpass filter of ~0.5–40 Hz. By the Nyquist theorem,
you need at least 2x the highest frequency (80 Hz). Adding headroom, **125 Hz**
is the practical minimum for a clean ECG waveform and is the standard for
cardiac monitoring. This saves significant storage compared to the Arduino
sketch's ~1000 Hz rate while preserving full waveform fidelity.

---

## Viewing the Output

### Option 1: Thonny IDE Plotter

1. Open Thonny, connect to the Pico
2. Run `serial_ecg.py`
3. Go to **View → Plotter** to see a live waveform

### Option 2: Serial Terminal

Use any serial terminal at **115200 baud** (MicroPython's default USB serial speed):

```bash
# Linux / macOS
screen /dev/ttyACM0 115200

# or
minicom -D /dev/ttyACM0 -b 115200
```

### Option 3: Processing Sketch

To use SparkFun's original Processing sketch, change the baud rate in the
Processing code to **115200** and either adjust for 0–65535 values or use the
`>> 6` shift in the MicroPython code to keep 0–1023 compatibility.

---

## Reading Recorded Binary Files

### On the Pico (REPL)

```python
from record_ecg import list_recordings
list_recordings()
```

### On a PC (Python 3)

Copy the `.bin` files from the Pico (via Thonny's file manager or the Wi-Fi
download server), then decode with this script:

```python
"""
read_ecg_bin.py

Reads binary ECG files recorded by record_ecg.py and converts
them to CSV or plots them with matplotlib.

Usage:
    python read_ecg_bin.py ecg_0000.bin              # Print info
    python read_ecg_bin.py ecg_0000.bin --csv         # Export to CSV
    python read_ecg_bin.py ecg_0000.bin --plot         # Plot waveform
"""

import struct
import sys

HEADER_SIZE = 16
LEADS_OFF_MARKER = 0xFFFF

def read_ecg_file(filepath):
    """Read a binary ECG file and return metadata + samples."""
    with open(filepath, "rb") as f:
        header = f.read(HEADER_SIZE)
        sample_rate, start_time, num_samples = struct.unpack("<HII", header[:10])

        raw = f.read()
        samples = struct.unpack("<{}H".format(len(raw) // 2), raw)

    return {
        "sample_rate": sample_rate,
        "start_time": start_time,
        "num_samples": num_samples,
        "samples": samples,
        "duration_sec": len(samples) / sample_rate,
    }

def to_csv(data, output_path):
    """Export samples to CSV with time column."""
    rate = data["sample_rate"]
    with open(output_path, "w") as f:
        f.write("time_sec,adc_value,leads_off\n")
        for i, sample in enumerate(data["samples"]):
            t = i / rate
            leads_off = 1 if sample == LEADS_OFF_MARKER else 0
            value = 0 if leads_off else sample
            f.write("{:.4f},{},{}\n".format(t, value, leads_off))
    print("Exported {} samples to {}".format(len(data["samples"]), output_path))

def plot(data):
    """Plot the ECG waveform using matplotlib."""
    try:
        import matplotlib.pyplot as plt
    except ImportError:
        print("pip install matplotlib")
        return

    rate = data["sample_rate"]
    samples = list(data["samples"])

    # Replace leads-off markers with NaN for clean plotting
    values = [float('nan') if s == LEADS_OFF_MARKER else s for s in samples]
    times = [i / rate for i in range(len(values))]

    plt.figure(figsize=(14, 4))
    plt.plot(times, values, linewidth=0.5, color='red')
    plt.xlabel("Time (seconds)")
    plt.ylabel("ADC Value (0-65535)")
    plt.title("ECG Recording - {} Hz, {:.0f} seconds".format(
        rate, data["duration_sec"]))
    plt.grid(True, alpha=0.3)
    plt.tight_layout()
    plt.show()

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python read_ecg_bin.py <file.bin> [--csv] [--plot]")
        sys.exit(1)

    filepath = sys.argv[1]
    data = read_ecg_file(filepath)

    print("File:        {}".format(filepath))
    print("Sample rate: {} Hz".format(data["sample_rate"]))
    print("Samples:     {:,}".format(len(data["samples"])))
    print("Duration:    {:.1f} seconds ({:.1f} minutes)".format(
        data["duration_sec"], data["duration_sec"] / 60))

    leads_off_count = sum(1 for s in data["samples"] if s == LEADS_OFF_MARKER)
    print("Leads-off:   {:,} samples ({:.1f}%)".format(
        leads_off_count, 100 * leads_off_count / len(data["samples"])))

    if "--csv" in sys.argv:
        csv_path = filepath.replace(".bin", ".csv")
        to_csv(data, csv_path)

    if "--plot" in sys.argv:
        plot(data)
```

---

## SDN Pin Details

The **SDN** (Shutdown) pin gives you software control over the AD8232's power state.
This is useful for battery-powered projects where you want to conserve energy between
recordings.

### How It Works

| SDN State          | AD8232 Behavior                     |
|--------------------|-------------------------------------|
| **HIGH (3.3V)**    | Chip is ON — actively monitoring    |
| **LOW (GND)**      | Chip is in SLEEP — near-zero power  |
| **Floating (N/C)** | Chip is ON — board pull-up defaults to HIGH |

### Wiring

Connect the AD8232's SDN pin to **GP22** (physical pin 29) on the Pico.

### Example Code

```python
from machine import Pin
import time

# SDN control pin
sdn = Pin(22, Pin.OUT)

# Wake up the AD8232
sdn.value(1)
print("AD8232 is ON")

# ... take your readings here ...

# Put the AD8232 to sleep to save power
sdn.value(0)
print("AD8232 is SLEEPING")

# Wake it back up when ready
time.sleep(10)
sdn.value(1)
print("AD8232 is ON again")
```

### Integrating SDN Into the Recording Script

In `record_ecg.py`, uncomment these lines near the top:

```python
sdn = Pin(22, Pin.OUT)
sdn.value(1)  # Ensure chip is ON at startup
```

Then call `sdn.value(0)` to sleep and `sdn.value(1)` to wake whenever needed.

> **Important:** After waking the AD8232 from shutdown, wait at least **100ms**
> before trusting the OUTPUT readings. The internal filters need time to settle.

---

## Troubleshooting

| Symptom                        | Likely Cause                          | Fix                                           |
|--------------------------------|---------------------------------------|------------------------------------------------|
| Only seeing `!` in output      | Leads disconnected or poor contact    | Check electrode pads, clean skin, press firmly |
| Very noisy / erratic signal    | Long or loose ground wire             | Use short direct GND wire, solder if possible  |
| Flat line (constant value)     | OUTPUT not connected to ADC pin       | Verify GP26 wiring                             |
| No output at all               | Pico not running the script           | Check Thonny connection, re-upload script      |
| Value hovers around 32768      | Normal idle (mid-rail) with no signal | Attach electrodes — this is expected behavior  |
| Signal looks good but inverted | R and L electrodes swapped            | Swap the R and L pads on your chest            |
| Disk full during recording     | Flash storage exhausted               | Download files, then call `delete_all()`       |
| Wi-Fi won't connect            | Wrong SSID/password or out of range   | Check credentials, move closer to router       |
| `OSError: [Errno 28]`         | Filesystem full                       | Delete old recordings with `delete_all()`      |

---

## File Summary

| File | Purpose | When to Use |
|---|---|---|
| `serial_ecg.py` | Live serial output at 125 Hz | Real-time visualization with Thonny plotter or Processing |
| `record_ecg.py` | Binary recording to flash | Long-term capture (1.5 hrs Pico W / 3.8 hrs Pico 2 W) |
| `wifi_record_ecg.py` | Wi-Fi file server + recording | Browse and download recordings from a browser |
| `read_ecg_bin.py` | PC-side decoder (Python 3) | Convert `.bin` files to CSV or plot with matplotlib |

---

## References

- [SparkFun AD8232 Hookup Guide](https://learn.sparkfun.com/tutorials/ad8232-heart-rate-monitor-hookup-guide/all)
- [AD8232 Datasheet (Analog Devices)](https://www.analog.com/media/en/technical-documentation/data-sheets/ad8232.pdf)
- [Raspberry Pi Pico W Datasheet](https://datasheets.raspberrypi.com/picow/pico-w-datasheet.pdf)
- [Raspberry Pi Pico 2 W Product Page](https://www.raspberrypi.com/products/raspberry-pi-pico-2/?variant=pico-2-w)
- [MicroPython ADC Documentation](https://docs.micropython.org/en/latest/library/machine.ADC.html)
- [MicroPython Downloads — Pico W](https://micropython.org/download/RPI_PICO_W/)
- [MicroPython Downloads — Pico 2 W](https://micropython.org/download/RPI_PICO2_W/)
- [Pimoroni MicroPython — RP2040 (Pico W)](https://github.com/pimoroni/pimoroni-pico/releases)
- [Pimoroni MicroPython — RP2350 (Pico 2 W)](https://github.com/pimoroni/pimoroni-pico-rp2350/releases)
- [DigiKey Product Page (SEN-12650)](https://www.digikey.ca/en/products/detail/sparkfun-electronics/12650/5824153)
