"""
Polite seed-site crawler.

Unlike ``utils.crawler_spider.crawling`` (which finds links through Google search
for a keyword), this crawler starts from fixed seed URLs and walks the site
itself, breadth first, staying on the site's own domains. It:

* obeys robots.txt (including Crawl-delay) and waits between requests
* only follows http(s) HTML pages, up to ``max_pages`` / ``max_depth``
* classifies each page into a category from its URL (entries, results, odds, news)
* extracts title, meta tags, headline, a text snippet and any HTML tables
  (race cards, result charts and odds boards are usually tables)

It has no Django dependency, so it can be used and tested on its own.
"""

import re
import time
from collections import deque
from urllib import robotparser
from urllib.parse import urljoin, urlparse, urldefrag

import requests
from bs4 import BeautifulSoup

from utils.racing_sites import CATEGORY_RULES

USER_AGENT = "FalconCrawler/1.0 (+https://github.com/robysrh-gif/falcon---ai-data-crawler)"

SKIP_EXTENSIONS = (
    ".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".ico", ".css", ".js",
    ".pdf", ".zip", ".mp4", ".mp3", ".mov", ".avi", ".xml", ".json", ".woff", ".woff2",
)

MAX_TABLES = 5
MAX_TABLE_ROWS = 50
SNIPPET_CHARS = 1000


def normalize_url(url):
    url, _ = urldefrag(url.strip())
    return url


def classify_url(url, site_rules=None, default="racing_general"):
    """Return the category for ``url`` using site rules first, then the generic rules."""
    parsed = urlparse(url)
    # Path and query only: host names such as "offtrackbetting.com" would match the rules.
    target = (parsed.path + ("?" + parsed.query if parsed.query else "")).lower()
    for pattern, category in list(site_rules or []) + CATEGORY_RULES:
        if re.search(pattern, target):
            return category
    return default


def extract_meta(soup):
    """Same flattening of <meta> tags as ``utils.crawler_spider.scraper``."""
    metadata = dict()
    for tag in soup.find_all("meta"):
        key = tag.attrs.get("name") or tag.attrs.get("property")
        content = tag.attrs.get("content")
        if key and content is not None:
            if ":" in key:
                key = key.split(":", 1)[1]
            metadata[key] = content
    return metadata


def extract_tables(soup):
    tables = list()
    for table in soup.find_all("table")[:MAX_TABLES]:
        rows = list()
        for tr in table.find_all("tr")[:MAX_TABLE_ROWS]:
            cells = [c.get_text(" ", strip=True) for c in tr.find_all(["th", "td"])]
            if any(cells):
                rows.append(cells)
        if rows:
            caption = table.find("caption")
            tables.append({
                "caption": caption.get_text(" ", strip=True) if caption else "",
                "rows": rows,
            })
    return tables


def extract_page(url, html, category):
    soup = BeautifulSoup(html, "html.parser")
    title = soup.title.get_text(strip=True) if soup.title else ""
    h1 = soup.find("h1")

    links = list()
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith(("mailto:", "tel:", "javascript:")):
            continue
        links.append(normalize_url(urljoin(url, href)))

    tables = extract_tables(soup)
    for tag in soup(["script", "style", "noscript", "nav", "footer", "header"]):
        tag.decompose()
    text = " ".join(soup.get_text(" ", strip=True).split())

    data = {
        "url": url,
        "category": category,
        "title": title,
        "headline": h1.get_text(" ", strip=True) if h1 else "",
        "meta": extract_meta(BeautifulSoup(html, "html.parser")),
        "snippet": text[:SNIPPET_CHARS],
        "tables": tables,
    }
    return data, links


