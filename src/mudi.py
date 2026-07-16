#!/usr/bin/env python3
"""MudiUI — event-driven instrument framework for the GL-E5800 front panel.

Object model (drives consistency across many pages):

    Theme        one palette + font set; every widget draws through it.
    DataSource   a self-gating subject: owns a subscriber list, POLLS ONLY while
                 count > 0, notifies subscribers on change (deduped). App-owned.
    Widget       PARAMETERIZED by bus keys — the same ArcGauge shows cellular signal
                 on SignalPage and WiFi link on WifiPage. Redraws reactively.
    Page         bundles configured widgets (its UI). SignalPage / WifiPage / SettingsPage.
    App          owns sources + key->source registry + display + touch + swipe nav
                 + settings (uci) + idle-blank + modal overlays.

Reused widgets prove the consistency thesis: WifiPage adds NO new widget classes.
The key=... bindings below are exactly what the future JSON layer will declare.

Run on the Mudi:  python3 mudi.py [seconds]   (no arg = until STOCK UI tapped)
Preview a frame:  python3 mudi.py --mock [signal|wifi|system|eth|settings] [hero|arc]
"""
import sys, os, time, json, subprocess, threading, signal, re, textwrap
import numpy as np
from PIL import Image, ImageDraw, ImageFont

W, H = 240, 320
BUS = "cpu"
_MISS = object()
_FLASH = b'\xff\x07' * (W * H)                            # full-screen cyan (RGB565 0x07FF) toggle flash

MUDI_VERSION = "0.1"
BL_DIR = "/sys/class/backlight/soc:backlight"            # brightness (20..120) + bl_power (0 on / 1 off)
# Full band lists backed up from the modem before the n71 lock (verified 2026-07-15) — used to
# RESTORE when band lock is turned off.
NR5G_BANDS_ALL = "2:5:7:12:13:14:25:26:29:30:38:41:48:66:70:71:77:78"
NSA_BANDS_ALL  = "2:5:7:12:14:25:26:30:38:41:48:66:71:77:78"
MODE_AT = {"auto": "AUTO", "5g": "NR5G", "lte": "LTE"}   # net_mode value -> AT+QNWPREFCFG mode_pref


# ───────────────────────── Theme ─────────────────────────
_ON_DEV = os.path.isdir("/etc/gl_screen/language/ttf")
_FD   = "/etc/gl_screen/language/ttf/" if _ON_DEV else "/usr/share/fonts/truetype/dejavu/"
_BOLD = "default_bold.ttf" if _ON_DEV else "DejaVuSans-Bold.ttf"
_MED  = "default_medium.ttf" if _ON_DEV else "DejaVuSans.ttf"
_MONO = "default_mono_medium.ttf" if _ON_DEV else "DejaVuSansMono.ttf"
def _load(name, sizes): return {s: ImageFont.truetype(_FD+name, s) for s in sizes}

class Theme:
    BG=(8,10,14); CYAN=(0,214,214); DIM=(120,130,145); GRID=(28,34,44)
    INK=(234,244,250); SUB=(150,160,175); PANEL=(11,14,19)
    BTN=(18,22,30); BTN_BD=(40,60,66); DOT_OFF=(0,66,66)
    bold = _load(_BOLD, (10,12,13,15))
    med  = _load(_MED,  (9,10,11))
    mono = _load(_MONO, (9,10,11,12,15,32))

def ctext(d, cx, y, t, f, fill): d.text((cx - d.textlength(t, font=f)/2, y), t, font=f, fill=fill)

def pack565(img):
    a = np.asarray(img, dtype=np.uint8).astype(np.uint16)
    r, g, b = a[:,:,0], a[:,:,1], a[:,:,2]
    return (((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)).astype("<u2").tobytes()


# ───────────────────────── DataSource (self-gating subject) ─────────────────────────
class DataSource:
    provides = (); cadence = 4.0; name = "source"

    def __init__(self):
        self._subs = {}; self._last = {}; self._count = 0
        self._lock = threading.Lock(); self._stop = threading.Event(); self._thread = None

    def subscribe(self, key, cb):
        with self._lock:
            self._subs.setdefault(key, []).append(cb); self._count += 1
            last = self._last.get(key, _MISS); start = self._count == 1
        if last is not _MISS: cb(last)                 # replay current value to late joiner
        if start: self._wake()                         # 0 -> 1: begin polling

    def unsubscribe(self, key, cb):
        with self._lock:
            lst = self._subs.get(key, [])
            if cb in lst: lst.remove(cb); self._count -= 1
            sleep = self._count == 0
        if sleep: self._stop.set()                     # 1 -> 0: stop polling

    def _emit(self, key, value):
        with self._lock:
            if self._last.get(key, _MISS) == value: return   # dedup: notify only on change
            self._last[key] = value; cbs = list(self._subs.get(key, []))
        for cb in cbs: cb(value)

    def _wake(self):
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name=self.name, daemon=True)
        self._thread.start()

    def _run(self):
        while not self._stop.is_set():
            try: self.poll()
            except Exception as e: self._emit(self.name + ".err", str(e)[:40])
            if self._stop.wait(self.cadence): return

    def poll(self): raise NotImplementedError


def _ubus(obj, method, args):
    o = subprocess.run(["ubus", "call", obj, method, json.dumps(args)],
                       capture_output=True, text=True, timeout=8)
    return json.loads(o.stdout) if o.stdout.strip() else {}

def _at(cmd, sub_id, timeout=5):                        # run an AT command via the modem ubus bridge
    return _ubus("modem.CPU.AT", "get_result_AT",
                 {"cmd": cmd, "sub_id": int(sub_id), "timeout": timeout})

def _san(s):                                            # SSIDs can carry emoji the panel font lacks
    t = "".join(ch for ch in (s or "") if 32 <= ord(ch) < 127).strip()
    return t or "—"

def _read(path):
    try:
        with open(path) as f: return f.read().strip()
    except Exception: return None


# ───────────────────────── Settings (uci-backed) ─────────────────────────
class Settings:
    """Reads/writes uci /etc/config/mudi (section 'main'). Falls back to DEFAULTS off-device."""
    PKG = "mudi"; SEC = "main"
    DEFAULTS = {"brightness": "90", "screen_timeout": "30", "stay_awake_charging": "1",
                "default_page": "0", "band_lock": "0", "net_mode": "auto",
                "longpress": "1.6", "start_on_boot": "1", "graph_style": "hero"}

    def __init__(self):
        self.vals = dict(self.DEFAULTS); self.load()

    def load(self):
        try:
            o = subprocess.run(["uci", "-q", "show", self.PKG], capture_output=True, text=True, timeout=4)
            pat = re.compile(r'%s\.%s\.(\w+)=(.*)' % (self.PKG, self.SEC))
            for line in o.stdout.splitlines():
                m = pat.match(line)
                if m: self.vals[m.group(1)] = m.group(2).strip().strip("'")
        except Exception:
            pass

    def get(self, k): return self.vals.get(k, self.DEFAULTS.get(k))

    def set(self, k, v):
        v = str(v); self.vals[k] = v
        try:
            subprocess.run(
                "uci -q get {p}.{s} >/dev/null 2>&1 || uci set {p}.{s}=settings; "
                "uci set {p}.{s}.{k}='{v}'; uci commit {p}".format(p=self.PKG, s=self.SEC, k=k, v=v),
                shell=True, timeout=4)
        except Exception:
            pass


