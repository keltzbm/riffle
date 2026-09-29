"""riffle.net against a fake urlopen: retries, rate limits, 404s, and streamed downloads."""

import gzip
import http.client
import io
import socket
import urllib.error

import pytest

from riffle import net


class Resp(io.BytesIO):
    """A response: readable in chunks, usable in `with`, with headers."""

    def __init__(self, body: bytes, length: bool = True):
        super().__init__(body)
        self.headers = {"Content-Length": str(len(body))} if length else {}


def http_error(code: int, retry_after: str | None = None) -> urllib.error.HTTPError:
    headers = {"Retry-After": retry_after} if retry_after else {}
    return urllib.error.HTTPError("https://example.test", code, "x", headers, None)


@pytest.fixture
def server(monkeypatch):
    """Answers queued per test; records every request and every sleep."""
    state = {"answers": [], "requests": [], "sleeps": []}

    def urlopen(req, timeout):
        state["requests"].append(req)
        answer = state["answers"].pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    monkeypatch.setattr(net.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(net.time, "sleep", state["sleeps"].append)
    return state


def test_every_request_identifies_the_tool(server):
    server["answers"] = [Resp(b"ok")]
    net.get("https://example.test", accept="application/json")
    req = server["requests"][0]
    ua = req.get_header("User-agent")
    assert ua.startswith("riffle/") and "github.com/keltzbm/riffle" in ua
    assert req.get_header("Accept") == "application/json"


def test_stalls_are_retried_with_growing_waits(server):
    server["answers"] = [TimeoutError("slow"), TimeoutError("slow"), Resp(b"ok")]
    assert net.get_text("https://example.test") == "ok"
    assert server["sleeps"] == [2.0, 4.0]


def test_gives_up_after_the_last_retry(server):
    server["answers"] = [urllib.error.URLError("down")] * 3
    with pytest.raises(net.NoAnswer, match=r"no answer after 3 tries \(down\)"):
        net.get("https://example.test")
    assert len(server["requests"]) == 3


def test_any_connection_error_is_retried_then_no_answer(server):
    server["answers"] = [OSError(65, "No route to host"), OSError(65, "No route to host"), Resp(b"ok")]
    assert net.get("https://example.test") == b"ok"
    server["answers"] = [ConnectionRefusedError()] * 3
    with pytest.raises(net.NoAnswer, match=r"no answer after 3 tries \(ConnectionRefusedError\)"):
        net.get("https://example.test")


def test_rate_limit_waits_for_retry_after(server):
    server["answers"] = [http_error(429, retry_after="7"), Resp(b"ok")]
    assert net.get("https://example.test") == b"ok"
    assert server["sleeps"] == [7.0]


def test_rate_limit_without_retry_after_uses_the_backoff(server):
    server["answers"] = [http_error(429, retry_after="Wed, 21 Oct 2026 07:28:00 GMT"), Resp(b"ok")]
    assert net.get("https://example.test") == b"ok"
    assert server["sleeps"] == [2.0]


def test_404_is_an_answer_not_a_failure(server):
    server["answers"] = [http_error(404)]
    assert net.get("https://example.test") is None
    assert server["sleeps"] == []
    server["answers"] = [http_error(404)]
    assert net.get_reply("https://example.test") is None


def test_a_reply_carries_the_answers_headers_by_lower_case_name(server):
    answer = Resp(b"ok")
    answer.headers["Last-Modified"] = "Sun, 27 Sep 2026 20:04:00 GMT"
    server["answers"] = [answer]
    reply = net.get_reply("https://example.test")
    assert reply == net.Reply(
        b"ok", {"content-length": "2", "last-modified": "Sun, 27 Sep 2026 20:04:00 GMT"}
    )


def test_get_text_treats_404_as_an_error(server):
    server["answers"] = [http_error(404)]
    with pytest.raises(net.FetchError, match="HTTP 404"):
        net.get_text("https://example.test")


def test_client_errors_are_not_retried(server):
    server["answers"] = [http_error(403)]
    with pytest.raises(net.FetchError, match="HTTP 403"):
        net.get("https://example.test")
    assert len(server["requests"]) == 1


def test_download_streams_in_chunks_and_reports_progress(server, tmp_path, monkeypatch):
    monkeypatch.setattr(net, "CHUNK", 4)
    server["answers"] = [Resp(b"0123456789")]
    seen = []
    dest = tmp_path / "sub" / "file.bin"
    assert net.download("https://example.test", dest, progress=lambda d, t: seen.append((d, t))) == 10
    assert dest.read_bytes() == b"0123456789"
    assert seen == [(4, 10), (8, 10), (10, 10)]
    assert not (tmp_path / "sub" / "file.bin.part").exists()


def test_download_without_content_length_reports_unknown_total(server, tmp_path):
    server["answers"] = [Resp(b"abc", length=False)]
    seen = []
    net.download("https://example.test", tmp_path / "f", progress=lambda d, t: seen.append(t))
    assert seen == [None]


def test_interrupted_download_leaves_no_file(server, tmp_path):
    class Dropped(Resp):
        def read(self, n=-1):
            raise ConnectionResetError("reset")

    server["answers"] = [Dropped(b"")]
    dest = tmp_path / "f"
    with pytest.raises(net.FetchError, match="interrupted"):
        net.download("https://example.test", dest)
    assert not dest.exists() and not (tmp_path / "f.part").exists()


def test_a_chunked_download_cut_off_is_a_fetch_error(server, tmp_path):
    class CutOff(Resp):
        def read(self, n=-1):
            raise http.client.IncompleteRead(b"par", 10)

    server["answers"] = [CutOff(b"", length=False)]
    with pytest.raises(net.FetchError, match=r"interrupted after 0 bytes \(IncompleteRead\(3 bytes read"):
        net.download("https://example.test", tmp_path / "f")
    assert list(tmp_path.iterdir()) == []


def test_a_download_short_of_its_content_length_is_kept_nowhere(server, tmp_path):
    short = Resp(b"01234")
    short.headers = {"Content-Length": "10"}  # the server promised 10 bytes and closed after 5
    server["answers"] = [short]
    with pytest.raises(net.FetchError, match="cut off after 5 of 10 bytes"):
        net.download("https://example.test", tmp_path / "f")
    assert list(tmp_path.iterdir()) == []


def test_an_answer_cut_off_mid_read_is_a_fetch_error(server):
    class CutOff(Resp):
        def read(self, n=-1):
            raise http.client.IncompleteRead(b"{", 100)

    server["answers"] = [CutOff(b"")]
    with pytest.raises(net.FetchError, match="answer broke off while reading"):
        net.get("https://example.test")


def test_a_timeout_mid_read_is_a_fetch_error(server):
    class Stalled(Resp):
        def read(self, n=-1):
            raise TimeoutError("timed out")

    server["answers"] = [Stalled(b"")]
    with pytest.raises(net.FetchError, match=r"answer broke off while reading \(timed out\)") as e:
        net.get("https://example.test")
    assert not isinstance(e.value, net.NoAnswer)  # it answered; only this answer is lost


def test_a_garbled_status_line_is_retried(server):
    server["answers"] = [http.client.BadStatusLine("garbage"), Resp(b"ok")]
    assert net.get("https://example.test") == b"ok"
    assert server["sleeps"] == [2.0]


def test_download_404_writes_nothing(server, tmp_path):
    server["answers"] = [http_error(404)]
    assert net.download("https://example.test", tmp_path / "f") is None
    assert list(tmp_path.iterdir()) == []


def test_a_download_can_name_other_statuses_that_mean_missing(server, tmp_path):
    server["answers"] = [http_error(403)]
    assert net.download("https://example.test", tmp_path / "f", missing=(403, 404)) is None
    server["answers"] = [http_error(403)]
    with pytest.raises(net.FetchError, match="HTTP 403"):
        net.download("https://example.test", tmp_path / "f")  # 404 only, unless the caller says
    assert list(tmp_path.iterdir()) == []


class Answered(Resp):
    """A response that says where it came from, as urlopen's do after a redirect."""

    def __init__(self, body: bytes, url: str, status: int = 200):
        super().__init__(body)
        self.status, self.url = status, url

    def geturl(self) -> str:
        return self.url


def test_get_once_is_one_request_and_any_status_is_an_answer(server):
    server["answers"] = [Answered(b"page", "https://example.test/moved"), http_error(503), http_error(404)]
    first = net.get_once("https://example.test/asked", accept="text/html")
    assert (first.status, first.url, first.body) == (200, "https://example.test/moved", b"page")
    assert net.get_once("https://example.test/asked").status == 503
    assert net.get_once("https://example.test/asked").status == 404
    assert len(server["requests"]) == 3 and server["sleeps"] == []  # no retries, no waits
    assert server["requests"][0].get_header("Accept") == "text/html"


def test_get_once_with_no_answer_is_a_fetch_error(server):
    server["answers"] = [TimeoutError("timed out")]
    with pytest.raises(net.FetchError, match="timed out"):
        net.get_once("https://example.test")


@pytest.mark.parametrize("value", ["-5", "nan", "inf", "Wed, 21 Oct 2026 07:28:00 GMT"])
def test_a_retry_after_that_isnt_a_wait_in_seconds_gets_the_backoff(server, value):
    server["answers"] = [http_error(503, retry_after=value), Resp(b"ok")]
    assert net.get("https://example.test") == b"ok"  # time.sleep would raise on -5 or nan
    assert server["sleeps"] == [2.0]


def test_the_network_is_up_when_any_host_answers(monkeypatch):
    tried = []

    def create_connection(address, timeout):
        tried.append(address[0])
        if address[0] == "api.scryfall.com":
            raise socket.gaierror(8, "nodename nor servname provided, or not known")
        return socket.socket()

    monkeypatch.setattr(net.socket, "create_connection", create_connection)
    assert net.wait_online("api.scryfall.com", "mtgjson.com", "tcgcsv.com") is not None
    assert tried == ["api.scryfall.com", "mtgjson.com"]


# ---- fetch_new: a list that may be one already kept --------------------------------------------

LIST = b'{"meta": {"as_of": "2026-09-29T06:36:03Z"}, "data": [' + b'{"a": 1},' * 3000 + b'{"a": 2}]}'
URL = "https://example.test/list"


class Answer(Resp):
    """A 200 with the headers a store sends."""

    def __init__(self, body: bytes, headers: dict[str, str] | None = None, length: bool = True):
        super().__init__(body, length)
        self.status = 200
        self.headers = {**self.headers, **(headers or {})}
        self.sent = 0

    def read(self, size=-1):
        chunk = super().read(size)
        self.sent += len(chunk)
        return chunk


class Dropping(Answer):
    """An answer that breaks off after its first read."""

    def __init__(self, body: bytes, error: BaseException):
        super().__init__(body)
        self.error, self.reads = error, 0

    def read(self, size=-1):
        self.reads += 1
        if self.reads > 1:
            raise self.error
        return super().read(size)


def never_kept(head: bytes) -> bool:
    return False


def test_fetch_new_asks_for_gzip_and_sends_the_last_etag(server, tmp_path):
    server["answers"] = [http_error(304)]
    got = net.fetch_new(URL, tmp_path / "list", never_kept, etag='"v1"')
    assert got == net.Fetched("unchanged", b"", '"v1"')
    req = server["requests"][0]
    assert req.get_header("Accept-encoding") == "gzip" and req.get_header("If-none-match") == '"v1"'
    assert list(tmp_path.iterdir()) == []


def test_fetch_new_with_no_etag_to_send_asks_plainly(server, tmp_path):
    server["answers"] = [Answer(LIST)]
    net.fetch_new(URL, tmp_path / "list", never_kept)
    assert server["requests"][0].get_header("If-none-match") is None


def test_a_list_already_kept_is_hung_up_on_after_its_first_bytes(server, tmp_path):
    answer = Answer(LIST, {"ETag": '"v2"'})
    server["answers"] = [answer]
    seen = []
    got = net.fetch_new(URL, tmp_path / "list", lambda head: seen.append(head) or True)
    assert got == net.Fetched("known", LIST[: net.HEAD], '"v2"')
    assert seen == [LIST[: net.HEAD]] and answer.closed and answer.sent == net.HEAD < len(LIST)
    assert list(tmp_path.iterdir()) == []


def test_a_new_list_sent_gzipped_is_kept_unpacked(server, tmp_path):
    server["answers"] = [Answer(gzip.compress(LIST), {"Content-Encoding": "gzip", "ETag": '"v3"'})]
    progress = []
    got = net.fetch_new(
        URL, tmp_path / "list", never_kept, progress=lambda done, total: progress.append((done, total))
    )
    assert got == net.Fetched("new", LIST[: net.HEAD], '"v3"', len(LIST))
    assert (tmp_path / "list").read_bytes() == LIST and progress[-1] == (len(LIST), None)


def test_a_new_list_sent_plain_is_kept_as_sent(server, tmp_path):
    server["answers"] = [Answer(LIST)]
    progress = []
    got = net.fetch_new(
        URL, tmp_path / "list", never_kept, progress=lambda done, total: progress.append(done)
    )
    assert got == net.Fetched("new", LIST[: net.HEAD], None, len(LIST))
    assert (tmp_path / "list").read_bytes() == LIST and progress == [net.HEAD, len(LIST)]


def test_a_gzip_list_that_never_ends_is_cut_off(server, tmp_path):
    server["answers"] = [Answer(gzip.compress(LIST)[:-20], {"Content-Encoding": "gzip"}, length=False)]
    with pytest.raises(net.FetchError, match="download cut off after"):
        net.fetch_new(URL, tmp_path / "list", never_kept)
    assert list(tmp_path.iterdir()) == []


def test_a_list_short_of_its_length_is_cut_off(server, tmp_path):
    answer = Answer(LIST)
    answer.headers["Content-Length"] = str(len(LIST) + 10)
    server["answers"] = [answer]
    with pytest.raises(net.FetchError, match="download cut off after"):
        net.fetch_new(URL, tmp_path / "list", never_kept)
    assert list(tmp_path.iterdir()) == []


def test_a_list_that_says_gzip_but_isn_t_is_a_fetch_error(server, tmp_path):
    server["answers"] = [Answer(b"not gzip at all" * 50, {"Content-Encoding": "gzip"})]
    with pytest.raises(net.FetchError, match="download interrupted after 0 bytes"):
        net.fetch_new(URL, tmp_path / "list", never_kept)


def test_a_list_broken_off_partway_leaves_nothing(server, tmp_path):
    server["answers"] = [Dropping(LIST, ConnectionResetError("reset"))]
    with pytest.raises(net.FetchError, match="download interrupted after 4,096 bytes"):
        net.fetch_new(URL, tmp_path / "list", never_kept)
    assert list(tmp_path.iterdir()) == []


def test_ctrl_c_partway_through_a_list_leaves_nothing(server, tmp_path):
    server["answers"] = [Dropping(LIST, KeyboardInterrupt())]
    with pytest.raises(KeyboardInterrupt):
        net.fetch_new(URL, tmp_path / "list", never_kept)
    assert list(tmp_path.iterdir()) == []


def test_fetch_new_404_is_none(server, tmp_path):
    server["answers"] = [http_error(404)]
    assert net.fetch_new(URL, tmp_path / "list", never_kept) is None
