"""Boot configuration for AD8232 ECG on Pico 2W.

This runs before main.py.  We disable the interactive REPL prompt so that
only clean ECG data lines appear on USB serial.
"""

import micropython

# Reduce heap fragmentation
micropython.alloc_emergency_exception_buf(100)
