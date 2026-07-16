# Graph Styles + Settings Scroll Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the page hero widget a user-selectable global style (starting with the two already in the code), and give the Settings page drag-to-scroll so it can keep growing.

**Architecture:** `HeroGraph` and `ArcGauge` become subclasses of a `Gauge` base that declares its own slot geometry (`STACK_Y`) and whether it already shows history (`SUPPLIES_HISTORY`). A `GAUGE_STYLES` registry maps a uci value to a class. Metric pages stop hardcoding y-coordinates and subclass `MetricPage`, which lays the stack out below `gauge.STACK_Y` and adds a `Trace` only when the style supplies none. Settings becomes a `ScrollPage` that renders its rows into a clipped viewport sub-image.

**Tech Stack:** Python 3, PIL (draw), numpy (RGB565 pack), stdlib `unittest`. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-07-16-graph-styles-and-settings-scroll-design.md`

## Global Constraints

- **Everything lives in `src/mudi.py`.** That single-file layout is the repo's established pattern and the deploy path depends on it (`ssh host 'cat > /usr/bin/mudi.py' < src/mudi.py`). Do **not** split it into modules.
- **No new runtime dependencies.** The target is OpenWrt/musl with opkg-installed `python3-light`, `python3-numpy`, `python3-pillow`. Only PIL, numpy, and the stdlib are available. `evdev` is imported *inside* `App._touch()` — keep it that way so `import mudi` works off-device.
- **Tests use stdlib `unittest` only.** pytest is not installed and must not be added. Run from the repo root: `python3 -m unittest discover -s tests -v`.
- **Panel is 240×320** (`mudi.W`, `mudi.H`). Never hardcode these numbers; use the constants.
- **Commits: green-lit for this work.** CLAUDE.md's default is "commit only when Kevin asks"; Kevin green-lit a commit per task for this plan on 2026-07-16, so each task's commit step runs as written. This green-light covers **this plan only** — it does not generalize.
- **Work happens directly on `main`** (Kevin's call, 2026-07-16 — the repo is single-`main` per CLAUDE.md).
- **On-device rules** (CLAUDE.md): deploy with `ssh host 'cat > /path' < file` (there is no sftp-server, `scp` fails). **Never reboot the Mudi remotely.** **Always restore `gl_screen`** after testing — `/dev/fb0` is single-owner.

## File Structure

- **Modify: `src/mudi.py`** — all production changes. Sections touched, in file order: `Settings.DEFAULTS`, the widget block (`Gauge`/`HeroGraph`/`ArcGauge`), the `Row` block, `Page` subclasses, `App`, and `_mock`/`main`.
- **Create: `tests/test_mudi.py`** — the whole test suite. One file matches the one-file source; splitting it would be more ceremony than the suite is worth.
- **Modify: `CLAUDE.md`** — §5 "Current UI state" and §12 "Current status" both claim the HeroGraph roll-out is pending. This work supersedes that.

---

### Task 1: `Gauge` contract, style registry, and a reusable mock harness

Introduces the style abstraction with **no behavior change** — pages still build exactly what they build today. The `Gauge` constructor takes `level`/`series`/`unit` with defaults, so the existing keyword call sites keep working untouched.

Also promotes the `MockApp` currently nested inside `_mock()` to module level, so tests and the preview path share one off-device App double.

**Files:**
- Modify: `src/mudi.py` — `Settings.DEFAULTS` (~line 128), `ArcGauge`/`HeroGraph` (~lines 330-385), `App` (~line 700), `_mock` (~line 910)
- Create: `tests/test_mudi.py`

**Interfaces:**
- Produces:
  - `class Gauge(Widget)` with class attrs `TOP: int`, `HEIGHT: int`, `STACK_Y: int`, `SUPPLIES_HISTORY: bool`, `LABEL: str`; ctor `(app, value, level=None, series=None, unit="", series_label=None)`; instance attrs `k_value`, `k_level`, `k_series`, `unit`, `series_label`, `value`.
  - `HeroGraph(Gauge)` — `TOP=32, HEIGHT=118, STACK_Y=172, SUPPLIES_HISTORY=True, LABEL="Hero"`, attr `hist: list`.
  - `ArcGauge(Gauge)` — `TOP=42, HEIGHT=100, STACK_Y=150, SUPPLIES_HISTORY=False, LABEL="Arc"`.
  - `GAUGE_STYLES: dict[str, type]` — `{"hero": HeroGraph, "arc": ArcGauge}`; insertion order drives the settings stepper.
  - `App.gauge_cls() -> type` — resolves `settings["graph_style"]`, falling back to `HeroGraph`.
  - `MOCK_DATA: dict` and `class MockApp(App)` at module level; ctor `MockApp(data=None)`.
  - `Settings.DEFAULTS["graph_style"] == "hero"`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_mudi.py`:

```python
"""MudiUI unit tests — stdlib unittest only (no pytest on the box or the dev machine).

Run from the repo root:  python3 -m unittest discover -s tests -v
"""
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
import mudi  # noqa: E402


def all_bg(img):
    """True if nothing was painted — every pixel is still the theme background.

       Uses numpy (already a hard dependency of mudi) rather than Image.getdata(), which is
       deprecated in Pillow 12 and removed in Pillow 14."""
    return bool((np.asarray(img) == mudi.Theme.BG).all())


class TestStyleRegistry(unittest.TestCase):
    def test_registry_order_drives_the_stepper(self):
        self.assertEqual(list(mudi.GAUGE_STYLES), ["hero", "arc"])

    def test_registry_maps_slugs_to_classes(self):
        self.assertIs(mudi.GAUGE_STYLES["hero"], mudi.HeroGraph)
        self.assertIs(mudi.GAUGE_STYLES["arc"], mudi.ArcGauge)

    def test_styles_have_labels(self):
        self.assertEqual(mudi.HeroGraph.LABEL, "Hero")
        self.assertEqual(mudi.ArcGauge.LABEL, "Arc")

    def test_hero_supplies_history_arc_does_not(self):
        self.assertTrue(mudi.HeroGraph.SUPPLIES_HISTORY)
        self.assertFalse(mudi.ArcGauge.SUPPLIES_HISTORY)

    def test_slot_geometry(self):
        self.assertEqual(
            (mudi.HeroGraph.TOP, mudi.HeroGraph.HEIGHT, mudi.HeroGraph.STACK_Y), (32, 118, 172))
        self.assertEqual(
            (mudi.ArcGauge.TOP, mudi.ArcGauge.HEIGHT, mudi.ArcGauge.STACK_Y), (42, 100, 150))

    def test_both_styles_subclass_gauge(self):
        self.assertTrue(issubclass(mudi.HeroGraph, mudi.Gauge))
        self.assertTrue(issubclass(mudi.ArcGauge, mudi.Gauge))


class TestGraphStyleSetting(unittest.TestCase):
    def test_default_is_hero(self):
        self.assertEqual(mudi.Settings.DEFAULTS["graph_style"], "hero")

    def test_gauge_cls_resolves_from_settings(self):
        a = mudi.MockApp()
        a.settings.vals["graph_style"] = "arc"
        self.assertIs(a.gauge_cls(), mudi.ArcGauge)
        a.settings.vals["graph_style"] = "hero"
        self.assertIs(a.gauge_cls(), mudi.HeroGraph)

    def test_gauge_cls_falls_back_to_hero_on_junk(self):
        a = mudi.MockApp()
        a.settings.vals["graph_style"] = "not-a-style"
        self.assertIs(a.gauge_cls(), mudi.HeroGraph)


class TestMockApp(unittest.TestCase):
    def test_serves_mock_data_to_subscribers(self):
        seen = []
        mudi.MockApp().subscribe("signal.rsrp", seen.append)
        self.assertEqual(seen, [mudi.MOCK_DATA["signal.rsrp"]])

    def test_unknown_key_is_a_no_op(self):
        seen = []
        mudi.MockApp().subscribe("nope.nothing", seen.append)
        self.assertEqual(seen, [])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m unittest discover -s tests -v`
