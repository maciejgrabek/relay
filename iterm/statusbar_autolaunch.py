#!/usr/bin/env python3
"""Relay status-bar provider - an iTerm2 AutoLaunch script.

WHY THIS EXISTS: once you drag the "Relay" component into your status bar,
iTerm2 saves it in your PROFILE - it stays configured even when relay is not
running, and iTerm2 renders a component with no provider as an ERROR. The
provider therefore has to outlive relay. iTerm2 starts this script when it
launches; it serves the badge always:

  - relay running -> shows the per-tab label relay publishes every tick
    (~/.relay/statusbar.json), including arm mode and swarm identity.
  - relay off     -> shows "⚫ RELAY: off". No errors.

A CLICK queues a line to ~/.relay/statusbar-clicks.jsonl; the running relay
applies it next tick with its normal guards (own panel tab never armable,
unknown sessions ignored). With relay off, a click does nothing.

INSTALL: ./install.sh offers to symlink this file into
~/Library/Application Support/iTerm2/Scripts/AutoLaunch/relay_statusbar.py
(a symlink, so `relay update` updates this too). Start it once via
Scripts > AutoLaunch > relay_statusbar.py, or restart iTerm2. While this
provider is installed, relay skips its own in-process registration - one
provider, no identifier conflicts.
"""
import asyncio
import os
import sys

import iterm2

# Resolve through the AutoLaunch symlink back to the repo so the pure helpers
# (label composition, state/click files) stay single-source.
sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import statusbar  # noqa: E402


async def main(connection):
    component = iterm2.StatusBarComponent(
        short_description="Relay",
        detailed_description="Relay arm state for this tab; click to cycle "
                             "off/safe/wild/insane (only while relay runs).",
        knobs=[],
        exemplar="\U0001f7e2 RELAY:safe",
        update_cadence=1.0,
        identifier="com.relay.arm",
    )

    @iterm2.StatusBarRPC
    async def render(knobs, session_id=iterm2.Reference("id")):
        return statusbar.read_state_label(session_id)

    async def on_click(session_id):
        # Queue only while relay is live - a click on the off badge is inert.
        if statusbar.state_fresh():
            statusbar.append_click(session_id)

    async def _heartbeat():
        # Liveness on a TIMER, not from render(). render only fires while the
        # component is placed AND its tab is visible, so a render-based
        # heartbeat goes stale whenever the badge isn't on screen even though
        # this process is fine - which would fool relay into starting a SECOND
        # provider (duplicate com.relay.arm registration). A steady timer makes
        # provider_alive() an honest "this process is running" signal, well
        # inside PROVIDER_ALIVE_MAX_AGE_S (15s).
        while True:
            statusbar.touch_provider_alive()
            await asyncio.sleep(5)

    statusbar.touch_provider_alive()   # announce liveness immediately
    await component.async_register(connection, render, onclick=on_click)
    asyncio.create_task(_heartbeat())


iterm2.run_forever(main)
