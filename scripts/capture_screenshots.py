"""Captures the README screenshots (docs/images/*.png) from a running Undine server with Playwright.

Setup, in a throw-away projects root so that your own projects stay untouched:

    export UNDINE_ROOT=/tmp/undine-shots
    pixi run python -m undine new demo --pdk sky130A
    pixi run python -m undine serve --port 8720 &
    pixi run python examples/make_inverter.py demo mylib
    pixi run python examples/route_inverter.py demo mylib http://127.0.0.1:8720
    pixi run python examples/make_tt_inverter.py demo mylib http://127.0.0.1:8720
    pixi run -e docs screenshots     # = python scripts/capture_screenshots.py http://127.0.0.1:8720 docs/images

or by hand: python scripts/capture_screenshots.py URL OUT_DIR [layout,schematic,...] (needs playwright).

The script opens a changeset, runs a simulation, the tapeout sign-off suite and DRC on the demo project (and adds,
then removes, one deliberate DRC violation), so run it against that throw-away project only. The history shot comes
last: it puts the demo project in git ($UNDINE_ROOT/demo, so set UNDINE_ROOT as above), merges two branches and leaves
edits uncommitted, which the other shots would then show.
"""
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

URL = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8720").rstrip("/")
OUT = Path(sys.argv[2] if len(sys.argv) > 2 else Path(__file__).resolve().parents[1] / "docs" / "images")
P, LIB = "demo", "mylib"
VIEW = {"width": 1440, "height": 900}


def api(path, body=None, actor="user"):
    req = urllib.request.Request(f"{URL}/api/p/{path}", data=None if body is None else json.dumps(body).encode(),
                                 headers={"content-type": "application/json", "X-Undine-Actor": actor})
    with urllib.request.urlopen(req) as r:
        return json.load(r)


def wait_job(job_id):
    while (j := api(f"{P}/jobs/{job_id}"))["state"] in ("queued", "running"):
        time.sleep(1)
    return j


def changeset() -> str:
    """An agent's proposal with checks: M2 widened in the schematic and synced into the layout."""
    for c in api(f"{P}/changesets"):
        if c["status"] == "open":
            return c["id"]
    a = "agent:optimizer"
    cs = api(f"{P}/changesets", {"title": "Widen the NMOS pull-down", "reason": "balance rise and fall times"}, a)
    pv = cs["preview"]
    api(f"{pv}/l/{LIB}/c/inv/schematic/edit",
        {"op": "set_props", "kind": "i", "id": "M2", "props": {"W": "2"}, "reason": "match the PMOS drive"}, a)
    api(f"{pv}/l/{LIB}/sdl/inv/sync", {}, a)
    api(f"{P}/changesets/{cs['id']}/verify", {})
    while any(j.get("state") in ("queued", "running") for j in api(f"{P}/changesets/{cs['id']}").get("verify", [])):
        time.sleep(1)
    return cs["id"]


def page(pw_browser, hash_: str) -> Page:
    pg = pw_browser.new_page(viewport=VIEW, device_scale_factor=2)
    pg.goto(f"{URL}/#/p/{P}/{hash_}")
    pg.wait_for_timeout(3000)
    return pg


def shot(pg: Page, name: str):
    pg.mouse.move(0, VIEW["height"] - 1)  # no hover effects
    pg.wait_for_timeout(600)
    pg.screenshot(path=str(OUT / f"{name}.png"))
    print("wrote", OUT / f"{name}.png")


def hide_boundary(pg: Page):
    """Hides the prBoundary hatch (the setting is remembered, so only toggle when it is shown)."""
    row = pg.locator(".layerrow", has_text="235/4")
    if "hidden" not in (row.get_attribute("class") or ""):
        row.locator("button.eye").click()
        pg.wait_for_timeout(1500)


def field(pg: Page, label: str, value: str):
    el = pg.locator(f'label.stack:has(span:text-is("{label}"))').locator("input, textarea").first
    el.fill(value)
    el.blur()


def layout(b):
    """Layout, with the pull-down transistor selected (the schematic name M2 comes from cross-probing)."""
    pg = page(b, f"o/{LIB}/inv/layout")
    pg.mouse.click(790, 780)
    pg.locator('button[title="Properties"]').click()
    shot(pg, "layout")


