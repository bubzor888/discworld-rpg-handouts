"""Build one-page player handouts: general.md (top 2/3) + one species file (bottom 1/3).

Usage:  python build.py            # build every species in species/
        python build.py troll dwarf  # build only these
Output: dist/Discworld Primer - <Species>.pdf  (plus .html for debugging)
"""
import base64
import io
import re
import subprocess
import sys
from pathlib import Path

import markdown
from PIL import Image, ImageChops, ImageDraw, ImageFilter

HERE = Path(__file__).parent
DIST = HERE / "dist"
EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"

CSS = """
@page { size: letter; margin: 0; }
* { box-sizing: border-box; }
html, body { margin: 0; padding: 0; }
body { width: 8.5in; height: 11in; padding: 0.4in; font-family: 'Palatino Linotype', 'Book Antiqua', Georgia, serif;
       font-size: 9.8pt; line-height: 1.24; color: #1d1a16; background: #fffdf7;
       display: flex; flex-direction: column; gap: 0.12in; }
header { text-align: center; border-bottom: 2px solid #7a1f1f; padding-bottom: 3px; }
header h1 { margin: 0; font-size: 20pt; letter-spacing: 1px; color: #7a1f1f; font-variant: small-caps; }
header p { margin: 0; font-style: italic; font-size: 8.6pt; color: #5a5148; }
#general { flex: 1 1 0; min-height: 0; overflow: hidden; }
#tone { flex: none; text-align: center; font-style: italic; }
#tone p { margin: 0; }
#tone strong { font-style: normal; color: #7a1f1f; font-variant: small-caps; }
ul ul { margin-top: 2px; }
#bottom { flex: none; display: flex; gap: 0.16in; align-items: center; }
figure { flex: none; width: 1.75in; margin: 0; text-align: center; }
.frame { overflow: hidden; }  /* with CROPS: the image is oversized and pulled in on all sides */
figure img { display: block; width: 100%; height: auto; mix-blend-mode: multiply; filter: brightness(1.06) contrast(1.12); }
figcaption { font-size: 8pt; font-style: italic; color: #5a5148; }
figcaption a { color: #7a1f1f; }
#species { flex: 1 1 0; border: 2px solid #7a1f1f; border-radius: 6px;
           background: #f6ecd9; padding: 0.08in 0.16in; font-size: 9.4pt; line-height: 1.22; }
h2 { margin: 0 0 3px; font-size: 12pt; color: #7a1f1f; font-variant: small-caps;
     border-bottom: 1px solid #c9b68f; break-after: avoid; }
#general h2:not(:first-child) { margin-top: 16px; }
#species h1 { margin: 0 0 3px; font-size: 14pt; color: #7a1f1f; font-variant: small-caps; text-align: center; }
p { margin: 0 0 4px; }
ul { margin: 0; padding-left: 14px; }
li { margin-bottom: 2.5px; break-inside: avoid; }
strong { color: #2b1c10; }
footer { text-align: center; font-size: 7pt; color: #8a8072; }
"""

# Marks the page so the build can detect text spilling out of either box.
OVERFLOW_JS = """
<script>
window.addEventListener('load', () => {
  const bad = ['general','species'].filter(id => {
    const e = document.getElementById(id);
    return e.scrollHeight > e.clientHeight + 1 || e.scrollWidth > e.clientWidth + 1;
  });
  document.documentElement.setAttribute('data-overflow', bad.join(',') || 'none');
});
</script>
"""

TEMPLATE = """<!doctype html>
<html><head><meta charset="utf-8"><title>Discworld Primer - {name}</title>
<style>{css}</style></head>
<body>
<header><h1>Welcome to the Discworld</h1>
<p>A quick primer for players new to Terry Pratchett's world</p></header>
<section id="tone">{tone}</section>
<section id="general">{general}</section>
<section id="bottom">{figure}<section id="species">{species}</section></section>
<footer>Unofficial fan-made handout for private tabletop use. Discworld is the creation of Sir Terry Pratchett.</footer>
{js}
</body></html>
"""


