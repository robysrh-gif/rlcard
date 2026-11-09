import json

from django.contrib.auth.models import User
from django.test import SimpleTestCase, TestCase

from crawler.models import CrawledLinks, Keyword, Link, UserProfile
from crawler.seed_crawl import crawl_site
from scheduler.models import ScrapedLink
from utils.racing_sites import CATEGORIES, SEED_SITES
from utils.site_crawler import SiteCrawler, classify_url


class FakeResponse:
    def __init__(self, url, status_code=200, text="", content_type="text/html; charset=utf-8"):
        self.url = url
        self.status_code = status_code
        self.text = text
        self.headers = {"Content-Type": content_type}


class FakeSession:
    """Serves canned pages; anything unknown is a 404."""

    def __init__(self, pages):
        self.pages = pages
        self.headers = {}
        self.requested = []

    def get(self, url, timeout=None):
        self.requested.append(url)
        if url in self.pages:
            value = self.pages[url]
            if isinstance(value, FakeResponse):
                return value
            return FakeResponse(url, text=value)
        return FakeResponse(url, status_code=404)


SITE = {
    "name": "testtrack",
    "start_urls": ["https://www.testtrack.com/"],
    "allowed_domains": ["www.testtrack.com"],
    "default_category": "racing_general",
    "category_rules": [],
}

HOME = """
<html><head><title>Test Track</title><meta property="og:description" content="Racing hub"></head>
<body><h1>Welcome</h1>
<a href="/entries/2026-11-09">Entries</a>
<a href="/results/2026-11-08#top">Results</a>
<a href="/odds/race-1">Odds</a>
<a href="/news/big-win">News</a>
<a href="/private/admin">Admin</a>
<a href="https://elsewhere.com/page">Off site</a>
<a href="/logo.png">Logo</a>
<a href="mailto:x@y.com">Mail</a>
</body></html>
"""

ENTRIES = """
<html><head><title>Entries</title></head><body><h1>Race 1</h1>
<table><caption>Race 1</caption>
<tr><th>PP</th><th>Horse</th><th>Jockey</th><th>ML</th></tr>
<tr><td>1</td><td>Fast Falcon</td><td>J. Smith</td><td>5/2</td></tr>
</table><a href="/entries/2026-11-10">Tomorrow</a></body></html>
"""

ROBOTS = "User-agent: *\nDisallow: /private/\n"


def fake_site_pages():
    base = "https://www.testtrack.com"
    return {
        base + "/robots.txt": ROBOTS,
        base + "/": HOME,
        base + "/entries/2026-11-09": ENTRIES,
        base + "/results/2026-11-08": "<html><title>Results</title><body>chart</body></html>",
        base + "/odds/race-1": "<html><title>Odds</title><body>odds</body></html>",
        base + "/news/big-win": "<html><title>News</title><body>story</body></html>",
        base + "/private/admin": "<html>secret</html>",
    }


def make_crawler(pages, **kwargs):
    kwargs.setdefault("delay", 0)
    return SiteCrawler(SITE, session=FakeSession(pages), sleep=lambda s: None, log=lambda m: None, **kwargs)


class ClassifyUrlTests(SimpleTestCase):

    def test_generic_rules(self):
        self.assertEqual(classify_url("https://x.com/entries/aqu"), "racing_entries")
        self.assertEqual(classify_url("https://x.com/racecards/ascot"), "racing_entries")
        self.assertEqual(classify_url("https://x.com/results/2026-11-08"), "racing_results")
        self.assertEqual(classify_url("https://x.com/odds/race-3"), "racing_odds")
        self.assertEqual(classify_url("https://x.com/news/some-story"), "racing_news")
        self.assertEqual(classify_url("https://x.com/about"), "racing_general")

    def test_host_name_is_ignored(self):
        self.assertEqual(classify_url("https://www.offtrackbetting.com/about"), "racing_general")

    def test_site_rules_win(self):
        rules = [(r"/static/chart/", "racing_results")]
        self.assertEqual(classify_url("https://e.com/static/chart/summary/x.html", rules), "racing_results")

    def test_seed_config_is_valid(self):
        names = [s["name"] for s in SEED_SITES]
        self.assertEqual(len(names), len(set(names)))
        for site in SEED_SITES:
            self.assertIn(site["default_category"], CATEGORIES)
            for _, category in site["category_rules"]:
                self.assertIn(category, CATEGORIES)
        for category in CATEGORIES:
            self.assertLessEqual(len(category), 20)  # Category.name max_length


