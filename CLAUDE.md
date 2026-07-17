# MudiUI — custom front-panel UI for the GL-E5800 "Mudi"

**Goal:** a community open-source UI that draws to the Mudi's built-in 240×320 front LCD, as a
companion to (and eventual replacement for) GL's stock on-screen UI (`gl_screen`).

Everything here was reverse-engineered from the live device (starting 2026-07-15). **Trust the
box over this doc if they ever disagree** — then fix the doc.

## Working agreements
- **Deploy transfer:** the box has **no sftp-server**, so `scp` fails — use `ssh host 'cat > /path' < file`.
- **Commit only when Kevin asks.** This is a git repo (single `main`).
- **Don't reboot the Mudi remotely** — it's a travel router; a reboot can drop the cellular ssh
  link. Cold-boot tests happen when Kevin is at the device.
- **Always restore `gl_screen` after testing** — `/dev/fb0` is single-owner; a stray process
  left running blocks the stock UI.

---

## 1. Device access
- **SSH:** `ssh root@<router-ip>` (GL default `192.168.8.1`; key auth). Remote shell is BusyBox `ash`.
- **Hardware:** GL.iNet **GL-E5800** ("Mudi", 5G travel router), Qualcomm **SDXPINN**,
  `aarch64_cortex-a53` (quad Cortex-A55), GL firmware **4.8.5** / OpenWrt 23.05.4, kernel 5.15.170.
  Userspace is **musl** (`/lib/ld-musl-aarch64.so.1`). Modem: **Quectel RG650V**.
  Resources are ample: ~1 GB RAM free, ~2.3 GB free `/overlay`.

## 2. The display hardware (what the app targets)

| Property | Value |
|---|---|
| Framebuffer | **`/dev/fb0`** (char 29,0), driver `fb_gc9307c` |
| Resolution | **240 × 320, portrait** (`virtual_size 240,320`, mode `U:240x320p-0`) |
| Pixel format | **16 bpp, RGB565**, little-endian |
| Stride | **480 bytes/row** (240 × 2) → framebuffer = 240×320×2 = **153,600 bytes** |
| Touch | **`/dev/input/event0`**, `chsc_cap_touch` (capacitive, I2C `1-002e`), `EV=b` (EV_KEY+EV_ABS). ABS_X/ABS_Y report **0–240 / 0–320 = panel pixels, no calibration** |
| Backlight | `/sys/class/backlight/soc:backlight/brightness`, `max_brightness 120`. GL applies a **+20 offset** (`reference/gl_screen_scripts/platform.sh` `set_brightness`) → usable ~20–120. `bl_power` = on/off |
| Other inputs | `event1` = `pmic_pwrkey` (front/power button — **exclusively grabbed by `lpm`, unreadable by us**), `event2` = `pmic_resin` (reset), `event3/4` = `aw_sar0` SAR RF sensors. **No free/spare button exists** |

- **evdev input devices allow multiple concurrent readers; the framebuffer is single-owner.**
  This asymmetry is load-bearing (see the toggle watcher in §5).

