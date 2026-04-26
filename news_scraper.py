#!/usr/bin/env python3
"""
News & Social Media Scraper
Fetches newsworthy articles and trending social media posts from multiple sources.

Sources covered:
  - RSS/News feeds : BBC, Reuters, The Guardian, NPR, CNN, AP, TechCrunch, Ars Technica
  - Hacker News    : Top stories via the public Firebase API
  - Reddit         : Hot posts from r/news, r/worldnews, r/technology, r/science, r/politics
  - GitHub Trending: Trending repositories (scraped from GitHub's trending page)

Usage:
    python news_scraper.py
    python news_scraper.py --keywords "AI climate"
    python news_scraper.py --sources rss reddit hackernews --limit 15
    python news_scraper.py --output results.json
    python news_scraper.py --list-sources
"""

import argparse
import json
import sys
import time
import urllib.request
import urllib.error
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime
from html import unescape
import re
import ssl


# ──────────────────────────────────────────────────────────────────────────────
# Config
# ──────────────────────────────────────────────────────────────────────────────

RSS_FEEDS = {
    "bbc":        ("BBC News",        "http://feeds.bbci.co.uk/news/rss.xml"),
    "reuters":    ("Reuters",         "https://feeds.reuters.com/reuters/topNews"),
    "guardian":   ("The Guardian",    "https://www.theguardian.com/world/rss"),
    "npr":        ("NPR News",        "https://feeds.npr.org/1001/rss.xml"),
    "cnn":        ("CNN",             "http://rss.cnn.com/rss/edition.rss"),
    "ap":         ("AP News",         "https://rsshub.app/apnews/topics/apf-topnews"),
    "techcrunch": ("TechCrunch",      "https://techcrunch.com/feed/"),
    "ars":        ("Ars Technica",    "http://feeds.arstechnica.com/arstechnica/index"),
}

REDDIT_SUBREDDITS = [
    "news",
    "worldnews",
    "technology",
    "science",
    "politics",
    "business",
    "environment",
]

HN_TOP_N = 30  # how many HN top stories to fetch


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _ssl_ctx() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _get(url: str, timeout: int = 12, extra_headers: dict | None = None) -> bytes | None:
    headers = {"User-Agent": "NewsSocialScraper/1.0 (educational; contact@example.com)"}
    if extra_headers:
        headers.update(extra_headers)
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_ssl_ctx()) as resp:
            return resp.read()
    except Exception as exc:
        print(f"    [WARN] fetch failed for {url}: {exc}", file=sys.stderr)
        return None


def _get_json(url: str, timeout: int = 12, extra_headers: dict | None = None) -> dict | list | None:
    raw = _get(url, timeout=timeout, extra_headers=extra_headers)
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"    [WARN] JSON parse error for {url}: {exc}", file=sys.stderr)
        return None


def strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", "", unescape(text or "")).strip()


def truncate(text: str, n: int = 200) -> str:
    return text if len(text) <= n else text[: n - 3] + "..."


def matches_keywords(article: dict, keywords: list[str]) -> bool:
    if not keywords:
        return True
    haystack = (
        (article.get("title") or "") + " " + (article.get("description") or "")
    ).lower()
    return all(kw.lower() in haystack for kw in keywords)


# ──────────────────────────────────────────────────────────────────────────────
# Source scrapers
# ──────────────────────────────────────────────────────────────────────────────

def scrape_rss(feed_keys: list[str] | None = None) -> list[dict]:
    """Fetch articles from RSS/Atom feeds."""
    selected = {k: v for k, v in RSS_FEEDS.items() if feed_keys is None or k in feed_keys}
    articles: list[dict] = []

    for key, (name, url) in selected.items():
        print(f"  [RSS] {name}", file=sys.stderr)
        raw = _get(url)
        if raw is None:
            continue
        try:
            root = ET.fromstring(raw)
        except ET.ParseError:
            continue

        # RSS 2.0
        for item in root.iter("item"):
            title = strip_html(item.findtext("title", ""))
            link = (item.findtext("link") or "").strip()
            description = strip_html(item.findtext("description", ""))
            pub_date = item.findtext("pubDate", "")
            if title:
                articles.append({
                    "type": "news",
                    "source": name,
                    "title": title,
                    "link": link,
                    "description": description,
                    "published": pub_date,
                    "meta": {},
                })

        # Atom
        for entry in root.iter("{http://www.w3.org/2005/Atom}entry"):
            title = strip_html(entry.findtext("{http://www.w3.org/2005/Atom}title", ""))
            link_el = entry.find("{http://www.w3.org/2005/Atom}link")
            link = (link_el.get("href", "") if link_el is not None else "").strip()
            summary = strip_html(entry.findtext("{http://www.w3.org/2005/Atom}summary", ""))
            published = entry.findtext("{http://www.w3.org/2005/Atom}published", "")
            if title:
                articles.append({
                    "type": "news",
                    "source": name,
                    "title": title,
                    "link": link,
                    "description": summary,
                    "published": published,
                    "meta": {},
                })

    return articles


