"""v2rayN-style two-request, pooled-connection minimum latency."""
import asyncio
import time

import aiohttp
from aiohttp_socks import ProxyConnector


async def _requests(port, url):
    connector = ProxyConnector.from_url(f"socks5://127.0.0.1:{port}", rdns=True)
    timings, codes = [], []
    async with aiohttp.ClientSession(connector=connector, trust_env=False,
                                    cookie_jar=aiohttp.CookieJar(),
                                    skip_auto_headers={"User-Agent", "Accept-Encoding", "Accept"}) as client:
        for _ in range(2):
            started = time.perf_counter()
            async with client.get(url, allow_redirects=True, max_redirects=10,
                                  timeout=aiohttp.ClientTimeout(total=None)) as response:
                size = 0
                async for chunk in response.content.iter_chunked(8192):
                    size += len(chunk)
                    if size > 1024 * 1024:
                        raise ValueError("Delay response is too large.")
                timings.append(int((time.perf_counter() - started) * 1000))
                codes.append(response.status)
            await asyncio.sleep(.1)
    positive = [value for value in timings if value > 0]
    if not positive:
        raise ValueError("No positive delay.")
    # Like HttpClient.GetAsync in v2rayN, an HTTP response is timed regardless
    # of its status; it is not evidence of application/account availability.
    return min(positive), timings, codes


async def _cancellable(port, url, timeout, stop):
    # One cancellation budget covers both GETs, full bodies and both waits.
    task = asyncio.create_task(asyncio.wait_for(_requests(port, url), timeout))
    try:
        while not task.done():
            if stop.is_set():
                raise asyncio.CancelledError()
            await asyncio.wait({task}, timeout=.05)
        return await task
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def measure_delay(port, url, timeout, stop):
    try:
        return asyncio.run(_cancellable(port, url, timeout, stop))
    except asyncio.CancelledError:
        raise TimeoutError("Test cancelled.") from None
