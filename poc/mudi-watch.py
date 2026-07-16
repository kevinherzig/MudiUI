#!/usr/bin/env python3
"""MudiUI toggle watcher — always-on, tiny.

Reads the touch panel (/dev/input/event0) as a SECOND reader, so it works whether
MudiUI or gl_screen currently owns the screen (input devices allow multiple readers;
the framebuffer does not). A ~1.6s long-press (finger held still) toggles the MudiUI
service: running -> stop (gl_screen appears); stopped -> start (our gauges appear).

Quick taps and swipes are ignored, so normal MudiUI interaction is unaffected.
Runs as its own procd service (mudi-watch) that never stops — it's the way back.
"""
import time, select, subprocess
from evdev import InputDevice, ecodes

TOUCH = "/dev/input/event0"
HOLD = 1.6          # default seconds of still touch to trigger a toggle (overridden by uci)
MOVE_TOL = 40       # px of movement that reclassifies a hold as a swipe (cancels)
DEBOUNCE = 1.5      # seconds to ignore input right after a toggle

def read_hold():
    # long-press duration is user-configurable on the Settings page (uci mudi.main.longpress).
    # Re-read on each touch-down so a change takes effect without restarting the watcher.
    try:
        o = subprocess.run(["uci", "-q", "get", "mudi.main.longpress"],
                           capture_output=True, text=True, timeout=2)
        return float(o.stdout.strip()) or HOLD
    except Exception:
        return HOLD

def toggle():
    # MudiUI stays resident and pause/resumes on SIGUSR1 (freezing/thawing gl_screen),
    # so toggles are instant — no cold start of either UI. Fall back to starting the
    # service if the UI process somehow isn't running.
    r = subprocess.run(["pgrep", "-f", "/usr/bin/mudi.py"], capture_output=True, text=True)
    pids = r.stdout.split()
    if pids:
        subprocess.run(["kill", "-USR1", pids[0]])
    else:
        subprocess.run(["/etc/init.d/mudi", "start"])

def watch():
    dev = InputDevice(TOUCH)
    down_t = None; x0 = y0 = x = y = 0; fired = False; hold = HOLD
    while True:
        r, _, _ = select.select([dev.fd], [], [], 0.1)
        if r:
            for e in dev.read():
                if e.type == ecodes.EV_KEY and e.code == ecodes.BTN_TOUCH:
                    if e.value == 1: down_t = time.time(); x0, y0 = x, y; fired = False; hold = read_hold()
                    else: down_t = None; fired = False
                elif e.type == ecodes.EV_ABS:
                    if e.code in (ecodes.ABS_X, ecodes.ABS_MT_POSITION_X): x = e.value
                    elif e.code in (ecodes.ABS_Y, ecodes.ABS_MT_POSITION_Y): y = e.value
        if down_t and not fired:
            if abs(x - x0) > MOVE_TOL or abs(y - y0) > MOVE_TOL:
                down_t = None                                  # moved -> it's a swipe, not a hold
            elif time.time() - down_t >= hold:
                fired = True; down_t = None
                toggle()
                time.sleep(DEBOUNCE)

def main():
    while True:
        try:
            watch()
        except Exception as e:
            print("mudi-watch: reopening after error:", e); time.sleep(1)

if __name__ == "__main__":
    main()
