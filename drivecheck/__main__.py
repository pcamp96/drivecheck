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
    mode.add_argument("--demo", action="store_true", help="Simulated devices; never inspect host disks")
    mode.add_argument("--hardware", action="store_true", help="Use real Linux USB drives")
    parser.add_argument("--host", default=os.getenv("DRIVECHECK_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("DRIVECHECK_PORT", "8765")))
    parser.add_argument("--data-dir", type=Path)
    args = parser.parse_args()
    config = Config.from_env()
    if args.demo or args.hardware:
        config.demo = not args.hardware
    if args.data_dir:
        config.data_dir = args.data_dir.expanduser().resolve()
    if not config.demo:
        if platform.system() != "Linux":
            parser.error("Hardware mode requires Linux. Use --demo on this computer.")
        if os.geteuid() != 0:
            parser.error("Hardware mode requires root for drive self-tests and raw I/O. Use the systemd installer on your Pi.")
    from drivecheck.app import create_app

    app = create_app(config)
    print(f"DriveCheck {'simulation' if config.demo else 'hardware'} station")
    print(f"Dashboard: http://{args.host}:{args.port}")
    print(f"Read your sign-in token from {config.data_dir / 'access-token'} (or use DRIVECHECK_API_KEY)")
    uvicorn.run(app, host=args.host, port=args.port, workers=1, proxy_headers=False)


if __name__ == "__main__":
    main()
