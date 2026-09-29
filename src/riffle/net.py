"""HTTP for every outside source: Scryfall, mtgo.com, tcgcsv.com, MTGJSON, GoatBots,
Cardmarket, Card Kingdom, Mana Pool.

Every request sends a descriptive User-Agent, as the first three ask.
Scryfall also says a 429 must never be ignored or powered through, so
rate-limit answers wait — for Retry-After when the server sends one — before
the next try. A 404 is an answer, not a failure: callers get None and decide
what "missing" means. A download can name other statuses that mean missing
(Cardmarket's server answers 403 for a file it doesn't have).

Downloads stream to <dest>.part and are renamed into place only when
complete, so an interrupted run never leaves a truncated file. An answer cut
off partway is a FetchError like any other failure: a connection dropped or
timed out mid-read, a chunked answer that ends early (http.client's
IncompleteRead, which isn't an OSError), or a download that ends short of its
Content-Length (http.client just stops reading there). A host that gives no
answer at all, try after try, is a NoAnswer, so a source asking it for many
files can stop there instead of waiting out each one.

fetch_new asks for a list that may be one already kept, with one request: it sends the
last ETag, so a source that keeps them answers 304 and nothing more, asks for gzip, and
hangs up once the list's first bytes show it's kept.
"""

import http.client
import math
import socket
import time
import urllib.error
import urllib.request
import zlib
from collections.abc import Callable, Collection
from dataclasses import dataclass
from pathlib import Path

from riffle import __version__

USER_AGENT = f"riffle/{__version__} (github.com/keltzbm/riffle)"
RETRY_STATUS = {429, 500, 502, 503, 504}
CHUNK = 1 << 20
HEAD = 4096  # bytes of a list fetch_new reads before deciding whether it's kept

Progress = Callable[[int, int | None], None]  # (bytes so far, total bytes if known)


class FetchError(RuntimeError):
    """A source didn't answer, or answered with an error that retrying won't fix."""


class NoAnswer(FetchError):
    """No answer at all, however many tries: the host is down or unreachable, so a source's
    other requests won't do better this run."""


def _why(e: BaseException) -> str:
    return str(e) or type(e).__name__


def _retry_after(e: urllib.error.HTTPError) -> float | None:
    """Retry-After in seconds, or None for the default backoff: when there's none, when it's an
    HTTP date (the backoff is close enough), and when it's negative or not a number, which
    time.sleep would raise on."""
    value = e.headers.get("Retry-After") if e.headers else None
    try:
        seconds = float(value) if value is not None else None
    except ValueError:
        return None
    return seconds if seconds is not None and math.isfinite(seconds) and seconds >= 0 else None


def _open(
    url: str,
    accept: str,
    timeout: float,
    retries: int,
    missing: Collection[int] = (404,),
    headers: dict[str, str] | None = None,
    answers: Collection[int] = (),
):
    """An open response, or None for a status in missing. A status in answers is returned as
    the response it is (a 304). Retries stalls, rate limits, and 5xx."""
    sent = {"User-Agent": USER_AGENT, "Accept": accept, **(headers or {})}
    req = urllib.request.Request(url, headers=sent)
    for attempt in range(retries + 1):
        backoff = 2.0 * (attempt + 1)
        try:
            return urllib.request.urlopen(req, timeout=timeout)
        except urllib.error.HTTPError as e:  # before URLError: HTTPError is a subclass
            if e.code in missing:
                return None
            if e.code in answers:
                return e
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


def wait_online(*hosts: str, timeout: float = 120.0, pause: float = 5.0, port: int = 443) -> float | None:
    """Seconds until any of hosts accepted a connection, tried in order, or None if none did
    within timeout. A job launchd runs as the Mac wakes starts before the network is back.
    One host that's down, or whose name won't resolve, doesn't make the network look down."""
    start = time.monotonic()
    while True:
        for host in hosts:
            try:
                with socket.create_connection((host, port), timeout=pause):
                    return time.monotonic() - start
            except OSError:
                pass
        if time.monotonic() - start + pause > timeout:
            return None
        time.sleep(pause)


@dataclass
class Reply:
    body: bytes
    headers: dict[str, str]  # names lower-cased: "last-modified"