Expected: FAIL — `AttributeError: module 'mudi' has no attribute 'GAUGE_STYLES'` (and the same for `Gauge`, `MockApp`, `MOCK_DATA`).

- [ ] **Step 3: Add `graph_style` to the settings defaults**

In `src/mudi.py`, `class Settings`, replace the `DEFAULTS` dict:

```python
    DEFAULTS = {"brightness": "90", "screen_timeout": "30", "stay_awake_charging": "1",
                "default_page": "0", "band_lock": "0", "net_mode": "auto",
                "longpress": "1.6", "start_on_boot": "1", "graph_style": "hero"}
```

- [ ] **Step 4: Add the `Gauge` base class**

In `src/mudi.py`, immediately **above** `class ArcGauge`, insert:

```python
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
```

- [ ] **Step 5: Convert `ArcGauge` to a `Gauge` subclass**

Replace `class ArcGauge(Widget):` and its `__init__` (keep `wire`/`animate`/`draw` exactly as they are):

```python
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
```

- [ ] **Step 6: Convert `HeroGraph` to a `Gauge` subclass**

Replace `class HeroGraph(Widget):` and its `__init__` (keep `wire`/`_push`/`draw` exactly as they are for now — `series_label` rendering lands in Task 3):

```python
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
```

- [ ] **Step 7: Add the style registry**

Immediately **below** `class HeroGraph` (after its `draw` method, before `class StatsRow`), insert:

```python
# slug -> style class. Insertion order drives the settings stepper. A new style costs one class
# and one entry here — pages never change.
GAUGE_STYLES = {"hero": HeroGraph, "arc": ArcGauge}
```

- [ ] **Step 8: Add `App.gauge_cls()`**

In `class App`, directly below the `# ---- settings effects ----` comment and above `def _brightness`, insert:

```python
    def gauge_cls(self):                                 # selected hero style; junk value -> Hero
        return GAUGE_STYLES.get(self.settings.get("graph_style"), HeroGraph)
```

- [ ] **Step 9: Promote the mock harness to module level**

In `src/mudi.py`, in the `# ── entry ──` section, replace the `DATA = {...}` dict and the nested `class MockApp` inside `_mock()` with module-level definitions placed **above** `def _mock`:

```python
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
        self.data = MOCK_DATA if data is None else data
        self.wake = threading.Event(); self.theme = Theme
        self.current = None; self.pages = []; self.idx = 0
        self.settings = Settings(); self.modal = None
    def invalidate(self): pass
    def subscribe(self, key, cb):
        if key in self.data: cb(self.data[key])
    def unsubscribe(self, *a): pass
    def request_stock(self): pass
    def request_toggle(self): pass
    def apply_setting(self, *a): pass
    def open_modal(self, m): self.modal = m
    def close_modal(self): self.modal = None
```

Then in `_mock()`, delete the now-duplicated `DATA` dict and nested `MockApp` class, and replace the line `a = MockApp()` with:

```python
    a = MockApp()
```

(unchanged call — it now resolves to the module-level class).

- [ ] **Step 10: Run the tests to verify they pass**

Run: `python3 -m unittest discover -s tests -v`
Expected: PASS — 11 tests.

- [ ] **Step 11: Verify the preview path still works (no behavior change yet)**

Run: `python3 src/mudi.py --mock signal && python3 src/mudi.py --mock settings`
Expected: `wrote /tmp/mudi_signal.png` and `wrote /tmp/mudi_settings.png`, no traceback.

- [ ] **Step 12: Commit** *(only once Kevin green-lights committing — see Global Constraints)*

```bash
git add src/mudi.py tests/test_mudi.py
git commit -m "refactor: add Gauge style contract + registry, promote mock harness"
```

---

### Task 2: `MetricPage` — pages declare bindings, the style decides geometry

Removes the hardcoded y-coordinates from the four metric pages. This is the task that makes the style actually switchable.

**Files:**
- Modify: `src/mudi.py` — `SignalPage`/`WifiPage`/`SystemPage`/`EthernetPage` (~lines 616-660)
- Modify: `tests/test_mudi.py`

**Interfaces:**
- Consumes: `Gauge`, `HeroGraph`, `ArcGauge`, `GAUGE_STYLES`, `App.gauge_cls()`, `MockApp` (Task 1).
- Produces:
  - `class MetricPage(Page)` with class attrs `HEADER: dict`, `GAUGE: dict`, `STATS: list`, `PANEL: tuple`, and layout constants `STATS_TO_PANEL=36`, `PANEL_H=74`, `PANEL_TO_TRACE=10`, `TRACE_H=36`.
  - `SignalPage`, `WifiPage`, `SystemPage`, `EthernetPage` all subclass `MetricPage`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_mudi.py`, above the `if __name__` block:

```python
METRIC_PAGES = ("SignalPage", "WifiPage", "SystemPage", "EthernetPage")


def build_page(name, style):
    a = mudi.MockApp()
    a.settings.vals["graph_style"] = style
    return getattr(mudi, name)(a)


def only(page, cls):
    """The page's single widget of type cls (fails loudly if there isn't exactly one)."""
    found = [w for w in page.widgets if isinstance(w, cls)]
    assert len(found) == 1, "expected 1 %s, got %d" % (cls.__name__, len(found))
    return found[0]


