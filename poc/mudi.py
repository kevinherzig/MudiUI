#!/usr/bin/env python3
"""MudiUI — event-driven instrument framework for the GL-E5800 front panel.

Object model (drives consistency across many pages):

    Theme        one palette + font set; every widget draws through it.
    DataSource   a self-gating subject: owns a subscriber list, POLLS ONLY while
                 count > 0, notifies subscribers on change (deduped). App-owned.
    Widget       PARAMETERIZED by bus keys — the same ArcGauge shows cellular signal
                 on SignalPage and WiFi link on WifiPage. Redraws reactively.
    Page         bundles configured widgets (its UI). SignalPage / WifiPage.
    App          owns sources + key->source registry + display + touch + swipe nav.

Reused widgets prove the consistency thesis: WifiPage adds NO new widget classes.
The key=... bindings below are exactly what the future JSON layer will declare.

Run on the Mudi:  python3 mudi.py [seconds]   (no arg = until STOCK UI tapped)
Preview a frame:  python3 mudi.py --mock [signal|wifi]
"""
import sys, os, time, json, subprocess, threading, signal, re
import numpy as np
from PIL import Image, ImageDraw, ImageFont

W, H = 240, 320
BUS = "cpu"
STOCK_BTN = (12, H-34, W-12, H-10)
_MISS = object()


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
    mono = _load(_MONO, (9,11,12,15,32))

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

def _san(s):                                            # SSIDs can carry emoji the panel font lacks
    t = "".join(ch for ch in (s or "") if 32 <= ord(ch) < 127).strip()
    return t or "—"


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


def _read(path):
    try:
        with open(path) as f: return f.read().strip()
    except Exception: return None


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

class ArcGauge(Widget):
    """270° gauge: value_key shown in center, level_key (0..5) drives the arc."""
    def __init__(self, app, value, level, unit, cx=120, cy=92, r=50):
        super().__init__(app)
        self.k_value, self.k_level, self.unit = value, level, unit
        self.cx, self.cy, self.r = cx, cy, r
        self.value=None; self.target=0.0; self.frac=0.0
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
    def draw(self, d, th): [w.draw(d, th) for w in self.widgets]
    def on_touch(self, x, y):
        for w in self.widgets:
            if hasattr(w, "hit") and w.hit(x, y) and getattr(w, "action", None):
                w.action(); return True
        return False

class SignalPage(Page):
    title = "Signal"
    def build(self):
        a = self.app
        self.add(Header(a, title="sim.carrier", badge="sim.slot", right="net.mode",
                        flash="signal.rsrp", prefix="SIM"))
        self.add(ArcGauge(a, value="signal.rsrp", level="signal.level", unit="dBm  RSRP"))
        self.add(StatsRow(a, 150, [("RSRQ", "signal.rsrq"), ("SINR", "signal.sinr")]))
        self.add(InfoPanel(a, 12, 184, "SERVING CELL", "CELL", "cell.id",
                           [("BAND", "cell.band"), ("FREQ", "cell.freq"), ("BW", "cell.bw")]))
        self.add(Trace(a, "signal.rsrp"))
        self.add(Button(a, STOCK_BTN, "▶  STOCK UI", a.request_stock))

class WifiPage(Page):
    title = "WiFi"
    def build(self):
        a = self.app
        self.add(Header(a, title="wifi.ssid", badge="wifi.mode", right="wifi.band",
                        flash="wifi.signal", prefix=""))
        self.add(ArcGauge(a, value="wifi.signal", level="wifi.level", unit="dBm  LINK"))
        self.add(StatsRow(a, 150, [("RATE", "wifi.rate"), ("CHAN", "wifi.chan")]))
        self.add(InfoPanel(a, 12, 184, "ACCESS POINT", "SSID", "wifi.ap",
                           [("CLIENTS", "wifi.clients"), ("CH", "wifi.chan"), ("WIDTH", "wifi.width")]))
        self.add(Trace(a, "wifi.signal"))
        self.add(Button(a, STOCK_BTN, "▶  STOCK UI", a.request_stock))

class SystemPage(Page):
    title = "System"
    def build(self):
        a = self.app
        self.add(Header(a, title="System", badge="batt.state", right="sys.uptime",
                        flash="sys.load", prefix=""))
        self.add(ArcGauge(a, value="batt.pct", level="batt.level", unit="%  BATTERY"))
        self.add(StatsRow(a, 150, [("CPU", "sys.cputemp"), ("LOAD", "sys.load")]))
        self.add(InfoPanel(a, 12, 184, "RESOURCES", "RAM", "sys.ram",
                           [("FREE", "sys.free"), ("BATT", "batt.temp"), ("UP", "sys.uptime")]))
        self.add(Trace(a, "sys.load"))
        self.add(Button(a, STOCK_BTN, "▶  STOCK UI", a.request_stock))

class EthernetPage(Page):
    title = "Ethernet"
    def build(self):
        a = self.app
        self.add(Header(a, title="Ethernet", badge="eth.port", right="eth.link", flash="eth.rxn", prefix=""))
        self.add(ArcGauge(a, value="eth.speed", level="eth.level", unit="LINK"))
        self.add(StatsRow(a, 150, [("RX", "eth.rx"), ("TX", "eth.tx")]))
        self.add(InfoPanel(a, 12, 184, "LAN", "IP", "eth.ip",
                           [("PORT", "eth.port"), ("CLIENTS", "eth.clients"), ("PROTO", "eth.proto")]))
        self.add(Trace(a, "eth.rxn"))
        self.add(Button(a, STOCK_BTN, "▶  STOCK UI", a.request_stock))


