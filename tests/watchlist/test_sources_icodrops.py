"""Behavioral tests for the stdlib ICODrops adapter."""

from __future__ import annotations

import pathlib
import urllib.error

import pytest

from watchlist.scoring import UNKNOWN
from hermes_cli import __version__
from watchlist.sources import icodrops
from watchlist.sources.icodrops import ICODropsSource, USER_AGENT, matches_big_backers

FIXTURES = pathlib.Path(__file__).parent / "fixtures"

#: Real markup captured from icodrops.com/category/upcoming-ico/ on 2026-07-15,
#: trimmed to three rows.
#:
#: This is deliberately a captured page rather than hand-written HTML.  The
#: fixture this replaced was written to match the parser's own assumptions —
#: link-wrapping-div, always-present ticker — and the live site does neither.
#: Parser and fixture were wrong in the same way, so the suite was green while
#: the adapter returned zero projects from the real site and called it `empty`.
#: A fixture derived from the parser tests nothing; refresh this from the live
#: page, never edit it to satisfy a failing regex.
INDEX_HTML = (FIXTURES / "icodrops_upcoming.html").read_text()

#: Shaped like a live project page: the investor name element carries the fund,
#: and unrelated tooltips are present exactly as they are on the real page —
#: they must not be mistaken for investors.
DETAIL_HTML = """
<div class="Rounds-Card-Info-Block__head">Investors</div>
<div class="Rounds-Card-Info-Block__investors">
  <a class="Rounds-Card-Info-Block__top-investor" href="/fund/paradigm/">
    <img class="Rounds-Card-Info-Block__investor-icon avatar" src="x.webp" />
    <p class="Rounds-Card-Info-Block__investor-name">Paradigm</p>
  </a>
</div>
<div class="Tooltip-Section"
     data-tooltip-text="The value of a company before any new outside investment or financing."
     >
</div>
"""


class Response:
    def __init__(self, body: str, *, headers=None, final_url: str | None = None):
        self.body = body.encode()
        self.headers = headers or {}
        self.final_url = final_url
        self.offset = 0

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def geturl(self):
        return self.final_url or ""

    def read(self, size=-1):
        if size is None or size < 0:
            size = len(self.body) - self.offset
        start = self.offset
        self.offset = min(len(self.body), start + size)
        return self.body[start : self.offset]


def _config(**fetch):
    return {
        "fetch": {
            "timeout_seconds": 0.5,
            "delay_seconds": 0,
            "max_details_per_run": 40,
            **fetch,
        }
    }


def test_user_agent_tracks_hermes_version_and_backers_match_token_boundaries():
    assert f"Hermes-Agent/{__version__}" in USER_AGENT
    assert matches_big_backers(["Paradigm", "Coinbase Ventures"]) == [
        "coinbase ventures",
        "paradigm",
    ]
    assert matches_big_backers(["NotParadigm", "Jumpstart Capital"]) == []
    assert (
        matches_big_backers([
            "_Paradigm",
            "éParadigm",
            "Paradigm\u0301",
            "\u0301Paradigm",
        ])
        == []
    )


@pytest.mark.parametrize(
    "url",
    [
        "http://icodrops.com/category/upcoming-ico/",
        "https://evil.example/category/upcoming-ico/",
        "https://icodrops.com:8443/category/upcoming-ico/",
        "https://user:password@icodrops.com/category/upcoming-ico/",
    ],
)
def test_fetch_rejects_non_icodrops_urls_before_opening(monkeypatch, url):
    opened = False

    def fail_open(*_args, **_kwargs):
        nonlocal opened
        opened = True
        raise AssertionError("an invalid URL must not be opened")

    monkeypatch.setattr(icodrops, "_open", fail_open)
    with pytest.raises(ValueError):
        icodrops.fetch(url)
    assert opened is False


def test_redirect_handler_rejects_off_host_before_following():
    request = icodrops.urllib.request.Request(icodrops.UPCOMING_URL)
    handler = icodrops._ICODropsRedirectHandler()

    with pytest.raises(ValueError, match="host is not allowed"):
        handler.redirect_request(
            request,
            object(),
            302,
            "Found",
            {},
            "https://evil.example/redirected",
        )

    redirected = handler.redirect_request(
        request,
        object(),
        302,
        "Found",
        {},
        "https://www.icodrops.com/redirected",
    )
    assert redirected.full_url == "https://www.icodrops.com/redirected"


def test_fetch_rejects_off_host_final_response(monkeypatch):
    monkeypatch.setattr(
        icodrops,
        "_open",
        lambda *_args, **_kwargs: Response(
            "safe-looking body", final_url="https://evil.example/final"
        ),
    )

    with pytest.raises(ValueError, match="host is not allowed"):
        icodrops.fetch(icodrops.UPCOMING_URL)


def test_fetch_caps_response_body_even_without_content_length(monkeypatch):
    monkeypatch.setattr(icodrops, "MAX_RESPONSE_BYTES", 8)
    monkeypatch.setattr(
        icodrops,
        "_open",
        lambda *_args, **_kwargs: Response("123456789"),
    )

    with pytest.raises(ValueError, match="exceeds 8 byte limit"):
        icodrops.fetch(icodrops.UPCOMING_URL)


def test_fetch_rejects_content_length_over_cap_before_read(monkeypatch):
    monkeypatch.setattr(icodrops, "MAX_RESPONSE_BYTES", 8)

    class NoReadResponse(Response):
        def read(self, _size=-1):
            raise AssertionError("oversized Content-Length should fail first")

    monkeypatch.setattr(
        icodrops,
        "_open",
        lambda *_args, **_kwargs: NoReadResponse(
            "small", headers={"Content-Length": "9"}
        ),
    )

    with pytest.raises(ValueError, match="exceeds 8 byte limit"):
        icodrops.fetch(icodrops.UPCOMING_URL)


