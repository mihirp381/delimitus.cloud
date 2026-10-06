# A room in one-point perspective, everything converging on the screen —
# drawn and lit exactly as before, then printed in spectrum ink on white.
import math, random
from array import array
from dither import Buf, dither, png

W, H = 1280, 720
VX, VY = 760.0, 352.0           # vanishing point = the middle of the screen
b = Buf(W, H)
rnd = random.Random(11)

# low-frequency value noise, so surfaces are not flat
NG = 48
grid = [[rnd.random() for _ in range(NG + 1)] for _ in range(NG + 1)]
def noise(x, y):
    fx = x / W * NG; fy = y / H * NG
    i = int(fx); j = int(fy); tx = fx - i; ty = fy - j
    a = grid[j][i]; c = grid[j][i + 1]; d = grid[j + 1][i]; e = grid[j + 1][i + 1]
    return (a * (1 - tx) + c * tx) * (1 - ty) + (d * (1 - tx) + e * tx) * ty

def tex(base, amt=0.35):
    return lambda x, y: base * (1.0 - amt + amt * 2 * noise(x, y))

def toward(p, t):                # push a point toward the vanishing point
    return (p[0] + (VX - p[0]) * t, p[1] + (VY - p[1]) * t)

# ── the room ──────────────────────────────────────────────────────────
BW = [(300, 118), (1220, 118), (1220, 566), (300, 566)]           # back wall
b.poly([(-4, -40), (300, 118), (300, 566), (-4, 764)], tex(0.13))  # left wall
b.poly([(1220, 118), (1284, 74), (1284, 712), (1220, 566)], tex(0.11))
b.poly([(-4, -40), (300, 118), (1220, 118), (1284, 74)], tex(0.10))  # ceiling
b.poly([(-4, 764), (300, 566), (1220, 566), (1284, 712)], tex(0.105, 0.30))  # floor
b.poly(BW, tex(0.150, 0.22))

# ceiling strip lights, small and far — depth cue, not a spotlight
for k in (0, 1):
    x0 = 210 + k * 620; x1 = x0 + 300
    a1 = toward((x0, -18), 0.30); a2 = toward((x1, -18), 0.30)
    b.poly([a1, a2, toward(a2, 0.40), toward(a1, 0.40)], 0.30)
    b.poly([toward(a1, 0.28), toward(a2, 0.28), toward(a2, 0.44), toward(a1, 0.44)], 0.20)

# floor seams, receding
for k in range(9):
    p = (-260 + k * 250, 780)
    b.line(p[0], p[1], *toward(p, 0.985), v=0.16, w=2.2)

# skirting + wall trim
b.line(300, 566, 1220, 566, 0.30, 5)
b.line(300, 118, 1220, 118, 0.24, 3)
b.line(300, 118, 300, 566, 0.26, 3); b.line(1220, 118, 1220, 566, 0.22, 3)
for x in range(300, 1224, 46): b.line(x, 130, x, 560, 0.178, 1.4)

# window on the left wall, foreshortened
b.poly([(28, 96), (232, 202), (232, 470), (28, 556)], 0.34)
b.poly([(44, 122), (222, 214), (222, 456), (44, 528)], 0.14)
for k in range(15):
    t = k / 14.0
    y0 = 128 + t * 388; y1 = 220 + t * 228
    b.poly([(46, y0), (220, y1), (220, y1 + 9), (46, y0 + 15)], 0.30)
b.line(133, 168, 133, 492, 0.24, 5)

# noticeboard + clock, back wall
b.rect(1006, 168, 1188, 300, 0.28); b.rect(1014, 176, 1180, 292, 0.135)
for i, y in enumerate(range(192, 286, 16)):
    b.rect(1028, y, 1028 + (132 if i % 3 else 84), y + 4, 0.30)
b.ell(378, 196, 30, 30, 0.34); b.ell(378, 196, 24, 24, 0.16)
b.line(378, 196, 378, 178, 0.55, 3); b.line(378, 196, 391, 202, 0.55, 3)

# shelf with boxes, right wall
b.poly([(1224, 300), (1284, 276), (1284, 292), (1224, 314)], 0.30)
b.poly([(1232, 246), (1284, 224), (1284, 278), (1232, 300)], 0.20)

