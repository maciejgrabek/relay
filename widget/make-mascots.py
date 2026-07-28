#!/usr/bin/env python3
"""Generate painted mascot art for the desktop widget, via fal.ai.

    python3 widget/make-mascots.py --skin crt            # one skin, all moods
    python3 widget/make-mascots.py --skin crt --only guarding
    python3 widget/make-mascots.py --list
    python3 widget/make-mascots.py --skin crt --dry-run  # prompts only, no API

Output: widget/mascots/<skin>/<mood>.png, transparent background, committed as
assets. The ASCII creature stays the source of truth and the fallback - this
only gives the widget a nicer body to draw when one exists.

THE KEY NEVER TOUCHES THIS REPO.
Resolution order, all outside the tree: $FAL_KEY, then $FAL_KEY_FILE, then
~/.relay/fal.key (mode 600). No path inside the repo is ever consulted, so
there is no ignore rule to forget. The key is never logged, never written to
output metadata, and never included in an error message. lib/secret_scan.sh
runs as a pre-commit hook to catch a pasted key regardless.

Stdlib only (urllib), like the rest of relay - no fal_client, no pip install.

Character consistency is the hard part, not the API call. Generating eight
moods independently gives eight different creatures. So: ONE reference image
per skin, then every other mood is an EDIT of that reference. Same creature,
different face.
"""
import argparse
import base64
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_ROOT = os.path.join(HERE, "mascots")

# fal.run is the synchronous endpoint; these models return in seconds.
# NOT flux-pro/v1 - fal.run reads the trailing "/v1" as a URL path and 404s.
# All three verified against the live API before this script was finished.
TEXT2IMG = "https://fal.run/fal-ai/flux/schnell"
IMG2IMG = "https://fal.run/fal-ai/flux-pro/kontext"
REMBG = "https://fal.run/fal-ai/imageutils/rembg"

# The eight states effective_mascot_state can produce, and what each must read
# as at a glance. Wording is deliberately about EXPRESSION, not scene: the
# creature is the same creature in every one of them.
MOODS = {
    "idle":      "relaxed, eyes half-lidded, off duty, faintly bored",
    "guarding":  "alert and calm, watchful eyes looking slightly to the side",
    "working":   "focused and busy, eyes narrowed in concentration, in motion",
    "alarmed":   "wide-eyed and urgent, alert, needs attention right now",
    "critical":  "distressed, X-shaped eyes, overloaded, sparking",
    "paused":    "frozen mid-motion, eyes closed, deliberately still, dimmed",
    "flinch":    "recoiling, startled, leaning back from something dangerous",
    "celebrate": "delighted, eyes bright and curved, arms up, a small triumph",
}

# One line per skin describing the BODY only. Mood comes from MOODS above, so
# the two never fight. Names match config.MASCOT_NAMES.
SKINS = {
    "crt":     "a small retro CRT computer terminal with a face on its screen, chunky beige-green casing, tiny antenna on top",
    "invader": "a chunky pixel-art space invader creature, blocky arcade alien",
    "owl":     "a small round owl with big round eyes and tufted ears",
    "cat":     "a small round cat with pointed ears and a curled tail",
    "core":    "a glowing reactor core orb held in a metal frame",
    "beacon":  "a small lighthouse beacon with a rotating lamp and a face",
    "ghost":   "a small rounded ghost with a wavy lower edge",
    "crab":    "a small round crab with raised claws",
    "droid":   "a small boxy service robot with an antenna and treads",
    "bug":     "a small round beetle with folded wing cases and antennae",
    "skull":   "a small friendly cartoon skull, not gruesome",
    "toaster": "a chrome pop-up toaster with a face on its front panel",
    "atom":    "a glowing atom with orbiting electron rings and a face in the nucleus",
    "moth":    "a small fuzzy moth with broad patterned wings",
    "tank":    "a tiny cartoon tank with a stubby barrel and a face on the hull",
}

# Shared style. Flat background on purpose - rembg cuts it away cleanly, and a
# hard-edged subject mattes far better than one blended into a gradient.
STYLE = (
    "cute mascot character, thick clean outlines, flat cel shading, "
    "limited palette of phosphor green (#6effa0) and near-black, "
    "centred single character, full body, facing viewer, "
    "solid flat magenta background, no text, no letters, no watermark, "
    "no shadow on the background, crisp edges, sticker art"
)


def load_key() -> str:
    """Read the fal.ai key from OUTSIDE the repo. Never prints or returns it
    anywhere it could be logged."""
    k = os.environ.get("FAL_KEY", "").strip()
    if k:
        return k
    for path in (os.environ.get("FAL_KEY_FILE"),
                 os.path.expanduser("~/.relay/fal.key")):
        if path and os.path.isfile(path):
            with open(path) as f:
                k = f.read().strip()
            if k:
                return k
    sys.exit(
        "no fal.ai key found.\n"
        "  put it at ~/.relay/fal.key (chmod 600), or set $FAL_KEY.\n"
        "  it must live OUTSIDE this repo - never add it to the tree.")


