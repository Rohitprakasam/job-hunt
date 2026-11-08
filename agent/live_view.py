"""Live view: on-demand VNC/noVNC human-in-the-loop bridge for job board logins, OTPs, and captchas.

Starts Xvfb, x11vnc (with a fresh 8-char password), and websockify (noVNC on 127.0.0.1:6080).
Sends a WhatsApp prompt with the private live-view link and password.
Shuts down completely when the prompt is resolved, timed out (15m), or the stage ends.
Port 6080 has no listener at any other time.
"""

from __future__ import annotations

import logging
import os
import secrets
import shutil
import socket
import string
import subprocess
import time
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime

log = logging.getLogger(__name__)

DEFAULT_DISPLAY = ":99"
DEFAULT_PORT = 6080
DEFAULT_VNC_PORT = 5900
DEFAULT_TIMEOUT_MINUTES = 15


@dataclass
class LiveViewSession:
    site: str
    reason: str
    password: str
    link: str
    display: str = DEFAULT_DISPLAY
    port: int = DEFAULT_PORT
    vnc_port: int = DEFAULT_VNC_PORT
    vnc_proc: subprocess.Popen | None = None
    ws_proc: subprocess.Popen | None = None
    started_at: datetime | None = None
    timeout_minutes: int = DEFAULT_TIMEOUT_MINUTES


def generate_password(length: int = 8) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def is_port_open(port: int, host: str = "127.0.0.1", timeout: float = 0.5) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        return s.connect_ex((host, port)) == 0


def is_display_active(display: str = DEFAULT_DISPLAY) -> bool:
    display_num = display.lstrip(":")
    lock_file = f"/tmp/.X{display_num}-lock"
    socket_file = f"/tmp/.X11-unix/X{display_num}"

    # 1. Check lock file PID
    if os.path.exists(lock_file):
        try:
            with open(lock_file) as f:
                pid_str = f.read().strip()
            if pid_str:
                pid = int(pid_str)
                os.kill(pid, 0)
                return True
        except (ProcessLookupError, ValueError):
            # Process is dead; remove stale lock and socket
            for p in (lock_file, socket_file):
                try:
                    if os.path.exists(p):
                        os.remove(p)
                except Exception:
                    pass
        except PermissionError:
            return True
        except Exception:
            pass

    # 2. Check UNIX socket connection if available
    if hasattr(socket, "AF_UNIX") and os.path.exists(socket_file):
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.settimeout(0.5)
                s.connect(socket_file)
                return True
        except Exception:
            try:
                os.remove(socket_file)
            except Exception:
                pass

    return False


def start_xvfb(
    display: str = DEFAULT_DISPLAY, width: int = 1280, height: int = 900
) -> subprocess.Popen | None:
    """Start Xvfb on display if not already running."""
    if is_display_active(display):
        log.debug("Xvfb already active on %s", display)
        return None

    # Clean any stale files before starting
    display_num = display.lstrip(":")
    for p in (f"/tmp/.X{display_num}-lock", f"/tmp/.X11-unix/X{display_num}"):
        try:
            if os.path.exists(p):
                os.remove(p)
        except Exception:
            pass

    xvfb_bin = shutil.which("Xvfb")
    if not xvfb_bin:
        log.warning("Xvfb binary not found")
        return None
    cmd = [xvfb_bin, display, "-screen", "0", f"{width}x{height}x24"]
    log.info("starting Xvfb on %s", display)
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(50):
        if is_display_active(display):
            return proc
        time.sleep(0.1)
    return proc


def stop_xvfb(proc: subprocess.Popen | None, display: str = DEFAULT_DISPLAY) -> None:
    if proc:
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
    display_num = display.lstrip(":")
    for p in (f"/tmp/.X{display_num}-lock", f"/tmp/.X11-unix/X{display_num}"):
        try:
            if os.path.exists(p):
                os.remove(p)
        except Exception:
            pass