def schematic(b):
    pg = page(b, f"o/{LIB}/inv/schematic")
    pg.mouse.click(745, 650)
    shot(pg, "schematic")


def simulation(b):
    pg = page(b, f"o/{LIB}/tb_inv/schematic?panel=sim")
    pg.get_by_text("Run ngspice").click()
    pg.wait_for_timeout(1500)
    for _ in range(120):  # until the newest job (first in the list) shows its run time
        if re.search(r"jobs\s+\d{6}\s+\S+\s+[\d.]+s", pg.locator(".inspector").first.inner_text(), re.I):
            break
        pg.wait_for_timeout(1000)
    pg.get_by_text("scroll to zoom the plot").wait_for(timeout=30_000)  # the waveform is drawn
    def trace(name, on):
        box = pg.locator("label", has=pg.get_by_text(name, exact=True)).locator("input[type=checkbox]")
        box.check() if on else box.uncheck()
    trace("v(out)", True)
    trace("v(vdd)", False)
    pg.wait_for_timeout(2000)
    pg.locator(".inspector").first.evaluate("e => e.scrollTop = 0")
    chart = pg.locator(".inspector canvas").first.bounding_box()
    pg.mouse.move(chart["x"] + 52 + 0.305 * (chart["width"] - 60), chart["y"] + chart["height"] / 2)
    for _ in range(6):  # zoom in on the falling input edge at 6 ns: the output rises after the gate delay
        pg.mouse.wheel(0, -120)
        pg.wait_for_timeout(150)
    pg.wait_for_timeout(1000)
    shot(pg, "simulation")


def changes(b):
    changeset()
    pg = page(b, f"o/{LIB}/inv/schematic")
    pg.get_by_role("button", name="Changes").click()
    pg.get_by_text("Widen the NMOS pull-down").first.click()
    pg.wait_for_timeout(1500)
    shot(pg, "changeset")


def tapeout(b):
    """Tiny Tapeout tile; layout checks and the sign-off suite."""
    pg = page(b, f"o/{LIB}/tt_um_undine_inv/layout")
    hide_boundary(pg)
    pg.locator('button[title="Tapeout: shuttle checks and bundle"]').click()
    pg.wait_for_timeout(1500)
    field(pg, "ua[] descriptions: text for ua[0], ua[1], ... separated by commas", "input A, output Y")
    field(pg, "Title", "Undine inverter")
    field(pg, "Author", "Undine demo")
    field(pg, "Description", "A CMOS inverter from ua[0] to ua[1], drawn in Undine")
    pg.wait_for_timeout(1500)
    pg.get_by_text("Run all checks").click()
    pg.wait_for_timeout(3000)
    quiet = 0
    while quiet < 3:  # no job queued or running for a few seconds
        pg.wait_for_timeout(2000)
        busy = re.search(r"running|queued", pg.locator(".inspector").first.inner_text(), re.I)
        quiet = 0 if busy else quiet + 1
    pg.locator(".inspector").first.evaluate("e => e.scrollTop = e.scrollHeight")
    pg.wait_for_timeout(2500)
    shot(pg, "tapeout")


def drc(b):
    """One met1 box 0.07 um from the Y route (min spacing 0.14 um), removed again afterwards."""
    lay = f"{P}/l/{LIB}/layout/inv"
    box = [-120, 2100, 90, 2800]
    met1 = next(l["id"] for l in api(f"{lay}/layers") if l["source"] == "68/20")
    api(f"{lay}/create", {"kind": "box", "layer": met1, "pts": box})
    try:
        pg = page(b, f"o/{LIB}/inv/layout?panel=verify")
        pg.get_by_text("Run DRC on inv").click()
        pg.get_by_text(re.compile(r"\d+ violations in \d+ rules")).first.wait_for(timeout=300_000)
        pg.wait_for_timeout(2500)
        shot(pg, "drc")
    finally:
        bad = api(f"{lay}/select", {"box": box, "point": False})
        api(f"{lay}/delete", {"refs": [o["ref"] for o in bad if o.get("layer") == "68/20" and o["bbox"] == box]})
        api(f"{P}/l/{LIB}/save", {})


