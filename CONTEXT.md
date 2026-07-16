# MudiUI — custom UI proof-of-concept for the GL-E5800 Mudi front screen

**Goal:** build a proof-of-concept UI that draws to the Mudi's built-in front LCD, as an
alternative/companion to GL's stock on-screen UI. This folder is a fresh workspace —
start here. It is **not** part of the `~/router` docs repo.

Everything below was reverse-engineered from the live device on 2026-07-15. Trust the box
over this doc if they ever disagree.

---

## 1. Device access

- **SSH:** **`ssh root@192.168.8.1`** (key auth, ed25519, works). Remote shell is BusyBox `ash`.
  - ⚠️ This is the **home** Mudi. Do not confuse it with `192.168.8.1` = *a peer's* Mudi `redacted`, a different box.
- **Hardware:** GL.iNet **GL-E5800** ("Mudi", 5G travel router), Qualcomm SDXPINN,
  `aarch64_cortex-a53`, GL firmware **4.8.5** / OpenWrt 23.05.4, kernel 5.15.170.
  Userspace is **musl** (`/lib/ld-musl-aarch64.so.1`).
- Tailnet node `redacted` `redacted` if you need it off-LAN.

## 2. The display hardware (what the PoC targets)

| Property | Value |
|---|---|
| Framebuffer | **`/dev/fb0`** (char 29,0), driver `fb_gc9307c` |
| Resolution | **240 × 320, portrait** (`virtual_size 240,320`, mode `U:240x320p-0`) |
| Pixel format | **16 bpp, RGB565**, little-endian |
| Stride | **480 bytes/row** (240 × 2) → framebuffer = 240×320×2 = **153,600 bytes** |
| Touch | **`/dev/input/event0`**, `chsc_cap_touch` (capacitive, I2C `1-002e`), `EV=b` (EV_KEY+EV_ABS), ABS_X/ABS_Y (+ multitouch ABS bits) |
| Backlight | `/sys/class/backlight/soc:backlight/brightness`, `max_brightness 120`. GL applies a **+20 offset** (see `reference/gl_screen_scripts/platform.sh` `set_brightness`), so usable range is ~20–120. `bl_power` = on/off. |
| Other physical inputs | `event1` = `pmic_pwrkey` (front/power button — **owned by the UI stack**, see §4), `event2` = `pmic_resin` (reset), `event3/4` = `aw_sar0` SAR RF sensors. **No free/spare button exists.** |

Sanity-draw test (fills screen, proves you own the panel):
```sh
# on the Mudi, AFTER stopping gl_screen (see §5)
cat /dev/urandom | head -c 153600 > /dev/fb0     # noise
head -c 153600 /dev/zero      > /dev/fb0          # black
```

## 3. How GL's stock screen works (`gl_screen`) — and why we can't extend it

- Package **`gl-sdk4-screen-large`** (`git-2026.142.39025`), **closed source** — confirmed:
  ships as a compiled ipk only from GL's binary feed (`fw.gl-inet.com/.../glinet`); nothing
  public on GitHub (`gl-inet/glinet` = ipks only; code search = 0 hits).
- **`/usr/bin/gl_screen`** = a **2.3 MB stripped aarch64 ELF** built on **LVGL** (embedded C GUI lib).
  Copy saved here: `reference/gl_screen.aarch64.elf` (for `strings`/analysis).
  - Uses LVGL's **fbdev** driver on `/dev/fb0` + **evdev** driver on `/dev/input/event0`.
  - Links: libcurl, libubus, libblobmsg_json, libubox, libjansson, **libfreetype**, libgcc_s, libc.
  - Launched by `/etc/init.d/gl_screen` (procd, `respawn`, `nice -20`), started at boot with
    `-c /tmp/gl_screen/config`.
- It reads **editable data** at startup (all under `/etc/gl_screen/`, copies in `reference/`):
  - `config/reference/dpr/layout` (`reference/config/layout.dpr`) — **1002 geometry keys** (X/Y/W/H, font sizes). *Styling only.*
  - `language/text/default` (`reference/config/language_text_default`) — **828 label strings** (`KEY "value"`).
  - `image/*.png` (~170 icons) — not copied here; pull from the box if needed.
  - `scripts/*.lua` + `platform.sh` + `common.sh` — thin glue (copies in `reference/gl_screen_scripts/`).