CROPS = {"dwarf": 3}  # species -> % trimmed off every edge of the picture (scan borders)
CUTOUTS = {"igor", "golem", "gargoyle", "zombie", "werewolf", "vampire"}    # species whose picture has a paper background to remove (and is then cropped to the drawing)
INK = {"bizarre"}  # species whose picture is black line art on tinted paper (ink becomes the only opaque part)
FIGURE_WIDTH = {"werewolf": 2.1, "vampire": 2.1, "bizarre": 2.1, "igor": 2.1, "golem": 2.1, "gargoyle": 2.1, "zombie": 2.1}  # species -> picture width in inches (default 1.75)

CREDITS = {  # species -> (artist, link), or a plain caption string when there's no link
    "bizarre": ("Naiche Washburn", "https://www.artstation.com/aggroart"),
    "dwarf": 'Art by <a href="https://www.paulkidby.com/">Paul Kidby</a>, originally featured in <i>The Art of Discworld</i> (2004)',
    "troll": ("Matt Smith", "https://matt-illustration.squarespace.com/"),
    "igor": ("Steven Player", "https://playergallery.com/playergallery/board_game.html#53"),
    "gargoyle": ("Steven Player", "https://playergallery.com/playergallery/board_game.html#36"),
    "werewolf": ("Steven Player", "https://playergallery.com/playergallery/board_game.html#90"),
    "vampire": ("Steven Player", "https://playergallery.com/playergallery/board_game.html#83"),
    "zombie": ("Steven Player", "https://playergallery.com/playergallery/board_game.html#74"),
    "golem": ("Steven Player", "https://playergallery.com/playergallery/board_game.html#33"),
    "human": ("Paul Kidby", "https://www.paulkidby.com/"),
}


def cutout_png(path: Path) -> bytes:
    """Remove a light, low-colour paper background (only the part connected to the edge, so
    highlights inside the drawing survive), then crop to the drawing. Returns PNG bytes."""
    im = Image.open(path).convert("RGB")
    w, h = im.size
    r, g, b = im.split()
    sat = ImageChops.subtract(ImageChops.lighter(ImageChops.lighter(r, g), b),
                              ImageChops.darker(ImageChops.darker(r, g), b))
    lightness = ImageChops.darker(ImageChops.darker(r, g), b)
    paper = ImageChops.multiply(sat.point(lambda v: 255 if v < 48 else 0),
                                lightness.point(lambda v: 255 if v > 150 else 0))
    seeds = [(x, y) for x in range(0, w, 7) for y in (0, h - 1)] + \
            [(x, y) for y in range(0, h, 7) for x in (0, w - 1)]
    for seed in seeds:
        if paper.getpixel(seed) == 255:
            ImageDraw.floodfill(paper, seed, 128)
    alpha = ImageChops.invert(paper.point(lambda v: 255 if v == 128 else 0))
    alpha = alpha.filter(ImageFilter.MinFilter(3)).filter(ImageFilter.GaussianBlur(1.2))
    rgba = im.copy()
    rgba.putalpha(alpha)
    x0, y0, x1, y1 = alpha.point(lambda v: 255 if v > 40 else 0).getbbox()
    pad = int(0.02 * max(w, h))
    rgba = rgba.crop((max(0, x0 - pad), max(0, y0 - pad), min(w, x1 + pad), min(h, y1 + pad)))
    buf = io.BytesIO()
    rgba.save(buf, "PNG")
    return buf.getvalue()


def ink_png(path: Path) -> bytes:
    """Line art on tinted paper: turn ink darkness into transparency so only the lines remain.
    The paper level is the most common light grey; anything darker becomes ink."""
    im = Image.open(path).convert("L")
    w, h = im.size
    hist = im.histogram()
    paper = max(range(100, 256), key=lambda v: hist[v])
    span = 0.55 * paper
    alpha = im.point(lambda v: 0 if paper - v < 14 else min(255, int((paper - v - 14) * 255 / span)))
    rgba = Image.new("RGBA", im.size, (35, 28, 24, 0))
    rgba.putalpha(alpha)
    x0, y0, x1, y1 = alpha.point(lambda v: 255 if v > 60 else 0).getbbox()
    pad = int(0.02 * max(w, h))
    rgba = rgba.crop((max(0, x0 - pad), max(0, y0 - pad), min(w, x1 + pad), min(h, y1 + pad)))
    buf = io.BytesIO()
    rgba.save(buf, "PNG")
    return buf.getvalue()


