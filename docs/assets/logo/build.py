#!/usr/bin/env python3
"""
Генератор фирменного знака для yandex-wiki-search-mcp.

Всё построено из параметров — меняйте константы ниже и пересобирайте:
    pip install cairosvg fonttools uharfbuzz
    python3 build.py

Шрифты (Inter, JetBrains Mono — SIL OFL) ожидаются в ./fonts/.
"""
import os

OUT = os.environ.get("LOGO_OUT", os.path.join(os.path.dirname(os.path.abspath(__file__)), "dist"))
SVG = os.path.join(OUT, "svg")
PNG = os.path.join(OUT, "png")
for d in (SVG, PNG):
    os.makedirs(d, exist_ok=True)

# ─────────────────────────────────────────────────────────────── палитра
C = {
    "blue":  "#2A4DDB",   # основной
    "ink":   "#0E1530",   # тёмный фон
    "sky":   "#DDE4FF",   # светлый фон
    "mist":  "#9FB2FF",   # приглушённый акцент на тёмном
    "white": "#FFFFFF",
}


# ────────────────────────────────────────── знак: складка-серпантин
def serpentine(rows, y0, dy, xl, xr):
    """Непрерывный штрих, сложенный `rows` раз. Радиус поворота = dy/2."""
    r = dy / 2
    d = [f"M {xl} {y0}"]
    right, y = True, y0
    for _ in range(rows - 1):
        tx = xr if right else xl
        d.append(f"H {tx}")
        y += dy
        d.append(f"A {r} {r} 0 0 {1 if right else 0} {tx} {y}")
        right = not right
    d.append(f"H {xr if right else xl}")
    return " ".join(d)


MARK, MARK_W = serpentine(4, 142, 76, 168, 344), 44        # основной, 4 сгиба
COMPACT, COMPACT_W = serpentine(3, 150, 106, 182, 330), 58  # для 16–32 px


def stroke(d, color, w):
    return (f'<path d="{d}" fill="none" stroke="{color}" stroke-width="{w}" '
            f'stroke-linecap="round" stroke-linejoin="round"/>')


def mark(color, compact=False):
    return stroke(COMPACT if compact else MARK, color,
                  COMPACT_W if compact else MARK_W)


# ────────────────────────────────── альтернативный знак: скобки + строки
BR_L = "M 166 116 H 138 A 28 28 0 0 0 110 144 V 368 A 28 28 0 0 0 138 396 H 166"
BR_R = "M 346 116 H 374 A 28 28 0 0 1 402 144 V 368 A 28 28 0 0 1 374 396 H 346"
BARS = [(178, 176, 334), (178, 256, 334), (178, 336, 268)]


def mark_alt(color):
    s = stroke(BR_L, color, 42) + stroke(BR_R, color, 42)
    for x1, y, x2 in BARS:
        s += stroke(f"M {x1} {y} H {x2}", color, 44)
    return s


# ─────────────────────────────────────────────────────────────── обёртки
def svg(body, w=512, h=512, extra=""):
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" '
            f'width="{w}" height="{h}" fill="none"{extra}>{body}</svg>\n')


def write(path, body, w=512, h=512, extra=""):
    with open(path, "w") as f:
        f.write(svg(body, w, h, extra))
    return path


sq = lambda fill: f'<rect width="512" height="512" rx="115" fill="{fill}"/>'
circ = lambda fill: f'<circle cx="256" cy="256" r="256" fill="{fill}"/>'
S = lambda n: os.path.join(SVG, n)

write(S("logo-primary.svg"), sq(C["blue"]) + mark(C["white"]))
write(S("logo-dark.svg"),    sq(C["ink"])  + mark(C["white"]))
write(S("logo-light.svg"),   sq(C["sky"])  + mark(C["blue"]))
write(S("logo-circle.svg"),  circ(C["blue"]) + mark(C["white"]))
write(S("logo-mono.svg"),    mark("currentColor"), extra=' color="#0E1530"')
write(S("logo-compact.svg"), sq(C["blue"]) + mark(C["white"], compact=True))
write(S("logo-compact-mono.svg"), mark("currentColor", compact=True),
      extra=' color="#0E1530"')

write(S("logo-alt-primary.svg"), sq(C["blue"]) + mark_alt(C["white"]))
write(S("logo-alt-light.svg"),   sq(C["sky"])  + mark_alt(C["blue"]))
write(S("logo-alt-mono.svg"),    mark_alt("currentColor"), extra=' color="#0E1530"')


# ────────────────────────────────────────────── леттеринг → кривые
import uharfbuzz as hb
from fontTools.ttLib import TTFont
from fontTools.varLib import instancer
from fontTools.pens.svgPathPen import SVGPathPen

INTER, MONO = "fonts/Inter.ttf", "fonts/JetBrainsMono.ttf"
_cache = {}


