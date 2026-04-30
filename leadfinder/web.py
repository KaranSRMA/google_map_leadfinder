from __future__ import annotations

import re
import socket
import ssl
from html.parser import HTMLParser
from typing import Optional
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, quote_plus, urljoin, urlsplit
from urllib.request import Request, build_opener

from .models import (
    ContactCandidate,
    DIRECTORY_DOMAINS,
    MESSAGEABLE_SOCIAL_DOMAINS,
    PageSnapshot,
    SOCIAL_DOMAINS,
    WebsiteCandidate,
    WebsiteDecision,
)


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/135.0.0.0 Safari/537.36"
)
SEARCH_ENGINE_HOSTS = {
    "bing.com",
    "www.bing.com",
    "duckduckgo.com",
    "www.duckduckgo.com",
    "html.duckduckgo.com",
    "google.com",
    "www.google.com",
}
BUSINESS_NAME_STOPWORDS = {
    "and",
    "the",
    "for",
    "with",
    "private",
    "limited",
    "pvt",
    "ltd",
    "llp",
    "inc",
    "co",
    "company",
}


class SnapshotParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[str] = []
        self.text_parts: list[str] = []
        self.title_parts: list[str] = []
        self._skip_depth = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        attrs_map = dict(attrs)
        lowered_tag = tag.lower()
        if lowered_tag in {"script", "style", "noscript"}:
            self._skip_depth += 1
            return

        if lowered_tag == "title":
            self._in_title = True
            return

        if lowered_tag == "a":
            href = attrs_map.get("href")
            if href:
                self.links.append(href)

        if lowered_tag == "meta":
            content = attrs_map.get("content")
            name = (attrs_map.get("name") or attrs_map.get("property") or "").lower()
            if content and name in {"description", "og:description", "og:title", "twitter:description"}:
                self.text_parts.append(content)

    def handle_endtag(self, tag: str) -> None:
        lowered_tag = tag.lower()
        if lowered_tag in {"script", "style", "noscript"} and self._skip_depth:
            self._skip_depth -= 1
        if lowered_tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._in_title:
            self.title_parts.append(data)
        self.text_parts.append(data)


def normalize_hostname(url: str) -> str:
    hostname = urlsplit(url).netloc.lower()
    return hostname[4:] if hostname.startswith("www.") else hostname


def matches_domain(hostname: str, domain: str) -> bool:
    return hostname == domain or hostname.endswith(f".{domain}")


def is_social_website(url: str) -> bool:
    hostname = normalize_hostname(url)
    return any(matches_domain(hostname, domain) for domain in SOCIAL_DOMAINS)


def is_messageable_social(url: str) -> bool:
    hostname = normalize_hostname(url)
    return any(matches_domain(hostname, domain) for domain in MESSAGEABLE_SOCIAL_DOMAINS)


def is_directory_website(url: str) -> bool:
    hostname = normalize_hostname(url)
    return any(matches_domain(hostname, domain) for domain in DIRECTORY_DOMAINS)


def classify_http_status(status_code: Optional[int]) -> bool:
    if status_code is None:
        return False
    return 200 <= status_code < 400 or status_code in {401, 403, 405, 429}


def probe_website(url: str, timeout_seconds: float = 12.0) -> tuple[str, Optional[int], str]:
    request = Request(url, headers={"User-Agent": USER_AGENT})
    opener = build_opener()

    try:
        with opener.open(request, timeout=timeout_seconds) as response:
            status_code = getattr(response, "status", None) or response.getcode()
            final_url = response.geturl() or url
            response.read(256)
            return final_url, int(status_code), f"HTTP {status_code}"
    except HTTPError as exc:
        final_url = exc.geturl() or url
        return final_url, exc.code, f"HTTP {exc.code}"
    except ssl.SSLCertVerificationError:
        return url, None, "SSL certificate error"
    except ssl.SSLError:
        return url, None, "SSL error"
    except socket.timeout:
        return url, None, "Timed out"
    except URLError as exc:
        reason = exc.reason
        if isinstance(reason, socket.timeout):
            return url, None, "Timed out"
        return url, None, str(reason)
    except ValueError:
        return url, None, "Invalid website URL"


