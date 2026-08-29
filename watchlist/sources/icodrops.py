"""Stdlib ICODrops adapter for the upcoming-launch watchlist."""

from __future__ import annotations

import re
import time
import urllib.error
import urllib.request
from html import unescape
from typing import Any, Iterable, Mapping
from urllib.parse import urlsplit

from hermes_cli import __version__ as _HERMES_VERSION
from hermes_time import now as _hermes_now
from watchlist.config import load_config
from watchlist.scoring import UNKNOWN, contains_keyword_token
from watchlist.sources import RawCandidate, SourceResult, register_source

UPCOMING_URL = "https://icodrops.com/category/upcoming-ico/"
BASE_URL = "https://icodrops.com"
USER_AGENT = f"Hermes-Agent/{_HERMES_VERSION} (IDO watchlist; stdlib urllib)"

# The adapter only needs the two canonical ICODrops hostnames.  Keep this
# allowlist exact: accepting arbitrary subdomains or a non-default port would
# let a redirect turn a routine scrape into an SSRF primitive.
_ALLOWED_HOSTS = frozenset({"icodrops.com", "www.icodrops.com"})
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
_READ_CHUNK_BYTES = 64 * 1024

# Preserve the working parser's source vocabulary.  Matching is data-driven so
# the scanner can store observed names while scoring remains independent.
BIG_BACKER_KEYWORDS = (
    "a16z",
    "binance",
    "coinbase",
    "coinbase ventures",
    "jump",
    "dragonfly",
    "paradigm",
    "pantera",
    "multicoin",
    "sequoia",
    "polygon",
    "okx",
    "htx",
    "huobi",
    "binance labs",
    "echo",
    "yzi",
    "tron",
    "spartan",
    "bybit",
    "kraken",
)

NOISY_TOKENS = {
    "the total amount of funds raised from all investing rounds.",
    "the value of a company before any new outside investment or financing. pre-valuations are subjective, and can be based on a company’s financials, comparable exits in the market, and the makeup of the founders and team.",
    "a measure that shows what the market cap would be if the max coin circulation aupply was taken.",
    "the total amount of cryptocurrency transactions that have been traded in the past 24 hours.",
}


_ROW_RE = r'<li class="Tbl-Row[^>]*>.*?</li>'

#: The listing wraps the link in the project div, not the other way round.  The
#: inherited regex had them inverted and matched 0 of 50 live rows — verified
#: against icodrops.com/category/upcoming-ico/ on 2026-07-15.  The ticker is
#: optional: ICODrops omits the element entirely for projects without one.
_PROJECT_RE = re.compile(
    r'<div class="Cll-Project">\s*'
    r'<a class="Cll-Project__link" href="(/[^"/]+/)"[^>]*>\s*'
    r'<p class="Cll-Project__name[^"]*">\s*(.*?)\s*</p>'
    r'(?:\s*<p class="Cll-Project__ticker">\s*(.*?)\s*</p>)?',
    re.S,
)

#: Investor names on a project page.  Anchoring on a `__title">Investors`
#: heading (as the inherited script did) no longer matches any live page.
_INVESTOR_NAME_RE = re.compile(
    r'class="Rounds-Card-Info-Block__investor-name"[^>]*>\s*([^<]+?)\s*<'
)


def _validate_url(url: str) -> None:
    """Reject URLs that leave the HTTPS ICODrops origin.

    This is called for the initial request and by the redirect handler before
    urllib opens each next hop.  Credentials and non-default ports are
    rejected as well, even when the parsed hostname happens to be allowed.
    """

    if not isinstance(url, str) or not url:
        raise ValueError("ICODrops URL must be a non-empty string")
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise ValueError(f"invalid ICODrops URL: {url!r}") from exc
    if parsed.scheme.casefold() != "https":
        raise ValueError("ICODrops URL must use HTTPS")
    if hostname is None or hostname.casefold() not in _ALLOWED_HOSTS:
        raise ValueError("ICODrops URL host is not allowed")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("ICODrops URL credentials are not allowed")
    if port not in (None, 443):
        raise ValueError("ICODrops URL must use the default HTTPS port")


