"""v2rayN-style two-request, pooled-connection minimum latency."""
import asyncio
import time

import aiohttp
from aiohttp_socks import ProxyConnector as SocksProxyConnector
from python_socks.async_.asyncio import Proxy as SocketProxy


class ProxyConnector(SocksProxyConnector):
    """Let aiohttp own TLS, so cancelling a stalled handshake closes its socket."""

    async def _connect_via_proxy(self, host, port, ssl=None, timeout=None):
        # python-socks' stream API waits for StreamWriter.wait_closed() when a
        # TLS handshake is cancelled. On unresponsive peers that can add 60 s.
        # Its socket API handles SOCKS only; asyncio owns and cancels TLS here.
        proxy = SocketProxy(proxy_type=self._proxy_type, host=self._proxy_host,
                            port=self._proxy_port, username=self._proxy_username,
                            password=self._proxy_password, rdns=self._rdns)
        sock = await proxy.connect(dest_host=host, dest_port=port, timeout=timeout)
        try:
            return await self._loop.create_connection(
                self._factory, sock=sock, ssl=ssl,
                server_hostname=host if ssl else None,
                ssl_handshake_timeout=timeout if ssl else None,
            )
        except BaseException:
            sock.close()
            raise


async def _requests(port, url, timeout):
    connector = ProxyConnector.from_url(f"socks5://127.0.0.1:{port}", rdns=True)
    timings, codes = [], []
    async with aiohttp.ClientSession(connector=connector, trust_env=False,
                                    cookie_jar=aiohttp.CookieJar(),
                                    skip_auto_headers={"User-Agent", "Accept-Encoding", "Accept"}) as client:
        for _ in range(2):
            started = time.perf_counter()
            async with client.get(url, allow_redirects=True, max_redirects=10,
                                  timeout=aiohttp.ClientTimeout(total=None, sock_connect=timeout)) as response:
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
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    task = asyncio.create_task(_requests(port, url, timeout))
    try:
        while not task.done():
            if stop.is_set():
                raise asyncio.CancelledError()
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise asyncio.TimeoutError()
            await asyncio.wait({task}, timeout=min(.05, remaining))
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
