# The horizon: the hero's scanline, remembered as a dithered band of light.
# 1-bit and transparent — the page uses it as a MASK over the spectrum
# gradient, so the colour lives in CSS and can drift; this file only decides
# where the dots are.  Dense at the centre row, thinning out both ways.
# Dithered at half resolution and written with 2x2 dots, so the grain matches
# the hero print and each dot carries enough colour to read as colour.
import math
from dither import Buf, dither, png
W, H, S = 80, 110, 2
C = (H - 1) / 2.0
b = Buf(W, H)
for y in range(H):
    d = abs(y - C)
    v = 0.84 * math.exp(-(d / 7.0) ** 2) + 0.16 * math.exp(-(d / 26.0) ** 2)
    if d < 0.6: v = 1.0
    for x in range(W):
        b.a[y * W + x] = max(0.0, min(1.0, v))
small = dither(b.a, W, H, 2)
idx = bytearray(W * S * H * S)
for y in range(H * S):
    for x in range(W * S):
        idx[y * W * S + x] = small[(y // S) * W + x // S]
print('fade.png', png('fade.png', W * S, H * S, idx, [(0, 0, 0), (0x1D, 0x1D, 0x1F)], 1), 'bytes')
