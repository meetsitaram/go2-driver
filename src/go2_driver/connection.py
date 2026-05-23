"""
WebRTC connection management for the Unitree Go2.

Handles AP/STA mode selection, connection with retry logic, and the asyncio
event loop that runs in a background thread.
"""

import asyncio
import logging
import sys
import time
import threading

from unitree_webrtc_connect.webrtc_driver import (
    UnitreeWebRTCConnection,
    WebRTCConnectionMethod,
)

# Suppress noisy library logs
logging.getLogger("asyncio").setLevel(logging.CRITICAL)
logging.getLogger("root").setLevel(logging.CRITICAL)
logging.getLogger().setLevel(logging.CRITICAL)

GO2_AP_IP  = "192.168.12.1"
GO2_LAN_IP = "192.168.123.161"

MAX_RETRIES = 3
RETRY_DELAY = 5
KEEPALIVE_INTERVAL = 0.5


def _patch_error_handler():
    """Monkey-patch the library's error handler to avoid corrupting our status line."""
    try:
        import unitree_webrtc_connect.msgs.error_handler as _eh
        _orig = _eh.handle_error

        def _safe(message):
            try:
                _orig(message)
            except Exception:
                data = message.get("data", "")
                sys.stdout.write(f"\n  [robot error] {data}\n")
                sys.stdout.flush()

        _eh.handle_error = _safe
    except Exception:
        pass


def resolve_ip(mode: str, ip: str | None) -> str:
    if mode == "ap":
        return GO2_AP_IP
    if mode == "sta":
        if not ip:
            raise ValueError("--mode sta requires --ip <GO2_IP>")
        return ip
    if mode == "lan":
        return GO2_LAN_IP
    return GO2_AP_IP


def _make_conn(mode: str, ip: str, aes_key: str | None = None) -> UnitreeWebRTCConnection:
    if mode == "ap":
        return UnitreeWebRTCConnection(WebRTCConnectionMethod.LocalAP)
    kwargs: dict = {"ip": ip}
    if aes_key:
        kwargs["aes_128_key"] = aes_key
    return UnitreeWebRTCConnection(WebRTCConnectionMethod.LocalSTA, **kwargs)


async def _async_connect(conn: UnitreeWebRTCConnection):
    await asyncio.wait_for(conn.connect(), timeout=15)
    if not conn.isConnected:
        raise ConnectionError("WebRTC connected but data channel did not open")


class Go2Connection:
    """Manages a WebRTC connection to the Go2 with retry logic and a background event loop."""

    def __init__(self, mode: str, ip: str | None, aes_key: str | None = None,
                 loop: asyncio.AbstractEventLoop | None = None):
        """
        Args:
            mode: Connection mode (ap, sta, lan).
            ip: Robot IP address.
            aes_key: Optional AES-128 key for firmware auth.
            loop: If provided, use this event loop instead of creating a new one.
                  Caller is responsible for running the loop. Keepalive still starts.
        """
        _patch_error_handler()
        self.mode = mode
        self.ip = resolve_ip(mode, ip)
        self.aes_key = aes_key
        self.conn: UnitreeWebRTCConnection | None = None
        self._external_loop = loop is not None
        self.loop: asyncio.AbstractEventLoop = loop or asyncio.new_event_loop()
        self._loop_thread: threading.Thread | None = None
        self._keepalive_task: asyncio.Task | None = None

    def connect(self) -> UnitreeWebRTCConnection:
        """Connect with retries. Returns the live connection. Starts the event loop thread."""
        for attempt in range(1, MAX_RETRIES + 1):
            self.conn = _make_conn(self.mode, self.ip, self.aes_key)
            try:
                print(f"  Connecting to Go2 (attempt {attempt}/{MAX_RETRIES}) ...")
                self.loop.run_until_complete(_async_connect(self.conn))
                print("  WebRTC connected\n")
                break
            except (asyncio.TimeoutError, ConnectionError, Exception) as exc:
                if attempt < MAX_RETRIES:
                    print(f"  Connection attempt {attempt} failed: {exc}")
                    print(f"  Waiting {RETRY_DELAY}s before retry ...")
                    time.sleep(RETRY_DELAY)
                    self.loop.close()
                    self.loop = asyncio.new_event_loop()
                else:
                    raise ConnectionError(
                        f"WebRTC connection failed after {MAX_RETRIES} attempts: {exc}"
                    ) from exc

        if not self._external_loop:
            self._loop_thread = threading.Thread(target=self.loop.run_forever, daemon=True)
            self._loop_thread.start()

        self._keepalive_task = asyncio.run_coroutine_threadsafe(
            self._keepalive_loop(), self.loop
        )
        return self.conn

    async def async_connect(self) -> UnitreeWebRTCConnection:
        """Async version of connect for use within a running event loop."""
        for attempt in range(1, MAX_RETRIES + 1):
            self.conn = _make_conn(self.mode, self.ip, self.aes_key)
            try:
                print(f"  [{self.ip}] Connecting (attempt {attempt}/{MAX_RETRIES}) ...")
                await asyncio.wait_for(self.conn.connect(), timeout=15)
                if not self.conn.isConnected:
                    raise ConnectionError("Data channel did not open")
                print(f"  [{self.ip}] Connected!")
                break
            except (asyncio.TimeoutError, ConnectionError, Exception) as exc:
                if attempt < MAX_RETRIES:
                    print(f"  [{self.ip}] Attempt {attempt} failed: {exc}")
                    await asyncio.sleep(RETRY_DELAY)
                else:
                    raise ConnectionError(
                        f"Connection to {self.ip} failed after {MAX_RETRIES} attempts: {exc}"
                    ) from exc

        self._keepalive_task = asyncio.ensure_future(self._keepalive_loop())
        return self.conn

    async def _keepalive_loop(self):
        """Send neutral controller frames to prevent WebRTC timeout."""
        import json
        neutral = json.dumps({
            "type": "msg",
            "topic": "rt/wirelesscontroller",
            "data": {"lx": 0, "ly": 0, "rx": 0, "ry": 0, "keys": 0},
        })
        while True:
            try:
                if self.conn and self.conn.datachannel:
                    self.conn.datachannel.channel.send(neutral)
            except Exception:
                pass
            await asyncio.sleep(KEEPALIVE_INTERVAL)

    def run_coroutine(self, coro, timeout: float = 2.0):
        """Schedule a coroutine on the background loop and wait for the result."""
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout=timeout)

    def disconnect(self):
        if self._keepalive_task:
            self._keepalive_task.cancel()
        if self.conn:
            try:
                asyncio.run_coroutine_threadsafe(
                    self.conn.disconnect(), self.loop
                ).result(timeout=5)
            except Exception:
                pass
        if not self._external_loop and self.loop.is_running():
            self.loop.call_soon_threadsafe(self.loop.stop)

    async def async_disconnect(self):
        """Async version of disconnect for use within a running event loop."""
        if self._keepalive_task:
            self._keepalive_task.cancel()
        if self.conn:
            try:
                await self.conn.disconnect()
            except Exception:
                pass
