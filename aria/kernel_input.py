"""
AriaOS Kernel Input — /dev/uinput
==================================
Inject keyboard and mouse events directly via Linux kernel.
No xdotool, no X11 dependency — pure kernel-level input.

Uses /dev/uinput to create a virtual input device and inject events.
Requires: sudo usermod -aG input $USER && sudo chmod 666 /dev/uinput
"""

import struct
import time
import os
import fcntl
import ctypes
from typing import List, Optional

# =========================================================================
# Linux input event constants (from linux/input.h & linux/uinput.h)
# =========================================================================

# Event types
EV_SYN = 0x00
EV_KEY = 0x01
EV_REL = 0x02
EV_ABS = 0x03

# Sync events
SYN_REPORT = 0x00

# Mouse buttons
BTN_LEFT = 0x110
BTN_RIGHT = 0x111
BTN_MIDDLE = 0x112

# Absolute axes
ABS_X = 0x00
ABS_Y = 0x01

# Relative axes
REL_X = 0x00
REL_Y = 0x01
REL_WHEEL = 0x08

# Key states
KEY_PRESS = 1
KEY_RELEASE = 0

# uinput ioctl
UINPUT_MAX_NAME_SIZE = 80
UI_SET_EVBIT = 0x40045564
UI_SET_KEYBIT = 0x40045565
UI_SET_RELBIT = 0x40045566
UI_SET_ABSBIT = 0x40045567
UI_DEV_CREATE = 0x5501
UI_DEV_DESTROY = 0x5502
UI_DEV_SETUP = 0x405c5503
UI_ABS_SETUP = 0x40185504

# input_event struct format: time_sec(L) time_usec(L) type(H) code(H) value(i)
INPUT_EVENT_FORMAT = 'LLHHi'
INPUT_EVENT_SIZE = struct.calcsize(INPUT_EVENT_FORMAT)

# Key codes mapping (subset — common keys)
KEY_MAP = {
    'a': 30, 'b': 48, 'c': 46, 'd': 32, 'e': 18, 'f': 33,
    'g': 34, 'h': 35, 'i': 23, 'j': 36, 'k': 37, 'l': 38,
    'm': 50, 'n': 49, 'o': 24, 'p': 25, 'q': 16, 'r': 19,
    's': 31, 't': 20, 'u': 22, 'v': 47, 'w': 17, 'x': 45,
    'y': 21, 'z': 44,
    '0': 11, '1': 2, '2': 3, '3': 4, '4': 5, '5': 6,
    '6': 7, '7': 8, '8': 9, '9': 10,
    ' ': 57, '\n': 28, '\t': 15,
    '.': 52, ',': 51, '-': 12, '=': 13, '/': 53,
    ';': 39, "'": 40, '[': 26, ']': 27, '\\': 43,
    '`': 41,
    'enter': 28, 'space': 57, 'backspace': 14, 'tab': 15,
    'escape': 1, 'esc': 1,
    'up': 103, 'down': 108, 'left': 105, 'right': 106,
    'home': 102, 'end': 107, 'pageup': 104, 'pagedown': 109,
    'delete': 111, 'insert': 110,
    'f1': 59, 'f2': 60, 'f3': 61, 'f4': 62, 'f5': 63,
    'f6': 64, 'f7': 65, 'f8': 66, 'f9': 67, 'f10': 68,
    'f11': 87, 'f12': 88,
    'ctrl': 29, 'lctrl': 29, 'rctrl': 97,
    'shift': 42, 'lshift': 42, 'rshift': 54,
    'alt': 56, 'lalt': 56, 'ralt': 100,
    'super': 125, 'meta': 125,
}

# Characters that need shift
SHIFT_CHARS = set('ABCDEFGHIJKLMNOPQRSTUVWXYZ!@#$%^&*()_+{}|:"<>?~')
SHIFT_MAP = {
    '!': '1', '@': '2', '#': '3', '$': '4', '%': '5',
    '^': '6', '&': '7', '*': '8', '(': '9', ')': '0',
    '_': '-', '+': '=', '{': '[', '}': ']', '|': '\\',
    ':': ';', '"': "'", '<': ',', '>': '.', '?': '/',
    '~': '`',
}