def post(url: str, body: dict, key: str) -> dict:
    """One fal call. On failure, raise WITHOUT echoing the request headers -
    an exception that prints Authorization has leaked the key to your logs."""
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(),
        headers={"Authorization": f"Key {key}",
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:400]
        raise SystemExit(f"fal {url.rsplit('/', 2)[-2]}: HTTP {e.code}\n  {detail}")
    except urllib.error.URLError as e:
        raise SystemExit(f"fal unreachable: {e.reason}")


def first_image_url(res: dict) -> str:
    for k in ("images", "image"):
        v = res.get(k)
        if isinstance(v, list) and v:
            return v[0]["url"]
        if isinstance(v, dict):
            return v["url"]
    raise SystemExit(f"unexpected fal response shape: {list(res)}")


def fetch(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=120) as r:
        return r.read()


# The widget window is ~300px, so 1024px assets are ~9x bigger than anything it
# can show. At 15 skins x 8 moods that is ~54MB of git history for pixels nobody
# renders. sips ships with macOS (which relay requires anyway) and preserves the
# alpha channel, so no imaging library is needed.
ASSET_PX = 256


def downscale(path: str) -> None:
    """Shrink in place to ASSET_PX. Best-effort: if sips is missing we keep the
    full-size file rather than losing the generation we just paid for."""
    try:
        subprocess.run(["sips", "-Z", str(ASSET_PX), path, "--out", path],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       check=True)
    except Exception:
        print(f"    (sips unavailable - kept {ASSET_PX}px+ original)")


def data_uri(path: str) -> str:
    """A saved reference re-encoded for the edit endpoint. fal accepts data
    URIs, so the reference lives on disk instead of depending on a signed URL
    that expires."""
    with open(path, "rb") as f:
        return "data:image/png;base64," + base64.b64encode(f.read()).decode()


def cutout(png_url: str, key: str) -> bytes:
    """Flat background -> real alpha. Generating WITH transparency is not
    reliable on these models; matting a flat colour afterwards is."""
    res = post(REMBG, {"image_url": png_url}, key)
    return fetch(first_image_url(res))


# Every other mood is an edit of THIS one. Fixed, not "whichever mood happened
# to be first in the list" - otherwise regenerating a single mood later reseeds
# the character and you get a different creature than the one already on disk.
REFERENCE_MOOD = "guarding"


def generate(skin: str, moods, dry_run: bool, key) -> None:
    body = SKINS[skin]
    out_dir = os.path.join(OUT_ROOT, skin)
    ref_path = os.path.join(out_dir, "_reference.png")

    # The reference is generated once and kept. A rerun reuses it, so moods
    # produced weeks apart are still the same creature, and --only <mood> is
    # safe to run on its own.
    if not dry_run and not os.path.isfile(ref_path):
        print(f"[{skin}] reference ({REFERENCE_MOOD})")
        res = post(TEXT2IMG,
                   {"prompt": f"{body}, {MOODS[REFERENCE_MOOD]}, {STYLE}",
                    "image_size": "square_hd", "num_images": 1,
                    "output_format": "png"}, key)
        os.makedirs(out_dir, exist_ok=True)
        with open(ref_path, "wb") as f:
            f.write(fetch(first_image_url(res)))
        print(f"    -> {os.path.relpath(ref_path, os.path.dirname(HERE))} (kept)")

    for mood in moods:
        expr = MOODS[mood]
        if False:
            pass
        else:
            prompt = (f"change ONLY the facial expression and pose to: {expr}. "
                      f"Keep the same character, same colours, same style, "
                      f"same framing, same flat background.")
            print(f"[{skin}/{mood}]")

        if dry_run:
            print(f"    {prompt}\n")
            continue

        res = post(IMG2IMG, {"prompt": prompt, "image_url": data_uri(ref_path),
                             "num_images": 1, "output_format": "png"}, key)
        raw_url = first_image_url(res)

        png = cutout(raw_url, key)
        os.makedirs(out_dir, exist_ok=True)
        path = os.path.join(out_dir, f"{mood}.png")
        with open(path, "wb") as f:
            f.write(png)
        downscale(path)
        print(f"    -> {os.path.relpath(path, os.path.dirname(HERE))} "
              f"({os.path.getsize(path) // 1024}kb)")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--skin", help=f"one of: {', '.join(SKINS)}")
    ap.add_argument("--only", action="append",
                    help="generate just this mood (repeatable)")
    ap.add_argument("--list", action="store_true", help="list skins and moods")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the prompts and make no API calls")
    a = ap.parse_args()

    if a.list:
        print("skins:", ", ".join(SKINS))
        print("moods:", ", ".join(MOODS))
        return 0
    if not a.skin:
        return ap.print_usage() or 2
    if a.skin not in SKINS:
        sys.exit(f"unknown skin {a.skin!r}; try --list")

    # The reference mood must go first - every other mood is an edit of it.
    moods = list(MOODS)
    if a.only:
        bad = [m for m in a.only if m not in MOODS]
        if bad:
            sys.exit(f"unknown mood(s): {', '.join(bad)}; try --list")
        moods = [m for m in moods if m in a.only]

    key = None if a.dry_run else load_key()
    generate(a.skin, moods, a.dry_run, key)
    return 0


if __name__ == "__main__":
    sys.exit(main())