def test_success_ports_index_and_detail_parser(monkeypatch):
    calls = []

    def urlopen(request, timeout):
        calls.append((request.full_url, timeout, request.headers["User-agent"]))
        return Response(
            INDEX_HTML if request.full_url.endswith("upcoming-ico/") else DETAIL_HTML
        )

    monkeypatch.setattr("watchlist.sources.icodrops._open", urlopen)
    result = ICODropsSource(_config()).fetch()

    assert result.status == "ok"
    assert result.candidate_count == 3  # the captured page has 3 rows
    assert result.detail_fetch_count == 3
    assert result.detail_failure_count == 0
    assert result.detail_skipped_count == 0
    assert len(calls) == 4  # 1 index + 3 details
    assert "Hermes" in calls[0][2]

    names = [c.name for c in result.candidates]
    assert names == ["Spaceway Token", "Gno.land", "CLIX"]
    assert [c.ticker for c in result.candidates] == ["SPWAY", "GNOT", "CLIX"]

    candidate = result.candidates[0]
    assert candidate.slug == "/spaceway-token/"
    assert candidate.fields["investors"] == ["Paradigm"]
    assert candidate.fields["backer_matches"] == ["paradigm"]
    # The pre-valuation must come through clean.  On the real page a multi-line
    # tooltip <div> sits inside this cell, and a non-DOTALL tag strip leaves it
    # embedded ("$240 M <div class=...") so the number never parses and the
    # heaviest asymmetry input is silently lost on every project.
    assert candidate.fields["pre_valuation"] == "$240 M"
    # An unrelated tooltip is not an investor.
    assert "The value of a company" not in str(candidate.fields["investors"])


def test_empty_is_distinct_from_http_error(monkeypatch):
    monkeypatch.setattr(
        "watchlist.sources.icodrops._open",
        lambda *_args, **_kwargs: Response("<html></html>"),
    )
    empty = ICODropsSource(_config()).fetch()
    assert empty.status == "empty"
    assert empty.candidates == []

    def fail(*_args, **_kwargs):
        raise urllib.error.HTTPError("url", 503, "unavailable", {}, None)

    monkeypatch.setattr("watchlist.sources.icodrops._open", fail)
    failed = ICODropsSource(_config()).fetch()
    assert failed.status == "http_error"
    assert failed.status != empty.status


def test_index_parse_error_is_distinct(monkeypatch):
    monkeypatch.setattr(
        "watchlist.sources.icodrops._open",
        lambda *_args, **_kwargs: Response(INDEX_HTML),
    )
    monkeypatch.setattr(
        "watchlist.sources.icodrops.parse_projects_from_upcoming",
        lambda _html: (_ for _ in ()).throw(ValueError("bad fixture")),
    )
    result = ICODropsSource(_config()).fetch()
    assert result.status == "parse_error"
    assert result.candidates == []


def test_detail_failure_is_partial_and_keeps_investors_unknown(monkeypatch):
    def urlopen(request, timeout):
        if request.full_url.endswith("upcoming-ico/"):
            return Response(INDEX_HTML)
        raise urllib.error.URLError("detail unavailable")

    monkeypatch.setattr("watchlist.sources.icodrops._open", urlopen)
    result = ICODropsSource(_config()).fetch()

    assert result.status == "partial"
    assert result.detail_fetch_count == 3
    assert result.detail_failure_count == 3
    assert result.detail_skipped_count == 0
    assert result.candidates[0].fields["investors"] == UNKNOWN
    assert result.candidates[0].fields["backer_matches"] == UNKNOWN
    assert result.candidates[0].fields["backer_score"] == UNKNOWN
    assert result.detail_failures[0]["slug"] == "/spaceway-token/"


def test_detail_cap_is_partial_and_skipped_facts_are_unknown(monkeypatch):
    monkeypatch.setattr(
        "watchlist.sources.icodrops._open",
        lambda request, timeout: Response(
            INDEX_HTML if request.full_url.endswith("upcoming-ico/") else DETAIL_HTML
        ),
    )
    result = ICODropsSource(_config(max_details_per_run=1)).fetch()

    assert result.status == "partial"
    assert result.candidate_count == 3
    assert result.detail_fetch_count == 1
    assert result.detail_skipped_count == 2
    # Cap-skipped candidates must not look like "no backers".
    assert result.candidates[1].fields["investors"] == UNKNOWN
    assert result.candidates[1].fields["backer_matches"] == UNKNOWN


def test_rows_present_but_none_parsed_is_parse_error_not_empty(monkeypatch):
    """The failure that actually happens, and the one that hides itself.

    When ICODrops changes its markup the page still returns 200 with a full
    listing; only our regex stops matching.  Reporting that as `empty` says
    "there are no upcoming ICOs" when the truth is "we have gone blind", and
    the scanner then reports good health forever.  This is not hypothetical —
    the inherited parser did exactly this against the live site.
    """
    changed = INDEX_HTML.replace("Cll-Project__link", "Cll-Project__url")
    monkeypatch.setattr(
        "watchlist.sources.icodrops._open", lambda *_a, **_k: Response(changed)
    )
    result = ICODropsSource(_config()).fetch()

    assert result.status == "parse_error"
    assert result.candidates == []
    assert "none parsed" in (result.error or "")

    # And a genuinely empty listing is still `empty`, not a false alarm.
    monkeypatch.setattr(
        "watchlist.sources.icodrops._open",
        lambda *_a, **_k: Response("<ul class='Tbl'></ul>"),
    )
    assert ICODropsSource(_config()).fetch().status == "empty"