- **The menu tree, page navigation, and every button→action are compiled C inside the stripped
  ELF.** The `layout` file has zero logic — just coordinates. There is **no embedded Lua** in
  `gl_screen` (no `luaL_*`); the one `.lua` event file is a tiny `popen` bridge for a few fixed
  timing queries.
- **Consequence:** by editing config you can **relabel / re-icon / move / hide** existing items,
  but you **cannot add a new item, add a page, or bind custom logic** inside gl_screen.

## 4. Routes we already evaluated and RULED OUT (don't re-derive these)

- ❌ **Add items to gl_screen** — impossible without source (closed binary). No new pages/logic via config.
- ❌ **Rebuild GL's screen app** — source not published anywhere.
- ❌ **Hijack a physical button** — the E5800 has **no spare button**. The front button is the
  power key (`pmic_pwrkey`), consumed by the **`lpm`** daemon + gl_screen for screen wake/nav
  (`ubus call lpm set_screen`). Reset/SAR are the only other inputs. Nothing free to bind to.
- ⚠️ **Hijack an on-screen *toggle*** (watch `route_policy`/`wireless` uci with `inotifyd` and react)
  — technically works but only reacts to *existing* toggles; it does NOT give new UI. Parked in
  favor of the PoC below.

## 5. The PoC approach (Path C): our own app on the framebuffer

Since gl_screen can't be extended, the PoC is a **standalone app that draws to `/dev/fb0`**
and reads touch from `event0` — i.e. we replace/co-opt the panel, not gl_screen's internals.

### Coexistence with gl_screen (CRITICAL for testing)
`gl_screen` opens `/dev/fb0` and touch **exclusively** — two writers conflict. To test the PoC:
```sh
/etc/init.d/gl_screen stop      # procd stops it AND suspends respawn
# ... run your PoC, it now owns fb0 + event0 ...
/etc/init.d/gl_screen start     # restore GL's UI when done
```
(Do NOT just `kill` it — procd `respawn` restarts it in ~1s. Use the init script.)
Also stop it fighting for backlight/`lpm` blanking during a long test if needed.

**⚠️ Unblank the fbdev or you draw to a dark panel (verified 2026-07-15).** After
`gl_screen stop`, the display controller stays **blanked at the fbdev layer** — pixels you
write to `/dev/fb0` land in memory but the panel shows nothing. Setting
`backlight/brightness`+`bl_power` and `ubus call lpm set_screen '{"action":"active"}'` are
**NOT sufficient** (both reported "on"/"ok" while the screen stayed dark). The one thing that
lit it:
```sh
echo 0 > /sys/class/graphics/fb0/blank    # 0 = FBIOBLANK unblank; THIS wakes the panel
```
So the required take-over sequence is: `gl_screen stop` → `echo 0 > .../fb0/blank` → draw.
(An mmap+`FBIOBLANK` PoC binary must issue the unblank ioctl itself; sysfs `blank` is the
shell equivalent.) First-light RGB565 sanity draw that confirmed panel+byte-order+portrait
orientation (red top / green mid / blue bottom):
```sh
awk 'BEGIN{for(r=0;r<320;r++){if(r<107){lo=0;hi=248}else if(r<214){lo=224;hi=7}else{lo=31;hi=0}
for(c=0;c<240;c++)printf "%c%c",lo,hi}}' > /dev/fb0
```
Note: persistent `lpm` config already ships with `system_sleep_enable='0'` / `phy_sleep_time='0'`
(travel-router defaults) — no need to touch lpm sleep for a quick test.

### ⚡ Perf finding (measured on-device 2026-07-15) — Python rendering is fast enough

We installed `python3` + `python3-pillow` + `python3-numpy` on the box (from GL's opkg feed;
resources are ample: ~1 GB RAM free, 2.3 GB free `/overlay`, quad Cortex-A55) and benchmarked a
**live** dashboard (animated signal arc gauge + rolling line graph + text) rendered purely in
Python straight to `/dev/fb0`:

