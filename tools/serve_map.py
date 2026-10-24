"""Serve the route map over http://localhost so the map tiles load.

    python tools/serve_map.py             the published plan's map
    python tools/serve_map.py --dry-run   the dry-run map
"""
from __future__ import annotations

import argparse
import functools
import http.server
import socket
import socketserver
import threading
import webbrowser
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
MAP_DIR = PROJECT_DIR / "mapdata"


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="Serve the route map over http so street tiles can load",
    )
    p.add_argument("--port", type=int, default=8000,
                   help="Port to listen on (default 8000)")
    p.add_argument("--dry-run", action="store_true",
                   help="Open route_map_dryrun.html. Without this the newest "
                        "of the two map files is opened.")
    p.add_argument("--file", default=None,
                   help="Serve a specific file name from the mapdata folder")
    p.add_argument("--no-browser", action="store_true",
                   help="Do not open a browser window")
    return p.parse_args(argv)


def pick_page(args) -> Path | None:
    """Which map file to serve: the one asked for, otherwise the newest."""
    if args.file:
        page = MAP_DIR / args.file
        return page if page.exists() else None
    if args.dry_run:
        page = MAP_DIR / "route_map_dryrun.html"
        return page if page.exists() else None

    candidates = [
        MAP_DIR / "route_map_dryrun.html",
        MAP_DIR / "route_map.html",
    ]
    existing = [p for p in candidates if p.exists()]
    if not existing:
        return None
    return max(existing, key=lambda p: p.stat().st_mtime)


def free_port(start: int, tries: int = 20) -> int:
    """The first port from ``start`` that nothing else is listening on.

    Re-running this script while an old copy is still going would otherwise
    fail with "address already in use", which reads like a bug in the map.
    """
    for port in range(start, start + tries):
        with socket.socket() as probe:
            try:
                probe.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise SystemExit(
        f"No free port between {start} and {start + tries - 1}. "
        f"Close whatever is using them, or pass --port."
    )


def main(argv=None) -> int:
    args = parse_args(argv)

    page = pick_page(args)
    if page is None:
        print(f"\nNo map file in {MAP_DIR}.\n")
        print("Run the planner first, or draw the map with:")
        print("    python tools/view_map.py\n")
        return 1
    name = page.name

    port = free_port(args.port)

    class Quiet(http.server.SimpleHTTPRequestHandler):
        """Request handler with no logging and no caching."""

        def log_message(self, *args, **kwargs):
            pass

        def end_headers(self):
            self.send_header("Cache-Control", "no-store, must-revalidate")
            self.send_header("Expires", "0")
            super().end_headers()

    handler = functools.partial(Quiet, directory=str(MAP_DIR))

    url = f"http://localhost:{port}/{name}"
    with socketserver.TCPServer(("127.0.0.1", port), handler) as server:
        kind = "dry run" if "dryrun" in name else "published plan"
        print(f"\nServing {name} ({kind}) at:\n    {url}\n")
        print("The street map turns on by itself here, because the page now")
        print("has a real address to send to OpenStreetMap.\n")
        print("Press Ctrl+C to stop.\n")
        if not args.no_browser:
            threading.Timer(0.4, webbrowser.open, args=(url,)).start()
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\nStopped.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