class TestMetricPageLayout(unittest.TestCase):
    def test_all_metric_pages_subclass_metricpage(self):
        for name in METRIC_PAGES:
            self.assertTrue(issubclass(getattr(mudi, name), mudi.MetricPage), name)

    def test_page_builds_the_selected_style(self):
        for style, cls in (("hero", mudi.HeroGraph), ("arc", mudi.ArcGauge)):
            for name in METRIC_PAGES:
                self.assertIsInstance(only(build_page(name, style), mudi.Gauge), cls,
                                      "%s / %s" % (name, style))

    def test_hero_layout_matches_todays_signal_page(self):
        for name in METRIC_PAGES:
            p = build_page(name, "hero")
            self.assertEqual(only(p, mudi.StatsRow).y, 172, name)
            self.assertEqual(only(p, mudi.InfoPanel).y, 208, name)
            self.assertEqual([w for w in p.widgets if isinstance(w, mudi.Trace)], [], name)

    def test_arc_layout(self):
        for name in METRIC_PAGES:
            p = build_page(name, "arc")
            self.assertEqual(only(p, mudi.StatsRow).y, 150, name)
            self.assertEqual(only(p, mudi.InfoPanel).y, 186, name)
            trace = only(p, mudi.Trace)
            self.assertEqual((trace.y, trace.h), (270, 36), name)

    def test_arc_trace_graphs_the_pages_declared_series(self):
        for name, series in (("SignalPage", "signal.rsrp"), ("WifiPage", "wifi.signal"),
                             ("SystemPage", "sys.load"), ("EthernetPage", "eth.rxn")):
            self.assertEqual(only(build_page(name, "arc"), mudi.Trace).k, series, name)

    def test_hero_binds_the_pages_declared_series(self):
        for name, series in (("SignalPage", "signal.rsrp"), ("WifiPage", "wifi.signal"),
                             ("SystemPage", "sys.load"), ("EthernetPage", "eth.rxn")):
            self.assertEqual(only(build_page(name, "hero"), mudi.HeroGraph).k_series, series, name)

    def test_system_and_ethernet_label_their_curve(self):
        self.assertEqual(only(build_page("SystemPage", "hero"), mudi.Gauge).series_label, "LOAD")
        self.assertEqual(only(build_page("EthernetPage", "hero"), mudi.Gauge).series_label, "RX")

    def test_signal_and_wifi_need_no_curve_label(self):
        for name in ("SignalPage", "WifiPage"):
            self.assertIsNone(only(build_page(name, "hero"), mudi.Gauge).series_label, name)

    def test_every_page_and_style_actually_paints(self):
        from PIL import Image, ImageDraw
        for style in ("hero", "arc"):
            for name in METRIC_PAGES:
                p = build_page(name, style)
                p.wire()
                img = Image.new("RGB", (mudi.W, mudi.H), mudi.Theme.BG)
                p.draw(ImageDraw.Draw(img), mudi.Theme)
                self.assertFalse(all_bg(img),
                                 "%s/%s drew nothing but background" % (name, style))
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m unittest discover -s tests -v`
Expected: FAIL — `AttributeError: module 'mudi' has no attribute 'MetricPage'`.

- [ ] **Step 3: Add `MetricPage`**

In `src/mudi.py`, directly below `class Page` and above `class SignalPage`, insert:

```python
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
```

- [ ] **Step 4: Convert the four metric pages**

Replace `class SignalPage` through the end of `class EthernetPage` (up to but not including `class SettingsPage`) with:

```python
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
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python3 -m unittest discover -s tests -v`
Expected: PASS — 20 tests.

- [ ] **Step 6: Eyeball both styles**

Run: `python3 src/mudi.py --mock signal`
Expected: `wrote /tmp/mudi_signal.png` — visually identical to `docs/images/mudi_signal.png` (Hero is the default).

- [ ] **Step 7: Commit** *(only once Kevin green-lights committing)*

```bash
git add src/mudi.py tests/test_mudi.py
git commit -m "refactor: MetricPage lays out from the style's slot; pages declare bindings only"
```

---

### Task 3: `HeroGraph` labels its curve

When a page headlines one metric but graphs another (System: `batt.pct` over `sys.load`; Ethernet: `eth.speed` over `eth.rxn`), the merged hero must say what the curve is.

**Files:**
- Modify: `src/mudi.py` — `HeroGraph.draw` (~line 364)
- Modify: `tests/test_mudi.py`

**Interfaces:**
- Consumes: `HeroGraph` with `series_label` (Task 1), `MockApp` (Task 1).
- Produces: no new names — `HeroGraph.draw` honors `self.series_label`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_mudi.py`, above the `if __name__` block:

```python
class TestHeroSeriesLabel(unittest.TestCase):
    LABEL_BAND = (12, mudi.HeroGraph.TOP + 66, 90, mudi.HeroGraph.TOP + 78)

    def render(self, series_label):
        from PIL import Image, ImageDraw
        g = mudi.HeroGraph(mudi.MockApp(), value="batt.pct", series="sys.load",
                           unit="%  BATTERY", series_label=series_label)
        g.hist = [1.0, 2.0, 1.5, 3.0, 2.5]
        img = Image.new("RGB", (mudi.W, mudi.H), mudi.Theme.BG)
        g.draw(ImageDraw.Draw(img), mudi.Theme)
        return img

    def test_label_changes_the_frame(self):
        self.assertNotEqual(self.render("LOAD").tobytes(), self.render(None).tobytes())

    def test_label_band_is_empty_without_a_label(self):
        band = self.render(None).crop(self.LABEL_BAND)
        self.assertTrue(all_bg(band))

    def test_label_band_is_painted_with_a_label(self):
        band = self.render("LOAD").crop(self.LABEL_BAND)
        self.assertFalse(all_bg(band))

    def test_label_needs_history_to_mean_anything(self):
        from PIL import Image, ImageDraw
        g = mudi.HeroGraph(mudi.MockApp(), value="batt.pct", series="sys.load",
                           unit="%  BATTERY", series_label="LOAD")
        img = Image.new("RGB", (mudi.W, mudi.H), mudi.Theme.BG)
        g.draw(ImageDraw.Draw(img), mudi.Theme)          # hist empty -> must not raise
        self.assertTrue(all_bg(img.crop(self.LABEL_BAND)))
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m unittest discover -s tests -v -k SeriesLabel`
Expected: FAIL — `test_label_changes_the_frame` and `test_label_band_is_painted_with_a_label` fail, because `series_label` is stored but never drawn.

- [ ] **Step 3: Draw the label**

In `HeroGraph.draw`, inside the `if len(self.hist) >= 2:` block, insert the label directly **above** the existing `"hi"` line so it reads:

```python
        if len(self.hist) >= 2:
            lo, hi = min(self.hist), max(self.hist)
            if self.series_label:                        # the curve isn't the headline -> name it
                d.text((x, y+66), self.series_label, font=th.mono[9], fill=th.DIM)
            d.text((x, y+80),  "hi", font=th.mono[9], fill=th.DIM)
```