- **⏰ NO USABLE RTC — the wall clock STEPS FORWARD BY HOURS a few seconds after every boot.**
  `hwclock -r` reads *1970*; the box boots with a stale `time.time()` and cellular/NTP then steps
  it. Measured live 2026-07-17: `mudi.py` started at uptime 71s stamped `Jul 16 21:40`, while the
  box had been up 714s and the wall clock read `Jul 17 07:31` — a **+34,900s jump landing seconds
  after MudiUI starts**. `time.monotonic()` is immune (it tracks `/proc/uptime`).
  **NEVER measure a duration with `time.time()` on this box** — idle timers, long-press holds,
  throughput `dt`. Anything that captures a start before the step reads a ~9.7h elapsed after it.
  This caused the panel to blank instantly on every cold boot despite `screen_timeout=300` (fixed
  2026-07-17: `App._idle_expired`, `App.last_touch`, `App.run`'s `t0`, `EthernetSource.poll`'s
  `dt`, and `mudi-watch.py`'s `down_t` all use `time.monotonic()`). A wall-clock read is only
  correct for an absolute timestamp you intend to display.

RGB565 packing (numpy, vectorized ~5 ms/frame — this Pillow has **no `BGR;16` packer**):
`((r&0xF8)<<8) | ((g&0xFC)<<3) | (b>>3)` stored as little-endian `<u2`.

Sanity-draw (after stopping gl_screen + unblanking, §5): `head -c 153600 /dev/zero > /dev/fb0`.

## 3. How GL's stock screen works (`gl_screen`) — and why we can't extend it
- Package **`gl-sdk4-screen-large`**, **closed source** (compiled ipk only from GL's binary feed).
- **`/usr/bin/gl_screen`** = a 2.3 MB stripped aarch64 ELF built on **LVGL**. Copy:
  `reference/gl_screen.aarch64.elf` (for `strings`/analysis). Uses LVGL's **fbdev** driver on
  `/dev/fb0` + **evdev** driver on `event0`. Launched by `/etc/init.d/gl_screen` (procd,
  `respawn`, `nice -20`) with `-c /tmp/gl_screen/config`.
- Editable data under `/etc/gl_screen/` (copies in `reference/`): `config/.../layout` (1002
  geometry keys, styling only), `language/text/default` (828 label strings), `image/*.png` (~170
  icons), `scripts/*.lua` + `platform.sh` + `common.sh` (thin glue).
- **The menu tree, navigation, and every button→action are compiled C inside the stripped ELF.**
  The layout file has zero logic; there is **no embedded Lua** in `gl_screen` (the one `.lua` is a
  tiny `popen` bridge for timing queries).
- **Consequence:** editing config can **relabel / re-icon / move / hide** existing items, but you
  **cannot add a page or bind custom logic** inside gl_screen. LVGL only repaints **dirty
  regions** (relevant to any freeze/restore trick).

## 4. Routes evaluated and RULED OUT (don't re-derive)
- ❌ **Add items to gl_screen** — impossible without source (closed binary).
- ❌ **Rebuild GL's screen app** — source not published anywhere.
- ❌ **Hijack a physical button** — no spare button; the front button is `pmic_pwrkey`, consumed
  by `lpm` + gl_screen. Nothing free to bind to. (We instead use a **touch long-press gesture**, §5.)
- ❌ **SIGSTOP→SIGCONT freeze/thaw of gl_screen** to swap fast — gl_screen thaws but **never
  repaints or responds** (verified: fb md5 stable 40 s, clock frozen). Abandoned; we kill +
  cold-start gl_screen for the stock-UI direction instead.
- ⚠️ **Hijack an on-screen toggle** (watch uci with `inotifyd`) — works but only reacts to
  *existing* toggles; no new UI. Parked in favor of our own framebuffer app.

## 5. The app: our own renderer on the framebuffer (`src/mudi.py`)

Since gl_screen can't be extended, MudiUI is a **standalone app that draws to `/dev/fb0`** and
reads touch from `event0` — we co-opt the panel, not gl_screen's internals.

### Coexistence + the CRITICAL unblank
`gl_screen` opens `/dev/fb0` exclusively. To take the panel:
```sh
/etc/init.d/gl_screen stop            # procd stops it AND suspends respawn (do NOT bare-kill it)
echo 0 > /sys/class/graphics/fb0/blank  # REQUIRED: fbdev stays blanked after stop; this wakes the panel
# ... draw ...
/etc/init.d/gl_screen start           # restore stock UI
```
**Unblank is mandatory** (verified 2026-07-15): after `gl_screen stop` the panel stays blanked at
the fbdev layer — pixels land in memory but nothing shows. Backlight/`bl_power` and
`lpm set_screen '{"action":"active"}'` are **not** sufficient; only `echo 0 > .../fb0/blank`
lights it. An mmap PoC would issue `FBIOBLANK` unblank directly. (`lpm` already ships
`system_sleep_enable='0'` / `phy_sleep_time='0'`, so no lpm sleep tweaks needed for tests.)

### ⚡ Perf — pure Python is fast enough (no C hot path)
Live dashboard rendered purely in Python straight to `/dev/fb0`:

| stage | median ms |
|---|---|
| PIL draw (whole frame) | 2.6 |
| numpy RGB565 pack | 5.0 |
| `write()` to /dev/fb0 | 0.08 |
| **total** | **7.75 → ~129 FPS** |

Stack = **PIL draws → numpy packs RGB565 → `write()` fb0 → python-evdev reads touch**. A C helper
(`libmudi`) is only ever a *targeted* later optimization (pack 5 ms→~0.1 ms, or mmap
double-buffer if tearing appears), never the foundation.

### Architecture — event-driven object framework
Small object model that keeps many instrumented pages consistent and polls nothing off-screen:
- **`Theme`** — one palette + font set (gl_screen TTFs at `/etc/gl_screen/language/ttf/`); every
  widget draws through it → visual consistency.
- **`DataSource`** — a *self-gating subject*: owns a per-key subscriber list, runs its polling
  thread **only while subscriber count > 0**, and `_emit()`s **only on change** (deduped). Sources
  emit display-ready strings (`cell.band`→`"n71"`) so widgets stay dumb.
- **`Widget`** (`Header`, `ArcGauge`, `HeroGraph`, `StatsRow`, `InfoPanel`, `Trace`, `Button`) —
  subscribes its bus keys on `wire()`, unsubscribes on `unwire()`, redraws reactively, may
  `animate()`. Parameterized by bus keys + labels → the same widget renders different data on
  different pages (real reuse; new pages add zero widget classes).
- **`Page`** — bundles widgets; a concrete page is ~6 lines of key wiring. **`App`** holds
  `pages[]`; `show()` unwires the old page (its sources lose subscribers → sleep) and wires the new.
- **Demand-driven polling is emergent & verified:** on SignalPage only `CellularSource` polls (3
  threads: render, touch, cellular); other sources idle until you swipe to them. ~0–1% CPU /
  ~31 MB RSS.
- **This is the runtime the future JSON layer targets:** `key="signal.rsrp"` widget bindings are
  already declarative — `Page.build()` can later be generated from JSON.

**Render-on-change (dirty-skip):** a 30 Hz loop recomputes a small render signature and only
draws+packs+writes when it differs. The panel is static ~20 s at a time (modem `cell_info`
refreshes on a regular ~20 s cycle; ubus latency ~8 ms), dropping CPU from **31%→1%** of a core.
Gauges ease at 30 fps only while animating.

### Current UI state
- **Four pages / four sources live:**
  - `SignalPage`←`CellularSource` — `signal.*`, `cell.*`, `net.mode`, `sim.carrier/slot`. Headlines
    and graphs the same key (`signal.rsrp`), so it needs no `series_label` (unlike System/Ethernet
    below, which headline a different metric than they graph).
  - `WifiPage`←`WifiSource` — `iwinfo devices`→`iwinfo info` per device (Client=uplink vs
    Master=AP) + `gl-clients list` online count. This box runs as a **repeater** (`wlan4` station
    uplink, `wlan0` local AP). `_san()` strips emoji the panel TTFs lack.
  - `SystemPage`←`SystemSource` — battery via `mcu`, CPU temp from
    `/sys/class/thermal/thermal_zone15-18`, load/mem/uptime from `ubus system info`.
  - `EthernetPage`←`EthernetSource` — eth0 carrier/speed from `/sys/class/net`, LAN IP + live
    rx/tx throughput from `br-lan` `/statistics/*_bytes` deltas.
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
- **Swipe navigation:** touch thread distinguishes tap (→button) from horizontal swipe
  (→prev/next page); page-indicator dots top-center.

### Stock-UI toggle: gesture watcher + resident pause/resume
The on-screen "STOCK UI" button was **removed** in favor of a touch gesture, because we can't use
the physical button. Two cooperating pieces:

- **`src/mudi-watch.py`** (service `mudi-watch`, always on) — opens `event0` as a **second
  reader** (works whether MudiUI *or* gl_screen owns the panel). A **~1.6 s long-press held still**
  (`HOLD=1.6`, movement >`MOVE_TOL=40 px` reclassifies as a swipe and cancels; `DEBOUNCE=1.5 s`)
  fires `toggle()`: `kill -USR1` the running `mudi.py`, or `/etc/init.d/mudi start` as fallback.
- **`mudi.py` toggle model** — MudiUI **stays resident** and flips on SIGUSR1 (sets a
  `_toggle_req` event; the render loop skips drawing while `paused`):
  - `_flash()` — full-screen cyan (`0x07FF`) flash confirms the touch (great timing feedback).
  - **→ Stock UI** (`_release_panel`): draw a "Stock UI / one moment" notice, then
    `/etc/init.d/gl_screen start` (cold start — gl_screen has a long startup, hence the notice).
  - **→ MudiUI** (`_take_panel`, overlap-free): `kill -STOP $(pidof gl_screen)` (instant freeze so
    it stops drawing), `echo 0 > .../fb0/blank`, then background `/etc/init.d/gl_screen stop` to
    reap it. This fixed the "both UIs active at once" overlap (gl_screen takes ~4 s to exit on
    `stop` and keeps drawing until then).
  - Freeze/thaw (SIGSTOP+SIGCONT to keep gl_screen warm) was tried and **abandoned** — gl_screen
    never repaints after thaw (§4).

### Operational gotchas (managing the live app over ssh)
- No `setsid`/`nohup`-friendly `setsid` — launch persistently with
  `nohup python3 /tmp/mudi.py </dev/null >/tmp/mudi.out 2>&1 &`.
- **BusyBox `ps`/`pgrep -f` count threads and match the invoking shell's cmdline.** For a true
  process count use `pidof python3` then check `/proc/<pid>/task`. Clean restart of a stray writer:
  `for p in $(pidof python3); do grep -q mudi.py /proc/$p/cmdline && kill -9 $p; done`.
- To iterate on code under the service: `cat > /usr/bin/mudi.py` then `/etc/init.d/mudi restart`.
- **`Settings → Start on boot` really does enable/disable the procd services** (`apply_setting`
  runs `/etc/init.d/{mudi,mudi-watch} {enable,disable}`), removing the `/etc/rc.d/S9*` symlinks.
  Its effect is **invisible until the next reboot** — the panel looks identical. If MudiUI
  doesn't come back after a cold boot, check `uci get mudi.main.start_on_boot` and
  `ls /etc/rc.d/ | grep mudi` FIRST; a missing `S99mudi` is the whole story (hit 2026-07-16).
- **Screenshot the live panel** (no scp/base64 on the box): `ssh root@<ip> 'head -c 153600
  /dev/fb0' > fb.raw`, then unpack `<u2` RGB565 with numpy. Best proof the panel truly renders.

### Install gotchas (baked into `src/install.sh`)
- **`python3-pillow` clashes with `gl-sdk4-screen-large`** on `/usr/lib/libfreetype.so.6` (GL
  bundles 6.20.4). Install with **`opkg install --nodeps python3-pillow`** so it reuses GL's
  freetype (never `--force-overwrite` — risks the stock UI). libjpeg.so.62 is already present.
- OpenWrt splits the stdlib: beyond `python3-light` you need `python3-numpy`, `python3-urllib`
  (numpy), `python3-logging` (PIL), `python3-ctypes`, `python3-cffi`, `python3-evdev`.
- **Verified on-box 2026-07-16: python 3.11.7, numpy 1.24.3, Pillow 9.5.0.** Two traps follow:
  - **`Image.getdata()` is deprecated (Pillow 12) but `Image.get_flattened_data` — the
    replacement its warning names — does NOT exist on Pillow 9.5.0.** Taking the warning's
    advice would `AttributeError` on the box. Read pixels with numpy (`np.asarray(img)`)
    instead; numpy is already a hard dep. Same reasoning bans `ImageDraw._image` (private,
    version-fragile) — pass the frame explicitly, which is why `Page.draw` takes `img`.
  - **`python3-unittest` is NOT installed**, so `python3 -m unittest` fails on the box. The
    test suite runs on the dev machine; for on-device checks write a plain-`assert` script.

### Future build tiers / toolchain (for when the PoC graduates)
- **Tier 1 — raw framebuffer, zero toolchain:** static aarch64-musl C binary that `mmap`s
  `/dev/fb0` + reads 16-byte `input_event`s. RGB565 as above; `BTN_TOUCH`=down/up, `ABS_X/Y` +
  `ABS_MT_*` for coords.
- **Tier 2 — LVGL** (same lib GL uses, guaranteed parity): upstream `github.com/lvgl/lvgl` with
  fbdev+evdev drivers (`lv_linux_fbdev_create("/dev/fb0")`, `lv_evdev_create(...,
  "/dev/input/event0")`), `LV_COLOR_DEPTH 16`, 240/320. Reference port: `github.com/gl-inet/gl-lvgl`
  (BE3600 — adapt panel specifics). FreeType is on the box.
- **SDK for an ipk:** `github.com/gl-inet/sdk` (precompiled) → `aarch64_cortex-a53` musl ipks.
  Verify triple `aarch64-openwrt-linux-musl` before assuming QSDK/BE3600 binaries port (E5800 =
  SDXPINN, a different SoC). Quick static Tier-1 binaries: any musl.cc cross-gcc.

## 6. Data sources (verified live via `ubus`; Python calls these)
Key objects: `cellular.*`, `mcu`, `lpm`, `gl-clients`, `system`, `network.interface.*`.
- **Battery / temp:** `ubus call mcu status` → `charge_percent`, `charging_status`, `fastcharge`,
  `temperature`.
- **Serving cell:** `ubus call cellular.network info '{"bus":"cpu","slot":N}'` → `cell_info`: `id`
  (NR cell identity hex), `mode` (`NR5G-SA FDD`…), `band` (int → `n71`), `rsrp`/`rsrq`/`sinr`
  (+`*_level` 0–5), `dl_bandwidth`, `tx_channel` (NR-ARFCN). Carrier MHz from NR-ARFCN (FR1,
  N≤599999): `MHz = ARFCN * 5 / 1000` (e.g. 127490 → 637.5 MHz).
- **⚠️ Dual-SIM active carrier** (both SIMs stay registered — don't trust a global operator):
  1. active slot = `ubus call cellular.modem status '{"bus":"cpu"}'` → `current_sim_slot`.
  2. carrier = `ubus call modem.CPU.AT get_result_AT '{"cmd":"AT+QSPN","sub_id":<active_slot>}'`.
     **`sub_id` MUST equal the active slot** — default `sub_id=0` returned the *wrong* SIM's
     operator. SIM home network is in `cellular.sim info` (`imsi`/`mcc`/`mnc`); serving network to
     *display* comes from the `sub_id`-scoped AT query, not the SIM's home MCC/MNC.
- **Throughput:** `ubus call cellular.collect get_traffic '{"bus":"cpu"}'`; Wi-Fi clients:
  `gl-clients`; interface stats: `network.interface.*` / `/proc/net/dev`.

## 7. Modem control (AT via ubus) — band/cell lock
AT commands: `ubus call modem.CPU.AT get_result_AT '{"cmd":<AT>, "sub_id":<slot>, "timeout":<s>}'`.
**Inner quotes need proper JSON escaping — build the payload with Python `json.dumps`.**
- **Band lock:** `AT+QNWPREFCFG="nr5g_band",<list>` (SA), `="nsa_nr5g_band",<list>` (NSA),
  `="lte_band",<list>`, `="mode_pref",<...>`. Colon-separated band lists. **Persists in NV across
  reboots.**
- **Cell lock:** `AT+QNWLOCK` (lock to a specific PCI/ARFCN).
- **Current state — locked to n71** (applied 2026-07-15, verified `nr5g_band=71` &
  `nsa_nr5g_band=71`; stays on NR5G-NSA b71, and signal actually improved: RSRP -105→-98, SINR
  2→8). **Restore full band lists:**
  - SA: `AT+QNWPREFCFG="nr5g_band",2:5:7:12:13:14:25:26:29:30:38:41:48:66:70:71:77:78`
  - NSA: `AT+QNWPREFCFG="nsa_nr5g_band",2:5:7:12:14:25:26:30:38:41:48:66:71:77:78`

## 8. MCU side channel (optional)
The `mcu` ubus object (readable Lua daemon `/usr/bin/mcu`, copy `reference/mcu.eco.lua`, run by
`eco`) exposes `send_custom_msg`, `cmd_json`, `cmd_string`, `status`, `set_warning`, …
`send_custom_msg` is how GL flashes toast text (`mcu_send_message()` at
`/lib/functions/gl_util.sh:2245` writes JSON to `/dev/ttyS0`; the toast helper is gated to
`model=e750`, so validate the `mcu` ubus route live on the E5800). Useful for MCU/battery/temp data.

## 9. Deployment — procd services + installer
Two procd services, both persisted in `/etc/sysupgrade.conf` (survive firmware upgrades; normal
reboots persist via `/overlay`):
- **`mudi`** (`/etc/init.d/mudi`, source `src/mudi.init`): `START=99` (after gl_screen → grabs the
  panel last), `respawn 3600 5 5`, runs `python3 /usr/bin/mudi.py --service`. In service mode a
  stock-UI request runs `/etc/init.d/mudi stop` (procd stops us → `finally` runs `gl_screen
  start`) — must **not** just exit, or `respawn` re-grabs the panel.
- **`mudi-watch`** (`/etc/init.d/mudi-watch`, source `src/mudi-watch.init`): `START=98`,
  `respawn 3600 5 0` (always restart — it's the way back). Never owns the framebuffer.
- **Installer** — `src/install.sh` (run **on the box**: `cd src && sh install.sh`): device guard
  (root + `240,320` `/dev/fb0` + E5800 model, `MUDI_FORCE=1` overrides model only), installs only
  missing opkg deps (pillow via `--nodeps`), deploys the 4 files, enables both services,
  idempotently registers them in `sysupgrade.conf`, starts them. **Idempotent → re-running is the
  update path.** `src/uninstall.sh` reverses it and restores gl_screen. *(Written 2026-07-16; not
  yet live-tested against the Mudi.)*
- **Manage:** `/etc/init.d/{mudi,mudi-watch} {start,stop,restart,enable,disable}`.

## 10. Persistence & risk
- Files outside `/etc/config/` are wiped by a firmware upgrade unless in `/etc/sysupgrade.conf`; a
  **factory/app reset** wipes them regardless — keep sources in this repo and re-deploy.
- An **OTA of `gl-sdk4-screen-large`** overwrites any `/etc/gl_screen/*` edits.
- `/dev/fb0` is **single-owner** — a stray process blocks gl_screen and vice versa.
- The Mudi is a **travel router on cellular** — reachability is intermittent by design.

## 11. Repo layout
```
MudiUI/
├── CLAUDE.md                     ← this file (project context + working agreements)
├── src/
│   ├── mudi.py                   ← the app (Theme, DataSources, Widgets, Pages, App, toggle model)
│   ├── mudi-watch.py             ← always-on long-press toggle watcher (second event0 reader)
│   ├── mudi.init / mudi-watch.init  ← procd init scripts
│   ├── install.sh / uninstall.sh ← on-device installer / uninstaller (device-guarded, idempotent)
│   ├── mudi_signal_live.py       ← earlier single-file monolith (reference)
│   ├── bench.py                  ← render perf spike
│   └── sample_modem.py           ← modem cadence spike
├── tests/
│   └── test_mudi.py              ← stdlib unittest suite (python3 -m unittest discover -s tests)
└── reference/
    ├── gl_screen.aarch64.elf     ← stock UI binary (stripped) for strings/analysis
    ├── mcu.eco.lua               ← readable MCU daemon (Lua/eco)
    ├── config/                   ← layout.dpr (1002 geometry keys), layout.ndpr,
    │                                language_text_default (828 strings), default_generic
    └── gl_screen_scripts/        ← platform.sh (brightness/blank glue), common.sh, *.lua
```

## 12. Current status / open threads
- ✅ Panel ownership + unblank, 129 FPS pure-Python render, event-driven framework, 4 live pages,
  demand-driven polling, dual-SIM active-carrier resolution, procd services, hero redesign
  (Signal), gesture toggle + resident pause/resume, installer, n71 band lock, selectable graph
  styles + scrollable Settings.
- ⏳ **Live-test the installer** on the Mudi (written but unrun); confirm idempotent re-run, then
  uninstall → gl_screen returns.
- ⏳ **Cold-boot test** the boot-time service start (done at the device, not remotely).
- ✅ **Deployed + live on the box 2026-07-16**: renders real data as a service; verified by
  reading `/dev/fb0` back. All 4 pages x both styles, the ScrollPage paste, no chrome bleed
  and the last Settings row all pass a plain-`assert` smoke script against Pillow 9.5.0.
- ⏳ **Cold-boot test** still pending (services re-enabled 2026-07-16 after `start_on_boot`
  had been switched off); and `Gesture.TOL` (8px) still needs a real finger — a tap that
  drifts >8px vertically latches as a scroll and is swallowed.
  `Gesture.TOL` (8px) may need tuning with a real finger.
- 🔭 Future: JSON scripting layer over the existing declarative widget bindings; optional
  `libmudi` C pack; ipk packaging.
