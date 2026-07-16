# Design: selectable graph styles + scrollable Settings

**Date:** 2026-07-16
**Status:** approved, ready for planning

## Goal

Let the user choose how a metric page's main visual is drawn, starting with the two styles already
in the code (`HeroGraph`, `ArcGauge`), selected by a single global setting. Adding the setting row
overflows the Settings page, so Settings gains drag-to-scroll.

Two decisions, made up front:

- **Style is global** — one choice for all pages, one uci key, one settings row. Matches the
  `Theme` philosophy (one palette, one look). Per-page styles were rejected as YAGNI for a 4-page
  panel.
- **Settings scrolls** — rather than splitting into two pages or nesting sections. Settings will
  keep growing; the scroll mechanism is the point.

## The problem the two styles pose

They are not drop-in swappable today, because each implies a different page layout:

- `HeroGraph` is 118px tall and **is** the trend line, so `SignalPage` omits the bottom `Trace` and
  pushes `StatsRow`/`InfoPanel` down to y=172/208.
- `ArcGauge` is ~100px and shows **no** history, so its pages put `StatsRow` at 150, `InfoPanel` at
  184, and add a `Trace` at 270 to compensate.

So a style is not just a widget — it decides its slot height and whether the page still needs a
separate history trace. The design makes that an explicit contract.

## 1. The `Gauge` contract and style registry

Both widgets become subclasses of a `Gauge` base with a uniform constructor. Each style ignores the
bindings it doesn't use (Hero ignores `level`, Arc ignores `series`), which is what keeps pages
free of style-specific config.

```python
class Gauge(Widget):
    TOP = 32; HEIGHT = 118; STACK_Y = 172
    SUPPLIES_HISTORY = False
    LABEL = "Gauge"                       # shown in the settings stepper
    def __init__(self, app, value, level, series, unit, series_label=None):
        super().__init__(app)
        self.k_value, self.k_level, self.k_series = value, level, series
        self.unit, self.series_label = unit, series_label
```

| style | `LABEL` | `TOP` | `HEIGHT` | bottom | `STACK_Y` | `SUPPLIES_HISTORY` |
|---|---|---|---|---|---|---|
| `HeroGraph` | `Hero` | 32 | 118 | 150 | 172 | `True` |
| `ArcGauge` | `Arc` | 42 | 100 | 142 | 150 | `False` |

`ArcGauge` keeps its existing `cx=120, cy=92, r=50`, which spans y 42–142 — hence `TOP=42`,
`HEIGHT=100`. `TOP`/`HEIGHT` are descriptive only; `STACK_Y` is what drives layout.

```python
GAUGE_STYLES = OrderedDict([("hero", HeroGraph), ("arc", ArcGauge)])
```

The registry drives both the settings stepper and page construction. **A third style costs one
class plus one registry entry, and zero page edits.**

`App.gauge_cls()` resolves the current class, defaulting to `HeroGraph` on an unknown value:

```python
def gauge_cls(self):
    return GAUGE_STYLES.get(self.settings.get("graph_style"), HeroGraph)
```

## 2. `MetricPage` — pages declare bindings, the style decides geometry

Signal/WiFi/System/Ethernet stop hardcoding y=150/172/184/208/270 and subclass `MetricPage`, which
lays the stack out below `gauge.STACK_Y` and adds a `Trace` only when the style supplies no history.

```python
class MetricPage(Page):
    HEADER = {}; GAUGE = {}; STATS = []; PANEL = None
    STATS_TO_PANEL = 36; PANEL_H = 74; PANEL_TO_TRACE = 10; TRACE_H = 36

    def build(self):
        a = self.app
        self.add(Header(a, **self.HEADER))
        g = self.add(a.gauge_cls()(a, **self.GAUGE))
        self.add(StatsRow(a, g.STACK_Y, self.STATS))
        py = g.STACK_Y + self.STATS_TO_PANEL
        self.add(InfoPanel(a, 12, py, *self.PANEL))
        if not g.SUPPLIES_HISTORY:
            self.add(Trace(a, self.GAUGE["series"],
                           y=py + self.PANEL_H + self.PANEL_TO_TRACE, h=self.TRACE_H))
```

