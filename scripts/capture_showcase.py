"""Captures the showcase images of the README and the SAR ADC case study (docs/images/showcase-*.png, hero.gif) from
a running Undine server with Playwright: the 11-bit SAR ADC of docs/tutorials/sar-adc.md.

Run it on a copy of the project's design files, in a throw-away projects root, so that the project itself stays
untouched (the script runs one simulation):

    export UNDINE_ROOT=/tmp/undine-showcase
    mkdir -p $UNDINE_ROOT/sar11
    cp -r ~/undine-projects/sar11/{project.json,undine.lock,libs,flows} $UNDINE_ROOT/sar11/
    pixi run python -m undine serve --port 8720 &
    pixi run -e docs python scripts/capture_showcase.py http://127.0.0.1:8720 docs/images

or only some of them: ... docs/images hero,layout (needs playwright and pillow).
"""
import io
import re
import sys
from pathlib import Path

from PIL import Image
from playwright.sync_api import Page, sync_playwright

URL = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8720").rstrip("/")
OUT = Path(sys.argv[2] if len(sys.argv) > 2 else Path(__file__).resolve().parents[1] / "docs" / "images")
P, LIB = "sar11", "sar"
VIEW = {"width": 1440, "height": 900}


def page(b, hash_: str, scale: int = 2, wait: int = 6000) -> Page:
    pg = b.new_page(viewport=VIEW, device_scale_factor=scale)
    pg.goto(f"{URL}/#/p/{P}/{hash_}")
    pg.wait_for_timeout(wait)
    return pg


def shot(pg: Page, name: str):
    pg.mouse.move(0, VIEW["height"] - 1)  # no hover effects
    pg.wait_for_timeout(600)
    pg.screenshot(path=str(OUT / f"{name}.png"))
    print("wrote", OUT / f"{name}.png")


def hide_boundary(pg: Page):
    """Hides the prBoundary hatch (the setting is remembered, so only toggle when it is shown)."""
    row = pg.locator(".layerrow", has_text="235/4")
    if row.count() and "hidden" not in (row.get_attribute("class") or ""):
        row.locator("button.eye").click()
        pg.wait_for_timeout(1500)


def cursor_um(pg: Page, x: float, y: float) -> tuple[float, float]:
    """The layout coordinate (um) the status bar shows with the pointer at screen point (x, y)."""
    pg.mouse.move(x, y)
    pg.wait_for_timeout(200)
    text = pg.locator(".statusbar, footer").first.inner_text()
    m = re.search(r"X\s*(-?[\d.]+)\s+Y\s*(-?[\d.]+)", text)
    return float(m[1]), float(m[2])


def screen_of(pg: Page, x_um: float, y_um: float) -> tuple[float, float]:
    """The screen point of a layout coordinate, from two pointer positions (the view is not rotated)."""
    (ax, ay), (bx, by) = cursor_um(pg, 500, 300), cursor_um(pg, 900, 700)
    sx, sy = (900 - 500) / (bx - ax), (700 - 300) / (by - ay)
    return 500 + (x_um - ax) * sx, 300 + (y_um - ay) * sy


def hero(b):
    """GIF: from the whole ADC (two CDACs, the switches, comparator and controller) down to the comparator's devices."""
    pg = page(b, f"o/{LIB}/sar_adc/layout", scale=1, wait=8000)
    hide_boundary(pg)
    x, y = screen_of(pg, 180.1, 122.5)  # sar_comp, between the switches and the controller
    pg.mouse.move(x, y)
    frames = []

    def grab(hold=1):
        im = Image.open(io.BytesIO(pg.screenshot())).convert("RGB").resize((960, 600), Image.LANCZOS)
        frames.extend([im] * hold)

    grab(10)
    for _ in range(24):  # small steps: a smooth zoom
        pg.mouse.wheel(0, -70)
        pg.wait_for_timeout(900)
        grab()
    grab(14)
    q = [f.quantize(colors=255, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE) for f in frames]
    q[0].save(OUT / "hero.gif", save_all=True, append_images=q[1:], duration=180, loop=0, optimize=True)
    print("wrote", OUT / "hero.gif")


def adc(b):
    """The whole ADC's layout: 382 x 176 um, DRC and LVS clean."""
    pg = page(b, f"o/{LIB}/sar_adc/layout", wait=8000)
    hide_boundary(pg)
    shot(pg, "showcase-layout")


def comparator(b):
    """The comparator's layout, at the transistors."""
    pg = page(b, f"o/{LIB}/sar_comp/layout", wait=8000)
    shot(pg, "showcase-comparator")


def schematic(b):
    """The top-level schematic: switches, CDACs, comparator and controller."""
    pg = page(b, f"o/{LIB}/sar_adc/schematic")
    pg.mouse.move(780, 490)
    for _ in range(2):
        pg.mouse.wheel(0, -120)
        pg.wait_for_timeout(400)
    pg.wait_for_timeout(1500)
    shot(pg, "showcase-schematic")


def cell(b):
    """The ADC's cell page: what it has to do (the intent, its specs), its views and checks."""
    pg = page(b, f"o/{LIB}/sar_adc/cell", wait=15000)
    shot(pg, "showcase-cell")


def simulation(b):
    """The comparator's testbench run in ngspice on the server: twelve decisions on a +/-0.45 mV input."""
    pg = page(b, f"o/{LIB}/tb_sar_comp/schematic?panel=sim")
    seps = pg.locator('.splitter[role="separator"]')  # widen the side panel: drag its splitter to the left
    box = seps.nth(seps.count() - 1).bounding_box()
    pg.mouse.move(box["x"] + 2, box["y"] + box["height"] / 2)
    pg.mouse.down()
    pg.mouse.move(700, box["y"] + box["height"] / 2, steps=10)
    pg.mouse.up()
    before = pg.locator(".inspector").first.inner_text()
    pg.get_by_text("Run ngspice").click()
    pg.wait_for_timeout(3000)
    for _ in range(900):  # until a new job is listed first with its run time
        text = pg.locator(".inspector").first.inner_text()
        m = re.search(r"JOBS\s+(\d{6})\s+\S+\s+[\d.]+s", text)
        if m and m[1] not in before:
            break
        pg.wait_for_timeout(1000)
    pg.get_by_text("scroll to zoom the plot").wait_for(timeout=120_000)

    def trace(name, on):
        box = pg.locator("label", has=pg.get_by_text(name, exact=True)).locator("input[type=checkbox]")
        if box.count():
            box.first.check() if on else box.first.uncheck()

    for name in re.findall(r"v\([^)]*\)", pg.locator(".inspector").first.inner_text()):
        trace(name, name in ("v(clk)", "v(outp)", "v(outn)"))
    pg.wait_for_timeout(2000)
    pg.locator(".inspector").first.evaluate("e => e.scrollTop = 0")
    shot(pg, "showcase-simulation")


SHOTS = [hero, adc, comparator, schematic, cell, simulation]


def main():
    only = set(sys.argv[3].split(",")) if len(sys.argv) > 3 else None
    OUT.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        b = p.chromium.launch()
        for f in SHOTS:
            if only is None or f.__name__ in only:
                f(b)
        b.close()


if __name__ == "__main__":
    main()