class CellularSource(DataSource):
    provides = ("signal.rsrp","signal.rsrq","signal.sinr","signal.level",
                "cell.id","cell.band","cell.freq","cell.bw","net.mode",
                "sim.carrier","sim.slot")
    cadence = 4.0; name = "cellular"

    def __init__(self, bus=BUS):
        super().__init__(); self.bus = bus; self._n = 0; self._slot = "1"

    def poll(self):
        if self._n % 8 == 0:
            try: self._poll_slow()                      # isolated: never blocks cell data
            except Exception as e: self._emit("sim.err", str(e)[:40])
        self._n += 1
        r = _ubus("cellular.network", "info", {"bus": self.bus, "slot": int(self._slot)})
        ci = r["networks"][0]["cell_info"]
        arfcn = int(ci.get("tx_channel", 0) or 0)
        self._emit("signal.rsrp", int(ci["rsrp"]))
        self._emit("signal.rsrq", "%s dB" % ci["rsrq"])
        self._emit("signal.sinr", "%s dB" % ci["sinr"])
        self._emit("signal.level", int(ci.get("rsrp_level", 0)))
        self._emit("cell.id", ci.get("id", "—"))
        self._emit("cell.band", "n%s" % ci["band"] if ci.get("band") else "—")
        self._emit("cell.freq", "%d MHz" % round(arfcn*5/1000.0) if arfcn else "—")
        self._emit("cell.bw", ci.get("dl_bandwidth", "—").replace("MHz", " MHz"))
        self._emit("net.mode", ci.get("mode", "—").split()[0])

    def _poll_slow(self):
        m = _ubus("cellular.modem", "status", {"bus": self.bus})
        slot = m.get("current_sim_slot")
        if slot is None and "modems" in m: slot = m["modems"][0].get("current_sim_slot")
        if slot: self._slot = str(slot)
        self._emit("sim.slot", self._slot)
        r = _ubus("modem.CPU.AT", "get_result_AT",
                  {"cmd": "AT+QSPN", "sub_id": int(self._slot), "timeout": 3})
        mm = re.search(r'\+QSPN:\s*"([^"]*)"', r.get("data", ""))
        if mm and mm.group(1): self._emit("sim.carrier", mm.group(1))


class WifiSource(DataSource):
    provides = ("wifi.signal","wifi.level","wifi.ssid","wifi.band","wifi.rate",
                "wifi.chan","wifi.freq","wifi.width","wifi.ap","wifi.clients","wifi.mode")
    cadence = 4.0; name = "wifi"

    def poll(self):
        up = ap = None
        for dv in _ubus("iwinfo", "devices", {}).get("devices", []):
            info = _ubus("iwinfo", "info", {"device": dv})
            if info.get("mode") == "Client" and up is None: up = info
            elif info.get("mode") == "Master" and ap is None: ap = info
        self._emit("wifi.mode", "RPT" if up else "AP")
        if up:
            q, qm = up.get("quality", 0), (up.get("quality_max") or 70)
            self._emit("wifi.signal", up.get("signal"))
            self._emit("wifi.level", round(q / qm * 5))
            self._emit("wifi.ssid", _san(up.get("ssid")))
            self._emit("wifi.band", "5 GHz" if up.get("frequency", 0) > 3000 else "2.4 GHz")
            self._emit("wifi.rate", "%d M" % round(up.get("bitrate", 0) / 1000))
            self._emit("wifi.chan", up.get("channel"))
            self._emit("wifi.freq", "%d MHz" % up.get("frequency", 0))
            self._emit("wifi.width", up.get("htmode", "—"))
        self._emit("wifi.ap", _san(ap.get("ssid")) if ap else "—")
        cl = _ubus("gl-clients", "list", {}).get("clients", {})
        self._emit("wifi.clients", sum(1 for c in cl.values() if c.get("online")))


