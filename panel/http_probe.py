"""Cancellable HTTP probes with absolute deadlines and bounded decoded bodies."""
import asyncio
import zlib

import aiohttp
from aiohttp_socks import ProxyConnectionError, ProxyError, ProxyTimeoutError

from .delay import ProxyConnector


class BodyTooLarge(ValueError):
    pass


async def read_limited(response, limit):
    encoding = response.headers.get("Content-Encoding", "identity").lower().strip()
    if encoding not in {"identity", "", "gzip", "deflate"}:
        raise ValueError("Unsupported response encoding.")
    decoder = zlib.decompressobj(31 if encoding == "gzip" else 15) if encoding in {"gzip", "deflate"} else None
    body, received = bytearray(), 0
    async for chunk in response.content.iter_chunked(16384):
        received += len(chunk)
        if received > limit:
            raise BodyTooLarge()
        if decoder:
            try:
                chunk = decoder.decompress(chunk, limit + 1 - len(body))
            except zlib.error:
                raise ValueError("Invalid compressed response.") from None
        body.extend(chunk)
        if len(body) > limit:
            raise BodyTooLarge()
    if decoder and (not decoder.eof or decoder.unused_data):
        raise ValueError("Invalid compressed response.")
    return bytes(body)


def run_bounded(operation, timeout, stop=None):
    async def run():
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        task = asyncio.create_task(operation)
        try:
            while not task.done():
                if stop is not None and stop.is_set():
                    raise TimeoutError("Request cancelled.")
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise TimeoutError("Request timed out.")
                await asyncio.wait({task}, timeout=min(.05, remaining))
            return await task
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    return asyncio.run(run())


def proxy_get(port, url, timeout, *, limit=0, stop=None):
    async def request():
        connector = ProxyConnector.from_url(f"socks5://127.0.0.1:{port}", rdns=True)
        async with aiohttp.ClientSession(connector=connector, trust_env=False,
                                        auto_decompress=False,
                                        headers={"Accept-Encoding": "identity"},
                                        timeout=aiohttp.ClientTimeout(total=timeout)) as client:
            async with client.get(url, allow_redirects=False) as response:
                body = await read_limited(response, limit) if limit and response.status == 200 else b""
                return response.status, response.headers, body
    try:
        return run_bounded(request(), timeout, stop)
    except (ProxyError, ProxyConnectionError, ProxyTimeoutError):
        raise OSError("Proxy request failed.") from None