class _ICODropsRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Allow redirects only when their next hop remains on ICODrops."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urllib.request.Request:
        # HTTPRedirectHandler resolves relative locations before calling this
        # method.  Validate that resolved URL before delegating, so the
        # off-host endpoint is never opened.
        _validate_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = urllib.request.build_opener(_ICODropsRedirectHandler())


def _open(request: urllib.request.Request, *, timeout: float) -> Any:
    """Open a request through the ICODrops-only redirect policy."""

    return _OPENER.open(request, timeout=timeout)


def _response_content_length(response: Any) -> int | None:
    headers = getattr(response, "headers", None)
    if headers is None:
        return None
    try:
        raw_length = headers.get("Content-Length")
    except AttributeError:
        return None
    if raw_length is None:
        return None
    try:
        length = int(raw_length)
    except (TypeError, ValueError):
        return None
    if length < 0:
        raise ValueError("ICODrops response has an invalid Content-Length")
    return length


def _read_limited(response: Any) -> bytes:
    """Read at most ``MAX_RESPONSE_BYTES`` from a response body."""

    content_length = _response_content_length(response)
    if content_length is not None and content_length > MAX_RESPONSE_BYTES:
        raise ValueError(f"ICODrops response exceeds {MAX_RESPONSE_BYTES} byte limit")

    chunks: list[bytes] = []
    total = 0
    while True:
        # Read one byte beyond the remaining budget so an absent or dishonest
        # Content-Length cannot silently pass the cap.
        read_size = min(_READ_CHUNK_BYTES, MAX_RESPONSE_BYTES - total + 1)
        chunk = response.read(read_size)
        if not chunk:
            break
        if not isinstance(chunk, bytes):
            raise TypeError("ICODrops response.read() must return bytes")
        total += len(chunk)
        if total > MAX_RESPONSE_BYTES:
            raise ValueError(
                f"ICODrops response exceeds {MAX_RESPONSE_BYTES} byte limit"
            )
        chunks.append(chunk)
        if total == MAX_RESPONSE_BYTES:
            # A body exactly at the limit is valid; probe once for an extra
            # byte before returning it.
            extra = response.read(1)
            if not isinstance(extra, bytes):
                raise TypeError("ICODrops response.read() must return bytes")
            if extra:
                raise ValueError(
                    f"ICODrops response exceeds {MAX_RESPONSE_BYTES} byte limit"
                )
            break
    return b"".join(chunks)


def count_rows(html: str) -> int:
    """How many listing rows the page contains, parsed or not.

    Rows-found-but-none-parsed is the signature of changed markup, and it must
    be distinguishable from a genuinely empty listing.
    """

    return len(re.findall(_ROW_RE, html, flags=re.S))


def clean_html(text: str) -> str:
    """Strip markup and collapse whitespace, preserving readable entities."""

    text = re.sub(r"<script[^>]*>[\s\S]*?</script>", " ", text)
    text = re.sub(r"<style[^>]*>[\s\S]*?</style>", " ", text)
    # DOTALL matters: ICODrops' tooltip tags span several lines, and `.` does
    # not cross newlines by default.  Without it the tag survives stripping and
    # a pre-valuation reads "$240 M <div class=..." instead of "$240 M", so the
    # highest-weighted asymmetry input silently never parses.
    text = re.sub(r"<.*?>", " ", text, flags=re.S)
    return " ".join(unescape(text).split()).strip()


def fetch(
    url: str,
    *,
    timeout_seconds: float = 30,
    user_agent: str = USER_AGENT,
) -> str:
    """Fetch one page with the adapter's descriptive request headers."""

    _validate_url(url)
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": user_agent,
            "Accept": "text/html,application/xhtml+xml",
        },
    )
    with _open(request, timeout=timeout_seconds) as response:
        # The redirect handler protects every intermediate hop.  Check the
        # final response too because custom handlers/proxies can report a URL
        # that differs from the last redirect location.
        geturl = getattr(response, "geturl", None)
        if callable(geturl):
            final_url = geturl()
            if final_url:
                _validate_url(final_url)
        return _read_limited(response).decode("utf-8", errors="ignore")