class SiteCrawler:

    def __init__(self, site, max_pages=50, max_depth=2, delay=2.0, session=None,
                 sleep=time.sleep, timeout=20, log=print):
        self.site = site
        self.max_pages = max_pages
        self.max_depth = max_depth
        self.delay = delay
        self.timeout = timeout
        self.sleep = sleep
        self.log = log
        self.session = session or requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
        self.allowed_domains = {d.lower() for d in site["allowed_domains"]}
        self._robots = dict()
        self._last_request = None

    # -- helpers -----------------------------------------------------------

    def is_allowed_domain(self, url):
        host = (urlparse(url).hostname or "").lower()
        return host in self.allowed_domains

    def should_visit(self, url):
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return False
        if parsed.path.lower().endswith(SKIP_EXTENSIONS):
            return False
        return self.is_allowed_domain(url)

    def _wait(self, extra_delay=0):
        delay = max(self.delay, extra_delay)
        if self._last_request is not None and delay > 0:
            elapsed = time.monotonic() - self._last_request
            if elapsed < delay:
                self.sleep(delay - elapsed)
        self._last_request = time.monotonic()

    def _get(self, url, crawl_delay=0):
        self._wait(crawl_delay)
        return self.session.get(url, timeout=self.timeout)

    def robots_for(self, url):
        """Fetch and cache robots.txt for the url's origin.

        Returns a RobotFileParser, or ``None`` when robots.txt could not be
        fetched (network error / 5xx): the host is then skipped, to stay on the
        safe side.
        """
        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        if origin in self._robots:
            return self._robots[origin]

        parser = robotparser.RobotFileParser()
        parser.set_url(origin + "/robots.txt")
        try:
            response = self._get(origin + "/robots.txt")
        except requests.RequestException as exc:
            self.log(f"[{self.site['name']}] robots.txt unreachable for {origin}: {exc}")
            parser = None
        else:
            if response.status_code in (401, 403):
                parser.disallow_all = True
            elif 400 <= response.status_code < 500:
                parser.allow_all = True
            elif response.status_code >= 500:
                self.log(f"[{self.site['name']}] robots.txt returned {response.status_code} for {origin}")
                parser = None
            else:
                parser.parse(response.text.splitlines())
        self._robots[origin] = parser
        return parser

    def can_fetch(self, url):
        parser = self.robots_for(url)
        return parser is not None and parser.can_fetch(USER_AGENT, url)

    def crawl_delay(self, url):
        parser = self.robots_for(url)
        if parser is None:
            return 0
        try:
            return parser.crawl_delay(USER_AGENT) or 0
        except AttributeError:
            return 0

    # -- main loop -----------------------------------------------------------

    def crawl(self):
        """Crawl the site. Returns ``(pages, errors)``.

        ``pages`` is a list of dicts as built by ``extract_page``; ``errors`` is a
        list of ``(url, reason)`` tuples for pages that were skipped or failed.
        """
        pages = list()
        errors = list()
        seen = set()
        queue = deque()

        for url in self.site["start_urls"]:
            url = normalize_url(url)
            if url not in seen:
                seen.add(url)
                queue.append((url, 0))

        while queue and len(pages) < self.max_pages:
            url, depth = queue.popleft()

            if self.robots_for(url) is None:
                errors.append((url, "robots.txt unavailable (site unreachable?)"))
                continue
            if not self.can_fetch(url):
                errors.append((url, "blocked by robots.txt"))
                continue

            try:
                response = self._get(url, self.crawl_delay(url))
            except requests.RequestException as exc:
                errors.append((url, f"request failed: {exc}"))
                continue

            if response.status_code != 200:
                errors.append((url, f"HTTP {response.status_code}"))
                continue
            content_type = response.headers.get("Content-Type", "text/html")
            if "html" not in content_type:
                errors.append((url, f"not HTML ({content_type})"))
                continue

            final_url = normalize_url(getattr(response, "url", None) or url)
            if final_url != url:
                if not self.is_allowed_domain(final_url):
                    errors.append((url, f"redirected off-site to {final_url}"))
                    continue
                seen.add(final_url)

            category = classify_url(final_url, self.site.get("category_rules"),
                                    self.site.get("default_category", "racing_general"))
            data, links = extract_page(final_url, response.text, category)
            pages.append(data)
            self.log(f"[{self.site['name']}] {category:<15} {final_url}")

            if depth < self.max_depth:
                for link in links:
                    if link not in seen and self.should_visit(link):
                        seen.add(link)
                        queue.append((link, depth + 1))

        return pages, errors