# a filing cabinet in the back-left corner, and a chair
b.rect(318, 356, 452, 566, 0.17)
for y in (386, 446, 506): b.rect(332, y, 438, y + 6, 0.30)
b.rect(500, 470, 610, 566, 0.12); b.rect(498, 462, 612, 472, 0.24)

# ── the desk ──────────────────────────────────────────────────────────
b.poly([(86, 612), (1208, 612), (1098, 470), (218, 470)], tex(0.40, 0.07))
b.rect(86, 612, 1208, 656, 0.185)
b.line(86, 612, 1208, 612, 0.50, 3)
b.rect(150, 656, 196, 720, 0.11); b.rect(1104, 656, 1150, 720, 0.11)

# papers, notebook, mug, floppies
b.poly([(214, 592), (424, 592), (398, 528), (240, 528)], 0.58)
b.poly([(232, 584), (406, 584), (384, 536), (252, 536)], 0.36)
for y in range(544, 580, 8): b.line(262, y, 372, y, 0.60, 1.8)
b.rect(452, 522, 508, 578, 0.32); b.ell(480, 522, 28, 10, 0.50); b.ell(480, 522, 18, 6, 0.14)
b.path([(508, 536), (534, 542), (534, 560), (508, 566)], 0.32, 5)
for k in range(4):
    y = 520 - k * 7
    b.poly([(1000, y + 40), (1108, y + 40), (1090, y + 22), (1014, y + 22)], 0.30 + k * 0.03)

# ── the tower ─────────────────────────────────────────────────────────
b.poly([(950, 300), (1086, 300), (1116, 278), (980, 278)], 0.24)
b.rect(950, 300, 1086, 470, 0.30); b.rect(950, 300, 1086, 307, 0.44)
b.rect(962, 322, 1074, 333, 0.17); b.rect(964, 324, 1054, 331, 0.42)
b.rect(962, 345, 1074, 354, 0.18)
for y in range(374, 432, 9): b.line(962, y, 1040, y, 0.22, 3)
b.ell(1064, 452, 5, 5, 0.98)

# ── the monitor ───────────────────────────────────────────────────────
b.poly([(694, 470), (826, 470), (813, 451), (707, 451)], 0.34)
b.rect(738, 430, 782, 453, 0.28)
b.poly([(620, 244), (900, 244), (948, 216), (668, 216)], 0.27)
b.poly([(900, 244), (948, 216), (948, 398), (900, 432)], 0.19)
b.rrect(618, 242, 902, 434, 16, 0.36)
b.rrect(634, 258, 886, 418, 12, 0.54)
b.rrect(641, 265, 879, 411, 10, 0.0)                 # THE SCREEN
b.rect(702, 421, 796, 427, 0.32)
b.ell(862, 424, 5.5, 4.5, 1.0)

# keyboard + mouse
b.poly([(560, 596), (966, 596), (930, 534), (596, 534)], 0.33)
b.poly([(570, 589), (956, 589), (924, 541), (602, 541)], 0.24)
for i in range(6):
    y = 545 + i * 8.4; x0 = 604 + i * 1.4; x1 = 922 - i * 1.4
    b.line(x0, y, x1, y, 0.44, 2)
for i in range(17):
    t = i / 16.0
    b.line(602 + t * 322, 541, 570 + t * 386, 589, 0.38, 1.6)
b.ell(1012, 566, 28, 18, 0.28); b.ell(1012, 558, 21, 11, 0.38)

# cables
b.path([(760, 436), (772, 500), (700, 606), (688, 656), (700, 720)], 0.14, 6)
b.path([(1010, 470), (1022, 530), (972, 612), (962, 700)], 0.12, 5)

# desk lamp
b.ell(322, 500, 48, 14, 0.34)
b.path([(322, 496), (386, 366), (470, 330)], 0.40, 7)
b.poly([(458, 314), (508, 306), (524, 350), (466, 354)], 0.48)
b.ell(496, 352, 28, 9, 0.70)

# foreground: the near edge of a second desk, out of the light
b.poly([(-40, 700), (420, 700), (330, 664), (-40, 664)], 0.13)
b.rrect(1010, 664, 1340, 780, 22, 0.10)

