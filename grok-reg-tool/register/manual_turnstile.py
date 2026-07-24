"""Human-in-the-loop Turnstile waiting helpers.

This module deliberately does not click, solve, or modify the challenge. It only
waits for a human to complete the challenge in the visible browser session.
"""

from __future__ import annotations

import time
from typing import Callable, Optional


DEFAULT_TAKEOVER_URL = "http://127.0.0.1:6080/vnc.html"


def wait_for_manual_turnstile(
    read_response: Callable[[], Optional[str]],
    *,
    timeout: float = 300,
    interval: float = 1,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    log: Callable[[str], None] = print,
    takeover_url: str = DEFAULT_TAKEOVER_URL,
) -> str:
    """Wait until a human completes Turnstile in the shared browser window."""
    log("[ACTION_REQUIRED] 等待人工完成 Turnstile 验证，请勿关闭当前任务。")
    log(f"[ACTION_REQUIRED] 请在本机打开: {takeover_url}")

    deadline = monotonic() + timeout
    while monotonic() < deadline:
        response = str(read_response() or "").strip()
        if response:
            log("[*] 人工验证已通过，自动继续注册流程。")
            return response
        sleep(interval)

    raise TimeoutError(f"人工 Turnstile 验证等待超时（{int(timeout)} 秒）")
