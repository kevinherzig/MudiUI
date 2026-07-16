#!/usr/bin/env python3
# Renderer perf spike: PIL draw -> numpy RGB565 pack -> write /dev/fb0
# Measures whether Python-side rendering can drive the 240x320 panel for a
# live-updating dashboard (gauge + rolling graph + text). Throwaway.
import time, math, sys
import numpy as np
from PIL import Image, ImageDraw, ImageFont

W, H = 240, 320
FB = "/dev/fb0"
N = int(sys.argv[1]) if len(sys.argv) > 1 else 120

font = ImageFont.load_default()

def pack565(img):
    a = np.asarray(img, dtype=np.uint8)
    r = a[:, :, 0].astype(np.uint16)
    g = a[:, :, 1].astype(np.uint16)
    b = a[:, :, 2].astype(np.uint16)
    v = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)
    return v.astype("<u2").tobytes()

# rolling history for the line graph
hist = [0.0] * 100

def draw_frame(t):
    img = Image.new("RGB", (W, H), (18, 20, 28))
    d = ImageDraw.Draw(img)
    # header bar
    d.rectangle((0, 0, W, 28), fill=(30, 34, 48))
    d.text((8, 8), "MudiUI  signal", font=font, fill=(200, 210, 230))
    # signal arc gauge (0..270deg), animated
    cx, cy, rad = 120, 96, 46
    frac = 0.5 + 0.45 * math.sin(t * 0.15)
    d.arc((cx-rad, cy-rad, cx+rad, cy+rad), 135, 135+270, fill=(45, 50, 66), width=10)
    d.arc((cx-rad, cy-rad, cx+rad, cy+rad), 135, 135+int(270*frac),
          fill=(80, 180, 240), width=10)
    d.text((cx-28, cy-8), "-%d dBm" % (110 - int(40*frac)), font=font, fill=(235, 240, 250))
    # rolling throughput line graph in a panel
    gx, gy, gw, gh = 12, 170, W-24, 110
    d.rectangle((gx, gy, gx+gw, gy+gh), outline=(50, 55, 72), fill=(24, 27, 38))
    hist.pop(0)
    hist.append(0.5 + 0.5*math.sin(t*0.3) * math.sin(t*0.07))
    pts = []
    for i, v in enumerate(hist):
        px = gx + int(i * gw / (len(hist)-1))
        py = gy + gh - int(v * (gh-6)) - 3
        pts.append((px, py))
    d.line(pts, fill=(120, 220, 140), width=2)
    d.text((gx+4, gy+4), "throughput", font=font, fill=(150, 160, 180))
    # two stat rows
    d.text((12, 290), "RSRQ -11 dB   SINR 14 dB", font=font, fill=(200, 205, 220))
    d.text((12, 304), "band n78   4CA   45.2 GB", font=font, fill=(160, 170, 190))
    return img

def main():
    draw_t, pack_t, write_t = [], [], []
    with open(FB, "r+b", buffering=0) as fb:
        # warm
        draw_frame(0);
        for n in range(N):
            t0 = time.perf_counter()
            img = draw_frame(n)
            t1 = time.perf_counter()
            buf = pack565(img)
            t2 = time.perf_counter()
            fb.seek(0); fb.write(buf)
            t3 = time.perf_counter()
            draw_t.append((t1-t0)*1000); pack_t.append((t2-t1)*1000); write_t.append((t3-t2)*1000)
    def stats(x):
        x=sorted(x); n=len(x)
        return (sum(x)/n, x[n//2], x[int(n*0.95)])
    dm,dmd,d95 = stats(draw_t); pm,pmd,p95 = stats(pack_t); wm,wmd,w95 = stats(write_t)
    tot = [a+b+c for a,b,c in zip(draw_t,pack_t,write_t)]
    tm,tmd,t95 = stats(tot)
    print("frames: %d   (ms: mean / median / p95)" % N)
    print("  draw : %6.2f / %6.2f / %6.2f" % (dm,dmd,d95))
    print("  pack : %6.2f / %6.2f / %6.2f" % (pm,pmd,p95))
    print("  write: %6.2f / %6.2f / %6.2f" % (wm,wmd,w95))
    print("  TOTAL: %6.2f / %6.2f / %6.2f  ->  %.1f FPS (median)" % (tm,tmd,t95, 1000.0/tmd))
    # blit ceiling: how fast can we push full frames if drawing were free?
    buf = pack565(draw_frame(0))
    with open(FB,"r+b",buffering=0) as fb:
        t0=time.perf_counter()
        for _ in range(200): fb.seek(0); fb.write(buf)
        el=time.perf_counter()-t0
    print("  blit ceiling (write-only): %.1f FPS (%.2f ms/frame)" % (200/el, el/200*1000))

if __name__ == "__main__":
    main()