The label sits inside the history block on purpose: it names the curve, so with no curve there is nothing to name.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python3 -m unittest discover -s tests -v`
Expected: PASS — 24 tests.

- [ ] **Step 5: Commit** *(only once Kevin green-lights committing)*

```bash
git add src/mudi.py tests/test_mudi.py
git commit -m "feat: HeroGraph labels its curve when it isn't the headline metric"
```

---

### Task 4: The `Graph style` setting row + metric-page rebuild

Adds the user-facing control. **After this task Settings has 11 rows (338px of content on a 320px panel), so the last row is off-screen until Task 5 adds scrolling.** That's a known transient — the tests assert the row exists, not that it's visible.

**Files:**
- Modify: `src/mudi.py` — `SettingsPage.build` (~line 662), `App.apply_setting` (~line 742)
- Modify: `tests/test_mudi.py`

**Interfaces:**
- Consumes: `GAUGE_STYLES`, `MetricPage`, `MockApp`, `App.gauge_cls()` (Tasks 1-2).
- Produces: `App._rebuild_metric_pages() -> None`; `App.apply_setting` routes `"graph_style"` to it.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_mudi.py`, above the `if __name__` block:

```python
class TestGraphStyleRow(unittest.TestCase):
    def row(self):
        page = mudi.SettingsPage(mudi.MockApp())
        rows = getattr(page, "rows", None) or page.widgets
        found = [r for r in rows
                 if isinstance(r, mudi.StepperRow) and r.skey == "graph_style"]
        self.assertEqual(len(found), 1, "expected exactly one graph_style row")
        return found[0]

    def test_row_offers_every_registered_style(self):
        self.assertEqual(self.row().options, list(mudi.GAUGE_STYLES))

    def test_row_shows_the_style_labels(self):
        r = self.row()
        self.assertEqual([r.fmt(v) for v in r.options], ["Hero", "Arc"])

    def test_row_wraps(self):
        self.assertTrue(self.row().wrap)

    def test_row_is_not_confirm_gated(self):
        self.assertFalse(self.row().confirm)             # cosmetic, instant, reversible


class TestMetricPageRebuild(unittest.TestCase):
    def app(self):
        a = mudi.MockApp()
        a.pages = [mudi.SignalPage(a), mudi.WifiPage(a), mudi.SystemPage(a),
                   mudi.EthernetPage(a), mudi.SettingsPage(a)]
        a.idx = 4
        a.current = a.pages[4]
        return a

    def test_rebuild_swaps_every_metric_page_to_the_new_style(self):
        a = self.app()
        for p in a.pages[:4]:
            self.assertIsInstance(only(p, mudi.Gauge), mudi.HeroGraph)
        a.settings.vals["graph_style"] = "arc"
        a._rebuild_metric_pages()
        for p in a.pages[:4]:
            self.assertIsInstance(only(p, mudi.Gauge), mudi.ArcGauge)

    def test_rebuild_preserves_the_settings_page_instance(self):
        a = self.app()
        settings_page = a.pages[4]
        a.settings.vals["graph_style"] = "arc"
        a._rebuild_metric_pages()
        self.assertIs(a.pages[4], settings_page)         # scroll pos + tapped row must survive
        self.assertIs(a.current, settings_page)

    def test_rebuild_never_leaves_current_dangling(self):
        a = self.app()
        a.idx = 0
        a.current = a.pages[0]
        a.settings.vals["graph_style"] = "arc"
        a._rebuild_metric_pages()
        self.assertIs(a.current, a.pages[0])             # render thread must never see a stale page
        self.assertIsInstance(only(a.current, mudi.Gauge), mudi.ArcGauge)

    def test_rebuild_keeps_page_count_and_order(self):
        a = self.app()
        before = [type(p) for p in a.pages]
        a.settings.vals["graph_style"] = "arc"
        a._rebuild_metric_pages()
        self.assertEqual([type(p) for p in a.pages], before)


class TestApplySettingRouting(unittest.TestCase):
    def test_graph_style_triggers_a_rebuild(self):
        class SpyApp(mudi.MockApp):
            apply_setting = mudi.App.apply_setting       # exercise the real router
            def __init__(self):
                super().__init__()
                self.rebuilds = 0
            def _rebuild_metric_pages(self):
                self.rebuilds += 1
        a = SpyApp()
        a.apply_setting("graph_style", "arc")
        self.assertEqual(a.rebuilds, 1)

    def test_other_settings_do_not_rebuild(self):
        class SpyApp(mudi.MockApp):
            apply_setting = mudi.App.apply_setting
            def __init__(self):
                super().__init__()
                self.rebuilds = 0
            def _rebuild_metric_pages(self):
                self.rebuilds += 1
        a = SpyApp()
        a.apply_setting("screen_timeout", "60")
        self.assertEqual(a.rebuilds, 0)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m unittest discover -s tests -v`
Expected: FAIL — `expected exactly one graph_style row` (0 found) and `AttributeError: 'MockApp' object has no attribute '_rebuild_metric_pages'`.

- [ ] **Step 3: Add the settings row**

In `SettingsPage.build`, insert the new row into the `rows` list directly **after** the `ToggleRow(a, "Awake on charge", "stay_awake_charging")` line and before the `Default page` stepper:

```python
            StepperRow(a, "Graph style", "graph_style", list(GAUGE_STYLES),
                       fmt=lambda v: GAUGE_STYLES[v].LABEL, wrap=True),
```

- [ ] **Step 4: Add `_rebuild_metric_pages`**

In `class App`, directly below `gauge_cls`, insert:

```python
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
```

- [ ] **Step 5: Route the setting to the rebuild**

In `App.apply_setting`, inside the `try:` block, add a branch after the `brightness` branch:

```python
            elif skey == "graph_style":
                self._rebuild_metric_pages()
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `python3 -m unittest discover -s tests -v`
Expected: PASS — 34 tests.

- [ ] **Step 7: Commit** *(only once Kevin green-lights committing)*

```bash
git add src/mudi.py tests/test_mudi.py
git commit -m "feat: add Graph style setting; rebuild metric pages on change"
```

---

### Task 5: `ScrollPage` — clipped viewport + `Row.oy`

Makes Settings scrollable so all 11 rows are reachable.

**Files:**
- Modify: `src/mudi.py` — `Row` and its four subclasses (~lines 450-546), `SettingsPage` (~line 662)
- Modify: `tests/test_mudi.py`

**Interfaces:**
- Consumes: `Row`, `Banner`, `SettingsPage`, `MockApp`.
- Produces:
  - `Page.draw(self, d, th, img=None)` — **signature change**; `img` is the frame being drawn. Widgets keep `draw(d, th)`.
  - `Row.oy: int` (class default `0`) and `Row.ry() -> int` returning `self.y - self.oy`.
  - `class ScrollPage(Page)` with `VIEW_TOP=30`, `VIEW_H=H-VIEW_TOP`, `BAR_W=3`; attrs `rows: list`, `scroll_y: int`, `content_h: int`; methods `add_row(r)`, `max_scroll() -> int`, `scrollable() -> bool`, `scroll_to(v) -> None`, `draw(d, th, img)`.
  - `SettingsPage(ScrollPage)` — rows placed from `y=0` in content coordinates.

**Why `img` is threaded through `draw`:** `ScrollPage` must composite a viewport sub-image onto the
frame, and `ImageDraw` exposes no public accessor for its target image. Reaching for the private
`d._image` works on Pillow 12 but the device runs whatever Pillow GL bundles, so passing the frame
explicitly is the version-proof option. The ripple is three call sites.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_mudi.py`, above the `if __name__` block:

