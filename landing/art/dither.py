"""Pure-python dithered scene renderer -> paletted PNG (index 0 transparent)."""
from array import array
import zlib, struct, math, random

BAYER = [
 [ 0,32, 8,40, 2,34,10,42],[48,16,56,24,50,18,58,26],
 [12,44, 4,36,14,46, 6,38],[60,28,52,20,62,30,54,22],
 [ 3,35,11,43, 1,33, 9,41],[51,19,59,27,49,17,57,25],
 [15,47, 7,39,13,45, 5,37],[63,31,55,23,61,29,53,21]]

class Buf:
    def __init__(s, w, h):
        s.w, s.h = w, h
        s.a = array('f', [0.0]) * (w * h)          # albedo
    def px(s, x, y, v):
        if 0 <= x < s.w and 0 <= y < s.h: s.a[y * s.w + x] = v
    def rect(s, x0, y0, x1, y1, v):
        s.poly([(x0, y0), (x1, y0), (x1, y1), (x0, y1)], v)
    def poly(s, pts, v):
        ys = [p[1] for p in pts]
        y0 = max(0, int(math.floor(min(ys)))); y1 = min(s.h - 1, int(math.ceil(max(ys))))
        n = len(pts); W = s.w; A = s.a
        cb = callable(v)
        for y in range(y0, y1 + 1):
            yc = y + 0.5; xs = []
            for i in range(n):
                ax, ay = pts[i]; bx, by = pts[(i + 1) % n]
                if (ay <= yc < by) or (by <= yc < ay):
                    xs.append(ax + (yc - ay) / (by - ay) * (bx - ax))
            xs.sort(); row = y * W
            for i in range(0, len(xs) - 1, 2):
                xa = max(0, int(math.ceil(xs[i] - 0.5))); xb = min(W - 1, int(math.floor(xs[i + 1] - 0.5)))
                if cb:
                    for x in range(xa, xb + 1): A[row + x] = v(x, y)
                else:
                    for x in range(xa, xb + 1): A[row + x] = v
    def line(s, x0, y0, x1, y1, v, w=1.0):
        dx, dy = x1 - x0, y1 - y0; L = math.hypot(dx, dy) or 1
        nx, ny = -dy / L * w / 2, dx / L * w / 2
        s.poly([(x0 + nx, y0 + ny), (x1 + nx, y1 + ny), (x1 - nx, y1 - ny), (x0 - nx, y0 - ny)], v)
    def path(s, pts, v, w=1.0):
        for i in range(len(pts) - 1):
            s.line(pts[i][0], pts[i][1], pts[i + 1][0], pts[i + 1][1], v, w)
    def ell(s, cx, cy, rx, ry, v, seg=48):
        s.poly([(cx + rx * math.cos(i * 2 * math.pi / seg), cy + ry * math.sin(i * 2 * math.pi / seg)) for i in range(seg)], v)
    def rrect(s, x0, y0, x1, y1, r, v):
        p = []
        for (cx, cy, a0) in ((x1 - r, y0 + r, -90), (x1 - r, y1 - r, 0), (x0 + r, y1 - r, 90), (x0 + r, y0 + r, 180)):
            for k in range(9):
                a = math.radians(a0 + k * 90 / 8); p.append((cx + r * math.cos(a), cy + r * math.sin(a)))
        s.poly(p, v)

def dither(a, w, h, levels, light=None, gamma=1.0, noise=0.0, jitter=None):
    """albedo -> palette indices 0..levels-1 (0 = empty)."""
    out = bytearray(w * h); top = levels - 1
    rnd = random.Random(7)
    for y in range(h):
        row = y * w; brow = BAYER[y & 7]
        for x in range(w):
            v = a[row + x]
            if v <= 0.0: continue
            if light: v *= light[row + x]
            if noise: v += (rnd.random() - 0.5) * noise
            if gamma != 1.0: v = v ** gamma if v > 0 else 0.0
            if v <= 0.0: continue
            if v > 1.0: v = 1.0
            f = v * top; b = int(f); fr = f - b
            if fr > (brow[x & 7] + 0.5) / 64.0: b += 1
            if b > top: b = top
            if b: out[row + x] = b
    return out

def png(path, w, h, idx, palette, bitdepth):
    ppb = 8 // bitdepth; rows = bytearray()
    for y in range(h):
        rows.append(0); off = y * w; acc = 0; n = 0; line = bytearray()
        for x in range(w):
            acc = (acc << bitdepth) | (idx[off + x] & ((1 << bitdepth) - 1)); n += 1
            if n == ppb: line.append(acc); acc = 0; n = 0
        if n: line.append(acc << (bitdepth * (ppb - n)))
        rows += line
    def ck(tag, data):
        return struct.pack('>I', len(data)) + tag + data + struct.pack('>I', zlib.crc32(tag + data) & 0xffffffff)
    plte = b''.join(bytes(c) for c in palette)
    trns = bytes([0] + [255] * (len(palette) - 1))
    out = (b'\x89PNG\r\n\x1a\n'
           + ck(b'IHDR', struct.pack('>IIBBBBB', w, h, bitdepth, 3, 0, 0, 0))
           + ck(b'PLTE', plte) + ck(b'tRNS', trns)
           + ck(b'IDAT', zlib.compress(bytes(rows), 9)) + ck(b'IEND', b''))
    open(path, 'wb').write(out)
    return len(out)