def parse_projects_from_upcoming(html: str) -> Iterable[dict[str, Any]]:
    """Yield the project rows used by the legacy ICODrops script."""

    for row in re.findall(_ROW_RE, html, flags=re.S):
        match = _PROJECT_RE.search(row)
        if not match:
            continue

        slug = match.group(1)
        name = clean_html(match.group(2))
        # The ticker element is absent for some projects, so the group is
        # optional and may be None.
        ticker = clean_html(match.group(3) or "")
        fields: dict[str, str] = {}
        for cls, content in re.findall(
            r'<div class="Tbl-Row__item Tbl-Row__item--([\w-]+)"[^>]*>(.*?)</div>',
            row,
            re.S,
        ):
            fields[cls] = clean_html(content)
        yield {
            "slug": slug,
            "name": name,
            "ticker": ticker,
            "url": BASE_URL + slug,
            "round": fields.get("round", ""),
            "raised": fields.get("raised", ""),
            "pre_valuation": fields.get("pre-valuation", ""),
            "investors_row": fields.get("investors", ""),
            "ecosystem": fields.get("ecosystem", ""),
        }


def parse_project_investors(project_html: str) -> list[str]:
    """Extract and de-duplicate investor labels from a project page."""

    # Only the investor-name element counts.  The inherited `data-tooltip-text`
    # fallback scraped every tooltip on the page and returned prose like "The
    # value of a company before any new outside investment…" as an investor —
    # which then matched no fund keyword and scored the project as having NO
    # BACKERS.  A fabricated fact on the highest-weighted risk input, produced
    # by a parser failure.  Better to find nothing and say so.
    cleaned: list[str] = []
    for raw in _INVESTOR_NAME_RE.findall(project_html):
        value = clean_html(raw)
        if not value or value.casefold() in NOISY_TOKENS:
            continue
        if value not in cleaned:
            cleaned.append(value)
    return cleaned


def matches_big_backers(investors: Iterable[str]) -> list[str]:
    matches: set[str] = set()
    # One investor label is one backer. Prefer the most specific alias so
    # "Coinbase Ventures" does not count as both "coinbase ventures" and
    # "coinbase", and "Binance Labs" does not become two confirmations.
    ordered_keywords = sorted(BIG_BACKER_KEYWORDS, key=len, reverse=True)
    for investor in investors:
        normalized = str(investor).casefold()
        for keyword in ordered_keywords:
            if contains_keyword_token(normalized, keyword):
                matches.add(keyword)
                break
    return sorted(matches)


def _unknown_detail_fields(fields: Mapping[str, Any]) -> dict[str, Any]:
    updated = dict(fields)
    updated["investors"] = UNKNOWN
    updated["backer_matches"] = UNKNOWN
    updated["backer_score"] = UNKNOWN
    return updated


