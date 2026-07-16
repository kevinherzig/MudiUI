#!/usr/bin/env python3
"""MudiUI — live Signal screen (companion PoC, Direction A "Dark Instrument").

Pure Python: background pollers hit ubus / AT for real cellular data, a render
loop draws with Pillow and pushes RGB565 to /dev/fb0, and a touch thread makes
the on-screen "STOCK UI" button hand the panel back to gl_screen.

Run on the Mudi:   python3 mudi_signal_live.py [seconds]
                   (no arg = run until the STOCK UI button is tapped)
Preview a frame off-device:   python3 mudi_signal_live.py --mock
"""
import sys, os, time, json, subprocess, threading, signal, math, re
import numpy as np
from PIL import Image, ImageDraw, ImageFont

W, H = 240, 320
BUS = "cpu"
MOCK = "--mock" in sys.argv
DURATION = next((float(a) for a in sys.argv[1:] if a.replace(".", "").isdigit()), None)

# ---- fonts (gl_screen's TTFs on device; DejaVu when previewing on a workstation) ----
_ON_DEVICE = os.path.isdir("/etc/gl_screen/language/ttf")
if _ON_DEVICE:
    FD = "/etc/gl_screen/language/ttf/"
    F_BOLD, F_SEMI, F_MED, F_MONO = "default_bold.ttf", "default_semibold.ttf", "default_medium.ttf", "default_mono_medium.ttf"
else:
    FD = "/usr/share/fonts/truetype/dejavu/"
    F_BOLD, F_SEMI, F_MED, F_MONO = "DejaVuSans-Bold.ttf", "DejaVuSans-Bold.ttf", "DejaVuSans.ttf", "DejaVuSansMono.ttf"

def _f(name, sz): return ImageFont.truetype(FD + name, sz)
BOLD  = {s: _f(F_BOLD, s) for s in (10, 12, 13, 15)}
MED   = {s: _f(F_MED,  s) for s in (9, 10, 11)}
MONO  = {s: _f(F_MONO, s) for s in (9, 11, 12, 15, 32)}

# ---- palette ----
BG=(8,10,14); CYAN=(0,214,214); DIM=(120,130,145); GRID=(28,34,44)
INK=(234,244,250); SUB=(150,160,175); PANEL=(11,14,19); BTN=(18,22,30); BTN_BD=(40,60,66)

STOCK_BTN = (12, H-34, W-12, H-10)   # x0,y0,x1,y1 hit rect

# ---- shared state ----
state = {
    "carrier": "—", "slot": "?", "mode": "—", "cell_id": "—", "band": None,
    "rsrp": None, "rsrq": None, "sinr": None, "rsrp_level": 0,
    "freq_mhz": None, "bw": "—", "hist": [], "updated": 0.0, "changed_at": 0.0,
    "err": None, "_key": None,
}
lock = threading.Lock()
stop_flag = threading.Event()

# ---- data layer ----
def ubus(obj, method, args):
    out = subprocess.run(["ubus", "call", obj, method, json.dumps(args)],
                         capture_output=True, text=True, timeout=8)
    return json.loads(out.stdout) if out.stdout.strip() else {}

def at(cmd, sub_id):
    r = ubus("modem.CPU.AT", "get_result_AT", {"cmd": cmd, "sub_id": sub_id, "timeout": 3})
    return r.get("data", "")

def poll_cell():
    r = ubus("cellular.network", "info", {"bus": BUS, "slot": int(state["slot"]) if state["slot"].isdigit() else 1})
    ci = r["networks"][0]["cell_info"]
    arfcn = int(ci.get("tx_channel", 0) or 0)
    rsrp, rsrq, sinr = int(ci["rsrp"]), int(ci["rsrq"]), int(ci["sinr"])
    cid = ci.get("id", "—"); key = (rsrp, rsrq, sinr, cid)
    with lock:
        changed = key != state["_key"]
        state["cell_id"] = cid
        state["mode"] = ci.get("mode", "—").split()[0]
        state["band"] = ci.get("band")
        state["rsrp"], state["rsrq"], state["sinr"] = rsrp, rsrq, sinr
        state["rsrp_level"] = int(ci.get("rsrp_level", 0))
        state["freq_mhz"] = round(arfcn * 5 / 1000.0) if arfcn else None
        state["bw"] = ci.get("dl_bandwidth", "—").replace("MHz", " MHz")
        state["updated"] = time.time(); state["err"] = None; state["_key"] = key
        if changed:                                    # flash + append history only on real change
            state["changed_at"] = time.time()
            state["hist"] = (state["hist"] + [rsrp])[-60:]