def text_path(text, font_file, size, wght, tracking=0.0, opsz=None):
    """Текст, переведённый в кривые. Возвращает (svg, ширина)."""
    axes = dict(wght=wght)
    if opsz:
        axes["opsz"] = opsz
    key = (font_file, tuple(sorted(axes.items())))
    if key not in _cache:
        _cache[key] = instancer.instantiateVariableFont(
            TTFont(font_file), axes, inplace=False)
    tt = _cache[key]
    upem = tt["head"].unitsPerEm
    sc = size / upem

    f = hb.Font(hb.Face(hb.Blob.from_file_path(font_file)))
    f.scale = (upem, upem)
    f.set_variations(axes)
    buf = hb.Buffer()
    buf.add_str(text)
    buf.guess_segment_properties()
    hb.shape(f, buf)

    gs, order = tt.getGlyphSet(), tt.getGlyphOrder()
    out, x, tu = [], 0.0, tracking * upem
    for info, pos in zip(buf.glyph_infos, buf.glyph_positions):
        pen = SVGPathPen(gs)
        gs[order[info.codepoint]].draw(pen)
        gd = pen.getCommands()
        if gd:
            out.append(f'<g transform="translate({(x+pos.x_offset)*sc:.2f} '
                       f'{-pos.y_offset*sc:.2f}) scale({sc:.6f} {-sc:.6f})">'
                       f'<path d="{gd}"/></g>')
        x += pos.x_advance + tu
    return "".join(out), (x - tu) * sc


# ── лок-ап 1: знак + имя пакета (моноширинный) ────────────────────────
MS, ICON, GAP = 60, 128, 32
pkg_d, pkg_w = text_path("yandex-wiki-search-mcp", MONO, MS, 600, tracking=-0.012)
RPAD = 12
W1, H1 = ICON + GAP + pkg_w + RPAD, ICON


def lockup_pkg(icon_bg, mark_c, text_c):
    g = f'<g transform="scale({ICON/512:.6f})">{sq(icon_bg)}{mark(mark_c)}</g>'
    g += (f'<g transform="translate({ICON+GAP:.2f} {H1/2 + MS*0.36:.2f})" '
          f'fill="{text_c}">{pkg_d}</g>')
    return g


write(S("lockup-package.svg"),      lockup_pkg(C["blue"], C["white"], C["ink"]),
      round(W1), round(H1))
write(S("lockup-package-dark.svg"), lockup_pkg(C["white"], C["blue"], C["white"]),
      round(W1), round(H1))

# ── лок-ап 2: знак + «Yandex Wiki / MCP SERVER» ───────────────────────
T1, T2, ICON2, GAP2 = 72, 26, 144, 36
t1_d, t1_w = text_path("Yandex Wiki", INTER, T1, 700, tracking=-0.022, opsz=32)
t2_d, t2_w = text_path("MCP SERVER", INTER, T2, 600, tracking=0.17, opsz=14)
W2, H2 = ICON2 + GAP2 + max(t1_w, t2_w) + RPAD, ICON2


def lockup_name(icon_bg, mark_c, text_c, sub_c):
    g = f'<g transform="scale({ICON2/512:.6f})">{sq(icon_bg)}{mark(mark_c)}</g>'
    g += (f'<g transform="translate({ICON2+GAP2:.2f} {H2/2-8:.2f})" '
          f'fill="{text_c}">{t1_d}</g>')
    g += (f'<g transform="translate({ICON2+GAP2:.2f} {H2/2+34:.2f})" '
          f'fill="{sub_c}">{t2_d}</g>')
    return g


write(S("lockup-name.svg"),
      lockup_name(C["blue"], C["white"], C["ink"], C["blue"]), round(W2), round(H2))
write(S("lockup-name-dark.svg"),
      lockup_name(C["white"], C["blue"], C["white"], C["mist"]), round(W2), round(H2))

# ── обложка репозитория (Open Graph, 1280×640) ────────────────────────
SW, SH, SI = 1280, 640, 132
st_d, st_w = text_path("yandex-wiki-search-mcp", MONO, 58, 600, tracking=-0.012)
ss_d, ss_w = text_path("MCP-сервер для Яндекс Вики", INTER, 34, 500, opsz=32)
bx = (SW - (SI + 34 + max(st_w, ss_w))) / 2
by = SH / 2
social = (f'<rect width="{SW}" height="{SH}" fill="{C["ink"]}"/>'
          f'<g transform="translate({bx:.1f} {by-SI/2:.1f}) scale({SI/512:.6f})">'
          f'{sq(C["blue"])}{mark(C["white"])}</g>'
          f'<g transform="translate({bx+SI+34:.1f} {by-6:.1f})" fill="{C["white"]}">{st_d}</g>'
          f'<g transform="translate({bx+SI+36:.1f} {by+42:.1f})" fill="{C["mist"]}">{ss_d}</g>')
write(S("social-preview.svg"), social, SW, SH)


# ────────────────────────────────────────────────────── растеризация
import cairosvg

for px in (1024, 512, 256, 128, 64):
    cairosvg.svg2png(url=S("logo-primary.svg"),
                     write_to=os.path.join(PNG, f"icon-{px}.png"),
                     output_width=px, output_height=px)
for px in (48, 32, 16):                      # мелочь — с компактного знака
    cairosvg.svg2png(url=S("logo-compact.svg"),
                     write_to=os.path.join(PNG, f"icon-{px}.png"),
                     output_width=px, output_height=px)
cairosvg.svg2png(url=S("lockup-package.svg"),
                 write_to=os.path.join(PNG, "lockup-package@2x.png"),
                 output_width=round(W1 * 2))
cairosvg.svg2png(url=S("social-preview.svg"),
                 write_to=os.path.join(PNG, "social-preview.png"),
                 output_width=SW)

print(f"lockup-package {round(W1)}x{round(H1)} | lockup-name {round(W2)}x{round(H2)}")
print("svg:", len(os.listdir(SVG)), " png:", len(os.listdir(PNG)))