# ── light ─────────────────────────────────────────────────────────────
L = array('f', [0.0]) * (W * H)
srcs = [(760, 340, 2 * 340 ** 2, 2.15),      # the screen — everything else is a rumour
        (128, 330, 2 * 210 ** 2, 0.22),      # the window
        (496, 392, 2 * 210 ** 2, 0.40),      # the lamp
        (420, 120, 2 * 190 ** 2, 0.14), (980, 120, 2 * 190 ** 2, 0.12),
        (1064, 452, 2 * 80 ** 2, 0.30)]
for y in range(H):
    row = y * W
    for x in range(W):
        v = 0.070
        for (sx, sy, dd, amp) in srcs:
            dx = x - sx; dy = y - sy
            e = (dx * dx + dy * dy) / dd
            if e < 12.0: v += amp * math.exp(-e)
        rx = (x - VX) / 900.0; ry = (y - VY) / 620.0
        L[row + x] = v * max(0.0, 1.0 - 0.86 * (rx * rx + ry * ry))


# ── colour: the value field printed as spectrum ink on white paper ─────
# More light means more ink; hue turns around the screen (blue upper-left to
# orange lower-right). Index 0 stays transparent so the page supplies the
# ground. The hue threshold reuses the value dither's Bayer row and there is
# no per-pixel noise: the pattern repeats, so the PNG compresses ~18% smaller.
import sys
from dither import BAYER
OUT = sys.argv[1] if len(sys.argv) > 1 else 'hero.png'

STOPS = [(0x0A, 0x84, 0xFF), (0x6E, 0x5B, 0xFF), (0xC2, 0x4B, 0xF0),
         (0xFF, 0x37, 0x8C), (0xFF, 0x5E, 0x3A), (0xFF, 0x9F, 0x0A)]
def spectrum(t):
    t = min(1.0, max(0.0, t)) * (len(STOPS) - 1)
    i = min(int(t), len(STOPS) - 2); f = t - i
    a, c = STOPS[i], STOPS[i + 1]
    return tuple(a[k] + (c[k] - a[k]) * f for k in range(3))

NH = 16                                  # hue steps
INK = [0.22, 0.50, 0.80, 1.0]            # tint strength per ink level (1.0 = the pure stop)
THETA = 0.6                              # radians: where the orange end of the ring points
pal = [(255, 255, 255)]
for s in INK:
    for h in range(NH):
        c = spectrum(h / (NH - 1.0))
        pal.append(tuple(int(round(255 + (c[k] - 255) * s)) for k in range(3)))

NL = len(INK)
out = bytearray(W * H)
for y in range(H):
    row = y * W; brow = BAYER[y & 7]
    for x in range(W):
        v = b.a[row + x]
        if v <= 0.0: continue
        v *= L[row + x]
        v = min(1.0, v ** 1.14) if v > 0 else 0.0
        f = v * NL; lv = int(f)
        if f - lv > (brow[x & 7] + 0.5) / 64.0: lv += 1
        if lv > NL: lv = NL
        if lv <= 0: continue
        ht = (0.5 + 0.5 * math.cos(math.atan2(y - VY, x - VX) - THETA)) * (NH - 1); hi = int(ht)
        if ht - hi > (brow[(x + 3) & 7] + 0.5) / 64.0: hi += 1
        out[row + x] = 1 + (lv - 1) * NH + min(NH - 1, hi)

# The screen is pale glass, not a hole: when the lit picture (.enter__glass)
# collapses to its line, it does so on glass rather than on the white page.
GLASS = len(pal); pal.append((0xEC, 0xEE, 0xFA))
SX0, SY0, SX1, SY1, SR = 641, 265, 879, 411, 10
for y in range(SY0, SY1 + 1):
    for x in range(SX0, SX1 + 1):
        cx = min(max(x + 0.5, SX0 + SR), SX1 - SR); cy = min(max(y + 0.5, SY0 + SR), SY1 - SR)
        if (x + 0.5 - cx) ** 2 + (y + 0.5 - cy) ** 2 <= SR * SR:
            out[y * W + x] = GLASS
print(OUT, png(OUT, W, H, out, pal, 8), 'bytes,', len(pal), 'colours')
print('screen  x %.3f%% %.3f%%  y %.3f%% %.3f%%  centre %.3f%% %.3f%%'
      % (641/W*100, 879/W*100, 265/H*100, 411/H*100, 760/W*100, 338/H*100))