| stage | median ms |
|---|---|
| PIL draw (whole frame) | 2.6 |
| numpy RGB565 pack | 5.0 |
| `write()` to /dev/fb0 | 0.08 |
| **total** | **7.75 → ~129 FPS** |
| blit ceiling (write-only) | 36,000 FPS |

**Implication:** there is **no performance hot path** that needs C. For the companion phase the
stack can be **pure Python**: PIL draws → numpy packs RGB565 → `write()` fb0 → `python-evdev`
reads touch. A C helper (`libmudi`) is only ever a *targeted* optimization later (RGB565 pack
5 ms→~0.1 ms, or an mmap double-buffer if tearing appears), not the foundation. This revises the
original "raw C library for the hot path" assumption.

### Architecture (event-driven object framework) — `poc/mudi.py`

The companion is built on a small object model (chosen with Kevin 2026-07-15) that keeps
many instrumented pages consistent and polls nothing that isn't on screen:

- **`Theme`** — one palette + font set; every widget draws through it → visual consistency.
- **`DataSource`** — a *self-gating subject*: owns a per-key subscriber list, runs its own
  polling thread **only while subscriber count > 0**, and `_emit()`s to subscribers **only on
  change** (deduped). `CellularSource` is the first (provides `signal.*`, `cell.*`, `net.mode`,
  `sim.carrier/slot`). App-owned; a `key→source` registry routes subscriptions.
- **`Widget`** (`Header`, `ArcGauge`, `StatsRow`, `ServingCell`, `Trace`, `Button`) — subscribes
  its bus keys on `wire()`, unsubscribes on `unwire()`, redraws reactively, may `animate()`.
- **`Page`** — bundles widgets (its UI). A concrete page (`SignalPage`) is ~6 lines wiring shared
  widgets to keys. `App.show(page)` unwires the old page and wires the new one.
- **Demand-driven polling is emergent:** showing a page subscribes its widgets → source counts
  rise → those sources wake; hiding it drops counts → sources sleep. Verified live: running
  SignalPage shows exactly 3 threads (render, touch, cellular poller); the poller exists only
  because widgets subscribed. Same ~0–1% CPU / 31 MB as the monolith.
- **This is the runtime the JSON layer will target:** the `key="signal.rsrp"` widget bindings are
  the declarative bindings — `Page.build()` can later be generated from JSON instead of Python.
- `poc/mudi_signal_live.py` is the earlier single-file monolith, kept as reference.

**Widgets are parameterized by bus keys → real reuse (added `WifiPage` 2026-07-15):**
- The 6 widget classes (`Header/ArcGauge/StatsRow/InfoPanel/Trace/Button`) take their bus keys +
  labels as constructor args, so **the same `ArcGauge` renders cellular signal on SignalPage and
  WiFi link on WifiPage**. `WifiPage` added **zero** new widget classes — consistency by
  construction. Sources emit display-ready strings (e.g. `cell.band`→`"n71"`) so widgets stay dumb.
- **`WifiSource`** (provides `wifi.*`) reads `iwinfo devices`→`iwinfo info` per device (classifies
  Client=uplink/repeater vs Master=AP) + `gl-clients list` for online count. This box runs as a
  **repeater**: `wlan4` = station uplinked to an upstream SSID, `wlan0` = local AP. SSIDs can carry
  emoji the panel TTFs lack → `_san()` strips to printable ASCII.
- **Swipe navigation:** `App` holds `pages[]`; the touch thread distinguishes tap (→button) from
  horizontal swipe (→prev/next page). Page-indicator dots drawn top-center. Swiping unwires the old
  page (its sources lose subscribers → sleep) and wires the new one (its sources wake) — verified:
  on SignalPage only `CellularSource` polls, others idle until you swipe to them.