def poll_slow():
    m = ubus("cellular.modem", "status", {"bus": BUS})
    slot = str(m["modems"][0]["current_sim_slot"])
    resp = at("AT+QSPN", int(slot))
    name = re.search(r'\+QSPN:\s*"([^"]*)"', resp)
    with lock:
        state["slot"] = slot
        if name and name.group(1): state["carrier"] = name.group(1)

def poller():
    # modem refreshes cell_info every ~20s and ubus is ~8ms, so 4s polling is plenty;
    # carrier / active slot change rarely -> refresh every ~32s.
    try: poll_slow()
    except Exception as e:
        with lock: state["err"] = str(e)[:40]
    i = 0
    while not stop_flag.is_set():
        try: poll_cell()
        except Exception as e:
            with lock: state["err"] = str(e)[:40]
        i += 1
        if i % 8 == 0:
            try: poll_slow()
            except Exception as e:
                with lock: state["err"] = str(e)[:40]
        if stop_flag.wait(4.0): return

# ---- rendering ----
def ctext(d, cx, y, t, f, fill): d.text((cx - d.textlength(t, font=f)/2, y), t, font=f, fill=fill)

def rsrp_to_frac(dbm):                             # -120..-70 dBm -> 0..1 for the trace
    return max(0.03, min(0.97, (dbm + 120) / 50.0))

def draw(anim_frac, live):
    with lock: s = dict(state); hist = list(state["hist"])
    img = Image.new("RGB", (W, H), BG); d = ImageDraw.Draw(img)
    # header: carrier + SIM badge + live dot + mode
    cf = BOLD[15]; d.text((10, 6), s["carrier"], font=cf, fill=INK)
    bx = 10 + d.textlength(s["carrier"], font=cf) + 8
    bl = f"SIM{s['slot']}"; bf = MONO[9]
    d.rounded_rectangle((bx, 8, bx + d.textlength(bl, font=bf) + 10, 22), radius=4, fill=BTN, outline=BTN_BD)
    d.text((bx+5, 9), bl, font=bf, fill=CYAN)
    mode = s["mode"] or "—"; hd = MONO[11]
    dotx = W-12-d.textlength(mode, font=hd)-14              # live dot: dim idle, bright on refresh
    d.ellipse((dotx, 12, dotx+8, 20), fill=CYAN if live else (0, 66, 66))
    d.text((W-10-d.textlength(mode, font=hd), 9), mode, font=hd, fill=CYAN)
    d.line((10, 25, W-10, 25), fill=GRID, width=1)
    # arc gauge (thinner ring + smaller numeral so the reading isn't cramped)
    cx, cy, r = 120, 92, 50
    d.arc((cx-r, cy-r, cx+r, cy+r), 135, 135+270, fill=GRID, width=8)
    if anim_frac > 0: d.arc((cx-r, cy-r, cx+r, cy+r), 135, 135+int(270*anim_frac), fill=CYAN, width=8)
    ctext(d, cx, cy-22, str(s["rsrp"]) if s["rsrp"] is not None else "--", MONO[32], INK)
    ctext(d, cx, cy+15, "dBm  RSRP", MONO[11], DIM)
    # RSRQ / SINR
    for lx, (k, v) in zip((44, 140), (("RSRQ", s["rsrq"]), ("SINR", s["sinr"]))):
        d.text((lx, 150), k, font=MONO[11], fill=DIM)
        d.text((lx, 163), f"{v} dB" if v is not None else "-- dB", font=MONO[15], fill=INK)
    # serving-cell panel
    px0, py0, px1, py1 = 12, 184, W-12, 258
    d.rounded_rectangle((px0, py0, px1, py1), radius=8, outline=GRID, width=1, fill=PANEL)
    d.text((px0+8, py0+6), "SERVING CELL", font=MONO[9], fill=CYAN)
    d.text((px0+8, py0+24), "CELL", font=MONO[9], fill=DIM)
    d.text((px0+52, py0+22), str(s["cell_id"]), font=MONO[15], fill=INK)
    cells = [("BAND", f"n{s['band']}" if s['band'] else "—"),
             ("FREQ", f"{s['freq_mhz']} MHz" if s['freq_mhz'] else "—"),
             ("BW", s["bw"])]
    cw = (px1-px0)/3
    for i, (k, v) in enumerate(cells):
        mx = px0 + i*cw + 8
        d.text((mx, py0+46), k, font=MONO[9], fill=DIM)
        d.text((mx, py0+57), v, font=MONO[12], fill=INK)
    # live trace of real RSRP history
    gx, gy, gw, gh = 12, 266, W-24, 14
    d.rectangle((gx, gy, gx+gw, gy+gh), outline=GRID)
    if len(hist) >= 2:
        pts = [(gx+2 + i*(gw-4)/(len(hist)-1), gy+gh-2 - rsrp_to_frac(v)*(gh-4)) for i, v in enumerate(hist)]
        d.line(pts, fill=CYAN, width=1)
    # STOCK UI button
    d.rounded_rectangle(STOCK_BTN, radius=11, fill=BTN, outline=BTN_BD, width=1)
    ctext(d, W/2, STOCK_BTN[1]+5, "▶  STOCK UI", BOLD[12], CYAN)
    return img