```python
def fake_scroll_page(app, n_rows):
    """A ScrollPage with a known row count, so scroll math doesn't depend on the settings list."""
    class _Page(mudi.ScrollPage):
        def build(self):
            self.add(mudi.Banner(self.app, "Fake"))
            y = 0
            for i in range(n_rows):
                self.add_row(mudi.ActionRow(self.app, "row %d" % i, lambda: None).place(y))
                y += mudi.Row.H
            self.content_h = y
    return _Page(app)


class TestScrollMath(unittest.TestCase):
    def test_short_page_does_not_scroll(self):
        p = fake_scroll_page(mudi.MockApp(), 3)
        self.assertFalse(p.scrollable())
        self.assertEqual(p.max_scroll(), 0)

    def test_long_page_scrolls_by_the_overflow(self):
        p = fake_scroll_page(mudi.MockApp(), 20)
        self.assertEqual(p.content_h, 20 * mudi.Row.H)
        self.assertTrue(p.scrollable())
        self.assertEqual(p.max_scroll(), 20 * mudi.Row.H - p.VIEW_H)

    def test_scroll_clamps_at_both_ends(self):
        p = fake_scroll_page(mudi.MockApp(), 20)
        p.scroll_to(-500)
        self.assertEqual(p.scroll_y, 0)
        p.scroll_to(99999)
        self.assertEqual(p.scroll_y, p.max_scroll())

    def test_scroll_wakes_the_render_loop_only_on_change(self):
        a = mudi.MockApp()
        p = fake_scroll_page(a, 20)
        a.wake.clear()
        p.scroll_to(0)                                   # already there
        self.assertFalse(a.wake.is_set())
        p.scroll_to(10)
        self.assertTrue(a.wake.is_set())


class TestScrollTouch(unittest.TestCase):
    def test_touch_translates_screen_y_to_content_y(self):
        p = fake_scroll_page(mudi.MockApp(), 20)
        p.scroll_to(p.max_scroll())
        hits = []
        for r in p.rows:
            r.act = lambda x, r=r: hits.append(r)
        p.on_touch(20, p.VIEW_TOP + 5)
        expected = [r for r in p.rows if r.in_row(p.max_scroll() + 5)]
        self.assertEqual(hits, expected)
        self.assertEqual(len(hits), 1)

    def test_touch_above_the_viewport_hits_no_row(self):
        p = fake_scroll_page(mudi.MockApp(), 20)
        for r in p.rows:
            r.act = lambda x: self.fail("a row acted on a touch in the chrome")
        self.assertFalse(p.on_touch(20, 5))

    def test_unscrolled_touch_hits_the_row_under_the_finger(self):
        p = fake_scroll_page(mudi.MockApp(), 20)
        hits = []
        for r in p.rows:
            r.act = lambda x, r=r: hits.append(r)
        p.on_touch(20, p.VIEW_TOP + mudi.Row.H + 2)
        self.assertEqual(hits, [p.rows[1]])


class TestScrollDraw(unittest.TestCase):
    def render(self, page):
        from PIL import Image, ImageDraw
        img = Image.new("RGB", (mudi.W, mudi.H), mudi.Theme.BG)
        page.draw(ImageDraw.Draw(img), mudi.Theme, img)
        return img

    def test_half_scrolled_rows_never_bleed_into_the_chrome(self):
        p = fake_scroll_page(mudi.MockApp(), 20)
        p.scroll_to(mudi.Row.H // 2)                     # half a row above the fold
        img = self.render(p)
        gap = img.crop((0, 26, mudi.W, p.VIEW_TOP))      # between banner rule and viewport
        self.assertTrue(all_bg(gap))

    def test_scrollbar_appears_only_when_scrollable(self):
        short = self.render(fake_scroll_page(mudi.MockApp(), 3))
        long_ = self.render(fake_scroll_page(mudi.MockApp(), 20))
        strip = (mudi.W - 3, mudi.ScrollPage.VIEW_TOP, mudi.W, mudi.H)
        self.assertTrue(all_bg(short.crop(strip)))
        self.assertFalse(all_bg(long_.crop(strip)))

    def test_scrolling_changes_what_is_drawn(self):
        p = fake_scroll_page(mudi.MockApp(), 20)
        top = self.render(p).tobytes()
        p.scroll_to(p.max_scroll())
        self.assertNotEqual(self.render(p).tobytes(), top)


class TestSettingsPageScrolls(unittest.TestCase):
    def page(self):
        return mudi.SettingsPage(mudi.MockApp())

    def test_settings_is_a_scrollpage(self):
        self.assertIsInstance(self.page(), mudi.ScrollPage)

    def test_rows_start_at_the_content_origin(self):
        self.assertEqual(self.page().rows[0].y, 0)

    def test_content_height_covers_every_row(self):
        p = self.page()
        self.assertEqual(p.content_h, len(p.rows) * mudi.Row.H)

    def test_every_row_is_reachable_by_scrolling(self):
        p = self.page()
        p.scroll_to(p.max_scroll())
        last = p.rows[-1]
        self.assertLessEqual(last.y + mudi.Row.H - p.scroll_y, p.VIEW_H)

    def test_settings_paints_at_every_scroll_extreme(self):
        from PIL import Image, ImageDraw
        p = self.page()
        for pos in (0, p.max_scroll() // 2, p.max_scroll()):
            p.scroll_to(pos)
            img = Image.new("RGB", (mudi.W, mudi.H), mudi.Theme.BG)
            p.draw(ImageDraw.Draw(img), mudi.Theme, img)
            self.assertFalse(all_bg(img),
                             "settings drew nothing but background at scroll %d" % pos)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m unittest discover -s tests -v`
Expected: FAIL — `AttributeError: module 'mudi' has no attribute 'ScrollPage'`.

- [ ] **Step 3: Give `Row` a render-time scroll offset**

In `class Row`, add the `oy` class attribute and the `ry()` helper, and rewrite `draw_label` to use it:

```python
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
```

- [ ] **Step 4: Make every row subclass draw through `ry()`**

Replace the four `draw` methods. Only the y-source changes — each takes a local `y = self.ry()`:

```python
    def draw(self, d, th):                               # ToggleRow
        self.draw_label(d, th)
        y = self.ry()
        on = self.on(); w, h = 46, 18; x1 = W-14; x0 = x1-w; y0 = y+5
        d.rounded_rectangle((x0, y0, x1, y0+h), radius=9, fill=th.CYAN if on else th.BTN, outline=th.BTN_BD)
        kx = (x1-16) if on else (x0+2)
        d.ellipse((kx, y0+2, kx+14, y0+16), fill=th.BG if on else th.DIM)
```

```python
    def draw(self, d, th):                               # StepperRow
        self.draw_label(d, th)
        y = self.ry(); y0 = y+5
        d.rounded_rectangle((116, y0, 136, y0+18), radius=4, fill=th.BTN, outline=th.BTN_BD)
        ctext(d, 126, y0+3, "-", th.bold[13], th.CYAN)
        d.rounded_rectangle((W-34, y0, W-14, y0+18), radius=4, fill=th.BTN, outline=th.BTN_BD)
        ctext(d, W-24, y0+3, "+", th.bold[13], th.CYAN)
        ctext(d, (138 + W-38)/2, y+8, self.fmt(self.options[self.index()]), th.mono[12], th.INK)
```

```python
    def draw(self, d, th):                               # SliderRow
        self.draw_label(d, th)
        y = self.ry()
        v = self.cur(); yc = y+14
        d.line((self.x0, yc, self.x1, yc), fill=th.GRID, width=2)
        kx = self.x0 + (v - self.lo) / (self.hi - self.lo) * (self.x1 - self.x0)
        d.line((self.x0, yc, kx, yc), fill=th.CYAN, width=2)
        d.ellipse((kx-4, yc-4, kx+4, yc+4), fill=th.INK)
        d.text((W-36, y+8), str(v), font=th.mono[11], fill=th.SUB)
```

```python
    def draw(self, d, th):                               # ActionRow
        self.draw_label(d, th)
        d.text((W-22, self.ry()+6), "›", font=th.bold[15], fill=th.CYAN)
```

- [ ] **Step 5: Thread the frame image through `Page.draw`**

`ScrollPage` needs to composite onto the frame, and `ImageDraw` has no public accessor for it. Pass
it explicitly rather than reaching for `d._image`.

In `class Page`, replace the `draw` method:

```python
    def draw(self, d, th, img=None): [w.draw(d, th) for w in self.widgets]
```

In `App.run`, replace the page-draw line inside the render block:

```python
                        self.current.draw(d, th, img); self._dots(d, th)
```

In `_mock`, replace its page-draw line:

```python
    page.draw(d, Theme, img)
```

- [ ] **Step 6: Add `ScrollPage`**

In `src/mudi.py`, directly below `class Page` and above `class MetricPage`, insert:

```python
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
            self.scroll_y = nv; self.app.wake.set()

    def wire(self):   [w.wire() for w in self.widgets + self.rows]
    def unwire(self): [w.unwire() for w in self.widgets + self.rows]
    def animate(self): return any([w.animate() for w in self.widgets + self.rows])

    def draw(self, d, th, img=None):
        for w in self.widgets: w.draw(d, th)             # fixed chrome, straight to the frame
        vp = Image.new("RGB", (W, self.VIEW_H), th.BG)   # clips rows to the viewport, exactly
        vd = ImageDraw.Draw(vp)
        for r in self.rows:
            r.oy = self.scroll_y                         # render thread only -> no race
            r.draw(vd, th)
        img.paste(vp, (0, self.VIEW_TOP))
        if self.scrollable(): self._bar(d, th)

    def _bar(self, d, th):
        h = max(12, self.VIEW_H * self.VIEW_H / self.content_h)
        y = self.VIEW_TOP + self.scroll_y / self.content_h * self.VIEW_H
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
```

- [ ] **Step 7: Convert `SettingsPage` to a `ScrollPage`**

Replace the `class SettingsPage(Page):` declaration and the placement loop at the end of its `build`:

```python
class SettingsPage(ScrollPage):
```

and the tail of `build` (keep the `rows = [...]` list exactly as it is, including the Graph style row from Task 4):

```python
        y = 0                                            # content coords; ScrollPage offsets them
        for r in rows:
            self.add_row(r.place(y)); y += Row.H
        self.content_h = y
```

- [ ] **Step 8: Run the tests to verify they pass**

Run: `python3 -m unittest discover -s tests -v`
Expected: PASS — 49 tests.

- [ ] **Step 9: Eyeball the scrolled settings page**

Run: `python3 src/mudi.py --mock settings`
Expected: `wrote /tmp/mudi_settings.png`. Open it: the Graph style row reads `Hero`, the banner is intact, and a short cyan scrollbar sits at the top-right of the viewport.

- [ ] **Step 10: Commit** *(only once Kevin green-lights committing)*

```bash
git add src/mudi.py tests/test_mudi.py
git commit -m "feat: scrollable Settings via clipped viewport ScrollPage"
```

---

### Task 6: `Gesture` classifier + the touch loop's vertical axis

`App._touch` reads evdev and can't be unit tested, so the gesture *decision* moves into a pure class that can. The loop keeps only I/O.

**Files:**
- Modify: `src/mudi.py` — new `Gesture` class, `App._touch` (~line 830)
- Modify: `tests/test_mudi.py`