- **Four pages / four sources live (2026-07-15):** `SignalPage`←`CellularSource`,
  `WifiPage`←`WifiSource`, `SystemPage`←`SystemSource` (battery gauge via `mcu`, CPU temp from
  `/sys/class/thermal/thermal_zone15-18`, load/mem/uptime from `ubus system info`, load as float →
  feeds `Trace`), `EthernetPage`←`EthernetSource` (eth0 carrier/speed from `/sys/class/net`,
  currently DOWN — travel router on cellular; LAN IP + live rx/tx throughput from `br-lan`
  `/statistics/*_bytes` deltas). `Header.title` accepts a bus key (has a `.`) OR a literal string
  (page names like "System"/"Ethernet").

**Operational gotchas (managing the live app over ssh):**
- No `setsid` on the box — launch persistently with `nohup python3 /tmp/mudi.py </dev/null
  >/tmp/mudi.out 2>&1 &`.
- **BusyBox `ps`/`pgrep -f` count threads and match the invoking shell's own cmdline.** For a true
  process count use `pidof python3` then check `/proc/<pid>/task`. Launching without killing the
  prior instance leaves TWO writers fighting over `/dev/fb0`. Clean restart:
  `for p in $(pidof python3); do kill -9 $p; done` (filter by cmdline), then relaunch one.

**Live-app findings (`poc/mudi_signal_live.py`, measured 2026-07-15):**
- **Modem cadence:** `cellular.network info` `cell_info` refreshes on a **very regular ~20 s
  cycle** (measured inter-change gaps 19.2 / 20.0 / 20.2 s). ubus call latency is **~8 ms avg**
  (min 4, max 12) — polling is nearly free, so 4 s cell polling / 32 s carrier polling is ample.
- **Render only on change (dirty-skip):** tick a 30 Hz loop but recompute a small render
  signature `(round(frac,3), fresh, values…)` and only draw+pack+write when it differs. The
  panel is genuinely static ~20 s at a time, so this drops CPU from **31% of one core (flat
  20 FPS) → 1% of one core**, same visuals. Gauge eases at 30 fps only while animating; the
  "live" dot flashes for 1.2 s on a real value change (not a constant blink). RSS ~31 MB.
- **Touch:** ABS_X/ABS_Y report **0–240 / 0–320 — already panel pixels, no calibration**.
  Tap the on-screen STOCK UI button → app restores gl_screen and exits.
- **Lifecycle:** app does `gl_screen stop` + fbdev unblank on start, restores `gl_screen` in a
  `finally` (also on SIGINT/SIGTERM/button). Run with a numeric arg = seconds (safety
  auto-restore for ssh testing); no arg = run until the button is tapped.

**Install gotchas (for reproducing / packaging):**
- `python3-pillow` clashes with `gl-sdk4-screen-large` on `/usr/lib/libfreetype.so.6` (GL bundles
  its own 6.20.4). Install pillow with `opkg install --nodeps python3-pillow` so it reuses GL's
  existing freetype instead of `--force-overwrite` (which risks GL's stock UI). libjpeg.so.62 is
  already present.
- OpenWrt splits the stdlib: beyond `python3-light` you need `python3-urllib` (numpy),
  `python3-logging` (PIL), plus `python3-ctypes`/`python3-cffi` for the binding path, and
  `python3-numpy`, `python3-evdev`.
- This Pillow build has **no `BGR;16` raw packer** — `img.tobytes("raw","BGR;16")` raises
  `No packer found`. Pack RGB565 with numpy instead (vectorized, ~5 ms/frame):
  `((r&0xF8)<<8)|((g&0xFC)<<3)|(b>>3)` as little-endian `<u2`.
- File transfer: box has **no sftp-server**, so `scp` fails — use `ssh host 'cat > /path' < file`.
- Benchmark script kept at `poc/bench.py`.

### Two build tiers (pick per how far the PoC needs to go)

**Tier 1 — raw framebuffer, zero toolchain (fastest first light).**
A tiny C program (or even shell) that `mmap`s `/dev/fb0`, writes RGB565 pixels, and reads
`struct input_event` from `/dev/input/event0`. Cross-compile a static aarch64 musl binary, scp
it over. Good enough to prove: draw shapes/text, read a touch coord, light a "button". No deps.
- RGB565: `pix = ((r&0xF8)<<8) | ((g&0xFC)<<3) | (b>>3)`.
- Touch: read 16-byte `input_event`s; `type==EV_ABS`, `code==ABS_X/ABS_Y` for coords, `code==ABS_MT_*` for multitouch; `type==EV_KEY code==BTN_TOUCH` for down/up.

**Tier 2 — LVGL (real UI, the honest PoC).** Same GUI lib GL uses, so parity is guaranteed.
- Upstream: `github.com/lvgl/lvgl` — enable the **fbdev** + **evdev** drivers (LVGL has both
  built in: `lv_linux_fbdev_create("/dev/fb0")`, `lv_evdev_create(LV_INDEV_TYPE_POINTER,
  "/dev/input/event0")`). Set `LV_COLOR_DEPTH 16`, hor/ver res 240/320.
- GL's own port for reference: **`github.com/gl-inet/gl-lvgl`** (LVGL as an OpenWrt package +
  LCD/brightness helpers — built for BE3600, but the fbdev/evdev pattern is identical; adapt
  panel specifics only).
