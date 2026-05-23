"""
Posture control helpers for the Unitree Go2.

Provides blocking helpers to command the robot into a known posture
(standing or crouched) and verify it reached the target via body height.
"""

import asyncio
import json
import time

from unitree_webrtc_connect.constants import RTC_TOPIC, SPORT_CMD

STANDING_HEIGHT_MIN = 0.28
CROUCHED_HEIGHT_MAX = 0.12

POSTURE_STANDING = "standing"
POSTURE_CROUCHED = "crouched"


async def _send_sport_cmd(conn, cmd_name: str) -> dict:
    """Send a sport API command and return the response dict."""
    resp = await conn.datachannel.pub_sub.publish_request_new(
        RTC_TOPIC["SPORT_MOD"],
        {"api_id": SPORT_CMD[cmd_name]},
    )
    return resp


async def _get_body_height(conn, timeout: float = 3.0) -> float | None:
    """
    Subscribe to sportmodestate and read pos_z (body height).
    Returns the height or None if no reading within timeout.
    """
    result = {"z": None}
    event = asyncio.Event()

    def _on_msg(msg):
        try:
            data = msg if isinstance(msg, dict) else json.loads(msg)
            d = data.get("data", data)
            if isinstance(d, str):
                d = json.loads(d)

            # body_height is the most direct field
            if "body_height" in d:
                result["z"] = float(d["body_height"])
                event.set()
                return

            pos = d.get("position", {})
            if isinstance(pos, list) and len(pos) >= 3:
                result["z"] = float(pos[2])
            elif isinstance(pos, dict):
                result["z"] = float(pos.get("z", 0.0))
            event.set()
        except Exception:
            pass

    dc = conn.datachannel
    dc.pub_sub.subscribe("rt/lf/sportmodestate", _on_msg)

    try:
        await asyncio.wait_for(event.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        pass

    return result["z"]


async def ensure_posture_async(
    conn,
    target: str = POSTURE_STANDING,
    timeout: float = 8.0,
    poll_interval: float = 0.5,
    standing_height_min: float = STANDING_HEIGHT_MIN,
    crouched_height_max: float = CROUCHED_HEIGHT_MAX,
) -> bool:
    """
    Command the robot to stand or crouch and verify it reached the target.

    Args:
        conn: The raw UnitreeWebRTCConnection (not Go2Connection wrapper).
        target: POSTURE_STANDING or POSTURE_CROUCHED.
        timeout: Max seconds to wait for posture confirmation.
        poll_interval: Seconds between height checks.
        standing_height_min: Height threshold to confirm standing.
        crouched_height_max: Height threshold to confirm crouched.

    Returns:
        True if posture confirmed within timeout, False otherwise.
    """
    if target == POSTURE_STANDING:
        cmd = "StandUp"
        check = lambda z: z is not None and z >= standing_height_min
    elif target == POSTURE_CROUCHED:
        cmd = "StandDown"
        check = lambda z: z is not None and z <= crouched_height_max
    else:
        raise ValueError(f"Unknown posture target: {target}")

    await _send_sport_cmd(conn, cmd)

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        z = await _get_body_height(conn, timeout=2.0)
        if check(z):
            return True
        await asyncio.sleep(poll_interval)

    return False


def ensure_posture(
    go2_conn,
    target: str = POSTURE_STANDING,
    timeout: float = 8.0,
    standing_height_min: float = STANDING_HEIGHT_MIN,
    crouched_height_max: float = CROUCHED_HEIGHT_MAX,
) -> bool:
    """
    Blocking wrapper around ensure_posture_async for use with Go2Connection.

    Args:
        go2_conn: A connected Go2Connection instance.
        target: POSTURE_STANDING or POSTURE_CROUCHED.
        timeout: Max seconds to wait.
        standing_height_min: Height threshold to confirm standing.
        crouched_height_max: Height threshold to confirm crouched.

    Returns:
        True if posture confirmed, False on timeout.
    """
    coro = ensure_posture_async(
        go2_conn.conn,
        target=target,
        timeout=timeout,
        standing_height_min=standing_height_min,
        crouched_height_max=crouched_height_max,
    )
    return go2_conn.run_coroutine(coro, timeout=timeout + 2)