def kill_stale_live_view_procs(port: int = DEFAULT_PORT, vnc_port: int = DEFAULT_VNC_PORT) -> None:
    """Kill any stale x11vnc or websockify instances before starting a new view."""
    if os.name != "nt" and shutil.which("pkill"):
        for proc_name in ("websockify", "x11vnc"):
            try:
                subprocess.run(
                    ["pkill", "-9", "-f", proc_name],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except Exception:
                pass
        for _ in range(15):
            if not is_port_open(port) and not is_port_open(vnc_port):
                break
            time.sleep(0.2)


def open_view(
    site: str,
    reason: str,
    wa_client=None,
    timeout_minutes: int = DEFAULT_TIMEOUT_MINUTES,
    display: str = DEFAULT_DISPLAY,
    port: int = DEFAULT_PORT,
    vnc_port: int = DEFAULT_VNC_PORT,
) -> LiveViewSession | None:
    """Start x11vnc and websockify for human interaction and notify user via WhatsApp.

    If the WhatsApp send fails or 24h window is closed, the view is NOT opened and None is returned.
    """
    kill_stale_live_view_procs(port, vnc_port)

    link = os.environ.get("JOB_AGENT_SECRET_LIVE_VIEW_LINK", "").strip()
    if not link:
        from pathlib import Path

        env_file = Path.home() / ".config" / "job-agent" / "env"
        if env_file.exists():
            try:
                for line in env_file.read_text(encoding="utf-8").splitlines():
                    clean_line = line.strip().removeprefix("export ")
                    if clean_line.startswith("JOB_AGENT_SECRET_LIVE_VIEW_LINK="):
                        link = clean_line.split("=", 1)[1].strip().strip("\"'")
                        break
            except Exception:
                pass
    if not link:
        log.warning("JOB_AGENT_SECRET_LIVE_VIEW_LINK is not set; cannot open live view")
        return None

    # Check WhatsApp window if wa_client is provided
    password = generate_password(8)
    msg = (
        f"🔐 {site.capitalize()} needs you ({reason}). Open {link}, enter {password}. {timeout_minutes} min."
    )

    if wa_client is not None:
        try:
            if hasattr(wa_client, "window_open") and not wa_client.window_open():
                log.warning("WhatsApp 24-hour window is closed; skipping live view for %s", site)
                return None
            status = wa_client.send_text(msg, kind="live_view", priority=10)
            if status not in ("sent", "delivered"):
                log.warning("WhatsApp message not sent (status=%s); skipping live view", status)
                return None
        except Exception as exc:
            log.warning("failed to send WhatsApp live view prompt: %s", exc)
            return None

    # Find binaries
    x11vnc_bin = shutil.which("x11vnc")
    websockify_bin = shutil.which("websockify")
    if not x11vnc_bin or not websockify_bin:
        log.error("x11vnc or websockify binary not found; cannot start live view")
        return None

    session = LiveViewSession(
        site=site,
        reason=reason,
        password=password,
        link=link,
        display=display,
        port=port,
        vnc_port=vnc_port,
        started_at=datetime.now(UTC),
        timeout_minutes=timeout_minutes,
    )

    try:
        # Start x11vnc
        vnc_cmd = [
            x11vnc_bin,
            "-display",
            display,
            "-passwd",
            password,
            "-forever",
            "-shared",
            "-rfbport",
            str(vnc_port),
            "-quiet",
        ]
        log.info("starting x11vnc on %s port %d", display, vnc_port)
        session.vnc_proc = subprocess.Popen(vnc_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        # Wait for VNC port
        vnc_ready = False
        for _ in range(25):
            poll_vnc = session.vnc_proc.poll()
            if poll_vnc is not None and isinstance(poll_vnc, int):
                raise RuntimeError(f"x11vnc process exited unexpectedly with code {poll_vnc}")
            if is_port_open(vnc_port):
                vnc_ready = True
                break
            time.sleep(0.2)
        if not vnc_ready:
            raise RuntimeError(f"x11vnc failed to listen on port {vnc_port}")

        # Start websockify serving noVNC on 127.0.0.1:6080
        novnc_web = "/usr/share/novnc"
        ws_cmd = [websockify_bin, "--web", novnc_web, f"127.0.0.1:{port}", f"127.0.0.1:{vnc_port}"]
        log.info("starting websockify on port %d -> %d", port, vnc_port)
        session.ws_proc = subprocess.Popen(ws_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        # Wait for port 6080
        ws_ready = False
        for _ in range(25):
            poll_ws = session.ws_proc.poll()
            if poll_ws is not None and isinstance(poll_ws, int):
                raise RuntimeError(f"websockify process exited unexpectedly with code {poll_ws}")
            if is_port_open(port):
                ws_ready = True
                break
            time.sleep(0.2)
        if not ws_ready:
            raise RuntimeError(f"websockify failed to listen on port {port}")

        return session
    except Exception as exc:
        log.error("error starting live view: %s", exc)
        close_view(session)
        return None


def close_view(session: LiveViewSession | None) -> None:
    """Terminate websockify and x11vnc and verify port 6080 has no listener."""
    if session is None:
        return
    for proc, _name in ((session.ws_proc, "websockify"), (session.vnc_proc, "x11vnc")):
        if proc is not None:
            try:
                proc.terminate()
                proc.wait(timeout=1)
            except Exception:
                try:
                    proc.kill()
                    proc.wait(timeout=1)
                except Exception:
                    pass
    session.ws_proc = None
    session.vnc_proc = None

    # Wait up to 3s to ensure ports are completely closed
    for _ in range(15):
        if not is_port_open(session.port) and not is_port_open(session.vnc_port):
            break
        time.sleep(0.2)
    if is_port_open(session.port) or is_port_open(session.vnc_port):
        kill_stale_live_view_procs(session.port, session.vnc_port)
    log.info("live view closed for %s", session.site)


@contextmanager
def live_view(
    site: str,
    reason: str,
    wa_client=None,
    timeout_minutes: int = DEFAULT_TIMEOUT_MINUTES,
    display: str = DEFAULT_DISPLAY,
):
    session = open_view(site, reason, wa_client=wa_client, timeout_minutes=timeout_minutes, display=display)
    try:
        yield session
    finally:
        close_view(session)


def wait_for_resolution(
    session: LiveViewSession | None,
    check_fn: Callable[[], bool],
    timeout_seconds: int = 900,
    poll_interval_s: float = 2.0,
) -> bool:
    """Poll check_fn() until True (user solved challenge/logged in) or timeout."""
    if session is None:
        return False
    start = time.time()
    while time.time() - start < timeout_seconds:
        if check_fn():
            log.info("human challenge resolved for %s", session.site)
            return True
        time.sleep(poll_interval_s)
    log.warning("live view timed out (%ds) for %s", timeout_seconds, session.site)
    return False