- Fonts: FreeType is on the box (gl_screen links it); or bake a bitmap font into LVGL.

### Toolchain
- **GL/OpenWrt SDK:** `github.com/gl-inet/sdk` (precompiled) — produces `aarch64_cortex-a53`
  musl binaries/ipks that match this device. This is the clean path for an ipk you can `opkg install`.
- Kevin already has **QSDK/BE3600 resources** (see `~/router` memory `reference_glinet_qsdk.md`) —
  related but a *different* SoC (E5800 = SDXPINN). Verify the toolchain triple matches
  (`aarch64-openwrt-linux-musl`) before assuming binaries are portable.
- Quick-and-dirty: any `aarch64-linux-musl` cross-gcc (musl.cc) for a **static** Tier-1 binary
  avoids the whole SDK for first light.

## 5b. Data sources for the companion UI (verified live 2026-07-15)

All via `ubus` (Python can call these). Key objects: `cellular.*`, `mcu`, `lpm`, `gl-clients`,
`system`, `network.interface.*`. Flagship fields:

- **Battery / temp:** `ubus call mcu status` → `charge_percent`, `charging_status`,
  `fastcharge`, `temperature`.
- **Serving cell (signal + tower):** `ubus call cellular.network info '{"bus":"cpu","slot":N}'`
  → `cell_info`: `id` (NR cell identity, hex), `mode` (e.g. `NR5G-SA FDD`), `band` (int → `n71`),
  `rsrp`/`rsrq`/`sinr` (+ `*_level` 0–5), `dl_bandwidth` (e.g. `15MHz`), `tx_channel` (NR-ARFCN).
  - **Carrier frequency from NR-ARFCN** (FR1, N ≤ 599999): `MHz = ARFCN * 5 / 1000`
    (5 kHz raster). e.g. 127490 → 637.5 MHz.