# ───────────────────────── App ─────────────────────────
class App:
    def __init__(self, sources):
        self.wake = threading.Event(); self.stop = threading.Event()
        self.sources = sources
        self.registry = {k: s for s in sources for k in s.provides}
        self.pages = []; self.idx = 0; self.current = None; self.theme = Theme
        self.service = False                              # True when launched by the procd service

    def invalidate(self): self.wake.set()
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
            dev = InputDevice("/dev/input/event0"); x = y = x0 = y0 = 0; down = False
            for e in dev.read_loop():
                if self.stop.is_set(): return
                if e.type == ecodes.EV_ABS:
                    if e.code in (ecodes.ABS_X, ecodes.ABS_MT_POSITION_X): x = e.value
                    elif e.code in (ecodes.ABS_Y, ecodes.ABS_MT_POSITION_Y): y = e.value
                elif e.type == ecodes.EV_KEY and e.code == ecodes.BTN_TOUCH:
                    if e.value == 1: down = True; x0, y0 = x, y
                    elif e.value == 0 and down:
                        down = False; dx = x - x0
                        if abs(dx) > 50 and abs(dx) > abs(y - y0):
                            self.show(self.idx + (1 if dx < 0 else -1))   # swipe -> next/prev
                        else:
                            self.current.on_touch(x, y)                    # tap -> button
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
        subprocess.run("/etc/init.d/gl_screen stop", shell=True, capture_output=True); time.sleep(1)
        subprocess.run("echo 0 > /sys/class/graphics/fb0/blank", shell=True, capture_output=True)
        for s in (signal.SIGINT, signal.SIGTERM):
            signal.signal(s, lambda *_: self.stop.set())
        threading.Thread(target=self._touch, daemon=True).start()
        self.show(start)
        th = self.theme; prev_anim = False; first = True; t0 = time.time()
        try:
            with open("/dev/fb0", "r+b", buffering=0) as fb:
                while not self.stop.is_set():
                    anim = self.current.animate()
                    if first or self.wake.is_set() or anim or prev_anim:
                        self.wake.clear()
                        img = Image.new("RGB", (W, H), th.BG); d = ImageDraw.Draw(img)
                        self.current.draw(d, th); self._dots(d, th)
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
def _mock(which):
    DATA = {"sim.carrier":"T-Mobile","sim.slot":"1","net.mode":"NR5G-SA",
            "signal.rsrp":-100,"signal.rsrq":"-13 dB","signal.sinr":"1 dB","signal.level":4,
            "cell.id":"187461017","cell.band":"n25","cell.freq":"1981 MHz","cell.bw":"20 MHz",
            "wifi.ssid":"Sun","wifi.mode":"RPT","wifi.band":"5 GHz","wifi.signal":-38,
            "wifi.level":5,"wifi.rate":"780 M","wifi.chan":149,"wifi.freq":"5745 MHz",
            "wifi.width":"VHT80","wifi.ap":"Travel2G","wifi.clients":0,
            "batt.pct":100,"batt.level":5,"batt.state":"FULL","batt.temp":"30.2°C",
            "sys.cputemp":"36°C","sys.load":1.39,"sys.ram":"36%","sys.free":"1019 MB","sys.uptime":"5d 1h",
            "eth.speed":"DOWN","eth.level":0,"eth.link":"DOWN","eth.port":"eth0","eth.ip":"192.168.8.1",
            "eth.rx":"0 KB/s","eth.tx":"0 KB/s","eth.rxn":0,"eth.clients":0,"eth.proto":"static"}
    class MockApp(App):
        def __init__(self): self.wake=threading.Event(); self.theme=Theme; self.current=None; self.pages=[]; self.idx=0
        def invalidate(self): pass
        def subscribe(self, key, cb):
            if key in DATA: cb(DATA[key])
        def unsubscribe(self, *a): pass
        def request_stock(self): pass
    a = MockApp()
    page = {"wifi": WifiPage, "system": SystemPage, "eth": EthernetPage}.get(which, SignalPage)(a)
    a.pages = [page]; page.wire()
    for _ in range(40): page.animate()
    img = Image.new("RGB", (W, H), Theme.BG); d = ImageDraw.Draw(img)
    page.draw(d, Theme)
    out = "/tmp/mudi_%s.png" % which; img.save(out); print("wrote", out)

def main():
    if "--mock" in sys.argv:
        which = next((w for w in ("wifi", "system", "eth") if w in sys.argv), "signal")
        _mock(which); return
    dur = next((float(a) for a in sys.argv[1:] if a.replace(".", "").isdigit()), None)
    app = App([CellularSource(), WifiSource(), SystemSource(), EthernetSource()])
    app.service = "--service" in sys.argv
    app.run([SignalPage(app), WifiPage(app), SystemPage(app), EthernetPage(app)], start=0, duration=dur)

if __name__ == "__main__":
    main()
