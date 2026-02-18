# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

AD8232 Single Lead Heart Rate Monitor — an ECG/EKG visualization system using the AD8232 sensor chip, an Arduino Pro 3.3V/8MHz, and a Processing desktop application. Originally by SparkFun Electronics (SEN-12650).

## Architecture

```
[AD8232 Sensor] → analog → [Arduino Pro 3.3V] → serial 9600 baud → [Processing App] → ECG display + BPM + PNG export
```

- **Arduino firmware** (`Software/Heart_Rate_Display_Arduino/Heart_Rate_Display_Arduino.ino`): Reads analog pin A0, sends raw ADC values (0–1023) or `!` (leads-off error) over serial at 9600 baud. Pins 10/11 are digital leads-off detection inputs.
- **Processing visualization** (`Software/Heart_Rate_Display_Processing/Heart_Rate_Display.pde`): Receives serial data, renders real-time ECG waveform (red = valid, blue = leads-off), calculates BPM using a 620.0 ADC threshold with a rolling 500-beat average, draws calibration grid, and exports PNG frames.
- **Hardware design** (`Hardware/`): Eagle schematic (.sch) and board (.brd) files.
- **Fritzing diagrams** (`Fritzing/`): Breadboard wiring reference.

## Build & Run

There is no traditional build system. Both components use their respective IDEs:

- **Arduino sketch**: Open `Software/Heart_Rate_Display_Arduino/Heart_Rate_Display_Arduino.ino` in Arduino IDE (1.0.5+), select board "Arduino Pro 3.3V/8MHz", upload.
- **Processing sketch**: Open `Software/Heart_Rate_Display_Processing/Heart_Rate_Display.pde` in Processing IDE (Java mode), run. The serial port is selected via `Serial.list()[2]` — adjust the index for your system.

## Serial Protocol

Unidirectional Arduino→Processing. ASCII newline-terminated messages: integer values `0`–`1023` (ADC reading) or `!` (leads-off detected).

## Key Configuration Points

- BPM detection threshold: `620.0` in the Processing sketch
- Serial port index: `Serial.list()[2]` in the Processing sketch `setup()`
- Sampling delay: `1ms` in the Arduino `loop()`

## Licensing

Hardware: CC BY-SA 3.0. Code: Beerware license.
