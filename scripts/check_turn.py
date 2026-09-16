"""
Check whether other people can actually call you.

    python scripts/check_turn.py

WebRTC audio is peer to peer. Without a relay, a call works between a
browser and a server on the same machine and nowhere else, because a
caller on another network has no route to your machine behind its router.

This script checks three things in order: that credentials are configured,
that the relay actually issues them, and that the relay is reachable. Each
one failing means something different, so each one is reported separately
rather than as a single pass or fail.
"""

import socket
import sys
from pathlib import Path
from urllib.parse import urlparse

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sophia import config  # noqa: E402

TICK = "  [ok]  "
CROSS = "  [--]  "


def main() -> None:
    print("\nWebRTC relay check\n" + "-" * 58)

    if config.TURN_URLS:
        print(f"{TICK}Using TURN_URLS from .env ({len(config.TURN_URLS)} url(s))")
        servers = config.ice_servers()
    elif config.METERED_APP_NAME and config.METERED_API_KEY:
        print(f"{TICK}Metered configured: app '{config.METERED_APP_NAME}'")
        print("        fetching credentials...")
        servers = config.ice_servers()
        relay = [s for s in servers if any("turn" in u for u in _urls(s))]
        if not relay:
            print(f"{CROSS}Metered returned no relay servers")
            print("\n  Check the app name and API key at dashboard.metered.ca")
            sys.exit(1)
        print(f"{TICK}Credentials issued: {len(relay)} relay entr(ies)")
    else:
        print(f"{CROSS}No relay configured")
        print(
            "\n  Calls will work on THIS machine only. Anyone else clicking\n"
            "  Start call will see the connection fail.\n\n"
            "  To fix, free and no card:\n"
            "    1. https://dashboard.metered.ca/signup?tool=turnserver\n"
            "    2. Copy your app name and API key\n"
            "    3. Put them in .env as METERED_APP_NAME and METERED_API_KEY\n"
        )
        sys.exit(1)

    # Can we actually reach the relay hosts?
    hosts = set()
    for server in servers:
        for url in _urls(server):
            parsed = urlparse(url.replace("turn:", "//").replace("turns:", "//").replace("stun:", "//"))
            if parsed.hostname:
                hosts.add((parsed.hostname, parsed.port or 3478))

    print()
    reachable = 0
    for host, port in sorted(hosts):
        try:
            socket.getaddrinfo(host, port)
        except socket.gaierror:
            print(f"{CROSS}{host}:{port} does not resolve")
            continue
        try:
            with socket.create_connection((host, port), timeout=5):
                print(f"{TICK}{host}:{port} reachable")
                reachable += 1
        except Exception:  # noqa: BLE001
            # UDP only relays legitimately refuse a TCP connection, so this
            # is worth reporting but is not proof of failure.
            print(f"        {host}:{port} resolves, no TCP (may still work over UDP)")
            reachable += 1

    print("-" * 58)
    if reachable:
        print(
            "\nRelay looks usable. The real test is a call from another\n"
            "device on a different network, ideally a phone on mobile data.\n"
        )
    else:
        print("\nNo relay host was reachable. Calls from other networks will fail.\n")
        sys.exit(1)


def _urls(server: dict) -> list[str]:
    urls = server.get("urls") or server.get("url") or []
    return [urls] if isinstance(urls, str) else list(urls)


if __name__ == "__main__":
    main()
