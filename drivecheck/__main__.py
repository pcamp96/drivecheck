"""Command-line entry point. Demo is the portable and non-probing default."""

import argparse
import os
import platform
from pathlib import Path

import uvicorn

from drivecheck.config import Config


def main():
    parser = argparse.ArgumentParser(description="DriveCheck drive intake station")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--demo", action="store_true", help="Simulated devices; never inspect host disks"
    )
    mode.add_argument(
        "--hardware", action="store_true", help="Discover real external drives on Linux or macOS"
    )
    parser.add_argument("--host", default=os.getenv("DRIVECHECK_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.getenv("DRIVECHECK_PORT", "8765")))
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Automatically test, notify, and safely eject eligible drives",
    )
    args = parser.parse_args()
    config = Config.from_env()
    if args.demo or args.hardware:
        config.demo = not args.hardware
    if args.data_dir:
        config.data_dir = args.data_dir.expanduser().resolve()
    if args.headless:
        config.headless = True
    if config.headless and config.demo:
        parser.error("Headless mode requires --hardware (or DRIVECHECK_DEMO=false)")
    if not config.demo and platform.system() not in {"Linux", "Darwin"}:
        parser.error("Hardware mode supports Linux and macOS. Use --demo for simulation.")
    from drivecheck.app import create_app

    app = create_app(config)
    server_config = uvicorn.Config(
        app,
        host=args.host,
        port=args.port,
        workers=1,
        proxy_headers=False,
        # Live dashboard streams stay open indefinitely. Bound HTTP draining so
        # shutdown reaches lifespan cleanup and cancels hardware work safely.
        timeout_graceful_shutdown=5,
    )
    # Reserve the endpoint before lifespan can announce readiness or queue work.
    listener = server_config.bind_socket()
    print(f"DriveCheck {'simulation' if config.demo else 'hardware'} station")
    display_host = "127.0.0.1" if args.host == "0.0.0.0" else args.host
    print(f"Dashboard: http://{display_host}:{listener.getsockname()[1]}")
    if args.host == "0.0.0.0":
        print(f"LAN dashboard: http://YOUR_STATION_IP:{listener.getsockname()[1]}")
    print(
        f"Read your sign-in token from {config.data_dir / 'access-token'} (or use DRIVECHECK_API_KEY)"
    )
    try:
        uvicorn.Server(server_config).run(sockets=[listener])
    finally:
        listener.close()


if __name__ == "__main__":
    main()