def evaluate_website(website: str) -> WebsiteDecision:
    if not website:
        return WebsiteDecision(
            keep=True,
            priority="High",
            priority_reason="No working website found",
            website_status="Missing website",
            website_type="none",
        )

    if is_social_website(website):
        return WebsiteDecision(
            keep=True,
            priority="Low",
            priority_reason="Only social profiles are available",
            website_status="Social profile link",
            website_type="social",
            website=website,
        )

    final_url, status_code, status_text = probe_website(website)

    if final_url and is_social_website(final_url):
        return WebsiteDecision(
            keep=True,
            priority="Low",
            priority_reason="Only social profiles are available",
            website_status=status_text if status_code is not None else "Redirected to social profile",
            website_type="social",
            website_http_status=str(status_code or ""),
            website=final_url,
        )

    if classify_http_status(status_code):
        return WebsiteDecision(
            keep=False,
            priority="",
            priority_reason="Working website detected",
            website_status=status_text,
            website_type="domain",
            website_http_status=str(status_code or ""),
            website=final_url or website,
        )

    return WebsiteDecision(
        keep=True,
        priority="Medium",
        priority_reason="Website looks broken, expired, or unreachable",
        website_status=status_text,
        website_type="domain",
        website_http_status=str(status_code or ""),
        website=final_url or website,
    )


def normalize_outbound_url(url: str) -> str:
    if not url:
        return ""
    parsed = urlsplit(url)
    if "google." in parsed.netloc and parsed.path == "/url":
        query = dict(parse_qsl(parsed.query))
        return query.get("q") or query.get("url") or ""
    if parsed.scheme in {"http", "https"}:
        return url
    return ""


def normalize_link(base_url: str, href: str) -> str:
    href = (href or "").strip()
    if not href or href.startswith(("javascript:", "#")):
        return ""
    if href.startswith(("tel:", "mailto:")):
        return href
    if "whatsapp" in href or "wa.me/" in href or "wa.link/" in href:
        return href
    return normalize_outbound_url(urljoin(base_url, href))


def fetch_page_snapshot(url: str, timeout_seconds: float = 12.0) -> Optional[PageSnapshot]:
    request = Request(url, headers={"User-Agent": USER_AGENT})
    opener = build_opener()
    response = None

    try:
        response = opener.open(request, timeout=timeout_seconds)
    except HTTPError as exc:
        response = exc
    except (URLError, ssl.SSLError, socket.timeout, ValueError):
        return None

    try:
        final_url = response.geturl() or url
        raw_bytes = response.read()
        content = raw_bytes.decode("utf-8", errors="ignore")
    finally:
        response.close()

    parser = SnapshotParser()
    parser.feed(content)
    links = [
        normalized
        for href in parser.links
        if (normalized := normalize_link(final_url, href))
    ]
    text = re.sub(r"\s+", " ", " ".join(parser.text_parts)).strip()
    title = re.sub(r"\s+", " ", " ".join(parser.title_parts)).strip()
    return PageSnapshot(
        url=url,
        final_url=final_url,
        title=title,
        text=text,
        html=content,
        links=dedupe_urls(links),
    )


def tokenize_business_name(value: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9]+", value.lower())
        if len(token) >= 3 and token not in BUSINESS_NAME_STOPWORDS
    }


def score_business_domain(url: str, business_name: str) -> int:
    business_tokens = tokenize_business_name(business_name)
    if not business_tokens:
        return 0

    host = normalize_hostname(url)
    domain_tokens = {
        token
        for token in re.findall(r"[a-z0-9]+", host)
        if len(token) >= 3 and token not in {"www", "com", "co", "in", "org", "net"}
    }
    overlap = len(business_tokens & domain_tokens)
    return overlap * 5