- **⚠️ Dual-SIM active carrier (both SIMs stay registered — don't trust a global operator):**
  1. active slot = `ubus call cellular.modem status '{"bus":"cpu"}'` → `current_sim_slot`.
  2. carrier name = `ubus call modem.CPU.AT get_result_AT '{"cmd":"AT+QSPN","sub_id":<active_slot>}'`
     (parse the quoted name). **`sub_id` selects the SIM/subscriber and MUST equal the active
     slot** — the default `sub_id=0` returned the *wrong* SIM's operator (AT&T) while the active
     slot 1 is T-Mobile. `AT+COPS?` with the same `sub_id` agrees. SIM home network also in
     `cellular.sim info` (`imsi`/`mcc`/`mnc`); serving network (what to display) comes from the
     `sub_id`-scoped AT query, not the SIM's home MCC/MNC.
- **Throughput/traffic:** `ubus call cellular.collect get_traffic '{"bus":"cpu"}'`; Wi-Fi clients:
  `gl-clients`; interface stats: `network.interface.*` / `/proc/net/dev`.

## 6. Screen-feedback / MCU side channel (bonus, optional)

The `mcu` ubus object (readable Lua daemon `/usr/bin/mcu`, copy: `reference/mcu.eco.lua`, run by
the `eco` interpreter) talks to the panel's microcontroller over serial and exposes:
```
ubus -v list mcu   # -> send_custom_msg, cmd_json{cmd}, cmd_string{cmd}, status, set_warning, ...
```
`send_custom_msg` is how GL flashes toast text (see `mcu_send_message()` at
`/lib/functions/gl_util.sh:2245`, which writes JSON to `/dev/ttyS0`; the toast helper is gated to
`model=e750`, so on the E5800 validate the `mcu` **ubus** route live). Not needed for a
framebuffer PoC, but useful if you want to drive the MCU/battery/temperature data.

### Deployed as a procd service (2026-07-15) — boots with the router

- **App installed persistently at `/usr/bin/mudi.py`** (source: `poc/mudi.py`); init script at
  `/etc/init.d/mudi` (source: `poc/mudi.init`). Both added to `/etc/sysupgrade.conf` so they
  survive firmware upgrades (normal reboots already persist via `/overlay`).
- **procd service:** `START=99` (after gl_screen so we grab the panel last), `respawn 3600 5 5`
  (crash recovery, gives up after >5 crashes/hour), launched as `python3 /usr/bin/mudi.py
  --service`. Enabled → `/etc/rc.d/S99mudi` symlink = starts at boot. (Not reboot-tested to avoid
  dropping the cellular ssh link, but the symlink + working `start_service` guarantee boot start.)
- **STOCK UI button under the service:** `App.request_stock()` checks `self.service`; in service
  mode it runs `/etc/init.d/mudi stop` (procd stops us → our `finally` runs `gl_screen start`).
  Must NOT just exit — procd `respawn` would instantly restart us and re-grab the panel.
  Validated: stop → gl_screen returns, no respawn; `mudi start` brings the companion back.
- **Manage:** `/etc/init.d/mudi {start,stop,restart,enable,disable}`. To iterate on the code:
  `cat > /usr/bin/mudi.py` then `/etc/init.d/mudi restart`.

## 7. Persistence & risk (for when the PoC graduates)

- Custom files outside `/etc/config/` are **wiped by a firmware upgrade** unless listed in
  **`/etc/sysupgrade.conf`**. A **factory/app reset** wipes them regardless — keep sources here
  and re-deploy.
- An **OTA update of `gl-sdk4-screen-large`** overwrites any `/etc/gl_screen/*` edits.
- `/dev/fb0` is a **single owner** — a stray PoC process left running blocks gl_screen and vice
  versa. Always restore gl_screen after testing.
- The Mudi is a **travel router on cellular** — reachability is intermittent by design; don't
  assume the box is up.

## 8. What's in this folder

```
MudiUI/
├── CONTEXT.md                     ← this file
├── poc/                           ← put PoC source here
└── reference/
    ├── gl_screen.aarch64.elf      ← stock UI binary (stripped) for strings/analysis
    ├── mcu.eco.lua                ← readable MCU daemon (Lua/eco)
    ├── config/
    │   ├── layout.dpr             ← 1002 geometry keys (element X/Y/W/H, font sizes)
    │   ├── layout.ndpr            ← non-DPR ratios/animation timings
    │   ├── language_text_default  ← 828 UI label strings
    │   └── default_generic
    └── gl_screen_scripts/
        ├── platform.sh            ← brightness/perf/blank glue (backlight offset lives here)
        ├── common.sh              ← gen_screen_config (uci→/tmp/gl_screen/config)
        ├── gen_image.lua, kv_config.lua
        └── gl_screen_event.lua.luac (compiled bytecode)
```

## 9. Suggested first step in the restarted session

1. Confirm the panel: `ssh root@192.168.8.1 '/etc/init.d/gl_screen stop'`, then draw a test
   pattern to `/dev/fb0` (§2) and eyeball the screen. Restart gl_screen after.
2. Decide Tier 1 (raw fb, static musl binary) vs Tier 2 (LVGL) for the PoC scope.
3. Build "hello screen": fill background, draw one on-screen button, read a touch, toggle its
   color. That proves draw + input end-to-end. Everything else is iteration.