class SystemSource(DataSource):
    provides = ("batt.pct","batt.level","batt.state","batt.temp",
                "sys.cputemp","sys.load","sys.ram","sys.free","sys.uptime")
    cadence = 5.0; name = "system"

    def poll(self):
        mcu = _ubus("mcu", "status", {})
        pct = int(mcu.get("charge_percent", 0))
        self._emit("batt.pct", pct)
        self._emit("batt.level", round(pct / 20))
        cs = mcu.get("charging_status", 0)
        self._emit("batt.state", "CHG" if cs else ("FULL" if pct >= 100 else "BATT"))
        self._emit("batt.temp", "%s°C" % mcu.get("temperature", "--"))
        temps = [int(t) for z in range(15, 19)
                 for t in [_read("/sys/class/thermal/thermal_zone%d/temp" % z)]
                 if t and t.lstrip("-").isdigit()]
        if temps: self._emit("sys.cputemp", "%d°C" % (max(temps) // 1000))
        info = _ubus("system", "info", {})
        self._emit("sys.load", round(info.get("load", [0])[0] / 65536.0, 2))
        mem = info.get("memory", {}); tot = mem.get("total", 1); avail = mem.get("available", 0)
        self._emit("sys.ram", "%d%%" % int((tot - avail) / tot * 100))
        self._emit("sys.free", "%d MB" % (avail // 1048576))
        up = info.get("uptime", 0)
        self._emit("sys.uptime", "%dd %dh" % (up // 86400, (up % 86400) // 3600))


class EthernetSource(DataSource):
    provides = ("eth.speed","eth.level","eth.link","eth.port","eth.ip",
                "eth.rx","eth.tx","eth.rxn","eth.clients","eth.proto")
    cadence = 2.0; name = "eth"

    def __init__(self, dev="eth0", lan="br-lan"):
        super().__init__(); self.dev = dev; self.lan = lan; self._prev = None; self._pt = None

    def poll(self):
        self._emit("eth.port", self.dev)
        up = _read("/sys/class/net/%s/carrier" % self.dev) == "1"
        spd = _read("/sys/class/net/%s/speed" % self.dev)
        self._emit("eth.link", ("%s Mb" % spd) if (up and spd and spd != "-1") else ("UP" if up else "DOWN"))
        self._emit("eth.speed", spd if (up and spd not in ("-1", None)) else "DOWN")
        self._emit("eth.level", {"10":1,"100":2,"1000":4,"2500":5}.get(spd, 0) if up else 0)
        st = _ubus("network.interface.lan", "status", {})
        ip = next((a.get("address") for a in st.get("ipv4-address", [])), "—")
        self._emit("eth.ip", ip); self._emit("eth.proto", st.get("proto", "—"))
        rx = int(_read("/sys/class/net/%s/statistics/rx_bytes" % self.lan) or 0)
        tx = int(_read("/sys/class/net/%s/statistics/tx_bytes" % self.lan) or 0)
        now = time.time()
        if self._prev:
            dt = (now - self._pt) or 1
            rxk = int((rx - self._prev[0]) / dt / 1024); txk = int((tx - self._prev[1]) / dt / 1024)
            self._emit("eth.rxn", max(0, rxk))
            self._emit("eth.rx", "%d KB/s" % max(0, rxk)); self._emit("eth.tx", "%d KB/s" % max(0, txk))
        self._prev = (rx, tx); self._pt = now
        cl = _ubus("gl-clients", "list", {}).get("clients", {})
        self._emit("eth.clients", sum(1 for c in cl.values() if c.get("online")))


# ───────────────────────── Widgets (parameterized by bus keys) ─────────────────────────
class Widget:
    def __init__(self, app):
        self.app = app; self._inv = app.invalidate; self._subs = []
    def _sub(self, key, setter):                        # subscribe + auto-invalidate on change
        def cb(v): setter(v); self._inv()
        self._subs.append((key, cb)); self.app.subscribe(key, cb)
    def wire(self): pass
    def unwire(self):
        for key, cb in self._subs: self.app.unsubscribe(key, cb)
        self._subs = []
    def animate(self): return False
    def draw(self, d, th): pass

class Header(Widget):
    """title (left) + prefixed badge + live dot + right label."""
    def __init__(self, app, title, badge, right, flash, prefix=""):
        super().__init__(app)
        self.k_title, self.k_badge, self.k_right, self.k_flash, self.prefix = title, badge, right, flash, prefix
        self.title="—"; self.badge="?"; self.right="—"; self.bright=False
    def _bind(self, val, setter):                       # bus key (has '.') or literal text
        if isinstance(val, str) and "." in val: self._sub(val, setter)
        else: setter(val)
    def wire(self):
        self._bind(self.k_title, lambda v: setattr(self, "title", str(v)))
        self._bind(self.k_badge, lambda v: setattr(self, "badge", str(v)))
        self._bind(self.k_right, lambda v: setattr(self, "right", str(v)))
        self._sub(self.k_flash, lambda v: self._flash())
    def _flash(self):
        self.bright = True; threading.Timer(0.8, self._off).start()
    def _off(self): self.bright = False; self._inv()
    def draw(self, d, th):
        cf = th.bold[15]; d.text((10, 6), self.title, font=cf, fill=th.INK)
        bx = 10 + d.textlength(self.title, font=cf) + 8
        bl = self.prefix + self.badge; bf = th.mono[9]
        d.rounded_rectangle((bx, 8, bx + d.textlength(bl, font=bf) + 10, 22), radius=4, fill=th.BTN, outline=th.BTN_BD)
        d.text((bx+5, 9), bl, font=bf, fill=th.CYAN)
        hd = th.mono[11]; rw = d.textlength(self.right, font=hd)
        d.ellipse((W-12-rw-14, 12, W-12-rw-6, 20), fill=th.CYAN if self.bright else th.DOT_OFF)
        d.text((W-10-rw, 9), self.right, font=hd, fill=th.CYAN)
        d.line((10, 25, W-10, 25), fill=th.GRID, width=1)

class Banner(Widget):
    """plain title bar for pages with no live header data (e.g. Settings)."""
    def __init__(self, app, text): super().__init__(app); self.text = text
    def draw(self, d, th):
        d.text((12, 6), self.text, font=th.bold[15], fill=th.INK)
        d.line((10, 25, W-10, 25), fill=th.GRID, width=1)

class Gauge(Widget):
    """Base for the interchangeable page-hero styles (see GAUGE_STYLES).

       A style is not just a widget — it declares the slot it occupies, where the page's stack
       resumes below it (STACK_Y), and whether it already shows history. If it doesn't, the page
       adds a Trace to compensate. That contract is what lets MetricPage stay style-agnostic.

       The ctor is uniform across styles: each ignores the bindings it doesn't use (Hero ignores
       level, Arc ignores series), which is what keeps pages free of style-specific config."""
    TOP = 32; HEIGHT = 118; STACK_Y = 172
    SUPPLIES_HISTORY = False
    LABEL = "Gauge"                                     # shown in the settings stepper

    def __init__(self, app, value, level=None, series=None, unit="", series_label=None):
        super().__init__(app)
        self.k_value, self.k_level, self.k_series = value, level, series
        self.unit, self.series_label = unit, series_label
        self.value = None

class ArcGauge(Gauge):
    """270° gauge: value_key shown in center, level_key (0..5) drives the arc.
       Shows no history, so pages using it get a Trace."""
    TOP = 42; HEIGHT = 100; STACK_Y = 150               # arc spans y42..142 (cy=92, r=50)
    SUPPLIES_HISTORY = False
    LABEL = "Arc"

    def __init__(self, app, value, level=None, series=None, unit="", series_label=None,
                 cx=120, cy=92, r=50):
        super().__init__(app, value, level, series, unit, series_label)
        self.cx, self.cy, self.r = cx, cy, r
        self.target = 0.0; self.frac = 0.0
    def wire(self):
        self._sub(self.k_value, lambda v: setattr(self, "value", v))
        self._sub(self.k_level, lambda v: setattr(self, "target", max(0.0, min(1.0, v/5.0))))
    def animate(self):
        if abs(self.target - self.frac) > 0.004:
            self.frac += (self.target - self.frac) * 0.22; return True
        self.frac = self.target; return False
    def draw(self, d, th):
        cx, cy, r = self.cx, self.cy, self.r
        d.arc((cx-r, cy-r, cx+r, cy+r), 135, 135+270, fill=th.GRID, width=8)
        if self.frac > 0: d.arc((cx-r, cy-r, cx+r, cy+r), 135, 135+int(270*self.frac), fill=th.CYAN, width=8)
        ctext(d, cx, cy-22, str(self.value) if self.value is not None else "--", th.mono[32], th.INK)
        ctext(d, cx, cy+15, self.unit, th.mono[11], th.DIM)

class HeroGraph(Gauge):
    """Hero: big current value + hi/lo range (left) and a large auto-scaled area chart (right).
       Shows trend not just level, and gives the graph the real estate. Supplies its own history,
       so pages using it need no Trace."""
    TOP = 32; HEIGHT = 118; STACK_Y = 172
    SUPPLIES_HISTORY = True
    LABEL = "Hero"

    def __init__(self, app, value, level=None, series=None, unit="", series_label=None,
                 x=12, y=TOP, w=W-24, h=HEIGHT):
        super().__init__(app, value, level, series, unit, series_label)
        self.x, self.y, self.w, self.h = x, y, w, h
        self.hist = []
    def wire(self):
        self._sub(self.k_value, lambda v: setattr(self, "value", v))
        self._sub(self.k_series, self._push)
    def _push(self, v):
        if isinstance(v, (int, float)): self.hist = (self.hist + [v])[-96:]
    def draw(self, d, th):
        x, y, w, h = self.x, self.y, self.w, self.h
        d.text((x, y+12), str(self.value) if self.value is not None else "--", font=th.mono[32], fill=th.INK)
        d.text((x+2, y+50), self.unit, font=th.mono[11], fill=th.DIM)
        gx, gy = x+98, y; gw, gh = x+w-gx, h
        if len(self.hist) >= 2:
            lo, hi = min(self.hist), max(self.hist)
            if self.series_label:                        # the curve isn't the headline -> name it
                d.text((x, y+66), self.series_label, font=th.mono[9], fill=th.DIM)
            d.text((x, y+80),  "hi", font=th.mono[9], fill=th.DIM)
            # %g (not %d): series bound here range from sub-1 floats (sys.load) to signed ints
            # (signal.rsrp) to small positive ints (eth.rxn) — %d truncated floats to 0/1. %g only
            # goes scientific at >=7 significant digits, far above any series this binds today.
            d.text((x+22, y+80), "%g" % round(hi, 2), font=th.mono[11], fill=th.SUB)
            d.text((x, y+96),  "lo", font=th.mono[9], fill=th.DIM)
            d.text((x+22, y+96), "%g" % round(lo, 2), font=th.mono[11], fill=th.SUB)
            d.rectangle((gx, gy, gx+gw, gy+gh), outline=th.GRID)
            for frac in (0.25, 0.5, 0.75):                 # faint gridlines
                yy = gy + gh*frac; d.line((gx+1, yy, gx+gw-1, yy), fill=(18, 22, 30))
            span = (hi - lo) or 1
            f = lambda v: (v - lo) / span * 0.84 + 0.08
            pts = [(gx+2 + i*(gw-4)/(len(self.hist)-1), gy+gh-2 - f(v)*(gh-4)) for i, v in enumerate(self.hist)]
            d.polygon(pts + [(pts[-1][0], gy+gh-1), (pts[0][0], gy+gh-1)], fill=(6, 40, 46))
            d.line(pts, fill=th.CYAN, width=2)
            ex, ey = pts[-1]; d.ellipse((ex-3, ey-3, ex+3, ey+3), fill=th.INK)
        else:
            d.rectangle((gx, gy, gx+gw, gy+gh), outline=th.GRID)

# slug -> style class. Insertion order drives the settings stepper. A new style costs one class
# and one entry here — pages never change.
GAUGE_STYLES = {"hero": HeroGraph, "arc": ArcGauge}

class StatsRow(Widget):
    """two labelled values side by side: pairs = [(label, key), ...]."""
    def __init__(self, app, y, pairs):
        super().__init__(app); self.y = y; self.pairs = pairs; self.vals = {}
    def wire(self):
        for _, key in self.pairs:
            self._sub(key, (lambda k: (lambda v: self.vals.__setitem__(k, v)))(key))
    def draw(self, d, th):
        for lx, (label, key) in zip((44, 140), self.pairs):
            v = self.vals.get(key)
            d.text((lx, self.y), label, font=th.mono[11], fill=th.DIM)
            d.text((lx, self.y+13), str(v) if v is not None else "--", font=th.mono[15], fill=th.INK)

class InfoPanel(Widget):
    """titled panel: one big id field + up to 3 mini-cells. cells=[(label,key)]."""
    def __init__(self, app, x, y, title, big_label, big_key, cells):
        super().__init__(app)
        self.x, self.y, self.title = x, y, title
        self.big_label, self.big_key, self.cells = big_label, big_key, cells
        self.vals = {}
    def wire(self):
        for key in [self.big_key] + [k for _, k in self.cells]:
            self._sub(key, (lambda k: (lambda v: self.vals.__setitem__(k, v)))(key))
    def draw(self, d, th):
        x0, y0, x1, y1 = self.x, self.y, W-12, self.y+74
        d.rounded_rectangle((x0, y0, x1, y1), radius=8, outline=th.GRID, width=1, fill=th.PANEL)
        d.text((x0+8, y0+6), self.title, font=th.mono[9], fill=th.CYAN)
        d.text((x0+8, y0+24), self.big_label, font=th.mono[9], fill=th.DIM)
        bx = x0 + 8 + d.textlength(self.big_label, font=th.mono[9]) + 8
        d.text((bx, y0+22), str(self.vals.get(self.big_key, "—")), font=th.mono[15], fill=th.INK)
        cw = (x1-x0)/3
        for i, (label, key) in enumerate(self.cells):
            mx = x0 + i*cw + 8
            d.text((mx, y0+46), label, font=th.mono[9], fill=th.DIM)
            d.text((mx, y0+57), str(self.vals.get(key, "—")), font=th.mono[12], fill=th.INK)

class Trace(Widget):
    """auto-scaling history line for a numeric series (works for any signal)."""
    def __init__(self, app, series, x=12, y=266, w=W-24, h=14):
        super().__init__(app); self.k = series; self.x, self.y, self.w, self.h = x, y, w, h; self.hist = []
    def wire(self): self._sub(self.k, self._push)
    def _push(self, v):
        if isinstance(v, (int, float)): self.hist = (self.hist + [v])[-60:]
    def draw(self, d, th):
        x, y, w, h = self.x, self.y, self.w, self.h
        d.rectangle((x, y, x+w, y+h), outline=th.GRID)
        if len(self.hist) >= 2:
            lo, hi = min(self.hist), max(self.hist); span = (hi - lo) or 1
            f = lambda v: (v - lo) / span * 0.9 + 0.05
            pts = [(x+2 + i*(w-4)/(len(self.hist)-1), y+h-2 - f(v)*(h-4)) for i, v in enumerate(self.hist)]
            d.line(pts, fill=th.CYAN, width=1)

class Button(Widget):
    def __init__(self, app, rect, label, action):
        super().__init__(app); self.rect = rect; self.label = label; self.action = action
    def hit(self, x, y):
        x0, y0, x1, y1 = self.rect; return x0 <= x <= x1 and y0 <= y <= y1
    def draw(self, d, th):
        d.rounded_rectangle(self.rect, radius=11, fill=th.BTN, outline=th.BTN_BD, width=1)
        ctext(d, W/2, self.rect[1]+5, self.label, th.bold[12], th.CYAN)


# ───────────────────────── Settings rows (touch controls, uci-backed) ─────────────────────────
class Row(Widget):
    """one settings line. Reads app.settings on draw; writes + applies on tap.
       Rows implement on_touch(x,y)->bool (Page.on_touch calls it) instead of hit+action.

       self.y is a CONTENT coordinate. oy is the scroll offset the render thread subtracts to get
       a viewport coordinate — ScrollPage.draw sets it, and nothing else ever reads it (the touch
       thread does its own translation), so there's no race."""
    H = 28
    oy = 0
    def __init__(self, app, label, skey=None):
        super().__init__(app); self.label = label; self.skey = skey; self.y = 0
    def place(self, y): self.y = y; return self
    def ry(self): return self.y - self.oy               # content y -> viewport y
    def in_row(self, ry): return self.y <= ry < self.y + self.H
    def on_touch(self, x, y):
        if not self.in_row(y): return False
        self.act(x); return True
    def act(self, x): pass
    def draw_label(self, d, th):
        d.text((14, self.ry()+8), self.label, font=th.mono[11], fill=th.INK)
    def _commit(self, nv):
        self.app.settings.set(self.skey, str(nv)); self.app.apply_setting(self.skey, str(nv)); self._inv()

class ToggleRow(Row):
    def __init__(self, app, label, skey, confirm=False, msg_on=None, msg_off=None):
        super().__init__(app, label, skey)
        self.confirm = confirm; self.msg_on = msg_on; self.msg_off = msg_off
    def on(self): return str(self.app.settings.get(self.skey)) in ("1", "true", "on")
    def act(self, x):
        nv = "0" if self.on() else "1"
        if self.confirm:
            self.app.open_modal(Confirm(self.app, (self.msg_on if nv == "1" else self.msg_off) or "Apply?",
                                        lambda: self._commit(nv)))
        else:
            self._commit(nv)
    def draw(self, d, th):
        self.draw_label(d, th)
        y = self.ry()
        on = self.on(); w, h = 46, 18; x1 = W-14; x0 = x1-w; y0 = y+5
        d.rounded_rectangle((x0, y0, x1, y0+h), radius=9, fill=th.CYAN if on else th.BTN, outline=th.BTN_BD)
        kx = (x1-16) if on else (x0+2)
        d.ellipse((kx, y0+2, kx+14, y0+16), fill=th.BG if on else th.DIM)

class StepperRow(Row):
    """[-] value [+] over a discrete option list. wrap = cyclic picker; confirm = gated by modal."""
    def __init__(self, app, label, skey, options, fmt=None, wrap=False, confirm=False):
        super().__init__(app, label, skey)
        self.options = list(options); self.fmt = fmt or (lambda v: str(v))
        self.wrap = wrap; self.confirm = confirm
    def index(self):
        v = str(self.app.settings.get(self.skey))
        return self.options.index(v) if v in self.options else 0
    def act(self, x):
        i = self.index(); n = len(self.options)
        if x >= W-34: j = i+1
        elif x <= 136: j = i-1
        else: return
        j = (j % n) if self.wrap else max(0, min(n-1, j))
        if j == i: return
        nv = self.options[j]
        if self.confirm:
            self.app.open_modal(Confirm(self.app, "Set %s to %s?" % (self.label, self.fmt(nv)),
                                        lambda: self._commit(nv)))
        else:
            self._commit(nv)
    def draw(self, d, th):
        self.draw_label(d, th)
        y = self.ry(); y0 = y+5
        d.rounded_rectangle((116, y0, 136, y0+18), radius=4, fill=th.BTN, outline=th.BTN_BD)
        ctext(d, 126, y0+3, "-", th.bold[13], th.CYAN)
        d.rounded_rectangle((W-34, y0, W-14, y0+18), radius=4, fill=th.BTN, outline=th.BTN_BD)
        ctext(d, W-24, y0+3, "+", th.bold[13], th.CYAN)
        ctext(d, (138 + W-38)/2, y+8, self.fmt(self.options[self.index()]), th.mono[12], th.INK)

class SliderRow(Row):
    """tap-to-set slider over [lo, hi]. Applies live (brightness)."""
    def __init__(self, app, label, skey, lo, hi):
        super().__init__(app, label, skey); self.lo = lo; self.hi = hi
        self.x0 = 112; self.x1 = W-42
    def cur(self):
        try: return max(self.lo, min(self.hi, int(float(self.app.settings.get(self.skey)))))
        except Exception: return self.lo
    def act(self, x):
        frac = max(0.0, min(1.0, (x - self.x0) / (self.x1 - self.x0)))
        self._commit(int(round(self.lo + frac * (self.hi - self.lo))))
    def draw(self, d, th):
        self.draw_label(d, th)
        y = self.ry()
        v = self.cur(); yc = y+14
        d.line((self.x0, yc, self.x1, yc), fill=th.GRID, width=2)
        kx = self.x0 + (v - self.lo) / (self.hi - self.lo) * (self.x1 - self.x0)
        d.line((self.x0, yc, kx, yc), fill=th.CYAN, width=2)
        d.ellipse((kx-4, yc-4, kx+4, yc+4), fill=th.INK)
        d.text((W-36, y+8), str(v), font=th.mono[11], fill=th.SUB)

class ActionRow(Row):
    def __init__(self, app, label, action):
        super().__init__(app, label); self.action = action
    def act(self, x): self.action()
    def draw(self, d, th):
        self.draw_label(d, th)
        d.text((W-22, self.ry()+6), "›", font=th.bold[15], fill=th.CYAN)


# ───────────────────────── Modal overlays ─────────────────────────
class Confirm:
    """message + Cancel / OK. Captures all touches; closes itself on either button."""
    def __init__(self, app, msg, on_ok):
        self.app = app; self.msg = msg; self.on_ok = on_ok
        self.cancel = (30, 190, W/2-4, 220); self.ok = (W/2+4, 190, W-30, 220)
    def _in(self, r, x, y): return r[0] <= x <= r[2] and r[1] <= y <= r[3]
    def on_touch(self, x, y):
        if self._in(self.ok, x, y): self.app.close_modal(); self.on_ok()
        elif self._in(self.cancel, x, y): self.app.close_modal()
        return True                                       # modal swallows every touch
    def draw(self, d, th):
        d.rectangle((0, 0, W-1, H-1), fill=th.BG)
        d.rounded_rectangle((20, 96, W-20, 236), radius=10, fill=th.PANEL, outline=th.BTN_BD)
        yy = 118
        for ln in textwrap.wrap(self.msg, 24):
            ctext(d, W/2, yy, ln, th.mono[12], th.INK); yy += 18
        d.rounded_rectangle(self.cancel, radius=8, fill=th.BTN, outline=th.BTN_BD)
        ctext(d, (self.cancel[0]+self.cancel[2])/2, self.cancel[1]+8, "Cancel", th.bold[12], th.DIM)
        d.rounded_rectangle(self.ok, radius=8, fill=th.BTN, outline=th.CYAN)
        ctext(d, (self.ok[0]+self.ok[2])/2, self.ok[1]+8, "OK", th.bold[12], th.CYAN)

class About:
    """read-only info panel with a single Close button."""
    def __init__(self, app):
        self.app = app; self.close = (W/2-40, 236, W/2+40, 266)
        model = _read("/tmp/sysinfo/model") or _read("/proc/gl-hw-info/model") or "GL-E5800"
        fw = _read("/etc/glversion") or "—"
        ip = "—"
        try:
            st = _ubus("network.interface.lan", "status", {})
            ip = next((a.get("address") for a in st.get("ipv4-address", [])), "—")
        except Exception:
            pass
        self.lines = [("Model", model), ("Firmware", fw), ("MudiUI", "v" + MUDI_VERSION), ("LAN", ip)]
    def on_touch(self, x, y):
        if self.close[0] <= x <= self.close[2] and self.close[1] <= y <= self.close[3]:
            self.app.close_modal()
        return True
    def draw(self, d, th):
        d.rectangle((0, 0, W-1, H-1), fill=th.BG)
        d.rounded_rectangle((20, 70, W-20, 280), radius=10, fill=th.PANEL, outline=th.BTN_BD)
        ctext(d, W/2, 84, "About", th.bold[15], th.CYAN)
        yy = 118
        for label, val in self.lines:
            d.text((40, yy), label, font=th.mono[10], fill=th.DIM)
            d.text((110, yy), str(val), font=th.mono[11], fill=th.INK); yy += 26
        d.rounded_rectangle(self.close, radius=8, fill=th.BTN, outline=th.CYAN)
        ctext(d, W/2, self.close[1]+8, "Close", th.bold[12], th.CYAN)


# ───────────────────────── Pages ─────────────────────────
class Page:
    title = "Page"
    def __init__(self, app):
        self.app = app; self.widgets = []; self.build()
    def add(self, w): self.widgets.append(w); return w
    def build(self): pass
    def wire(self):   [w.wire() for w in self.widgets]
    def unwire(self): [w.unwire() for w in self.widgets]
    def animate(self): return any([w.animate() for w in self.widgets])
    def draw(self, d, th, img=None): [w.draw(d, th) for w in self.widgets]
    def on_touch(self, x, y):
        for w in self.widgets:
            if hasattr(w, "on_touch") and w.on_touch(x, y): return True     # settings rows
            if hasattr(w, "hit") and w.hit(x, y) and getattr(w, "action", None):
                w.action(); return True                                     # buttons
        return False

class ScrollPage(Page):
    """A page whose rows scroll under fixed chrome.

       self.widgets = fixed chrome (the Banner). self.rows = scrolling content, laid out in
       CONTENT coordinates from y=0. PIL has no clip region, so drawing rows straight to the frame
       would let a half-scrolled row paint over the banner; instead rows render into a viewport
       sub-image that clips them exactly, and it gets pasted below the chrome."""
    VIEW_TOP = 30
    VIEW_H = H - VIEW_TOP
    BAR_W = 3

    def __init__(self, app):
        self.rows = []; self.scroll_y = 0; self.content_h = 0
        super().__init__(app)                            # Page.__init__ calls build()

    def add_row(self, r): self.rows.append(r); return r
    def max_scroll(self): return max(0, self.content_h - self.VIEW_H)
    def scrollable(self): return self.max_scroll() > 0

    def scroll_to(self, v):
        nv = max(0, min(self.max_scroll(), v))
        if nv != self.scroll_y:
            self.scroll_y = nv; self.app.invalidate()

    def wire(self):   [w.wire() for w in self.widgets + self.rows]
    def unwire(self): [w.unwire() for w in self.widgets + self.rows]
    def animate(self): return any([w.animate() for w in self.widgets + self.rows])

    def draw(self, d, th, img):
        sy = self.scroll_y                                # snapshot once: touch thread can mutate
                                                            # scroll_y mid-frame otherwise, tearing rows
        vp = Image.new("RGB", (W, self.VIEW_H), th.BG)   # clips rows to the viewport, exactly
        vd = ImageDraw.Draw(vp)
        for r in self.rows:
            r.oy = sy                                     # render thread only -> no race
            r.draw(vd, th)
        img.paste(vp, (0, self.VIEW_TOP))
        for w in self.widgets: w.draw(d, th)              # chrome drawn AFTER the paste, which would
                                                            # otherwise clobber anything below VIEW_TOP
        if self.scrollable(): self._bar(d, th, sy)

    def _bar(self, d, th, sy):
        h = max(12, self.VIEW_H * self.VIEW_H / self.content_h)
        y = self.VIEW_TOP + sy / self.content_h * self.VIEW_H
        d.rectangle((W-self.BAR_W, y, W-1, y+h), fill=th.CYAN)

    def on_touch(self, x, y):
        for w in self.widgets:
            if hasattr(w, "hit") and w.hit(x, y) and getattr(w, "action", None):
                w.action(); return True
        if y < self.VIEW_TOP: return False
        cy = y - self.VIEW_TOP + self.scroll_y           # screen -> content
        for r in self.rows:
            if r.on_touch(x, cy): return True
        return False

class MetricPage(Page):
    """A metric page declares BINDINGS ONLY — the selected Gauge style decides the geometry.

       The style says where the stack resumes below it (STACK_Y) and whether it already shows
       history; if it doesn't, we add a Trace. That's why switching styles needs no page edits."""
    HEADER = {}; GAUGE = {}; STATS = []; PANEL = None
    STATS_TO_PANEL = 36; PANEL_H = 74; PANEL_TO_TRACE = 10; TRACE_H = 36

    def build(self):
        a = self.app
        self.add(Header(a, **self.HEADER))
        g = self.add(a.gauge_cls()(a, **self.GAUGE))
        self.add(StatsRow(a, g.STACK_Y, self.STATS))
        py = g.STACK_Y + self.STATS_TO_PANEL
        self.add(InfoPanel(a, 12, py, *self.PANEL))
        if not g.SUPPLIES_HISTORY:                      # arc shows level, not trend -> add a trace
            self.add(Trace(a, self.GAUGE["series"],
                           y=py + self.PANEL_H + self.PANEL_TO_TRACE, h=self.TRACE_H))

class SignalPage(MetricPage):
    title  = "Signal"
    HEADER = dict(title="sim.carrier", badge="sim.slot", right="net.mode",
                  flash="signal.rsrp", prefix="SIM")
    GAUGE  = dict(value="signal.rsrp", level="signal.level",
                  series="signal.rsrp", unit="dBm  RSRP")
    STATS  = [("RSRQ", "signal.rsrq"), ("SINR", "signal.sinr")]
    PANEL  = ("SERVING CELL", "CELL", "cell.id",
              [("BAND", "cell.band"), ("FREQ", "cell.freq"), ("BW", "cell.bw")])

class WifiPage(MetricPage):
    title  = "WiFi"
    HEADER = dict(title="wifi.ssid", badge="wifi.mode", right="wifi.band",
                  flash="wifi.signal", prefix="")
    GAUGE  = dict(value="wifi.signal", level="wifi.level",
                  series="wifi.signal", unit="dBm  LINK")
    STATS  = [("RATE", "wifi.rate"), ("CHAN", "wifi.chan")]
    PANEL  = ("ACCESS POINT", "SSID", "wifi.ap",
              [("CLIENTS", "wifi.clients"), ("CH", "wifi.chan"), ("WIDTH", "wifi.width")])

class SystemPage(MetricPage):
    title  = "System"
    HEADER = dict(title="System", badge="batt.state", right="sys.uptime",
                  flash="sys.load", prefix="")
    # headlines the battery but graphs load -> the curve is labelled so the hero reads honestly
    GAUGE  = dict(value="batt.pct", level="batt.level",
                  series="sys.load", unit="%  BATTERY", series_label="LOAD")
    STATS  = [("CPU", "sys.cputemp"), ("LOAD", "sys.load")]
    PANEL  = ("RESOURCES", "RAM", "sys.ram",
              [("FREE", "sys.free"), ("BATT", "batt.temp"), ("UP", "sys.uptime")])

class EthernetPage(MetricPage):
    title  = "Ethernet"
    HEADER = dict(title="Ethernet", badge="eth.port", right="eth.link",
                  flash="eth.rxn", prefix="")
    GAUGE  = dict(value="eth.speed", level="eth.level",
                  series="eth.rxn", unit="LINK", series_label="RX")
    STATS  = [("RX", "eth.rx"), ("TX", "eth.tx")]
    PANEL  = ("LAN", "IP", "eth.ip",
              [("PORT", "eth.port"), ("CLIENTS", "eth.clients"), ("PROTO", "eth.proto")])

class SettingsPage(ScrollPage):
    title = "Settings"
    PAGE_NAMES = ("Signal", "WiFi", "System", "Eth")
    TIMEOUTS = {"0": "Off", "15": "15s", "30": "30s", "60": "1m", "300": "5m"}
    def build(self):
        a = self.app
        self.add(Banner(a, "Settings"))
        rows = [
            SliderRow(a, "Brightness", "brightness", 20, 120),
            StepperRow(a, "Screen timeout", "screen_timeout", ["0", "15", "30", "60", "300"],
                       fmt=lambda v: self.TIMEOUTS.get(v, v)),
            ToggleRow(a, "Awake on charge", "stay_awake_charging"),
            StepperRow(a, "Graph style", "graph_style", list(GAUGE_STYLES),
                       fmt=lambda v: GAUGE_STYLES[v].LABEL, wrap=True),
            StepperRow(a, "Default page", "default_page", ["0", "1", "2", "3"],
                       fmt=lambda v: self.PAGE_NAMES[int(v)], wrap=True),
            ToggleRow(a, "Lock band n71", "band_lock", confirm=True,
                      msg_on="Lock modem to band n71?", msg_off="Restore all NR bands?"),
            StepperRow(a, "Network mode", "net_mode", ["auto", "5g", "lte"],
                       fmt=lambda v: {"auto": "Auto", "5g": "5G", "lte": "LTE"}[v],
                       wrap=True, confirm=True),
            StepperRow(a, "Long-press", "longpress", ["1.0", "1.3", "1.6", "2.0"],
                       fmt=lambda v: v + "s"),
            ActionRow(a, "Return to stock UI", a.request_toggle),
            ToggleRow(a, "Start on boot", "start_on_boot"),
            ActionRow(a, "About", lambda: a.open_modal(About(a))),
        ]
        y = 0                                            # content coords; ScrollPage offsets them
        for r in rows:
            self.add_row(r.place(y)); y += Row.H
        self.content_h = y


class Gesture:
    """Pure gesture classifier for the touch loop — no I/O, so it's testable.

       Scroll needs |dy| > |dx| and swipe needs |dx| > |dy|, so the two can never both fire.
       Once a drag latches as a scroll it stays one for the rest of the gesture, which stops a
       curving finger from firing a page change mid-scroll."""
    TOL = 8                                              # px before a drag counts as a scroll
    SWIPE = 50                                           # px before a drag counts as a page swipe

    def __init__(self):
        self.x0 = self.y0 = 0; self.scroll0 = 0
        self.scrollable = False; self.scrolling = False

    def down(self, x, y, scroll0=0, scrollable=False):
        self.x0, self.y0 = x, y; self.scroll0 = scroll0
        self.scrollable = scrollable; self.scrolling = False

    def move(self, x, y):
        """-> the new scroll_y while dragging a scrollable page, else None."""
        if not self.scrollable: return None
        dy = y - self.y0
        if not self.scrolling and abs(dy) > self.TOL and abs(dy) > abs(x - self.x0):
            self.scrolling = True
        return (self.scroll0 - dy) if self.scrolling else None

    def up(self, x, y):
        """-> ('scroll', None) | ('swipe', +1 next / -1 prev) | ('tap', (x, y))."""
        if self.scrolling:
            self.scrolling = False
            return ("scroll", None)
        dx = x - self.x0; dy = y - self.y0
        if abs(dx) > self.SWIPE and abs(dx) > abs(dy):
            return ("swipe", 1 if dx < 0 else -1)
        return ("tap", (x, y))


# ───────────────────────── App ─────────────────────────
class App:
    def __init__(self, sources):
        self.wake = threading.Event(); self.stop = threading.Event()
        self.sources = sources
        self.registry = {k: s for s in sources for k in s.provides}
        self.pages = []; self.idx = 0; self.current = None; self.theme = Theme
        self.service = False                              # True when launched by the procd service
        self.paused = False                               # True while gl_screen owns the panel
        self._toggle_req = threading.Event()
        self.settings = Settings()
        self.modal = None                                 # Confirm / About overlay, or None
        self.blanked = False; self._wokeup = False; self.last_touch = time.time()

    def invalidate(self): self.wake.set()

    # ---- modal overlay ----
    def open_modal(self, m): self.modal = m; self.wake.set()
    def close_modal(self): self.modal = None; self.wake.set()
    def request_toggle(self): self._toggle_req.set()      # menu path to the stock-UI toggle

    # ---- settings effects ----
    def gauge_cls(self):                                 # selected hero style; junk value -> Hero
        return GAUGE_STYLES.get(self.settings.get("graph_style"), HeroGraph)

    def _rebuild_metric_pages(self):
        """Re-build only the pages whose layout depends on the gauge style.

           SettingsPage is deliberately spared: you change the style FROM it, so rebuilding it
           would destroy the scroll position and the row under the user's finger. self.current is
           never set to None, so the render thread always sees a fully-built page — either the old
           one or the new one."""
        for i, p in enumerate(self.pages):
            if isinstance(p, MetricPage):
                live = (p is self.current)
                if live: p.unwire()                      # drop subscribers before adding new ones
                self.pages[i] = type(p)(self)
                if live:
                    self.current = self.pages[i]; self.current.wire()
        self.wake.set()

    def _brightness(self):
        try: return int(float(self.settings.get("brightness")))
        except Exception: return 90
    def _set_brightness(self, v):
        try:
            with open(BL_DIR + "/brightness", "w") as f: f.write(str(int(v)))
        except Exception: pass
    def _active_slot(self):
        try:
            m = _ubus("cellular.modem", "status", {"bus": BUS})
            s = m.get("current_sim_slot")
            if s is None and "modems" in m: s = m["modems"][0].get("current_sim_slot")
            return int(s or 1)
        except Exception:
            return 1
    def _apply_modem(self, skey, val):
        try:
            slot = self._active_slot()
            if skey == "band_lock":
                if val == "1":
                    _at('AT+QNWPREFCFG="nr5g_band",71', slot); _at('AT+QNWPREFCFG="nsa_nr5g_band",71', slot)
                else:
                    _at('AT+QNWPREFCFG="nr5g_band",%s' % NR5G_BANDS_ALL, slot)
                    _at('AT+QNWPREFCFG="nsa_nr5g_band",%s' % NSA_BANDS_ALL, slot)
            elif skey == "net_mode":
                _at('AT+QNWPREFCFG="mode_pref",%s' % MODE_AT.get(val, "AUTO"), slot)
        except Exception as e:
            print("modem apply", skey, "failed:", e)
    def apply_setting(self, skey, val):
        if skey in ("band_lock", "net_mode"):             # AT writes are slow -> off the touch thread
            threading.Thread(target=self._apply_modem, args=(skey, val), daemon=True).start(); return
        try:
            if skey == "brightness":
                if not self.blanked: self._set_brightness(int(val))
            elif skey == "start_on_boot":
                cmd = "enable" if val == "1" else "disable"
                for svc in ("mudi", "mudi-watch"): subprocess.Popen(["/etc/init.d/" + svc, cmd])
            elif skey == "graph_style":
                self._rebuild_metric_pages()
            # screen_timeout / stay_awake_charging / default_page / longpress: read where used
        except Exception as e:
            print("apply", skey, "failed:", e)

    # ---- idle blank ----
    def _timeout_secs(self):
        try: return int(self.settings.get("screen_timeout") or 0)
        except Exception: return 0
    def _stay_awake_active(self):
        if str(self.settings.get("stay_awake_charging")) != "1": return False
        try: return bool(_ubus("mcu", "status", {}).get("charging_status", 0))
        except Exception: return False
    def _blank(self):
        self.blanked = True
        for node, v in ((BL_DIR + "/bl_power", "1"), (BL_DIR + "/brightness", "0")):
            try:
                with open(node, "w") as f: f.write(v)
            except Exception: pass
    def _unblank(self):
        for node, v in ((BL_DIR + "/brightness", str(self._brightness())), (BL_DIR + "/bl_power", "0")):
            try:
                with open(node, "w") as f: f.write(v)
            except Exception: pass
        self.blanked = False; self.wake.set()

    # ---- panel takeover / handoff (gl_screen) ----
    def _take_panel(self):                               # take the framebuffer with NO overlap
        # gl_screen takes ~4s to exit on `stop`, and it keeps drawing the whole time -> both UIs
        # fight. So freeze its drawing INSTANTLY (safe here: we're killing it, not resuming), then
        # terminate it in the background while we draw.
        subprocess.run("kill -STOP $(pidof gl_screen) 2>/dev/null", shell=True)
        subprocess.run("echo 0 > /sys/class/graphics/fb0/blank", shell=True, capture_output=True)
        subprocess.Popen("/etc/init.d/gl_screen stop", shell=True)

    def _notice(self, text):                             # full-screen message drawn while we still own fb
        img = Image.new("RGB", (W, H), Theme.BG); d = ImageDraw.Draw(img)
        ctext(d, W/2, H/2-18, text, Theme.bold[15], Theme.CYAN)
        ctext(d, W/2, H/2+6, "one moment", Theme.mono[11], Theme.DIM)
        try:
            with open("/dev/fb0", "r+b", buffering=0) as f: f.seek(0); f.write(pack565(img))
        except Exception: pass

    def _release_panel(self):                            # show notice, then cold-start gl_screen over it
        self._notice("Stock UI")
        subprocess.Popen("/etc/init.d/gl_screen start", shell=True)

    def _flash(self, ms=110):                            # brief full-screen flash = "touch accepted"
        try:
            with open("/dev/fb0", "r+b", buffering=0) as f: f.seek(0); f.write(_FLASH)
        except Exception: pass
        time.sleep(ms / 1000.0)

    def _do_toggle(self):
        self._flash()
        self.paused = not self.paused
        if self.paused: self._release_panel()            # -> gl_screen (notice + cold start; it works)
        else:                                            # -> MudiUI (instant, resident)
            if self.blanked: self._unblank()
            self._take_panel(); self._set_brightness(self._brightness()); self.wake.set()

    def subscribe(self, key, cb):
        s = self.registry.get(key)
        if s: s.subscribe(key, cb)                       # unknown key -> no-op (shows placeholder)
    def unsubscribe(self, key, cb):
        s = self.registry.get(key)
        if s: s.unsubscribe(key, cb)
    def request_stock(self):
        # Hand the panel back to gl_screen. Under procd (respawn) we must STOP the service,
        # or it would restart us and re-grab the panel; procd's SIGTERM then runs our
        # finally: -> gl_screen start. Standalone: just exit the loop.
        if self.service:
            subprocess.Popen(["/etc/init.d/mudi", "stop"])
        else:
            self.stop.set()

    def show(self, i):
        if self.current: self.current.unwire()
        self.idx = i % len(self.pages)
        self.current = self.pages[self.idx]; self.current.wire(); self.wake.set()

    def _touch(self):
        try:
            from evdev import InputDevice, ecodes
            dev = InputDevice("/dev/input/event0"); x = y = 0; down = False; p = None
            g = Gesture()
            for e in dev.read_loop():
                if self.stop.is_set(): return
                if e.type == ecodes.EV_ABS:
                    if e.code in (ecodes.ABS_X, ecodes.ABS_MT_POSITION_X): x = e.value
                    elif e.code in (ecodes.ABS_Y, ecodes.ABS_MT_POSITION_Y): y = e.value
                    if down:
                        sv = g.move(x, y)
                        if sv is not None: p.scroll_to(sv)              # live follow (page at touch-down)
                elif e.type == ecodes.EV_KEY and e.code == ecodes.BTN_TOUCH:
                    if e.value == 1:
                        down = True; self.last_touch = time.time()
                        p = self.current
                        scrollable = (isinstance(p, ScrollPage) and p.scrollable()
                                      and self.modal is None and not self.paused
                                      and not self.blanked)
                        g.down(x, y, scroll0=(p.scroll_y if scrollable else 0),
                               scrollable=scrollable)
                        if self.blanked: self._unblank(); self._wokeup = True   # wake on touch-down
                    elif e.value == 0 and down:
                        down = False; self.last_touch = time.time()
                        kind, arg = g.up(x, y)
                        if self._wokeup: self._wokeup = False; continue        # waking touch: swallow
                        if self.paused: continue                               # gl_screen owns the UI
                        if self.modal is not None:
                            self.modal.on_touch(x, y); self.wake.set(); continue
                        if kind == "scroll": continue                          # drag, not a tap
                        if kind == "swipe": self.show(self.idx + arg)
                        else: self.current.on_touch(*arg)
        except Exception as e:
            print("touch disabled:", e)

    def _dots(self, d, th):
        n = len(self.pages)
        if n < 2: return
        gap = 10; total = (n-1)*gap; x = W/2 - total/2
        for i in range(n):
            c = th.CYAN if i == self.idx else th.GRID
            d.ellipse((x-2, 1, x+2, 5), fill=c); x += gap

    def run(self, pages, start=0, duration=None):
        self.pages = pages
        self._take_panel()                                # freeze gl_screen, take panel
        self._set_brightness(self._brightness())          # apply configured brightness
        for s in (signal.SIGINT, signal.SIGTERM):
            signal.signal(s, lambda *_: self.stop.set())
        signal.signal(signal.SIGUSR1, lambda *_: self._toggle_req.set())   # long-press toggle
        threading.Thread(target=self._touch, daemon=True).start()
        self.show(start)
        th = self.theme; prev_anim = False; first = True; t0 = time.time()
        try:
            with open("/dev/fb0", "r+b", buffering=0) as fb:
                while not self.stop.is_set():
                    if self._toggle_req.is_set():
                        self._toggle_req.clear(); self._do_toggle(); first = True
                    if self.paused:                        # gl_screen owns the panel; sit idle
                        self.wake.wait(0.2)
                        if duration and time.time()-t0 > duration: break
                        continue
                    if self.blanked:                       # backlight off; wait for a touch to wake
                        self.wake.wait(0.3)
                        if duration and time.time()-t0 > duration: break
                        continue
                    to = self._timeout_secs()              # idle-blank
                    if to > 0 and self.modal is None and time.time()-self.last_touch > to:
                        if self._stay_awake_active(): self.last_touch = time.time()
                        else: self._blank(); continue
                    anim = self.current.animate()
                    if first or self.wake.is_set() or anim or prev_anim:
                        self.wake.clear()
                        img = Image.new("RGB", (W, H), th.BG); d = ImageDraw.Draw(img)
                        self.current.draw(d, th, img); self._dots(d, th)
                        if self.modal is not None: self.modal.draw(d, th)
                        fb.seek(0); fb.write(pack565(img)); first = False
                    prev_anim = anim
                    if duration and time.time()-t0 > duration: break
                    if anim: self.stop.wait(1/30.0)
                    else: self.wake.wait(0.2)
        finally:
            self.stop.set()
            subprocess.run("/etc/init.d/gl_screen start", shell=True, capture_output=True)
            print("gl_screen restored")


# ───────────────────────── entry ─────────────────────────
MOCK_DATA = {"sim.carrier":"T-Mobile","sim.slot":"1","net.mode":"NR5G-SA",
             "signal.rsrp":-100,"signal.rsrq":"-13 dB","signal.sinr":"1 dB","signal.level":4,
             "cell.id":"187461017","cell.band":"n25","cell.freq":"1981 MHz","cell.bw":"20 MHz",
             "wifi.ssid":"Sun","wifi.mode":"RPT","wifi.band":"5 GHz","wifi.signal":-38,
             "wifi.level":5,"wifi.rate":"780 M","wifi.chan":149,"wifi.freq":"5745 MHz",
             "wifi.width":"VHT80","wifi.ap":"Travel2G","wifi.clients":0,
             "batt.pct":100,"batt.level":5,"batt.state":"FULL","batt.temp":"30.2°C",
             "sys.cputemp":"36°C","sys.load":1.39,"sys.ram":"36%","sys.free":"1019 MB",
             "sys.uptime":"5d 1h",
             "eth.speed":"DOWN","eth.level":0,"eth.link":"DOWN","eth.port":"eth0",
             "eth.ip":"192.168.8.1","eth.rx":"0 KB/s","eth.tx":"0 KB/s","eth.rxn":0,
             "eth.clients":0,"eth.proto":"static"}


class MockApp(App):
    """Off-device App double: serves MOCK_DATA, owns no framebuffer, sources or threads.
       Used by --mock previews and by tests. apply_setting is a no-op so a preview can't fire
       AT commands at a modem that isn't there."""
    def __init__(self, data=None):
        super().__init__([])                             # tolerates an empty source list, no threads
        self.data = MOCK_DATA if data is None else data
    def subscribe(self, key, cb):
        if key in self.data: cb(self.data[key])
    def unsubscribe(self, *a): pass
    def request_stock(self): pass                         # would spawn a subprocess on the real App
    def request_toggle(self): pass
    def apply_setting(self, *a): pass                      # real one fires AT commands at absent hardware


def _mock(which, style="hero", outdir="/tmp"):
    a = MockApp()
    a.settings.vals["graph_style"] = style
    page = {"wifi": WifiPage, "system": SystemPage, "eth": EthernetPage,
            "settings": SettingsPage}.get(which, SignalPage)(a)
    a.pages = [page]; page.wire()
    for _ in range(40): page.animate()
    import math                                          # synthetic history so graphs render
    for wdg in page.widgets:
        if hasattr(wdg, "hist"):                         # duck-typed: any new style seeds too
            # Seed from the DECLARED SERIES, not the headline value — SystemPage/EthernetPage
            # headline a different key than they graph (see MetricPage docstring), so seeding
            # from wdg.value would preview e.g. battery-scale noise under a LOAD label.
            key = getattr(wdg, "k_series", None) or getattr(wdg, "k", None)
            b = MOCK_DATA.get(key) if key else None
            if not isinstance(b, (int, float)): b = -100
            amp = max(abs(b) * 0.07, 0.05)               # scale noise to the series' own magnitude
            wdg.hist = [b + amp*math.sin(i*0.35) + amp*0.4*math.sin(i*0.85) for i in range(64)]
            if isinstance(b, int):                       # preserve numeric type of the source
                wdg.hist = [int(round(x)) for x in wdg.hist]
    img = Image.new("RGB", (W, H), Theme.BG); d = ImageDraw.Draw(img)
    page.draw(d, Theme, img)                             # img: ScrollPage composites onto it
    out = "%s/mudi_%s_%s.png" % (outdir, which, style); img.save(out); print("wrote", out)
    return page

def main():
    if "--mock" in sys.argv:
        which = next((w for w in ("wifi", "system", "eth", "settings") if w in sys.argv), "signal")
        style = next((s for s in GAUGE_STYLES if s in sys.argv), "hero")
        _mock(which, style); return
    dur = next((float(a) for a in sys.argv[1:] if a.replace(".", "").isdigit()), None)
    app = App([CellularSource(), WifiSource(), SystemSource(), EthernetSource()])
    app.service = "--service" in sys.argv
    try: start = int(app.settings.get("default_page"))
    except Exception: start = 0
    app.run([SignalPage(app), WifiPage(app), SystemPage(app), EthernetPage(app), SettingsPage(app)],
            start=start, duration=dur)

if __name__ == "__main__":
    main()
