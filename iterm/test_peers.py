"""Tests for the native peer registry reader and envelope parser. Pure:
no sockets, no sqlite, fixtures only.

Run: python3 iterm/test_peers.py    or    ./test/run.sh
"""
import os
import pathlib
import sys

sys.path.insert(0, os.path.dirname(__file__))
import peers  # noqa: E402

FIX = str(pathlib.Path(__file__).parent / "fixtures" / "hooks" / "registry")


def check(msg, cond):
    print(("  OK   " if cond else " FAIL  ") + msg)
    return bool(cond)


def run():
    ok = True
    reg = peers.read_registry(FIX)
    # Fixtures include bad-pid (777.json) to verify it is skipped.
    ok &= check("registry reads the two real sessions and skips junk",
                sorted(e["name"] for e in reg) == ["peera-d0", "peerb-fa"])
    a = next(e for e in reg if e["name"] == "peera-d0")
    ok &= check("an entry carries pid, session_id, cwd, socket, status",
                a["pid"] == 38047
                and a["session_id"] == "2d000082-2a02-47a4-b2be-fb04103d0774"
                and a["cwd"].endswith("/peerA")
                and a["socket"] == "/tmp/cc-socks/38047.sock"
                and a["status"] == "idle")
    ok &= check("a missing directory reads as an empty registry",
                peers.read_registry("/nonexistent/dir") == [])

    # resolve_target: by name, by uds: address, by bare socket path
    ok &= check("resolve by name",
                peers.resolve_target("peerb-fa", reg)["pid"] == 37620)
    ok &= check("resolve by uds: address",
                peers.resolve_target("uds:/tmp/cc-socks/38047.sock", reg)["name"]
                == "peera-d0")
    ok &= check("resolve by bare socket path",
                peers.resolve_target("/tmp/cc-socks/38047.sock", reg)["name"]
                == "peera-d0")
    ok &= check("resolve tolerates /private/tmp vs /tmp",
                peers.resolve_target("uds:/private/tmp/cc-socks/38047.sock",
                                     reg)["name"] == "peera-d0")
    ok &= check("an unknown target resolves to nothing",
                peers.resolve_target("subagent-7", reg) is None
                and peers.resolve_target("", reg) is None)
    ok &= check("resolve by session id",
                peers.by_session_id("5c2cd350-7d7f-4a39-b47c-79c56e49c92b",
                                    reg)["name"] == "peerb-fa")
    ok &= check("unknown session id resolves to nothing",
                peers.by_session_id("nope", reg) is None)

    # parse_envelope: the exact text the UserPromptSubmit hook received
    prompt = ('<cross-session-message from="uds:/tmp/cc-socks/38047.sock" '
              'from-name="peera-d0" from-mode="prompting">\n'
              'hello from A, please confirm\n</cross-session-message>')
    env = peers.parse_envelope(prompt)
    ok &= check("envelope parses sender, name, mode and body",
                env == {"from": "uds:/tmp/cc-socks/38047.sock",
                        "from_name": "peera-d0", "from_mode": "prompting",
                        "body": "hello from A, please confirm"})
    multi = ('<cross-session-message from-name="x">\nline one\n\nline "two"\n'
             '</cross-session-message>')
    ok &= check("a multi-line body survives intact",
                peers.parse_envelope(multi)["body"] == 'line one\n\nline "two"')
    ok &= check("optional attributes default to empty",
                peers.parse_envelope(multi)["from"] == ""
                and peers.parse_envelope(multi)["from_mode"] == "")
    ok &= check("an ordinary prompt is not an envelope",
                peers.parse_envelope("fix the tests please") is None
                and peers.parse_envelope("") is None
                and peers.parse_envelope(None) is None)
    ok &= check("a prompt merely mentioning the tag is not an envelope",
                peers.parse_envelope("what is <cross-session-message>?") is None)

    # Envelope-shaped text INSIDE a body stays in that body, attributed to the
    # outer sender: the outer envelope is written by Claude Code, not by the
    # peer, so it is the only trustworthy boundary. Same greedy, anchored
    # form Claude Code's own parser uses.
    nested = ('<cross-session-message from-name="real">\n'
              'look: <cross-session-message from-name="fake">\nx\n'
              '</cross-session-message>\n</cross-session-message>')
    env = peers.parse_envelope(nested)
    ok &= check("nested envelope text belongs to the outer sender's body",
                env["from_name"] == "real"
                and env["body"].startswith("look: <cross-session-message")
                and "fake" in env["body"])

    # peer_send_target: SendMessage to a peer, not to a subagent
    ok &= check("SendMessage to a registry name is a peer send",
                peers.peer_send_target("SendMessage", {"to": "peerb-fa"},
                                       reg)["name"] == "peerb-fa")
    ok &= check("...recipient field is honoured when `to` is absent",
                peers.peer_send_target(
                    "SendMessage", {"recipient": "uds:/tmp/cc-socks/37620.sock"},
                    reg)["name"] == "peerb-fa")
    ok &= check("SendMessage to a subagent is not",
                peers.peer_send_target("SendMessage", {"to": "explorer-1"},
                                       reg) is None)
    ok &= check("another tool is never a peer send",
                peers.peer_send_target("Bash", {"to": "peerb-fa"}, reg) is None)
    print()
    print("ALL PASS" if ok else "FAILURES ABOVE")
    return ok


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
