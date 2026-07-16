#!/usr/bin/env python3
"""Sample how often the modem's cell_info actually changes, + ubus call latency."""
import subprocess, json, time, sys

BUS = "cpu"; SLOT = 1
DUR = float(sys.argv[1]) if len(sys.argv) > 1 else 120.0
INTERVAL = 1.0

def cell():
    t = time.time()
    o = subprocess.run(["ubus", "call", "cellular.network", "info",
                        json.dumps({"bus": BUS, "slot": SLOT})],
                       capture_output=True, text=True, timeout=8)
    lat = (time.time() - t) * 1000
    ci = json.loads(o.stdout)["networks"][0]["cell_info"]
    return ci, lat

last = None; t0 = time.time(); changes = []; lats = []; n = 0
print("t(s)  event   rsrp rsrq sinr  id           lat_ms")
while time.time() - t0 < DUR:
    tp = time.time() - t0
    try:
        ci, lat = cell(); lats.append(lat); n += 1
    except Exception as e:
        print(f"{tp:6.1f} ERROR {e}"); time.sleep(INTERVAL); continue
    key = (ci["rsrp"], ci["rsrq"], ci["sinr"], ci["id"])
    if key != last:
        if last is not None: changes.append(tp)
        print(f"{tp:6.1f} CHANGE  {ci['rsrp']:>4} {ci['rsrq']:>4} {ci['sinr']:>4}  {ci['id']:<12} {lat:6.0f}")
        last = key
    time.sleep(INTERVAL)

print(f"\npolls={n}  latency ms: min={min(lats):.0f} avg={sum(lats)/len(lats):.0f} max={max(lats):.0f}")
print(f"value changes={len(changes)}")
if len(changes) >= 2:
    gaps = [changes[i+1]-changes[i] for i in range(len(changes)-1)]
    print(f"inter-change gap s: min={min(gaps):.1f} avg={sum(gaps)/len(gaps):.1f} max={max(gaps):.1f}")