def scrape_reddit(subreddits: list[str] | None = None, per_sub: int = 10) -> list[dict]:
    """
    Fetch hot posts from Reddit subreddits using the public JSON endpoint.
    No API key required for public subreddits.
    """
    subs = subreddits or REDDIT_SUBREDDITS
    articles: list[dict] = []

    for sub in subs:
        url = f"https://www.reddit.com/r/{sub}/hot.json?limit={per_sub}"
        print(f"  [Reddit] r/{sub}", file=sys.stderr)
        data = _get_json(url, extra_headers={"User-Agent": "NewsScraper/1.0"})
        if data is None:
            continue
        try:
            posts = data["data"]["children"]
        except (KeyError, TypeError):
            continue

        for post in posts:
            p = post.get("data", {})
            if p.get("is_self") and not p.get("selftext"):
                continue  # skip empty self-posts
            title = p.get("title", "")
            link = p.get("url", "")
            description = strip_html(p.get("selftext", "") or p.get("title", ""))
            score = p.get("score", 0)
            num_comments = p.get("num_comments", 0)
            permalink = "https://www.reddit.com" + p.get("permalink", "")
            flair = p.get("link_flair_text") or ""
            if title:
                articles.append({
                    "type": "social",
                    "source": f"Reddit r/{sub}",
                    "title": title,
                    "link": link,
                    "description": truncate(description),
                    "published": datetime.utcfromtimestamp(
                        p.get("created_utc", 0)
                    ).strftime("%a, %d %b %Y %H:%M UTC"),
                    "meta": {
                        "score": score,
                        "comments": num_comments,
                        "reddit_url": permalink,
                        "flair": flair,
                    },
                })

        time.sleep(0.5)  # be polite to Reddit

    return articles


def scrape_hackernews(top_n: int = HN_TOP_N) -> list[dict]:
    """
    Fetch top stories from Hacker News via the official public Firebase API.
    Docs: https://github.com/HackerNews/API
    """
    print(f"  [HackerNews] Top {top_n} stories", file=sys.stderr)
    ids = _get_json("https://hacker-news.firebaseio.com/v0/topstories.json")
    if not ids:
        return []

    articles: list[dict] = []
    for story_id in ids[:top_n]:
        data = _get_json(
            f"https://hacker-news.firebaseio.com/v0/item/{story_id}.json"
        )
        if not data or data.get("type") != "story":
            continue
        title = data.get("title", "")
        link = data.get("url") or f"https://news.ycombinator.com/item?id={story_id}"
        score = data.get("score", 0)
        comments = data.get("descendants", 0)
        by = data.get("by", "")
        ts = data.get("time", 0)
        published = datetime.utcfromtimestamp(ts).strftime("%a, %d %b %Y %H:%M UTC")
        if title:
            articles.append({
                "type": "social",
                "source": "Hacker News",
                "title": title,
                "link": link,
                "description": f"Posted by {by}",
                "published": published,
                "meta": {
                    "score": score,
                    "comments": comments,
                    "hn_url": f"https://news.ycombinator.com/item?id={story_id}",
                },
            })

    return articles


def scrape_github_trending() -> list[dict]:
    """
    Scrape GitHub's trending page for today's trending repositories.
    Parses the HTML directly (no API key needed).
    """
    print("  [GitHub Trending]", file=sys.stderr)
    raw = _get("https://github.com/trending")
    if raw is None:
        return []

    html = raw.decode("utf-8", errors="replace")
    articles: list[dict] = []

    # Extract repo blocks — each trending repo is in an <article> tag
    repo_pattern = re.compile(
        r'<article[^>]*class="[^"]*Box-row[^"]*"[^>]*>(.*?)</article>',
        re.DOTALL,
    )
    name_pattern = re.compile(r'href="/([^"]+)"[^>]*>\s*<span[^>]*>([^<]+)</span>\s*<span[^>]*>/</span>\s*<span[^>]*>([^<]+)</span>')
    desc_pattern = re.compile(r'<p[^>]*class="[^"]*col-9[^"]*"[^>]*>(.*?)</p>', re.DOTALL)
    lang_pattern = re.compile(r'itemprop="programmingLanguage">([^<]+)<')
    stars_pattern = re.compile(r'aria-label="([^"]*) stars"')
    star_today_pattern = re.compile(r'([\d,]+)\s+stars today')

    for match in repo_pattern.finditer(html):
        block = match.group(1)

        repo_match = re.search(r'href="/([^/]+/[^/"]+)"', block)
        if not repo_match:
            continue
        repo_path = repo_match.group(1).strip()
        if repo_path.count("/") != 1:
            continue

        desc_match = desc_pattern.search(block)
        description = strip_html(desc_match.group(1)) if desc_match else ""

        lang_match = lang_pattern.search(block)
        language = lang_match.group(1).strip() if lang_match else ""

        stars_match = stars_pattern.search(block)
        total_stars = stars_match.group(1).strip() if stars_match else ""

        today_match = star_today_pattern.search(block)
        stars_today = today_match.group(1).strip() if today_match else ""

        title = f"{repo_path}"
        if language:
            title += f"  [{language}]"
        meta_parts = []
        if total_stars:
            meta_parts.append(f"★ {total_stars} total")
        if stars_today:
            meta_parts.append(f"+{stars_today} today")

        articles.append({
            "type": "social",
            "source": "GitHub Trending",
            "title": title,
            "link": f"https://github.com/{repo_path}",
            "description": description,
            "published": datetime.utcnow().strftime("%a, %d %b %Y"),
            "meta": {
                "language": language,
                "stars_total": total_stars,
                "stars_today": stars_today,
            },
        })

    return articles[:25]