class KernelInput:
    """Kernel-level input injection via /dev/uinput."""

    def __init__(self, screen_width: int = 1280, screen_height: int = 800):
        self.screen_width = screen_width
        self.screen_height = screen_height
        self.fd = None
        self._setup_device()

    def _setup_device(self):
        """Create a virtual input device via uinput."""
        try:
            self.fd = os.open("/dev/uinput", os.O_WRONLY | os.O_NONBLOCK)

            # Enable event types
            fcntl.ioctl(self.fd, UI_SET_EVBIT, EV_KEY)
            fcntl.ioctl(self.fd, UI_SET_EVBIT, EV_ABS)
            fcntl.ioctl(self.fd, UI_SET_EVBIT, EV_REL)
            fcntl.ioctl(self.fd, UI_SET_EVBIT, EV_SYN)

            # Enable all key codes (0-255)
            for i in range(256):
                fcntl.ioctl(self.fd, UI_SET_KEYBIT, i)

            # Enable mouse buttons
            fcntl.ioctl(self.fd, UI_SET_KEYBIT, BTN_LEFT)
            fcntl.ioctl(self.fd, UI_SET_KEYBIT, BTN_RIGHT)
            fcntl.ioctl(self.fd, UI_SET_KEYBIT, BTN_MIDDLE)

            # Enable absolute axes (for mouse positioning)
            fcntl.ioctl(self.fd, UI_SET_ABSBIT, ABS_X)
            fcntl.ioctl(self.fd, UI_SET_ABSBIT, ABS_Y)

            # Enable relative axes (for scrolling)
            fcntl.ioctl(self.fd, UI_SET_RELBIT, REL_WHEEL)

            # Setup ABS axes with screen resolution
            # struct uinput_abs_setup
            for axis, max_val in [(ABS_X, self.screen_width), (ABS_Y, self.screen_height)]:
                abs_setup = struct.pack(
                    'HHiiiii',  # code, fuzz, flat, resolution, minimum, maximum, padding
                    axis, 0, 0, 0, 0, max_val, 0
                )
                try:
                    fcntl.ioctl(self.fd, UI_ABS_SETUP, abs_setup)
                except Exception:
                    pass  # Older kernels might not support this

            # Setup device info
            # struct uinput_setup
            name = b"AriaOS Virtual Input"
            name_padded = name + b'\x00' * (UINPUT_MAX_NAME_SIZE - len(name))

            dev_setup = struct.pack(
                f'HHI{UINPUT_MAX_NAME_SIZE}sI',
                0x01,   # BUS_PCI
                0x01,   # vendor
                0x01,   # product
                name_padded,
                0       # ff_effects_max
            )
            try:
                fcntl.ioctl(self.fd, UI_DEV_SETUP, dev_setup)
            except Exception:
                pass

            # Create the device
            fcntl.ioctl(self.fd, UI_DEV_CREATE)
            time.sleep(0.3)  # Wait for device to be registered

            print("[KernelInput] Virtual input device created successfully")

        except PermissionError:
            print("[KernelInput] Permission denied on /dev/uinput")
            print("  Run: sudo chmod 666 /dev/uinput")
            print("  And: sudo usermod -aG input $USER")
            self.fd = None
        except Exception as e:
            print(f"[KernelInput] Failed to create device: {e}")
            self.fd = None

    def _write_event(self, ev_type: int, code: int, value: int):
        """Write a single input event."""
        if self.fd is None:
            return

        now = time.time()
        sec = int(now)
        usec = int((now - sec) * 1_000_000)

        event = struct.pack(INPUT_EVENT_FORMAT, sec, usec, ev_type, code, value)
        os.write(self.fd, event)

    def _syn(self):
        """Send sync event (flushes pending events)."""
        self._write_event(EV_SYN, SYN_REPORT, 0)

    # =========================================================================
    # Mouse Actions
    # =========================================================================

    def move_to(self, x: int, y: int):
        """Move mouse to absolute screen position."""
        self._write_event(EV_ABS, ABS_X, x)
        self._write_event(EV_ABS, ABS_Y, y)
        self._syn()
        time.sleep(0.01)

    def click(self, x: int, y: int, button: int = BTN_LEFT):
        """Click at absolute screen position."""
        self.move_to(x, y)
        time.sleep(0.05)

        # Press
        self._write_event(EV_KEY, button, KEY_PRESS)
        self._syn()
        time.sleep(0.05)

        # Release
        self._write_event(EV_KEY, button, KEY_RELEASE)
        self._syn()
        time.sleep(0.05)

    def double_click(self, x: int, y: int):
        """Double-click at position."""
        self.click(x, y)
        time.sleep(0.1)
        self.click(x, y)

    def right_click(self, x: int, y: int):
        """Right-click at position."""
        self.click(x, y, BTN_RIGHT)

    def scroll(self, amount: int = -3):
        """Scroll up (positive) or down (negative)."""
        self._write_event(EV_REL, REL_WHEEL, amount)
        self._syn()

    def drag(self, x1: int, y1: int, x2: int, y2: int, steps: int = 20):
        """Drag from (x1,y1) to (x2,y2)."""
        self.move_to(x1, y1)
        time.sleep(0.1)

        self._write_event(EV_KEY, BTN_LEFT, KEY_PRESS)
        self._syn()
        time.sleep(0.05)

        for i in range(steps + 1):
            t = i / steps
            x = int(x1 + (x2 - x1) * t)
            y = int(y1 + (y2 - y1) * t)
            self.move_to(x, y)
            time.sleep(0.01)

        self._write_event(EV_KEY, BTN_LEFT, KEY_RELEASE)
        self._syn()

    # =========================================================================
    # Keyboard Actions
    # =========================================================================

    def key_press(self, key_code: int):
        """Press a key (and release)."""
        self._write_event(EV_KEY, key_code, KEY_PRESS)
        self._syn()
        time.sleep(0.02)
        self._write_event(EV_KEY, key_code, KEY_RELEASE)
        self._syn()
        time.sleep(0.02)

    def type_text(self, text: str, delay: float = 0.03):
        """Type a string of text character by character."""
        for char in text:
            if char in SHIFT_CHARS:
                # Need shift
                actual_char = SHIFT_MAP.get(char, char.lower())
                key_code = KEY_MAP.get(actual_char)
                if key_code:
                    # Hold shift
                    self._write_event(EV_KEY, KEY_MAP['shift'], KEY_PRESS)
                    self._syn()
                    time.sleep(0.01)
                    # Press key
                    self.key_press(key_code)
                    # Release shift
                    self._write_event(EV_KEY, KEY_MAP['shift'], KEY_RELEASE)
                    self._syn()
            else:
                key_code = KEY_MAP.get(char)
                if key_code:
                    self.key_press(key_code)

            time.sleep(delay)

    def key_combo(self, keys: List[str]):
        """Press a key combination (e.g. ['ctrl', 'c'])."""
        codes = [KEY_MAP.get(k.lower()) for k in keys]
        codes = [c for c in codes if c is not None]

        if not codes:
            return

        # Press all keys
        for code in codes:
            self._write_event(EV_KEY, code, KEY_PRESS)
            self._syn()
            time.sleep(0.02)

        # Release all keys (reverse order)
        for code in reversed(codes):
            self._write_event(EV_KEY, code, KEY_RELEASE)
            self._syn()
            time.sleep(0.02)

    def press_enter(self):
        """Press Enter."""
        self.key_press(KEY_MAP['enter'])

    def press_escape(self):
        """Press Escape."""
        self.key_press(KEY_MAP['escape'])

    def press_tab(self):
        """Press Tab."""
        self.key_press(KEY_MAP['tab'])

    def press_backspace(self, count: int = 1):
        """Press Backspace N times."""
        for _ in range(count):
            self.key_press(KEY_MAP['backspace'])

    # =========================================================================
    # Cleanup
    # =========================================================================

    def close(self):
        """Destroy the virtual device and close."""
        if self.fd is not None:
            try:
                fcntl.ioctl(self.fd, UI_DEV_DESTROY)
            except Exception:
                pass
            os.close(self.fd)
            self.fd = None
            print("[KernelInput] Device closed")

    def __del__(self):
        self.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


