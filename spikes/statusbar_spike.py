#!/usr/bin/env python3
"""SPIKE (throwaway) - can relay drive an iTerm2 status-bar component?

Before building tab-side arm/disarm (docs/drafts/tab-mode-switch-design.md) we
need to confirm iTerm2's status-bar API actually supports the load-bearing
behaviors. This standalone script registers ONE component and exercises them.
It shares nothing with relay - run it alongside a running relay if you like
(two API connections are fine).

WHAT THIS PROVES (or disproves):
  1. A component can be registered from a Python script and DISPLAY a string.
  2. The render callback knows WHICH session it is drawing in (per-session id).
  3. update_cadence re-renders on a timer, so external state changes show up.
  4. A CLICK fires a handler that receives the session id (the un-spoofable
     human-action channel we want arm/disarm to use).

RUN IT:
    python3 spikes/statusbar_spike.py         # keep this terminal open

ADD THE COMPONENT (one-time, in iTerm2):
    Settings (Cmd-,) > Profiles > <your profile> > Session tab >
    "Configure Status Bar" > drag "Relay Arm (spike)" into the bar > OK.
    (You may need the status bar enabled: same panel, "Status bar enabled".)

THEN:
    - Each tab's status bar should show a mode label (starts "o OFF").
    - CLICK the label: it should cycle  o OFF -> (.) SAFE -> ^ WILD -> * INSANE
      and this terminal prints a "click:" line with that tab's session id.
    - Open a second tab: it should track its OWN mode independently.

REPORT BACK: which of the four numbered behaviors worked. That tells us whether
to build the real feature as designed, or rethink the channel.

Ctrl-C to stop. This registers nothing permanent; the component vanishes when
the script exits.
"""
import asyncio

import iterm2

# Colored-emoji labels in the real display format: "RELAY:<mode>" with a
# per-mode colored circle (emoji carry their own color, so this gives
# green/amber/red WITHOUT the RPC needing dynamic text color, which iTerm2
# does not support). The real component also appends the swarm role when the
# tab is a registered coord/worker (the spike can't know that - it has no
# relay state - so it just shows the mode).
MODES = ["⚪ RELAY:off", "\U0001f7e2 RELAY:safe",
         "\U0001f7e1 RELAY:wild", "\U0001f534 RELAY:insane"]
state = {}   # session_id -> index into MODES


async def main(connection):
    component = iterm2.StatusBarComponent(
        short_description="Relay Arm (spike)",
        detailed_description="Shows and toggles a per-tab arm state (spike).",
        knobs=[],
        exemplar="(.) SAFE",
        update_cadence=1.0,        # re-render every second so a toggle shows
        identifier="com.relay.spike.arm",
    )

    @iterm2.StatusBarRPC
    async def render(knobs, session_id=iterm2.Reference("id")):
        # iTerm2 calls this per session; session_id is that session's id var.
        return MODES[state.get(session_id, 0)]

    async def on_click(session_id):
        state[session_id] = (state.get(session_id, 0) + 1) % len(MODES)
        print(f"click: {session_id} -> {MODES[state[session_id]]}", flush=True)

    await component.async_register(connection, render, onclick=on_click)
    print("registered 'Relay Arm (spike)'. Add it to your status bar and click "
          "it. Ctrl-C to stop.", flush=True)
    # Keep the process (and the registration) alive until you Ctrl-C.
    await asyncio.Event().wait()


iterm2.run_forever(main)
