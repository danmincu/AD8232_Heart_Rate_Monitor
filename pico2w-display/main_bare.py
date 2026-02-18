"""Bare-bones Arduino equivalent — no display, no filtering, just raw ADC."""

import sys
import time
from machine import ADC, Pin

adc = ADC(Pin(26))
lo_plus = Pin(2, Pin.IN, Pin.PULL_DOWN)
lo_minus = Pin(3, Pin.IN, Pin.PULL_DOWN)

while True:
    if lo_plus.value() == 1 or lo_minus.value() == 1:
        sys.stdout.write("!\n")
    else:
        sys.stdout.write(str(adc.read_u16() >> 6) + "\n")
    time.sleep_ms(1)
