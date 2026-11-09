"""
Run the seed-site crawler and store what it finds in FALCON's tables.

Each crawled page becomes a ``Link`` under ``Keyword(name=<site name>)`` and the
page's category (``racing_entries``, ``racing_results``, ``racing_odds``,
``racing_news`` or ``racing_general``). The page is also registered as a
``ScrapedLink`` so the scheduler can re-scrape it, and, when a user is given,
as a ``CrawledLinks`` row so it shows up on that user's dashboard and reports.
"""

import json

from crawler.models import Category, CrawledLinks, Keyword, Link, UserProfile
from scheduler.models import ScrapedLink
from utils.racing_sites import SEED_SITES, get_site
from utils.site_crawler import SiteCrawler


def save_page(page, keyword, userprofile=None, reschedule=1):
    category = Category.objects.get_or_create(name=page["category"])[0]
    scrape_data = json.dumps(page, ensure_ascii=False)

    link, created = Link.objects.get_or_create(
        keyword=keyword, category=category, link=page["url"],
        defaults={"scrape_data": scrape_data},
    )
    if not created:
        link.scrape_data = scrape_data
        link.save(update_fields=["scrape_data"])

    ScrapedLink.objects.update_or_create(
        link=page["url"],
        defaults={"scrape_data": scrape_data, "schedule_day": reschedule},
    )

    new_for_user = False
    if userprofile is not None:
        _, new_for_user = CrawledLinks.objects.get_or_create(
            userprofile=userprofile, link=link, defaults={"reschedule": reschedule},
        )
    return created, new_for_user


def crawl_site(site, userprofile=None, reschedule=1, save=True, **crawler_kwargs):
    """Crawl one site config. Returns a summary dict."""
    crawler = SiteCrawler(site, **crawler_kwargs)
    pages, errors = crawler.crawl()

    summary = {"site": site["name"], "pages": len(pages), "new_links": 0,
               "errors": errors, "by_category": {}}
    for page in pages:
        summary["by_category"][page["category"]] = summary["by_category"].get(page["category"], 0) + 1

    if save and pages:
        keyword = Keyword.objects.get_or_create(name=site["name"])[0]
        new_for_user = 0
        for page in pages:
            created, user_new = save_page(page, keyword, userprofile, reschedule)
            summary["new_links"] += int(created)
            new_for_user += int(user_new)
        if userprofile is not None and new_for_user:
            userprofile.crawled_links += new_for_user
            userprofile.scraped_data += len(pages)
            userprofile.save(update_fields=["crawled_links", "scraped_data"])
    return summary


def crawl_seed_sites(site_names=None, username=None, reschedule=1, save=True, **crawler_kwargs):
    """Crawl the named seed sites (all of them by default)."""
    sites = [get_site(n) for n in site_names] if site_names else SEED_SITES
    userprofile = None
    if username:
        from django.contrib.auth.models import User
        user = User.objects.get(username=username)
        userprofile = UserProfile.objects.get_or_create(user=user)[0]
    return [crawl_site(site, userprofile, reschedule, save, **crawler_kwargs) for site in sites]
