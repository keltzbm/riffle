"""HTTP for every outside source: Scryfall, mtgo.com, tcgcsv.com, MTGJSON, GoatBots.

Every request sends a descriptive User-Agent, as the first three ask.
Scryfall also says a 429 must never be ignored or powered through, so
rate-limit answers wait — for Retry-After when the server sends one — before
the next try. A 404 is an answer, not a failure: callers get None and decide
what "missing" means.

Downloads stream to <dest>.part and are renamed into place only when
complete, so an interrupted run never leaves a truncated file. An answer cut
off partway is a FetchError like any other failure: a connection dropped or
timed out mid-read, a chunked answer that ends early (http.client's
IncompleteRead, which isn't an OSError), or a download that ends short of its
Content-Length (http.client just stops reading there). A host that gives no
answer at all, try after try, is a NoAnswer, so a source asking it for many
files can stop there instead of waiting out each one.
"""

import http.client
import socket
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path

from riffle import __version__

USER_AGENT = f"riffle/{__version__} (github.com/keltzbm/riffle)"
RETRY_STATUS = {429, 500, 502, 503, 504}
CHUNK = 1 << 20

Progress = Callable[[int, int | None], None]  # (bytes so far, total bytes if known)


class FetchError(RuntimeError):
    """A source didn't answer, or answered with an error that retrying won't fix."""


class NoAnswer(FetchError):
    """No answer at all, however many tries: the host is down or unreachable, so a source's
    other requests won't do better this run."""


def _why(e: BaseException) -> str:
    return str(e) or type(e).__name__


def _retry_after(e: urllib.error.HTTPError) -> float | None:
    value = e.headers.get("Retry-After") if e.headers else None
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None  # an HTTP date; the default backoff is close enough


def _open(url: str, accept: str, timeout: float, retries: int):
    """An open response, or None for 404. Retries stalls, rate limits, and 5xx."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": accept})
    for attempt in range(retries + 1):
        backoff = 2.0 * (attempt + 1)
        try:
            return urllib.request.urlopen(req, timeout=timeout)
        except urllib.error.HTTPError as e:  # before URLError: HTTPError is a subclass
            if e.code == 404:
                return None
            if e.code not in RETRY_STATUS or attempt == retries:
                raise FetchError(f"HTTP {e.code}") from e
            wait = _retry_after(e) or backoff
        except (OSError, http.client.HTTPException) as e:  # timeouts, refusals, resets, a garbled answer
            if attempt == retries:
                raise NoAnswer(
                    f"no answer after {retries + 1} tries ({getattr(e, 'reason', None) or _why(e)})"
                ) from e
            wait = backoff
        time.sleep(wait)
    raise AssertionError("unreachable")


def wait_online(host: str, timeout: float = 120.0, pause: float = 5.0, port: int = 443) -> float | None:
    """Seconds until host accepted a connection, or None if it didn't within timeout. A job
    launchd runs as the Mac wakes starts before the network is back."""
    start = time.monotonic()
    while True:
        try:
            with socket.create_connection((host, port), timeout=pause):
                return time.monotonic() - start
        except OSError:
            if time.monotonic() - start + pause > timeout:
                return None
            time.sleep(pause)


def get(url: str, accept: str = "*/*", timeout: float = 60, retries: int = 2) -> bytes | None:
    """The whole body, or None for 404."""
    r = _open(url, accept, timeout, retries)
    if r is None:
        return None
    with r:
        try:
            return r.read()  # a short Content-Length body raises IncompleteRead here
        except (OSError, http.client.HTTPException) as e:
            raise FetchError(f"answer broke off while reading ({_why(e)})") from e


def get_text(url: str, accept: str = "text/html", timeout: float = 60, retries: int = 2) -> str:
    """The body as text. Here a 404 is an error: the page was expected to exist."""
    body = get(url, accept, timeout, retries)
    if body is None:
        raise FetchError("HTTP 404")
    return body.decode("utf-8", errors="replace")


def download(
    url: str,
    dest: Path,
    accept: str = "*/*",
    timeout: float = 60,
    retries: int = 2,
    progress: Progress | None = None,
) -> int | None:
    """Stream to dest, reporting progress per chunk. Bytes written, or None for 404."""
    r = _open(url, accept, timeout, retries)
    if r is None:
        return None
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    length = r.headers.get("Content-Length") if r.headers else None
    total = int(length) if length and length.isdigit() else None
    done = 0
    try:
        with r, tmp.open("wb") as f:
            while chunk := r.read(CHUNK):
                f.write(chunk)
                done += len(chunk)
                if progress:
                    progress(done, total)
    except (
        OSError,
        http.client.HTTPException,
    ) as e:  # timeouts, dropped connections, a cut-off chunked answer
        tmp.unlink(missing_ok=True)
        raise FetchError(f"download interrupted after {done:,} bytes ({_why(e)})") from e
    except BaseException:  # Ctrl-C: leave nothing half-written behind
        tmp.unlink(missing_ok=True)
        raise
    if total is not None and done != total:
        tmp.unlink(missing_ok=True)
        raise FetchError(f"download cut off after {done:,} of {total:,} bytes")
    tmp.replace(dest)
    return done