class SiteCrawlerTests(SimpleTestCase):

    def test_crawl_follows_site_links_and_obeys_robots(self):
        crawler = make_crawler(fake_site_pages())
        pages, errors = crawler.crawl()
        urls = {p["url"]: p for p in pages}

        self.assertEqual(set(urls), {
            "https://www.testtrack.com/",
            "https://www.testtrack.com/entries/2026-11-09",
            "https://www.testtrack.com/results/2026-11-08",
            "https://www.testtrack.com/odds/race-1",
            "https://www.testtrack.com/news/big-win",
        })
        self.assertIn(("https://www.testtrack.com/private/admin", "blocked by robots.txt"), errors)
        self.assertIn(("https://www.testtrack.com/entries/2026-11-10", "HTTP 404"), errors)

        requested = crawler.session.requested
        self.assertNotIn("https://www.testtrack.com/private/admin", requested)
        self.assertFalse(any("elsewhere.com" in u or u.endswith(".png") for u in requested))

        entries = urls["https://www.testtrack.com/entries/2026-11-09"]
        self.assertEqual(entries["category"], "racing_entries")
        self.assertEqual(entries["tables"][0]["caption"], "Race 1")
        self.assertEqual(entries["tables"][0]["rows"][1], ["1", "Fast Falcon", "J. Smith", "5/2"])
        self.assertEqual(urls["https://www.testtrack.com/"]["meta"]["description"], "Racing hub")

    def test_max_pages_and_depth(self):
        pages, _ = make_crawler(fake_site_pages(), max_pages=2).crawl()
        self.assertEqual(len(pages), 2)
        pages, _ = make_crawler(fake_site_pages(), max_depth=0).crawl()
        self.assertEqual([p["url"] for p in pages], ["https://www.testtrack.com/"])

    def test_robots_forbidden_blocks_everything(self):
        site_pages = fake_site_pages()
        site_pages["https://www.testtrack.com/robots.txt"] = FakeResponse(
            "https://www.testtrack.com/robots.txt", status_code=403)
        pages, errors = make_crawler(site_pages).crawl()
        self.assertEqual(pages, [])
        self.assertEqual(errors, [("https://www.testtrack.com/", "blocked by robots.txt")])

    def test_missing_robots_allows_crawl(self):
        site_pages = fake_site_pages()
        del site_pages["https://www.testtrack.com/robots.txt"]
        pages, _ = make_crawler(site_pages).crawl()
        self.assertIn("https://www.testtrack.com/private/admin", [p["url"] for p in pages])

    def test_crawl_delay_is_respected(self):
        site_pages = fake_site_pages()
        site_pages["https://www.testtrack.com/robots.txt"] = ROBOTS + "Crawl-delay: 7\n"
        sleeps = []
        crawler = SiteCrawler(SITE, session=FakeSession(site_pages), sleep=sleeps.append,
                              log=lambda m: None, delay=1, max_pages=2)
        crawler.crawl()
        self.assertTrue(sleeps)
        self.assertTrue(all(s > 6 for s in sleeps[1:]), sleeps)


class SeedCrawlPersistenceTests(TestCase):

    def test_pages_are_saved_and_updated(self):
        user = User.objects.create_user("racer", password="x")
        profile = UserProfile.objects.create(user=user)

        summary = crawl_site(SITE, userprofile=profile, delay=0, sleep=lambda s: None,
                             log=lambda m: None, session=FakeSession(fake_site_pages()))
        self.assertEqual(summary["pages"], 5)
        self.assertEqual(summary["new_links"], 5)
        self.assertEqual(summary["by_category"]["racing_entries"], 1)

        keyword = Keyword.objects.get(name="testtrack")
        link = Link.objects.get(keyword=keyword, link="https://www.testtrack.com/entries/2026-11-09")
        self.assertEqual(link.category.name, "racing_entries")
        self.assertEqual(json.loads(link.scrape_data)["title"], "Entries")
        self.assertEqual(ScrapedLink.objects.count(), 5)
        self.assertEqual(CrawledLinks.objects.filter(userprofile=profile).count(), 5)
        profile.refresh_from_db()
        self.assertEqual(profile.crawled_links, 5)

        # Second crawl: content changed, nothing duplicated.
        changed = fake_site_pages()
        changed["https://www.testtrack.com/entries/2026-11-09"] = "<html><title>Entries v2</title></html>"
        summary = crawl_site(SITE, userprofile=profile, delay=0, sleep=lambda s: None,
                             log=lambda m: None, session=FakeSession(changed))
        self.assertEqual(summary["new_links"], 0)
        self.assertEqual(Link.objects.count(), 5)
        self.assertEqual(CrawledLinks.objects.count(), 5)
        link.refresh_from_db()
        self.assertEqual(json.loads(link.scrape_data)["title"], "Entries v2")
        profile.refresh_from_db()
        self.assertEqual(profile.crawled_links, 5)

    def test_dry_run_saves_nothing(self):
        crawl_site(SITE, save=False, delay=0, sleep=lambda s: None, log=lambda m: None,
                   session=FakeSession(fake_site_pages()))
        self.assertEqual(Link.objects.count(), 0)