**Interfaces:**
- Consumes: `ScrollPage` (Task 5).
- Produces: `class Gesture` with `TOL=8`, `SWIPE=50`; `down(x, y, scroll0=0, scrollable=False) -> None`; `move(x, y) -> int | None` (new `scroll_y`, or `None` when not scrolling); `up(x, y) -> tuple` — one of `("scroll", None)`, `("swipe", +1|-1)` (`+1` = next page), `("tap", (x, y))`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_mudi.py`, above the `if __name__` block:

```python
class TestGesture(unittest.TestCase):
    def test_small_movement_is_a_tap(self):
        g = mudi.Gesture()
        g.down(100, 100, scroll0=0, scrollable=True)
        self.assertIsNone(g.move(102, 104))              # inside TOL
        self.assertEqual(g.up(102, 104), ("tap", (102, 104)))

    def test_vertical_drag_scrolls_and_follows_the_finger(self):
        g = mudi.Gesture()
        g.down(100, 200, scroll0=40, scrollable=True)
        self.assertEqual(g.move(100, 180), 60)           # dragged up 20 -> scroll += 20
        self.assertEqual(g.move(100, 230), 10)           # dragged down 30 past origin
        self.assertEqual(g.up(100, 230), ("scroll", None))

    def test_scroll_latches_and_stays_latched(self):
        g = mudi.Gesture()
        g.down(100, 200, scroll0=0, scrollable=True)
        self.assertEqual(g.move(100, 180), 20)           # latches
        self.assertEqual(g.move(140, 190), 10)           # now-horizontal move keeps scrolling
        self.assertEqual(g.up(140, 190), ("scroll", None))

    def test_vertical_drag_on_an_unscrollable_page_is_still_a_tap(self):
        g = mudi.Gesture()
        g.down(100, 200, scroll0=0, scrollable=False)
        self.assertIsNone(g.move(100, 150))
        self.assertEqual(g.up(100, 150), ("tap", (100, 150)))

    def test_horizontal_swipe_left_goes_to_the_next_page(self):
        g = mudi.Gesture()
        g.down(200, 100, scroll0=0, scrollable=True)
        self.assertIsNone(g.move(140, 105))              # dy inside TOL -> no scroll
        self.assertEqual(g.up(140, 105), ("swipe", 1))

    def test_horizontal_swipe_right_goes_to_the_previous_page(self):
        g = mudi.Gesture()
        g.down(40, 100, scroll0=0, scrollable=True)
        self.assertEqual(g.up(100, 105), ("swipe", -1))

    def test_a_swipe_short_of_the_threshold_is_a_tap(self):
        g = mudi.Gesture()
        g.down(100, 100, scroll0=0, scrollable=False)
        self.assertEqual(g.up(140, 100), ("tap", (140, 100)))   # dx=40 < SWIPE

    def test_mostly_vertical_diagonal_scrolls_not_swipes(self):
        g = mudi.Gesture()
        g.down(100, 200, scroll0=0, scrollable=True)
        self.assertEqual(g.move(160, 130), 70)           # dy=70 > dx=60 -> scroll
        self.assertEqual(g.up(160, 130), ("scroll", None))

    def test_mostly_horizontal_diagonal_swipes_not_scrolls(self):
        g = mudi.Gesture()
        g.down(100, 200, scroll0=0, scrollable=True)
        self.assertIsNone(g.move(180, 170))              # dy=30 < dx=80 -> never latches
        self.assertEqual(g.up(180, 170), ("swipe", -1))  # dx=+80 is rightward -> prev page

    def test_scroll_and_swipe_are_mutually_exclusive(self):
        for dx, dy in ((60, 70), (80, 30), (0, 60), (60, 0)):
            g = mudi.Gesture()
            g.down(100, 150, scroll0=0, scrollable=True)
            g.move(100 + dx, 150 + dy)
            kind, _ = g.up(100 + dx, 150 + dy)
            self.assertIn(kind, ("scroll", "swipe", "tap"))
            if kind == "scroll":
                self.assertGreater(abs(dy), abs(dx))
            if kind == "swipe":
                self.assertGreater(abs(dx), abs(dy))

    def test_down_resets_state_between_gestures(self):
        g = mudi.Gesture()
        g.down(100, 200, scroll0=0, scrollable=True)
        g.move(100, 150)
        self.assertEqual(g.up(100, 150), ("scroll", None))
        g.down(100, 100, scroll0=0, scrollable=True)     # fresh gesture
        self.assertEqual(g.up(101, 101), ("tap", (101, 101)))
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m unittest discover -s tests -v -k Gesture`
Expected: FAIL — `AttributeError: module 'mudi' has no attribute 'Gesture'`.

- [ ] **Step 3: Add the `Gesture` classifier**

In `src/mudi.py`, directly **above** `class App`, insert:

```python
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
```

- [ ] **Step 4: Rewire `App._touch` to use it**

Replace the whole `App._touch` method:

```python
    def _touch(self):
        try:
            from evdev import InputDevice, ecodes
            dev = InputDevice("/dev/input/event0"); x = y = 0; down = False
            g = Gesture()
            for e in dev.read_loop():
                if self.stop.is_set(): return
                if e.type == ecodes.EV_ABS:
                    if e.code in (ecodes.ABS_X, ecodes.ABS_MT_POSITION_X): x = e.value
                    elif e.code in (ecodes.ABS_Y, ecodes.ABS_MT_POSITION_Y): y = e.value
                    if down:
                        sv = g.move(x, y)
                        if sv is not None: self.current.scroll_to(sv)   # live follow
                elif e.type == ecodes.EV_KEY and e.code == ecodes.BTN_TOUCH:
                    if e.value == 1:
                        down = True; self.last_touch = time.time()
                        p = self.current
                        scrollable = (isinstance(p, ScrollPage) and p.scrollable()
                                      and self.modal is None and not self.paused)
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
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python3 -m unittest discover -s tests -v`
Expected: PASS — 60 tests.

- [ ] **Step 6: Commit** *(only once Kevin green-lights committing)*

```bash
git add src/mudi.py tests/test_mudi.py
git commit -m "feat: drag-to-scroll gesture; extract Gesture classifier from the touch loop"
```

---

### Task 7: Preview every style, update the docs, verify on the device

**Files:**
- Modify: `src/mudi.py` — `_mock` (~line 910), `main` (~line 950)
- Modify: `tests/test_mudi.py`
- Modify: `CLAUDE.md` — §5 "Current UI state", §12 "Current status / open threads"

**Interfaces:**
- Consumes: everything from Tasks 1-6.
- Produces: `_mock(which, style="hero")` writing `/tmp/mudi_<which>_<style>.png`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_mudi.py`, above the `if __name__` block:

```python
class TestMockPreview(unittest.TestCase):
    def test_mock_writes_a_png_per_page_and_style(self):
        for style in ("hero", "arc"):
            for which in ("signal", "wifi", "system", "eth", "settings"):
                mudi._mock(which, style)
                path = "/tmp/mudi_%s_%s.png" % (which, style)
                self.assertTrue(os.path.exists(path), path)

    def test_history_seeding_finds_any_widget_with_a_hist(self):
        a = mudi.MockApp()
        a.settings.vals["graph_style"] = "arc"
        page = mudi.SystemPage(a)
        page.wire()
        self.assertTrue([w for w in page.widgets if hasattr(w, "hist")],
                        "seeding relies on duck-typing, not an isinstance list")
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python3 -m unittest discover -s tests -v -k MockPreview`
Expected: FAIL — `TypeError: _mock() takes 1 positional argument but 2 were given`.

- [ ] **Step 3: Teach `_mock` about styles**

In `_mock`, change the signature, set the style before building, duck-type the history seeding, and name the output per style:

```python
def _mock(which, style="hero"):
    a = MockApp()
    a.settings.vals["graph_style"] = style
    page = {"wifi": WifiPage, "system": SystemPage, "eth": EthernetPage,
            "settings": SettingsPage}.get(which, SignalPage)(a)
    a.pages = [page]; page.wire()
    for _ in range(40): page.animate()
    import math                                          # synthetic history so graphs render
    for wdg in page.widgets:
        if hasattr(wdg, "hist"):                         # duck-typed: any new style seeds too
            b = getattr(wdg, "value", None)
            if not isinstance(b, (int, float)): b = -100
            wdg.hist = [b + 7*math.sin(i*0.35) + 3*math.sin(i*0.85) for i in range(64)]
    img = Image.new("RGB", (W, H), Theme.BG); d = ImageDraw.Draw(img)
    page.draw(d, Theme, img)                             # img: ScrollPage composites onto it
    out = "/tmp/mudi_%s_%s.png" % (which, style); img.save(out); print("wrote", out)
```