# ──────────────────────────────────────────────────────────────────────────────
# Orchestration
# ──────────────────────────────────────────────────────────────────────────────

SOURCE_MAP = {
    "rss": "News RSS feeds (BBC, Reuters, Guardian, NPR, CNN, AP, TechCrunch, Ars)",
    "reddit": "Reddit hot posts (news, worldnews, technology, science, politics…)",
    "hackernews": "Hacker News top stories",
    "github": "GitHub Trending repositories",
}


def scrape_all(
    sources: list[str] | None,
    keywords: list[str] | None,
    limit: int,
) -> list[dict]:
    enabled = set(sources) if sources else set(SOURCE_MAP.keys())
    all_articles: list[dict] = []

    print(f"\nScraping {len(enabled)} source group(s)...", file=sys.stderr)

    if "rss" in enabled:
        all_articles.extend(scrape_rss())
    if "reddit" in enabled:
        all_articles.extend(scrape_reddit())
    if "hackernews" in enabled:
        all_articles.extend(scrape_hackernews())
    if "github" in enabled:
        all_articles.extend(scrape_github_trending())

    if keywords:
        all_articles = [a for a in all_articles if matches_keywords(a, keywords)]

    return all_articles[:limit]


# ──────────────────────────────────────────────────────────────────────────────
# Output
# ──────────────────────────────────────────────────────────────────────────────

TYPE_LABELS = {"news": "NEWS", "social": "SOCIAL"}
SEP = "-" * 76


def print_articles(articles: list[dict]) -> None:
    if not articles:
        print("\nNo articles found.")
        return

    print(f"\n{'=' * 76}")
    print(f"  NEWS & SOCIAL DIGEST  —  {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"  {len(articles)} item(s) retrieved")
    print(f"{'=' * 76}\n")

    for i, art in enumerate(articles, 1):
        label = TYPE_LABELS.get(art.get("type", ""), "ITEM")
        print(f"[{i:>3}] [{label}]  {art['title']}")
        print(f"       Source    : {art['source']}")
        if art.get("published"):
            print(f"       Published : {art['published']}")
        if art.get("description"):
            print(f"       Snippet   : {truncate(art['description'], 180)}")

        meta = art.get("meta", {})
        if meta.get("score") is not None and meta["score"]:
            score_line = f"score={meta['score']}"
            if meta.get("comments"):
                score_line += f"  comments={meta['comments']}"
            if meta.get("flair"):
                score_line += f"  flair={meta['flair']}"
            print(f"       Social    : {score_line}")
        if meta.get("reddit_url"):
            print(f"       Reddit    : {meta['reddit_url']}")
        if meta.get("hn_url"):
            print(f"       HN        : {meta['hn_url']}")

        print(f"       URL       : {art['link']}")
        print(SEP)


# ──────────────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Scrape newsworthy articles and social media posts from the web.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="\n".join(
            ["\nAvailable source groups:"]
            + [f"  {k:<15} {v}" for k, v in SOURCE_MAP.items()]
        ),
    )
    parser.add_argument(
        "--sources",
        nargs="+",
        choices=list(SOURCE_MAP.keys()),
        default=None,
        metavar="SOURCE",
        help="Source groups to scrape (default: all). Choices: " + ", ".join(SOURCE_MAP),
    )
    parser.add_argument(
        "--keywords",
        nargs="+",
        default=None,
        metavar="WORD",
        help="Only show items containing ALL these keywords (case-insensitive).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=40,
        help="Maximum number of results to display (default: 40).",
    )
    parser.add_argument(
        "--output",
        type=str,
        default=None,
        metavar="FILE",
        help="Save results as JSON to this file.",
    )
    parser.add_argument(
        "--list-sources",
        action="store_true",
        help="Print available source groups and exit.",
    )

    args = parser.parse_args()

    if args.list_sources:
        print("\nAvailable source groups:")
        for k, v in SOURCE_MAP.items():
            print(f"  {k:<15} {v}")
        return

    articles = scrape_all(
        sources=args.sources,
        keywords=args.keywords,
        limit=args.limit,
    )

    print_articles(articles)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(articles, f, ensure_ascii=False, indent=2)
        print(f"\nSaved {len(articles)} items to {args.output}")


if __name__ == "__main__":
    main()