def extract_external_websites(
    snapshot: PageSnapshot,
    business_name: str,
    source_label: str,
    rank_base: int,
) -> list[WebsiteCandidate]:
    source_host = normalize_hostname(snapshot.final_url)
    eligible_links: list[str] = []
    unique_hosts: set[str] = set()

    for link in snapshot.links:
        if not link.startswith(("http://", "https://")):
            continue
        link_host = normalize_hostname(link)
        if (
            link_host == source_host
            or is_social_website(link)
            or is_directory_website(link)
            or "google." in link_host
            or link_host in {"wa.me", "wa.link", "api.whatsapp.com"}
        ):
            continue
        eligible_links.append(link)
        unique_hosts.add(link_host)

    candidates: list[WebsiteCandidate] = []

    for link in eligible_links:
        score = score_business_domain(link, business_name)
        if score <= 0 and len(unique_hosts) > 1:
            continue
        rank = rank_base + score
        candidates.append(WebsiteCandidate(url=link, source=source_label, rank=rank))

    return candidates


def extract_contacts(snapshot: PageSnapshot, source_label: str, rank_base: int) -> list[ContactCandidate]:
    candidates: list[ContactCandidate] = []
    text_blob = snapshot.text
    html_blob = snapshot.html

    for link in snapshot.links:
        lowered = link.lower()
        if lowered.startswith("tel:"):
            number = clean_phone(link[4:])
            if number:
                candidates.append(ContactCandidate(value=number, kind="phone", source=source_label, rank=rank_base))
        elif lowered.startswith("mailto:"):
            email = link[7:].split("?", 1)[0].strip()
            if email:
                candidates.append(ContactCandidate(value=email, kind="email", source=source_label, rank=rank_base - 2))
        elif "whatsapp" in lowered or "wa.me/" in lowered or "wa.link/" in lowered:
            candidates.append(ContactCandidate(value=link, kind="whatsapp", source=source_label, rank=rank_base - 1))

    for match in re.findall(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", text_blob, flags=re.IGNORECASE):
        candidates.append(ContactCandidate(value=match, kind="email", source=source_label, rank=rank_base - 3))

    for match in re.findall(r"https?://(?:wa\.me|wa\.link|api\.whatsapp\.com)[^\s\"'<>]+", html_blob, flags=re.IGNORECASE):
        candidates.append(ContactCandidate(value=match, kind="whatsapp", source=source_label, rank=rank_base - 1))

    for match in re.findall(r"(?<!\d)(?:\+?\d[\d\s().-]{7,}\d)(?!\d)", text_blob):
        number = clean_phone(match)
        if number:
            candidates.append(ContactCandidate(value=number, kind="phone", source=source_label, rank=rank_base - 4))

    return candidates


def clean_phone(value: str) -> str:
    cleaned = re.sub(r"\s+", " ", value).strip()
    digits = re.sub(r"\D", "", cleaned)
    if len(digits) < 8:
        return ""
    if cleaned.startswith("+"):
        return f"+{digits}"
    return cleaned


def dedupe_urls(urls: list[str]) -> list[str]:
    deduped: list[str] = []
    seen: set[str] = set()
    for url in urls:
        normalized = url.rstrip("/")
        if normalized and normalized not in seen:
            seen.add(normalized)
            deduped.append(url)
    return deduped


def contact_key(candidate: ContactCandidate) -> str:
    if candidate.kind == "phone":
        digits = re.sub(r"\D", "", candidate.value)
        return f"{candidate.kind}:{digits}"
    return f"{candidate.kind}:{candidate.value.strip().lower()}"


def dedupe_contacts(candidates: list[ContactCandidate]) -> list[ContactCandidate]:
    best: dict[str, ContactCandidate] = {}
    for candidate in candidates:
        key = contact_key(candidate)
        current = best.get(key)
        if current is None or candidate.rank > current.rank:
            best[key] = candidate
    return sorted(best.values(), key=lambda item: (-item.rank, item.kind, item.value.lower()))


def search_web(query: str, limit: int = 8) -> list[str]:
    search_url = f"https://www.bing.com/search?setlang=en-US&q={quote_plus(query)}"
    snapshot = fetch_page_snapshot(search_url, timeout_seconds=15.0)
    if snapshot is None:
        return []

    results: list[str] = []
    for link in snapshot.links:
        if not link.startswith(("http://", "https://")):
            continue
        hostname = normalize_hostname(link)
        if hostname in SEARCH_ENGINE_HOSTS or "google." in hostname:
            continue
        if "/search?" in link or "/images/search?" in link:
            continue
        if link not in results:
            results.append(link)
        if len(results) >= limit:
            break
    return results