def pack565(img):
    a = np.asarray(img, dtype=np.uint8).astype(np.uint16)
    r, g, b = a[:, :, 0], a[:, :, 1], a[:, :, 2]
    return (((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)).astype("<u2").tobytes()

# ---- touch: tap the STOCK UI button to hand back to gl_screen ----
def toucher():
    try:
        from evdev import InputDevice, ecodes
        dev = InputDevice("/dev/input/event0")
        x = y = 0; down = False
        for e in dev.read_loop():
            if stop_flag.is_set(): return
            if e.type == ecodes.EV_ABS:
                if e.code in (ecodes.ABS_X, ecodes.ABS_MT_POSITION_X): x = e.value
                elif e.code in (ecodes.ABS_Y, ecodes.ABS_MT_POSITION_Y): y = e.value
            elif e.type == ecodes.EV_KEY and e.code == ecodes.BTN_TOUCH:
                if e.value == 1: down = True
                elif e.value == 0 and down:
                    down = False
                    x0, y0, x1, y1 = STOCK_BTN
                    if x0 <= x <= x1 and y0 <= y <= y1:
                        print("STOCK UI tapped -> restoring gl_screen"); stop_flag.set(); return
    except Exception as e:
        print("touch disabled:", e)

# ---- lifecycle ----
def sh(cmd): subprocess.run(cmd, shell=True, capture_output=True)

def main():
    if MOCK:
        state.update(carrier="T-Mobile", slot="1", mode="NR5G-SA", cell_id="187461035",
                     band=71, rsrp=-100, rsrq=-13, sinr=1, rsrp_level=4, freq_mhz=637,
                     bw="15 MHz", hist=[-104,-101,-100,-99,-102,-100,-98,-100])
        state["changed_at"] = time.time()
        draw(0.8, True).save("/tmp/mudi_live_preview.png"); print("wrote /tmp/mudi_live_preview.png"); return

    sh("/etc/init.d/gl_screen stop"); time.sleep(1)
    sh("echo 0 > /sys/class/graphics/fb0/blank")
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop_flag.set())
    threading.Thread(target=poller, daemon=True).start()
    threading.Thread(target=toucher, daemon=True).start()

    # Tick at 30Hz but only *render* when something actually changed (dirty-skip).
    # Idle => a tuple compare + sleep (~0% CPU); a data change eases the gauge at 30fps
    # for ~0.5s and flashes the live dot for 1.2s, then goes quiet again.
    frac = 0.0; t0 = time.time(); last_sig = None
    try:
        with open("/dev/fb0", "r+b", buffering=0) as fb:
            while not stop_flag.is_set():
                with lock:
                    target = state["rsrp_level"]/5.0; ca = state["changed_at"]
                    vals = (state["rsrp"], state["rsrq"], state["sinr"], state["carrier"],
                            state["slot"], state["cell_id"], state["band"], state["freq_mhz"],
                            state["bw"], state["mode"], len(state["hist"]))
                frac += (target - frac) * 0.22                     # ease the gauge
                fresh = (time.time() - ca) < 1.2                   # flash dot right after a change
                sig = (round(frac, 3), fresh, vals)
                if sig != last_sig:
                    fb.seek(0); fb.write(pack565(draw(frac, fresh)))
                    last_sig = sig
                if DURATION and time.time()-t0 > DURATION: break
                if stop_flag.wait(1/30.0): break
    finally:
        stop_flag.set(); sh("/etc/init.d/gl_screen start")
        print("gl_screen restored")

if __name__ == "__main__":
    main()