def get_reply(url: str, accept: str = "*/*", timeout: float = 60, retries: int = 2) -> Reply | None:
    """The whole body and the answer's headers, or None for 404."""
    r = _open(url, accept, timeout, retries)
    if r is None:
        return None
    with r:
        try:
            body = r.read()  # a short Content-Length body raises IncompleteRead here
        except (OSError, http.client.HTTPException) as e:
            raise FetchError(f"answer broke off while reading ({_why(e)})") from e
        return Reply(body, {name.lower(): value for name, value in r.headers.items()})


def get(url: str, accept: str = "*/*", timeout: float = 60, retries: int = 2) -> bytes | None:
    """The whole body, or None for 404."""
    reply = get_reply(url, accept, timeout, retries)
    return None if reply is None else reply.body


@dataclass
class Answer:
    status: int
    url: str  # where the answer came from: not the URL asked for after a redirect
    body: bytes


def get_once(url: str, accept: str = "*/*", timeout: float = 60) -> Answer:
    """One request and no retries, so each attempt is one request its caller can log.
    Any HTTP status is an answer; only no answer at all is a FetchError."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": accept})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return Answer(r.status, r.geturl(), r.read())
    except urllib.error.HTTPError as e:  # before URLError: HTTPError is a subclass
        return Answer(e.code, e.geturl() or url, b"")
    except (OSError, http.client.HTTPException) as e:
        raise FetchError(_why(e)) from e


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
    missing: Collection[int] = (404,),
) -> int | None:
    """Stream to dest, reporting progress per chunk. Bytes written, or None for a status in
    missing, 404 unless the caller says otherwise."""
    r = _open(url, accept, timeout, retries, missing)
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


@dataclass
class Fetched:
    """What fetch_new found: "unchanged" (a 304 for the ETag sent), "known" (hung up once the
    list's first bytes showed it's kept) or "new" (written to dest whole, unpacked)."""

    status: str
    head: bytes  # the list's first bytes, unpacked; none when unchanged
    etag: str | None  # the answer's, or the one sent when unchanged
    size: int = 0  # bytes written


def fetch_new(
    url: str,
    dest: Path,
    known: Callable[[bytes], bool],
    etag: str | None = None,
    accept: str = "*/*",
    timeout: float = 60,
    retries: int = 2,
    progress: Progress | None = None,
) -> Fetched | None:
    """One request for a list that may be one already kept; None for 404. known gets the
    list's first HEAD bytes and says whether it's kept. A new list streams to dest as download
    does, gzip unpacked, and one cut off is a FetchError: short of its Content-Length, or a gzip
    stream that never ends."""
    headers = {"Accept-Encoding": "gzip"} | ({"If-None-Match": etag} if etag else {})
    r = _open(url, accept, timeout, retries, headers=headers, answers=(304,))
    if r is None:
        return None
    if r.status == 304:
        r.close()
        return Fetched("unchanged", b"", etag)
    tag = r.headers.get("ETag")
    unpack = zlib.decompressobj(31) if (r.headers.get("Content-Encoding") or "").lower() == "gzip" else None
    length = r.headers.get("Content-Length")
    total = int(length) if length and length.isdigit() else None
    sent = written = 0
    tmp = dest.with_name(dest.name + ".part")

    def more(size: int) -> bytes | None:
        nonlocal sent
        chunk = r.read(size)
        sent += len(chunk)
        if not chunk:
            return None
        return unpack.decompress(chunk) if unpack else chunk

    try:
        with r:
            head = b""
            while len(head) < HEAD and (got := more(HEAD)) is not None:
                head += got
            if known(head[:HEAD]):
                return Fetched("known", head[:HEAD], tag)
            dest.parent.mkdir(parents=True, exist_ok=True)
            with tmp.open("wb") as f:
                f.write(head)
                written = len(head)
                if progress:
                    progress(written, None)
                while (got := more(CHUNK)) is not None:
                    f.write(got)
                    written += len(got)
                    if progress:
                        progress(written, None)
    except (OSError, http.client.HTTPException, zlib.error) as e:
        tmp.unlink(missing_ok=True)
        raise FetchError(f"download interrupted after {written:,} bytes ({_why(e)})") from e
    except BaseException:  # Ctrl-C: leave nothing half-written behind
        tmp.unlink(missing_ok=True)
        raise
    if (total is not None and sent != total) or (unpack is not None and not unpack.eof):
        tmp.unlink(missing_ok=True)
        raise FetchError(f"download cut off after {written:,} bytes")
    tmp.replace(dest)
    return Fetched("new", head[:HEAD], tag, written)