class ICODropsSource:
    """ICODrops upcoming calendar and per-project detail adapter."""

    name = "icodrops"

    def __init__(self, config: Mapping[str, Any] | None = None) -> None:
        self.config = dict(config) if config is not None else load_config()

    def fetch(self) -> SourceResult:
        fetched_at = _hermes_now().isoformat()
        fetch_config = self.config.get("fetch", {})
        timeout = float(fetch_config.get("timeout_seconds", 30))
        delay = float(fetch_config.get("delay_seconds", 1.0))
        max_details = int(fetch_config.get("max_details_per_run", 40))

        try:
            index_html = fetch(
                UPCOMING_URL,
                timeout_seconds=timeout,
                user_agent=USER_AGENT,
            )
        except (
            urllib.error.HTTPError,
            urllib.error.URLError,
            TimeoutError,
            OSError,
        ) as exc:
            return SourceResult(
                source=self.name,
                status="http_error",
                error=f"{type(exc).__name__}: {exc}",
                fetched_at=fetched_at,
            )
        except Exception as exc:
            return SourceResult(
                source=self.name,
                status="http_error",
                error=f"{type(exc).__name__}: {exc}",
                fetched_at=fetched_at,
            )

        try:
            parsed_projects = list(parse_projects_from_upcoming(index_html))
        except Exception as exc:
            return SourceResult(
                source=self.name,
                status="parse_error",
                error=f"{type(exc).__name__}: {exc}",
                fetched_at=fetched_at,
            )

        candidates: list[RawCandidate] = []
        for project in parsed_projects:
            slug = str(project.get("slug", "")).strip()
            name = str(project.get("name", "")).strip()
            url = str(project.get("url", "")).strip()
            if not slug or not name or not url:
                continue
            fields = {
                "round": project.get("round", ""),
                "raised": project.get("raised", ""),
                "pre_valuation": project.get("pre_valuation", ""),
                "investors": UNKNOWN,
                "investors_row": project.get("investors_row", ""),
                "ecosystem": project.get("ecosystem", ""),
            }
            candidates.append(
                RawCandidate(
                    source=self.name,
                    slug=slug,
                    name=name,
                    ticker=str(project.get("ticker") or "").strip() or None,
                    url=url,
                    fields=fields,
                )
            )

        if not candidates:
            # "The listing is empty" and "we could not read the listing" are
            # opposite facts.  Rows present but none parsed means their markup
            # moved and we are blind — reporting that as `empty` is how a dead
            # scraper reports good health forever.  This is not hypothetical:
            # the inherited parser hit exactly this against the live site.
            row_count = count_rows(index_html)
            if row_count:
                return SourceResult(
                    source=self.name,
                    status="parse_error",
                    error=(
                        f"{row_count} listing rows found but none parsed — "
                        "ICODrops markup has likely changed"
                    ),
                    candidates=[],
                    fetched_at=fetched_at,
                    candidate_count=0,
                )
            return SourceResult(
                source=self.name,
                status="empty",
                candidates=[],
                fetched_at=fetched_at,
                candidate_count=0,
            )

        detail_failure_count = 0
        detail_skipped_count = max(0, len(candidates) - max_details)
        detail_failures: list[dict[str, str]] = []
        detail_fetch_count = 0
        for index, candidate in enumerate(candidates):
            if index >= max_details:
                candidate.fields = _unknown_detail_fields(candidate.fields)
                continue
            if delay > 0:
                time.sleep(delay)
            detail_fetch_count += 1
            try:
                detail_html = fetch(
                    candidate.url,
                    timeout_seconds=timeout,
                    user_agent=USER_AGENT,
                )
                investors = parse_project_investors(detail_html)
                if investors:
                    matches = matches_big_backers(investors)
                    candidate.fields["investors"] = investors
                    candidate.fields["backer_matches"] = matches
                    candidate.fields["backer_score"] = len(matches)
                else:
                    # We fetched the page and recognized no investor element.
                    # That is "we do not know", not "this project has no
                    # backers" — an empty list would score backer_overlap=0,
                    # inventing the most damning reading of a parse failure on
                    # the heaviest risk input.  UNKNOWN drags coverage instead.
                    candidate.fields["investors"] = UNKNOWN
                    candidate.fields["backer_overlap"] = UNKNOWN
            except Exception as exc:
                detail_failure_count += 1
                reason = f"{type(exc).__name__}: {exc}"
                detail_failures.append({"slug": candidate.slug, "reason": reason})
                candidate.fields = _unknown_detail_fields(candidate.fields)

        status = "partial" if detail_failure_count or detail_skipped_count else "ok"
        return SourceResult(
            source=self.name,
            status=status,
            candidates=candidates,
            error=(
                f"{detail_failure_count} detail fetch(es) failed"
                if detail_failure_count
                else None
            ),
            fetched_at=fetched_at,
            candidate_count=len(candidates),
            detail_fetch_count=detail_fetch_count,
            detail_failure_count=detail_failure_count,
            detail_skipped_count=detail_skipped_count,
            detail_failures=detail_failures,
        )


register_source(ICODropsSource)

# Descriptive alias for callers that use the adapter terminology from the
# source registry contract.
ICODropsAdapter = ICODropsSource


__all__ = [
    "UPCOMING_URL",
    "BASE_URL",
    "USER_AGENT",
    "MAX_RESPONSE_BYTES",
    "BIG_BACKER_KEYWORDS",
    "clean_html",
    "fetch",
    "parse_projects_from_upcoming",
    "parse_project_investors",
    "matches_big_backers",
    "ICODropsSource",
    "ICODropsAdapter",
]