def figure_html(species: str) -> str:
    """Species picture (species/<name>.jpg, any capitalisation) with credit; '' if none."""
    img = next((p for p in (HERE / "species").glob("*.jp*g") if p.stem.lower() == species), None)
    if img is None:
        return ""
    if species in INK:
        data, mime = base64.b64encode(ink_png(img)).decode(), "png"
    elif species in CUTOUTS:
        data, mime = base64.b64encode(cutout_png(img)).decode(), "png"
    else:
        data, mime = base64.b64encode(img.read_bytes()).decode(), "jpeg"
    c = CROPS.get(species, 0)
    crop = f"width:{100 + 2 * c}%;margin:-{c}%" if c else ""
    credit = ""
    if species in CREDITS:
        entry = CREDITS[species]
        if isinstance(entry, str):
            credit = f"<figcaption>{entry}</figcaption>"
        else:
            artist, link = entry
            credit = f'<figcaption>Drawn by <a href="{link}">{artist}</a></figcaption>'
    width = FIGURE_WIDTH.get(species, 1.75)
    return f'<figure style="width:{width}in"><div class="frame"><img src="data:image/{mime};base64,{data}" alt="{species}" style="{crop}"></div>{credit}</figure>'


def md(text: str) -> str:
    return markdown.markdown(text, extensions=["extra", "smarty"])


def build(species_file: Path) -> None:
    name = species_file.stem.capitalize()
    # general.md: the full-width tone line, a lone '---' line, then the full-width text.
    tone, general = (HERE / "general.md").read_text(encoding="utf-8-sig").split("\n---\n", 1)
    general_html = md(general)
    html = TEMPLATE.format(
        name=name,
        css=CSS,
        general=general_html,
        figure=figure_html(species_file.stem),
        tone=md(tone),
        species=md(species_file.read_text(encoding="utf-8-sig")),
        js=OVERFLOW_JS,
    )
    DIST.mkdir(exist_ok=True)
    html_path = DIST / f"Discworld Primer - {name}.html"
    pdf_path = html_path.with_suffix(".pdf")
    html_path.write_text(html, encoding="utf-8")

    uri = html_path.resolve().as_uri()
    dom = subprocess.run(
        [EDGE, "--headless", "--disable-gpu", "--virtual-time-budget=2000", "--dump-dom", uri],
        capture_output=True, text=True, encoding="utf-8",
    ).stdout
    m = re.search(r'data-overflow="([^"]*)"', dom)
    overflow = m.group(1) if m else "unknown"

    try:
        pdf_path.unlink(missing_ok=True)  # fails if a viewer has the old PDF open
    except PermissionError:
        print(f"[FAIL] {name:<9} overflow={overflow}; can't overwrite {pdf_path.name}: close it in your PDF viewer and rebuild")
        return
    subprocess.run(
        [EDGE, "--headless", "--disable-gpu", "--no-pdf-header-footer",
         f"--print-to-pdf={pdf_path}", uri],
        capture_output=True,
    )
    pages = len(re.findall(rb"/Type\s*/Page[^s]", pdf_path.read_bytes())) if pdf_path.exists() else 0
    flag = "OK" if overflow == "none" and pages == 1 else "CHECK"
    print(f"[{flag}] {name:<9} pages={pages} overflow={overflow}")


if __name__ == "__main__":
    wanted = {a.lower() for a in sys.argv[1:]}
    for f in sorted((HERE / "species").glob("*.md")):
        if not wanted or f.stem in wanted:
            build(f)
