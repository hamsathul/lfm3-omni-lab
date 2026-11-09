"""The web browser's only way out: a small HTTP proxy that refuses this computer and the local network.

The browser sends every request through it, plain http and https tunnels alike, loopback
included. It looks each host up itself and connects only to public internet addresses, so a
page can't reach a local service by an IP address, a .local name, or a public name that
resolves to a private address (DNS rebinding, localtest.me and the like). It listens on
127.0.0.1 only and keeps no logs beyond the last few refusals, which tell the model why a
page didn't open.
"""

import asyncio
import collections
import ipaddress
import socket
import time
import urllib.parse

LOCAL_SUFFIXES = (".local", ".localhost", ".internal", ".lan", ".home.arpa")
REFUSAL = "Addresses on this computer or the local network can't be opened."

# (time, host) of recent refusals, newest last.
refused = collections.deque(maxlen=20)


def is_local(host):
    """A host that names this computer or the local network, judged by its name alone."""
    host = (host or "").strip("[]").lower().rstrip(".")
    if not host or host == "localhost" or host.endswith(LOCAL_SUFFIXES):
        return True
    try:
        return not is_public(host)
    except ValueError:
        return False  # a name, not an address: its addresses are checked when it's looked up


def is_public(address):
    """An IP address on the public internet: not loopback, private, link-local, CGNAT
    (Tailscale's 100.64/10), multicast, reserved or unspecified."""
    ip = ipaddress.ip_address(address.split("%")[0])
    if ip.version == 6:
        embedded = ip.ipv4_mapped or ip.sixtofour or (ip.teredo[1] if ip.teredo else None)
        if embedded is not None:
            ip = embedded
    return ip.is_global and not ip.is_multicast


class Refused(Exception):
    pass


async def addresses(host, port):
    """The host's public addresses, or Refused if it is, or resolves to, anything local."""
    if is_local(host):
        raise Refused(host)
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    found = list(dict.fromkeys(info[4][0] for info in infos))
    # One private answer is enough to refuse: a name shouldn't straddle the two.
    if not found or not all(is_public(a) for a in found):
        raise Refused(host)
    return found


async def check(url):
    """None if the browser may open this http(s) address, else why not."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        return "Only http and https addresses can be opened."
    try:
        await addresses(parts.hostname, parts.port or (443 if parts.scheme.lower() == "https" else 80))
    except Refused:
        return REFUSAL
    except (socket.gaierror, UnicodeError):
        return None  # the browser reports a name that doesn't resolve better than we can
    return None


async def _connect(host, port):
    last = None
    for address in (await addresses(host, port))[:4]:
        try:
            return await asyncio.wait_for(asyncio.open_connection(address, port), 10)
        except (OSError, asyncio.TimeoutError) as exc:
            last = exc
    raise last or OSError("no address")


async def _pipe(reader, writer):
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    except (ConnectionError, OSError, asyncio.CancelledError):
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


def _split_target(target, default_port):
    """host:port from a CONNECT target, IPv6 brackets included."""
    parts = urllib.parse.urlsplit("//" + target)
    return parts.hostname, parts.port or default_port


def _page(status, reason, message):
    body = (f"<!doctype html><title>{reason}</title><p style='font:15px system-ui;margin:3em'>{message}</p>").encode()
    return (f"HTTP/1.1 {status} {reason}\r\nContent-Type: text/html; charset=utf-8\r\n"
            f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n").encode() + body


async def _handle(client_reader, client_writer):
    try:
        head = await asyncio.wait_for(client_reader.readuntil(b"\r\n\r\n"), 30)
        lines = head.decode("latin-1").split("\r\n")
        method, target, version = lines[0].split(" ", 2)
        if method.upper() == "CONNECT":
            host, port = _split_target(target, 443)
            try:
                upstream_reader, upstream_writer = await _connect(host, port)
            except Refused:
                refused.append((time.time(), host))
                client_writer.write(_page(403, "Forbidden", REFUSAL))
                return
            client_writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await client_writer.drain()
        else:
            url = urllib.parse.urlsplit(target)
            if url.scheme.lower() != "http" or not url.hostname:
                client_writer.write(_page(400, "Bad Request", "The browser's proxy only forwards web addresses."))
                return
            try:
                upstream_reader, upstream_writer = await _connect(url.hostname, url.port or 80)
            except Refused:
                refused.append((time.time(), url.hostname))
                client_writer.write(_page(403, "Forbidden", REFUSAL))
                return
            path = (url.path or "/") + (f"?{url.query}" if url.query else "")
            headers = [h for h in lines[1:] if h and not h.lower().startswith(
                ("proxy-connection:", "proxy-authorization:", "connection:", "keep-alive:"))]
            upgrade = any(h.lower().startswith("upgrade:") for h in headers)
            # One request per connection keeps this simple: the browser opens another for the next.
            headers.append("Connection: Upgrade" if upgrade else "Connection: close")
            request = f"{method} {path} {version}\r\n" + "".join(h + "\r\n" for h in headers) + "\r\n"
            upstream_writer.write(request.encode("latin-1"))
        await asyncio.gather(_pipe(client_reader, upstream_writer), _pipe(upstream_reader, client_writer))
    except (socket.gaierror, UnicodeError):
        client_writer.write(_page(502, "Bad Gateway", "That address couldn't be found."))
    except (OSError, asyncio.TimeoutError, ValueError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
        try:
            client_writer.write(_page(502, "Bad Gateway", "The site couldn't be reached."))
        except Exception:
            pass
    finally:
        try:
            await client_writer.drain()
        except Exception:
            pass
        client_writer.close()


class Proxy:
    """Started on first use in the app's event loop; `url` is what the browser is told to use."""

    def __init__(self):
        self.server = None

    async def start(self):
        if self.server is None:
            self.server = await asyncio.start_server(_handle, "127.0.0.1", 0)
        return self.url

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server.sockets[0].getsockname()[1]}" if self.server else None

    async def stop(self):
        if self.server is not None:
            self.server.close()
            self.server = None
