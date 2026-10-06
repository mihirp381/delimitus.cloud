"""Regenerate the two bitmaps and re-embed them in ../index.html.

    cd landing/art && python3 embed.py

Both are ordered-dither PNGs with a transparent background: the hero is an
8-bit paletted spectrum print, the horizon a 1-bit mask the page colours in
CSS.  index.html must hold exactly these two PNG data URIs, in this order: the
hero scene first (.enter__art), the horizon second (.exit's --horizon).
"""
import base64, re, subprocess, sys, pathlib

here = pathlib.Path(__file__).parent
page = here.parent / "index.html"

for script in ("hero.py", "fade.py"):
    subprocess.run([sys.executable, script], cwd=here, check=True)

uris = ["data:image/png;base64," + base64.b64encode((here / n).read_bytes()).decode()
        for n in ("hero.png", "fade.png")]

html = page.read_text(encoding="utf-8")
seen = iter(uris)
html, n = re.subn(r"data:image/png;base64,[A-Za-z0-9+/=]+", lambda m: next(seen), html)
if n != len(uris):
    raise SystemExit("expected %d data URIs in index.html, found %d" % (len(uris), n))
page.write_text(html, encoding="utf-8")
print("re-embedded %d bitmaps into %s" % (n, page))