- [ ] **Step 4: Accept a style on the command line**

In `main()`, replace the `--mock` branch:

```python
    if "--mock" in sys.argv:
        which = next((w for w in ("wifi", "system", "eth", "settings") if w in sys.argv), "signal")
        style = next((s for s in GAUGE_STYLES if s in sys.argv), "hero")
        _mock(which, style); return
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python3 -m unittest discover -s tests -v`
Expected: PASS — 62 tests.

- [ ] **Step 6: Render all eight frames and eyeball them**

```bash
for p in signal wifi system eth; do for s in hero arc; do python3 src/mudi.py --mock $p $s; done; done
python3 src/mudi.py --mock settings hero
```

Expected: nine `wrote /tmp/mudi_*.png` lines. Check by eye:
- **Hero frames:** no bottom trace; stats at y172; panel at y208. System's hero reads `LOAD` above its hi/lo; Ethernet's reads `RX`; Signal and WiFi have no curve label.
- **Arc frames:** arc centered at y92; stats at y150; panel at y186; trace at y270. **Signal now has a trace** — this is new and correct.
- **Settings:** Graph style row reads `Hero`; scrollbar visible top-right.

- [ ] **Step 7: Update CLAUDE.md §5**

In §5 "Current UI state", replace the `**Roll-out pending:**` bullet with:

```markdown
- **Graph styles are selectable** (`Settings → Graph style`, uci `mudi.main.graph_style`, default
  `hero`). `GAUGE_STYLES` maps a slug to a `Gauge` subclass; each style declares its slot
  (`TOP`/`HEIGHT`/`STACK_Y`) and whether it already shows history (`SUPPLIES_HISTORY`).
  `MetricPage` lays the stack out below `STACK_Y` and adds a `Trace` only when the style supplies
  none — so **a new style costs one class + one registry entry, and zero page edits**. Changing the
  setting rebuilds only the `MetricPage`s (`App._rebuild_metric_pages`); `SettingsPage` is spared
  so the scroll position survives.
- **Settings scrolls** — `ScrollPage` splits fixed chrome (`widgets`) from scrolling content
  (`rows`, laid out in content coords from y=0). PIL has no clip region, so rows render into a
  viewport sub-image pasted below the chrome; `Row.oy` is the render-thread-only scroll offset.
  Drag-to-scroll is classified by `Gesture` (pure, unit-tested): scroll needs `|dy|>|dx|`, swipe
  needs `|dx|>|dy|`, so they can't both fire.
```

- [ ] **Step 8: Update CLAUDE.md §12**

In §12, remove the `⏳ **Roll `HeroGraph` out**` line, and add to the ✅ line: `selectable graph styles + scrollable Settings`. Then add under §11's repo layout tree, below the `src/` block:

```
├── tests/
│   └── test_mudi.py              ← stdlib unittest suite (python3 -m unittest discover -s tests)
```

- [ ] **Step 9: Deploy and verify on the device**

Per CLAUDE.md — **no scp** (no sftp-server), **no reboot**, **restore gl_screen after**:

```bash
ssh root@<router-ip> 'cat > /usr/bin/mudi.py' < src/mudi.py
ssh root@<router-ip> '/etc/init.d/mudi restart'
```

Confirm on the panel:
1. Settings → **Graph style** steps `Hero` ⇄ `Arc`; all four metric pages change style immediately.
2. `ssh root@<router-ip> 'uci get mudi.main.graph_style'` → the selected slug (persistence).
3. Drag up/down on Settings scrolls; the scrollbar tracks; **About** (the last row) is reachable.
4. Horizontal swipe still changes pages from Settings and from every metric page.
5. Tapping a row still acts (toggle flips, slider sets, stepper steps) — a tap must not be eaten.

Optionally run the suite on the box itself (stdlib-only, so it works there):
`ssh root@<router-ip> 'cd /tmp && python3 -m unittest discover -s tests -v'` (after copying `tests/`).

- [ ] **Step 10: Restore the stock UI**

```bash
ssh root@<router-ip> '/etc/init.d/mudi stop && /etc/init.d/gl_screen start'
```

Expected: the stock GL screen returns. **Do not leave a stray writer on `/dev/fb0`.**

- [ ] **Step 11: Commit** *(only once Kevin green-lights committing)*

```bash
git add src/mudi.py tests/test_mudi.py CLAUDE.md
git commit -m "feat: --mock takes a style; document graph styles + settings scroll"
```

---

## Self-Review Notes

**Spec coverage:** §1 `Gauge` contract + registry → Task 1. §2 `MetricPage` + geometry table + page bindings → Task 2; the System/Ethernet `series_label` resolution → Tasks 2 (binding) + 3 (rendering). §3 setting + `_rebuild_metric_pages` → Task 4. §4 `ScrollPage` + `Row.oy` + touch axis → Tasks 5-6. §5 verification (`--mock` style arg, `hasattr(hist)`, 8 PNGs, on-device) → Task 7. Out-of-scope items are absent by construction.

**Known transient:** after Task 4, Settings has 11 rows (338px) on a 320px panel and the last row is off-screen. Task 5 adds the scroll *mechanism*, but nothing calls `scroll_to` until Task 6 wires the touch loop's vertical axis — so `scroll_y` stays pinned at 0 and the last row remains unreachable on-device through Task 5. **Tasks 4, 5 and 6 must ship together to reach a releasable state.**

**Deviations from spec, both deliberate:**

1. The spec writes the `Gauge` ctor as `(app, value, level, series, unit)`. The plan gives
   `level`/`series`/`unit`/`series_label` defaults so Task 1 is a pure no-behavior-change refactor —
   every existing call site is keyword-based and keeps working. Same interface, safer sequencing.
2. The spec says `ScrollPage.draw` pastes the viewport onto the frame but doesn't say how it reaches
   it. `ImageDraw` exposes no public accessor; the private `d._image` works on Pillow 12 (verified)
   but the device runs whatever Pillow GL bundles. So `Page.draw` gains an `img=None` parameter and
   the frame is passed explicitly (Task 5, Step 5). Three call sites: `App.run`, `_mock`, tests.

**Type consistency check:** `Trace.k`, `StatsRow.y`, `InfoPanel.y`, `Row.place()->self`,
`ActionRow(app, label, action)` all verified against the current source. `Gesture.up` returns
`+1` for a leftward swipe, matching the existing `self.show(self.idx + (1 if dx < 0 else -1))`.
Cumulative expected test counts (11 → 20 → 24 → 34 → 49 → 60 → 62) recounted and corrected.
Task 4's `graph_style` row test reads `getattr(page, "rows", None) or page.widgets` so it passes
both before and after Task 5 converts `SettingsPage` to a `ScrollPage`.
