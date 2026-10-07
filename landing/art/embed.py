"""Regenerate the horizon bitmap and re-embed it in ../index.html.

    cd landing/art && python3 embed.py

An ordered-dither PNG with a transparent background: a 1-bit mask the page
colours in CSS.  index.html must hold exactly this one PNG data URI, the
horizon (.exit's --horizon).
"""
import base64, re, subprocess, sys, pathlib

here = pathlib.Path(__file__).parent
page = here.parent / "index.html"

for script in ("fade.py",):
    subprocess.run([sys.executable, script], cwd=here, check=True)

uris = ["data:image/png;base64," + base64.b64encode((here / n).read_bytes()).decode()
        for n in ("fade.png",)]

html = page.read_text(encoding="utf-8")
seen = iter(uris)
html, n = re.subn(r"data:image/png;base64,[A-Za-z0-9+/=]+", lambda m: next(seen), html)
if n != len(uris):
    raise SystemExit("expected %d data URIs in index.html, found %d" % (len(uris), n))
page.write_text(html, encoding="utf-8")
print("re-embedded %d bitmaps into %s" % (n, page))
