"""Run the app on a public URL through a Cloudflare quick tunnel.

    .venv/bin/python run_public.py              # tunnel + app on port 8000
    .venv/bin/python run_public.py --no-app     # tunnel only (start the app yourself afterwards)
    .venv/bin/python run_public.py --port 8001
    .venv/bin/python run_public.py --local      # no tunnel: BASE_URL=http://localhost:8000 + app

Steps:
  1. Starts:  ~/.local/bin/cloudflared tunnel --url http://localhost:<port> --protocol http2
  2. Waits for the https://<random>.trycloudflare.com address it prints.
  3. Writes it to BASE_URL in .env (other lines are left as they are).
  4. Starts the app (uvicorn) so interview links and Twilio checks use that URL.
Ctrl+C stops everything.

With --local there is no tunnel: BASE_URL is set back to http://localhost:<port>
and only the app starts. Use it for local work, or to undo the tunnel URL.

Quick-tunnel URLs change on every run, so links sent during an earlier run stop
working. Reschedule those interviews to re-send them with the new URL.
"""

import argparse
import os
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEFAULT_CLOUDFLARED = Path.home() / ".local" / "bin" / "cloudflared"
URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
STOPPING = threading.Event()  # set on shutdown so expected "connection terminated" errors aren't shown


def update_env(env_path: Path, url: str) -> None:
    """Set BASE_URL in .env, keeping every other line untouched."""
    lines = env_path.read_text(encoding="utf-8").splitlines(keepends=True) if env_path.exists() else []
    new_line = f"BASE_URL={url}\n"
    for i, line in enumerate(lines):
        if re.match(r"^\s*BASE_URL\s*=", line):
            lines[i] = new_line
            break
    else:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        lines.append(new_line)
    env_path.write_text("".join(lines), encoding="utf-8")


def start_tunnel(cloudflared: Path, port: int, timeout: float, verbose: bool) -> tuple[subprocess.Popen, str]:
    cmd = [str(cloudflared), "tunnel", "--url", f"http://localhost:{port}", "--protocol", "http2"]
    print(f"[tunnel] {' '.join(cmd)}")
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    found: dict[str, str] = {}
    ready = threading.Event()
    recent: list[str] = []

    def reader() -> None:
        # Keep reading for the tunnel's whole life so its output pipe never fills up.
        for line in proc.stdout:
            line = line.rstrip()
            recent.append(line)
            del recent[:-20]
            if verbose:
                print(f"[cloudflared] {line}")
            m = URL_RE.search(line)
            if m and not found:
                found["url"] = m.group(0)
                ready.set()
            elif not verbose and " ERR " in line and found and not STOPPING.is_set():
                print(f"[cloudflared] {line}")
        ready.set()  # process ended

    threading.Thread(target=reader, daemon=True).start()
    ready.wait(timeout)
    if "url" not in found:
        stop(proc, "tunnel")
        tail = "\n".join(recent[-10:]) or "(no output)"
        sys.exit(f"[tunnel] No public URL within {timeout:.0f}s. Last cloudflared output:\n{tail}")
    return proc, found["url"]


def start_app(port: int) -> subprocess.Popen:
    uvicorn = ROOT / ".venv" / "bin" / "uvicorn"
    exe = [str(uvicorn)] if uvicorn.exists() else [sys.executable, "-m", "uvicorn"]
    cmd = exe + ["app.main:app", "--host", "127.0.0.1", "--port", str(port),
                 "--proxy-headers", "--forwarded-allow-ips", "127.0.0.1"]
    print(f"[app] {' '.join(cmd)}")
    return subprocess.Popen(cmd, cwd=ROOT)


def stop(proc: subprocess.Popen | None, name: str) -> None:
    STOPPING.set()
    if proc is None or proc.poll() is not None:
        return
    proc.send_signal(signal.SIGINT)
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        print(f"[{name}] did not stop, killing it")
        proc.kill()


def run_local(port: int, env_file: Path) -> int:
    url = f"http://localhost:{port}"
    update_env(env_file, url)
    print(f"[local] Set BASE_URL={url} in {env_file}")
    print("[local] Interview links will only open on this machine.")
    if re.search(r"(?mi)^\s*SESSION_HTTPS_ONLY\s*=\s*['\"]?true", env_file.read_text(encoding="utf-8")):
        print("[local] Warning: SESSION_HTTPS_ONLY=true in .env, so login won't work over http://localhost. "
              "Set it to false for local use.")
    app = start_app(port)
    print(f"[app] Open {url} (Ctrl+C to stop)")
    try:
        return app.wait()
    except KeyboardInterrupt:
        print("\nStopping...")
        return 0
    finally:
        stop(app, "app")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Expose the app on a public Cloudflare quick-tunnel URL.")
    p.add_argument("--port", type=int, default=8000, help="local port of the app (default 8000)")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--local", action="store_true",
                      help="no tunnel: set BASE_URL to http://localhost:<port> and run the app")
    mode.add_argument("--no-app", action="store_true", help="only run the tunnel and update .env")
    p.add_argument("--cloudflared", type=Path, default=DEFAULT_CLOUDFLARED,
                   help=f"path to cloudflared (default {DEFAULT_CLOUDFLARED})")
    p.add_argument("--env-file", type=Path, default=ROOT / ".env", help="env file to update (default ./.env)")
    p.add_argument("--timeout", type=float, default=45, help="seconds to wait for the tunnel URL (default 45)")
    p.add_argument("--verbose", action="store_true", help="show all cloudflared output")
    args = p.parse_args(argv)
    sys.stdout.reconfigure(line_buffering=True)  # show status lines immediately, even when piped to a file

    if args.local:
        return run_local(args.port, args.env_file)

    if not (args.cloudflared.is_file() and os.access(args.cloudflared, os.X_OK)):
        sys.exit(f"cloudflared not found at {args.cloudflared}. Install it with:\n"
                 "  mkdir -p ~/.local/bin && curl -L -o ~/.local/bin/cloudflared "
                 "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 "
                 "&& chmod +x ~/.local/bin/cloudflared")

    tunnel, app = None, None
    try:
        tunnel, url = start_tunnel(args.cloudflared, args.port, args.timeout, args.verbose)
        update_env(args.env_file, url)
        print(f"[tunnel] Public URL: {url}")
        print(f"[tunnel] Set BASE_URL={url} in {args.env_file}")
        print("[tunnel] Twilio webhook (if used): " + url + "/webhooks/twilio/sms")
        print("[tunnel] The URL can take up to a minute to start working (Cloudflare DNS).")

        if args.no_app:
            print("[tunnel] Running. Start the app now (it reads BASE_URL at startup). Ctrl+C to stop.")
            return tunnel.wait()

        app = start_app(args.port)
        print(f"[app] Open {url} (Ctrl+C stops the app and the tunnel)")
        # Stop as soon as either process exits.
        while tunnel.poll() is None and app.poll() is None:
            time.sleep(0.5)
        if tunnel.poll() is not None:
            print("[tunnel] cloudflared exited; stopping the app.")
        else:
            print("[app] The app exited; stopping the tunnel.")
        return 1
    except KeyboardInterrupt:
        print("\nStopping...")
        return 0
    finally:
        stop(app, "app")
        stop(tunnel, "tunnel")


if __name__ == "__main__":
    sys.exit(main())