A concrete page becomes pure key wiring:

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
```

### Geometry vs. today

- **Hero:** stats 172, panel 208, no trace — **exact match** to the current `SignalPage`.
- **Arc:** stats 150, panel **186**, trace 270 (h=36, ends 306).

The arc pages' `InfoPanel` **moves down 2px** (184 → 186). That is the cost of normalizing the
gap to a single `STATS_TO_PANEL` constant; it is imperceptible and is accepted deliberately rather
than carrying a per-style fudge factor.

`SignalPage` in Arc style now **gains** a `Trace` at 270 graphing `signal.rsrp`. This is correct:
the arc shows no history, so the page needs one.

### Page bindings

| page | `value` | `level` | `series` | `unit` | `series_label` |
|---|---|---|---|---|---|
| Signal | `signal.rsrp` | `signal.level` | `signal.rsrp` | `dBm  RSRP` | — |
| WiFi | `wifi.signal` | `wifi.level` | `wifi.signal` | `dBm  LINK` | — |
| System | `batt.pct` | `batt.level` | `sys.load` | `%  BATTERY` | `LOAD` |
| Ethernet | `eth.speed` | `eth.level` | `eth.rxn` | `LINK` | `RX` |

### The System/Ethernet pairing

Signal and WiFi headline the metric they graph, so their hi/lo needs no label. System headlines
`batt.pct` but traces `sys.load`; Ethernet headlines `eth.speed` but traces `eth.rxn`. Under Arc
those are two separate widgets and the mismatch is invisible. Under Hero they merge into one, so a
big battery % would sit over an unlabeled load graph.

**Resolution (approved):** keep the existing pairings and add an optional `series_label` to
`HeroGraph`. When set, it draws above the hi/lo block so it reads `LOAD / hi 1.4 / lo 0.2`. This
preserves both of today's looks exactly and stays honest about what the curve is.

`HeroGraph.draw` layout inside its slot (`y` = slot top):

| element | offset | font |
|---|---|---|
| value | `y+12` | `mono[32]` |
| unit | `y+50` | `mono[11]` |
| `series_label` (only when set) | `y+66` | `mono[9]`, `DIM` |
| hi | `y+80` | `mono[9]` / `mono[11]` |
| lo | `y+96` | `mono[9]` / `mono[11]` |

When `series_label` is `None` the layout is byte-for-byte today's.

## 3. The setting

- New uci key **`graph_style`**, default **`"hero"`**, added to `Settings.DEFAULTS`.
- New row, placed in the display group after "Awake on charge" and before "Default page":

  ```python
  StepperRow(a, "Graph style", "graph_style", list(GAUGE_STYLES),
             fmt=lambda v: GAUGE_STYLES[v].LABEL, wrap=True)
  ```

- `App.apply_setting("graph_style", v)` calls `_rebuild_metric_pages()`.

```python
def _rebuild_metric_pages(self):
    for i, p in enumerate(self.pages):
        if isinstance(p, MetricPage):
            live = (p is self.current)
            if live: p.unwire()
            self.pages[i] = type(p)(self)
            if live: self.current = self.pages[i]; self.current.wire()
    self.wake.set()
```

Only `MetricPage` instances are rebuilt. `SettingsPage` survives, so the user's scroll position and
the row they just tapped do not vanish underneath them. `self.current` is never set to `None`, so
the render thread can never observe a half-rebuilt state — it sees either the old page or the new
one. The old page is unwired before the new one is wired, so sources see subscriber counts drop and
rise rather than double-subscribing.

## 4. Scrolling

### `ScrollPage`

Splits **fixed chrome** (`self.widgets` — the Banner) from **scrolling content** (`self.rows`).

```python
class ScrollPage(Page):
    VIEW_TOP = 30
    VIEW_H = H - VIEW_TOP                 # 290
    def __init__(self, app):
        self.rows = []; self.scroll_y = 0; self.content_h = 0
        super().__init__(app)             # Page.__init__ calls build()
    def add_row(self, r): self.rows.append(r); return r
    def max_scroll(self): return max(0, self.content_h - self.VIEW_H)
    def scrollable(self): return self.max_scroll() > 0
    def scroll_to(self, v):
        nv = max(0, min(self.max_scroll(), v))
        if nv != self.scroll_y: self.scroll_y = nv; self.app.wake.set()