# =========================================================================
# Fallback: xdotool-based input (if /dev/uinput is not available)
# =========================================================================

class XdotoolInput:
    """Fallback input via xdotool if kernel access is not available."""

    def __init__(self, display: str = ":0"):
        self.env = {**os.environ, "DISPLAY": display}

    def _run(self, args: list):
        import subprocess
        subprocess.run(args, env=self.env, timeout=5)

    def click(self, x: int, y: int):
        self._run(["xdotool", "mousemove", str(x), str(y), "click", "1"])

    def right_click(self, x: int, y: int):
        self._run(["xdotool", "mousemove", str(x), str(y), "click", "3"])

    def type_text(self, text: str, delay: float = 30):
        self._run(["xdotool", "type", "--delay", str(int(delay * 1000)), text])

    def key_combo(self, keys: List[str]):
        combo = "+".join(keys)
        self._run(["xdotool", "key", combo])

    def move_to(self, x: int, y: int):
        self._run(["xdotool", "mousemove", str(x), str(y)])

    def close(self):
        pass


def create_input(prefer_kernel: bool = True, **kwargs) -> 'KernelInput':
    """Factory: create best available input method."""
    if prefer_kernel and os.path.exists("/dev/uinput"):
        try:
            return KernelInput(**kwargs)
        except Exception:
            pass

    return XdotoolInput(**kwargs)


# =========================================================================
# Quick test
# =========================================================================

if __name__ == "__main__":
    print("Testing KernelInput...")
    with create_input() as ki:
        print(f"Input device: {type(ki).__name__}")
        # Test: move mouse to center
        ki.move_to(640, 400)
        time.sleep(0.5)
        print("Mouse moved to center. Done!")
