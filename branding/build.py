"""Baut das Musikbot-Logo: ein Equalizer – fuenf runde Balken unterschiedlicher Hoehe.

Familie von Diddy: dieselbe Squircle-Kachel, dieselben Farbverlaeufe und Farbtoene, ein weisses,
fettes, rundes Zeichen – aber ein eigenes Zeichen.

Aufruf (im Repo-Ordner): python3 branding/build.py
Schreibt branding/logo/*.svg und kopiert den Standard-Ton (DEFAULT_TONE) als Icon ins Dashboard
(musikbot/static/icon.svg) und nach assets/icon.svg. PNG-Bilder: branding/export.sh.
Die SVG-Dateien nie von Hand aendern, sie werden ueberschrieben.
"""
import math
import os

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT = os.path.join(HERE, "logo")
SIZE = 1024
DEFAULT_TONE = "pink"   # Ton fuer Dashboard, Favicon und Bot-Profilbild

# Farbtoene wie bei Diddy (gleiche Namen), Kachel und Zeichen jeweils von oben nach unten.
PINK = ("#FF6FC0", "#E60A78")
TONES = {
    "pink": {"tile": ("#FF80CA", "#E60A78"), "letter": ("#FFFFFF", "#FFE4F2"), "inner": None},
    "blue": {"tile": ("#4D8DFF", "#1239B5"), "letter": ("#FFFFFF", "#DFEAFF"), "inner": None},
    "purple": {"tile": ("#B566FF", "#6418C9"), "letter": ("#FFFFFF", "#EEE0FF"), "inner": None},
    "black": {"tile": ("#2F2F35", "#0C0C0E"), "letter": ("#FFFFFF", "#E4E4EA"), "inner": PINK},
    "magenta": {"tile": ("#A8155F", "#4F0A30"), "letter": ("#FFF3F9", "#FFD3EA"), "inner": None},
}

def n(value):
    return f"{value:.2f}".rstrip("0").rstrip(".")


def towards(a, b, distance):
    length = math.hypot(b[0] - a[0], b[1] - a[1])
    return a[0] + (b[0] - a[0]) * distance / length, a[1] + (b[1] - a[1]) * distance / length


def xy(p):
    return f"{n(p[0])} {n(p[1])}"


def eq():
    """Fuenf Equalizer-Balken, unten buendig, runde Enden."""
    width, gap, base = 96, 42, 290
    heights = (300, 580, 420, 500, 230)
    total = len(heights) * width + (len(heights) - 1) * gap
    out = []
    for i, h in enumerate(heights):
        x = -total / 2 + i * (width + gap)
        r = width / 2
        out.append(f"M{n(x)} {n(base - r)}V{n(base - h + r)}A{r} {r} 0 0 1 {n(x + width)} {n(base - h + r)}"
                   f"V{n(base - r)}A{r} {r} 0 0 1 {n(x)} {n(base - r)}Z")
    return out


def squircle(x, y, size, radius, smoothing=0.6):
    """Abgerundetes Quadrat mit fliessenden Ecken (Form eines iOS-/macOS-App-Icons)."""
    budget = size / 2
    smoothing = min(smoothing, budget / radius - 1)
    p = min((1 + smoothing) * radius, budget)
    arc = 90 * (1 - smoothing)
    arc_len = math.sin(math.radians(arc / 2)) * radius * math.sqrt(2)
    alpha = (90 - arc) / 2
    p3_p4 = radius * math.tan(math.radians(alpha / 2))
    beta = 45 * smoothing
    c = p3_p4 * math.cos(math.radians(beta))
    d = c * math.tan(math.radians(beta))
    b = (p - arc_len - c - d) / 3
    a = 2 * b
    r, s = n(radius), n(arc_len)
    abc, ab, bc = a + b + c, a + b, b + c
    return (
        f"M{n(x + size - p)} {n(y)}"
        f"c{n(a)} 0 {n(ab)} 0 {n(abc)} {n(d)}a{r} {r} 0 0 1 {s} {s}c{n(d)} {n(c)} {n(d)} {n(bc)} {n(d)} {n(abc)}"
        f"V{n(y + size - p)}"
        f"c0 {n(a)} 0 {n(ab)} {n(-d)} {n(abc)}a{r} {r} 0 0 1 -{s} {s}c{n(-c)} {n(d)} {n(-bc)} {n(d)} {n(-abc)} {n(d)}"
        f"H{n(x + p)}"
        f"c{n(-a)} 0 {n(-ab)} 0 {n(-abc)} {n(-d)}a{r} {r} 0 0 1 -{s} -{s}c{n(-d)} {n(-c)} {n(-d)} {n(-bc)} {n(-d)} {n(-abc)}"
        f"V{n(y + p)}"
        f"c0 {n(-a)} 0 {n(-ab)} {n(d)} {n(-abc)}a{r} {r} 0 0 1 {s} -{s}c{n(c)} {n(-d)} {n(bc)} {n(-d)} {n(abc)} {n(-d)}"
        "Z"
    )


