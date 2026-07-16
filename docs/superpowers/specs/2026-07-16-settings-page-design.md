# MudiUI Settings page — design spec (2026-07-16)

## Goal
Add a user-facing Settings page to MudiUI, reachable as the last page in the swipe carousel
(gear dot). Touch-only UI on a 240×320 panel, so every control is a toggle / stepper / slider /
action — no text entry. Settings persist in **uci `/etc/config/mudi`** so they survive firmware
upgrades automatically.

## Entry point
`SettingsPage` is appended to `App.pages` after `EthernetPage` (index 4). Reached by the existing
horizontal swipe; its sources (none — it reads uci) mean it adds ~zero polling. A `Banner` widget
draws the "Settings" title bar.

## Persistence — `Settings` object
- uci package `mudi`, section `main` (type `settings`).
- `Settings.load()` parses `uci -q show mudi`; missing keys fall back to `DEFAULTS`. Degrades to
  defaults off-device (no uci) so `--mock` and dev hosts work.
- `Settings.set(k,v)` writes `uci set mudi.main.k=v; uci commit mudi` (creating the section if
  absent). All values stored as strings.
- Defaults: `brightness=90 screen_timeout=30 stay_awake_charging=1 default_page=0 band_lock=0
  net_mode=auto longpress=1.6 start_on_boot=1`.
- Shipped as `poc/mudi.config` → `/etc/config/mudi`; installer copies it **only if absent**
  (never clobbers user settings). `/etc/config/*` is already in the default sysupgrade backup.

## Setting-row widget family (new)
All subclass `Row(Widget)` (fixed height, `place(y)`, `in_row(y)`, `on_touch(x,y)->bool`):
- `SliderRow` — brightness (20–120). Tap on the track sets the value (tap-to-set, no drag).
- `StepperRow` — discrete options with `[−] value [+]`; `wrap` for cyclic pickers; `confirm` to
  gate behind a modal. Covers screen-timeout, long-press, default-page, network-mode.
- `ToggleRow` — on/off pill; optional `confirm`. Covers stay-awake, band-lock, start-on-boot.
- `ActionRow` — label + `›`; runs a callback. Covers return-to-stock, about.

Rows read the current value from `app.settings` on draw and, on change, call
`app.settings.set()` then `app.apply_setting(skey, val)`. `Page.on_touch` is generalized to call
`widget.on_touch(x,y)` when present (Button's hit+action path is kept).

## v1 rows
| Row | Type | Applies |
|---|---|---|
| Brightness | Slider 20–120 | live → sysfs backlight |
| Screen timeout | Stepper Off/15s/30s/1m/5m | read by idle-blank loop |
| Awake on charge | Toggle | read by idle-blank loop |
| Default page | Stepper (cyclic) Signal/WiFi/System/Eth | used at startup |
| Lock band n71 | Toggle + confirm | modem AT (verified cmds) |
| Network mode | Stepper (cyclic) Auto/5G/LTE + confirm | modem AT `mode_pref` |
| Long-press to switch | Stepper 1.0/1.3/1.6/2.0s | read by `mudi-watch` |
| Return to stock UI | Action | sets `_toggle_req` (same as gesture) |
| Start on boot | Toggle | `/etc/init.d/{mudi,mudi-watch} enable/disable` |
| About | Action | info modal |

Ten rows at 28 px fit 30→310 px with no scrolling in v1.

## New machinery
1. **Idle-blank subsystem** (`App`): track `last_touch`; when
   `now-last_touch > screen_timeout` (and not stay-awake-while-charging), blank the panel
   (`bl_power=1`, brightness 0). Any touch-down wakes it (restores configured brightness) and
   that waking touch is **swallowed** (doesn't also act). The long-press watcher still works while
   blanked (independent reader of event0). `stay_awake` is checked with a one-shot `mcu status`
   only at the blank transition (cheap, infrequent).
2. **Modal overlays** (`App.modal`): `Confirm` (message + Cancel/OK) gates modem writes so an
   accidental tap can't re-band the modem; `About` shows model/firmware/version/LAN IP. While a
   modal is open, swipes are ignored and taps route to the modal. Modem AT writes run on a daemon
   thread so the touch loop never blocks.

## Cross-process settings
- **long-press**: `mudi-watch.py` re-reads `uci get mudi.main.longpress` on each touch-down.
- **default-page**: read once at `mudi.py` startup to pick the start index.

## Modem specifics
- Band lock ON: `AT+QNWPREFCFG="nr5g_band",71` + `="nsa_nr5g_band",71` (verified live 2026-07-15).
  OFF restores the backed-up full lists (`NR5G_BANDS_ALL` / `NSA_BANDS_ALL`).
- Network mode: `AT+QNWPREFCFG="mode_pref",{AUTO|NR5G|LTE}`. **Standard Quectel syntax but not yet
  live-verified on this modem** — behind the confirm gate; verify on-device before trusting.
- `sub_id` = active slot (`cellular.modem status.current_sim_slot`), per the dual-SIM rule.

## Out of scope (deferred)
Enable/reorder pages, auto-cycle, unit/format toggles, hero-metric picker, theme, flash-confirm
toggle, cell lock, SIM switching, SA/NSA split, scrollable settings list, drag sliders.

## Testing
`py_compile` + `--mock settings` render check locally (device offline). Full on-device pass
(idle-blank, modem confirms, persistence across restart) when the Mudi is reachable.