def git(*args: str, ok: bool = True) -> str:
    """git in the demo project's directory, after checking that it is the server's project (git runs on the files)."""
    root = os.environ.get("UNDINE_ROOT")
    if not root:
        raise SystemExit("history: set UNDINE_ROOT to the server's (throw-away) projects root")
    d = Path(root).expanduser() / P
    if json.loads((d / "project.json").read_text()).get("created") != api(P)["created"]:
        raise SystemExit(f"history: {d} is not the server's project {P}")
    r = subprocess.run(["git", "-C", str(d), "-c", "user.name=Ada Designer", "-c", "user.email=ada@example.com", *args],
                       capture_output=True, text=True)
    if ok and r.returncode:
        raise SystemExit(f"git {' '.join(args)}: {r.stderr.strip()}")
    return r.stdout.strip()


def history(b):
    """History panel: git set up through the API and two branches that changed different cells merged (the merge
    commit with its cells to re-check), then uncommitted edits of inv (schematic, intent, a layout box saved and one
    not) and tb_inv, with inv's semantic diff open."""
    def prop(cell, inst, **props):
        api(f"{P}/l/{LIB}/c/{cell}/schematic/edit", {"op": "set_props", "kind": "i", "id": inst, "props": props})

    if not api(f"{P}/git")["repo"]:
        api(f"{P}/git/setup", {})
    if not git("branch", "--list", "faster-edges"):
        git("add", "-A")
        git("commit", "-qm", "Inverter: schematic, symbol and routed layout", ok=False)    # nothing new: fine
        main = git("symbolic-ref", "--short", "HEAD")
        git("checkout", "-qb", "faster-edges")
        prop("tb_inv", "Vin", value="pulse(0 1.8 1n 50p 50p 5n 10n)")
        git("add", "-A")
        git("commit", "-qm", "tb_inv: faster input edges")
        git("checkout", "-q", main)
        prop("inv", "M1", W="3")
        git("add", "-A")
        git("commit", "-qm", "inv: widen the PMOS to 3 um")
        git("merge", "-q", "--no-edit", "faster-edges")
    pg = page(b, f"o/{LIB}/tb_inv/schematic?history=1")
    pg.locator(".drawer.history .gitcommit .gitrow", has_text="Merge branch 'faster-edges'").first.click()
    pg.get_by_role("button", name="Cells to re-check").click()
    pg.get_by_text(re.compile(r"\d+ cells? to re-check")).wait_for()
    shot(pg, "history-merge")
    prop("inv", "M2", W="1.5")
    prop("tb_inv", "CL", value="30f")
    text = "Area over speed: the load is small"
    if not any(i.get("text") == text for i in api(f"{P}/l/{LIB}/c/inv/intent")["intent"]["items"]):
        api(f"{P}/l/{LIB}/c/inv/intent/edit", {"op": "add_item", "item": {"kind": "preference", "text": text}})
    lay = f"{P}/l/{LIB}/layout/inv"
    met1 = next(l["id"] for l in api(f"{lay}/layers") if l["source"] == "68/20")
    api(f"{lay}/create", {"kind": "box", "layer": met1, "pts": [-260, 1400, 260, 1700]})
    api(f"{P}/l/{LIB}/save", {})
    api(f"{lay}/create", {"kind": "box", "layer": met1, "pts": [600, 1400, 900, 1700]})    # not saved: the ring
    pg = page(b, f"o/{LIB}/inv/schematic?history=1")
    pg.locator(".drawer.history .gitcellrow .gitrow", has_text=f"{LIB}/inv").first.click()
    pg.wait_for_timeout(2500)
    shot(pg, "history")


SHOTS = [layout, schematic, simulation, changes, tapeout, drc, history]   # history last (see above)


def main():
    only = set(sys.argv[3].split(",")) if len(sys.argv) > 3 else None  # e.g. tapeout,drc
    OUT.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        b = p.chromium.launch()
        for f in SHOTS:
            if only is None or f.__name__ in only:
                f(b)
        b.close()


if __name__ == "__main__":
    main()