def gradient(name, stops, x1, y1, x2, y2):
    return (
        f'<linearGradient id="{name}" gradientUnits="userSpaceOnUse" x1="{n(x1)}" y1="{n(y1)}" '
        f'x2="{n(x2)}" y2="{n(y2)}"><stop stop-color="{stops[0]}"/>'
        f'<stop offset="1" stop-color="{stops[1]}"/></linearGradient>'
    )


def svg(body, defs="", uid=""):
    out = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {SIZE} {SIZE}" width="{SIZE}" '
           f'height="{SIZE}"><title>Musikbot</title>{defs}{body}</svg>\n')
    # Eindeutige IDs, damit mehrere Logos auf einer Seite sich nicht die Verlaeufe teilen
    for name in ("tile", "letter", "inner", "shadow"):
        out = out.replace(f'id="{name}"', f'id="{name}{uid}"').replace(f"url(#{name})", f"url(#{name}{uid})")
    return out


DY = -10   # Balken sitzen optisch etwas hoeher als die Mitte


def mark_paths(fill):
    return "".join(f'<path d="{d}" fill="{fill}"/>' for d in eq())


def icon_svg(tone, shape, uid=""):
    colors = TONES[tone]
    defs = [gradient("letter", colors["letter"], 0, -330, 0, 330)]
    mark = mark_paths("url(#letter)")
    if shape == "mac":
        box, scale = (100, 100, 824), 0.8
        tile = f'<path d="{squircle(*box, 185.4)}" fill="url(#tile)" filter="url(#shadow)"/>'
        defs.append(
            '<filter id="shadow" x="-10%" y="-10%" width="120%" height="125%">'
            '<feGaussianBlur in="SourceAlpha" stdDeviation="12"/><feOffset dy="10"/>'
            '<feComponentTransfer><feFuncA type="linear" slope=".28"/></feComponentTransfer>'
            '<feMerge><feMergeNode/><feMergeNode in="SourceGraphic"/></feMerge></filter>')
    elif shape == "rounded":
        box, scale = (0, 0, SIZE), 1.0
        tile = f'<path d="{squircle(*box, 230)}" fill="url(#tile)"/>'
    else:
        box, scale = (0, 0, SIZE), 1.0
        tile = f'<rect width="{SIZE}" height="{SIZE}" fill="url(#tile)"/>'
    x, y, size = box
    defs.insert(0, gradient("tile", colors["tile"], x + size * 0.28, y, x + size * 0.72, y + size))
    placed = f'<g transform="translate(512 {n(512 + DY * scale)}) scale({n(scale)})">{mark}</g>'
    return svg(tile + placed, f'<defs>{"".join(defs)}</defs>', uid)


def mark_svg():
    """Nur das Zeichen, einfarbig (currentColor)."""
    return svg(f'<g transform="translate(512 {n(512 + DY * 1.2)}) scale(1.2)">{mark_paths("currentColor")}</g>')


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)


def main():
    files = []
    for tone in TONES:
        for shape in ("square", "rounded", "mac"):
            path = os.path.join(OUT, f"musikbot-{tone}-{shape}.svg")
            write(path, icon_svg(tone, shape))
            files.append(path)
    path = os.path.join(OUT, "musikbot-mark.svg")
    write(path, mark_svg())
    files.append(path)
    default = icon_svg(DEFAULT_TONE, "rounded")
    write(os.path.join(ROOT, "musikbot", "static", "icon.svg"), default)
    write(os.path.join(ROOT, "assets", "icon.svg"), default)
    print(f"{len(files)} SVG-Dateien in {os.path.relpath(OUT)}; Icon ({DEFAULT_TONE}) ins Dashboard kopiert")


if __name__ == "__main__":
    main()