```

`wire`/`unwire`/`animate` cover `self.widgets + self.rows`.

**Rows move to content coordinates.** They are placed from `y=0` (today: `y=30`), and
`content_h` is the total stack height. Screen position is derived at draw/touch time.

### Drawing

PIL has no clip region, so a half-scrolled row drawn straight to the frame would bleed over the
banner. Instead:

1. Draw fixed chrome onto the frame.
2. Build a `240 × VIEW_H` viewport sub-image, draw rows into it, paste at `VIEW_TOP`. A row is
   clipped exactly by the image bounds — no bleed possible.
3. Draw the scrollbar onto the frame, only when `scrollable()`.

Rows need to render at `self.y - scroll_y` within the viewport image. `Row` gains an `oy` attribute
(class default `0`, so any non-scrolled use is unaffected); `ScrollPage.draw` sets `row.oy =
self.scroll_y` before drawing, and every `Row.draw` derives `y = self.y - self.oy` instead of using
`self.y` directly. `oy` lives on `Row` only — `Gauge` and the other widgets never scroll.

**`oy` is written only by the render thread.** The touch thread does its own translation via
`ScrollPage.on_touch` and never reads `oy`, so there is no race.

Scrollbar: 3px wide at the right edge, spanning `VIEW_TOP..H`; thumb height
`VIEW_H * VIEW_H / content_h`, offset `VIEW_TOP + scroll_y / content_h * VIEW_H`.

### Touch

`ScrollPage.on_touch` translates screen-y to content-y before dispatch:

```python
def on_touch(self, x, y):
    for w in self.widgets:
        if hasattr(w, "hit") and w.hit(x, y) and getattr(w, "action", None):
            w.action(); return True
    if y < self.VIEW_TOP: return False
    cy = y - self.VIEW_TOP + self.scroll_y
    for r in self.rows:
        if r.on_touch(x, cy): return True
    return False
```

`App._touch` gains a vertical axis. New state: `scroll0` (scroll at touch-down) and `scrolling`.

- **on `BTN_TOUCH` down:** record `x0, y0`; `scrolling = False`; `scroll0 = page.scroll_y` if the
  current page is a scrollable `ScrollPage` and no modal is open.
- **on `ABS_Y` while down** (scrollable page, no modal): `dy = y - y0`; latch
  `scrolling = True` once `abs(dy) > SCROLL_TOL (8)` **and** `abs(dy) > abs(x - x0)`; while
  latched, `page.scroll_to(scroll0 - dy)` — live follow.
- **on `BTN_TOUCH` up:** if `scrolling`, clear it and **swallow** the event (no tap dispatch).
  Otherwise the existing order is unchanged: modal → horizontal swipe (`abs(dx) > 50 and
  abs(dx) > abs(dy)`) → tap.

Because scrolling requires `abs(dy) > abs(dx)` and horizontal swipe requires `abs(dx) > abs(dy)`,
the two gestures cannot both fire. Page nav is untouched. `SliderRow`'s tap-on-x still works,
since a mostly-horizontal press never latches `scrolling`.

Live follow relies on `scroll_to` calling `wake.set()`; the render loop's `wake.wait(0.2)` makes it
responsive, and a scroll frame is a normal full redraw (~7.75ms), well inside budget.

### `SettingsPage`

```python
class SettingsPage(ScrollPage):
    def build(self):
        a = self.app
        self.add(Banner(a, "Settings"))
        rows = [...]                      # existing 10 + Graph style = 11
        y = 0
        for r in rows:
            self.add_row(r.place(y)); y += Row.H
        self.content_h = y
```

**Scale today:** viewport 290px, content 11 × 28 = 308px → **18px of scroll**. Marginal now,
accepted deliberately: Settings will keep growing and the mechanism is what unblocks that.

## 5. Verification

`mudi.py --mock <page>` already renders any page to a PNG off-device (it produced
`docs/images/*`). Two changes make it cover this work:

- Accept a style argument: `--mock signal arc` → writes `/tmp/mudi_signal_arc.png`. Implemented by
  setting `a.settings.vals["graph_style"] = style` before building the page (`MockApp` subclasses
  `App`, so it inherits `gauge_cls()`).
- Replace the history seeding's `isinstance(wdg, (HeroGraph, Trace))` with `hasattr(wdg, "hist")`,
  so any future style seeds automatically.

Coverage:

1. **4 pages × 2 styles = 8 PNGs**, eyeballed off-device. Assert the geometry table above: Hero →
   stats 172 / panel 208 / no Trace; Arc → stats 150 / panel 186 / Trace 270.
2. **Settings rendered at several scroll offsets** (0, 9, 18 = max), confirming no bleed over the
   banner and a correctly sized/positioned scrollbar.
3. **On-device:** deploy per CLAUDE.md (`ssh host 'cat > /usr/bin/mudi.py' < src/mudi.py`, then
   `/etc/init.d/mudi restart`), confirm the style stepper switches all four pages live, the uci
   value persists, drag-scroll tracks the finger, and horizontal swipe still changes pages.
   **Restore `gl_screen` afterwards.**

## Out of scope (YAGNI)

- Per-page style overrides.
- Scroll momentum / inertia, or a fling gesture.
- Making non-Settings pages scrollable.
- New styles beyond the two in place — the registry is the deliverable that makes them cheap.
