from django.core.management.base import BaseCommand, CommandError

from crawler.seed_crawl import crawl_seed_sites
from utils.racing_sites import SEED_SITES


class Command(BaseCommand):
    help = ("Crawl the configured seed sites (horse racing: entries, results, odds, news) "
            "and store the pages as FALCON links.")

    def add_arguments(self, parser):
        parser.add_argument("--site", action="append", dest="sites",
                            choices=[s["name"] for s in SEED_SITES],
                            help="Seed site to crawl (repeatable). Default: all.")
        parser.add_argument("--user", help="Username whose dashboard the links are added to.")
        parser.add_argument("--max-pages", type=int, default=50, help="Pages per site (default 50).")
        parser.add_argument("--max-depth", type=int, default=2, help="Link depth from the seed URLs (default 2).")
        parser.add_argument("--delay", type=float, default=2.0,
                            help="Seconds between requests (default 2; robots.txt Crawl-delay wins if larger).")
        parser.add_argument("--reschedule", type=int, default=1, help="Days until the page is re-scraped (default 1).")
        parser.add_argument("--dry-run", action="store_true", help="Crawl and report, but save nothing.")
        parser.add_argument("--list", action="store_true", help="List the seed sites and exit.")

    def handle(self, *args, **opts):
        if opts["list"]:
            for site in SEED_SITES:
                self.stdout.write(f"{site['name']:<28} {', '.join(site['start_urls'])}")
            return

        try:
            summaries = crawl_seed_sites(
                site_names=opts["sites"], username=opts["user"], reschedule=opts["reschedule"],
                save=not opts["dry_run"], max_pages=opts["max_pages"], max_depth=opts["max_depth"],
                delay=opts["delay"], log=lambda msg: self.stdout.write(msg),
            )
        except Exception as exc:  # unknown user, etc.
            raise CommandError(str(exc))

        for s in summaries:
            cats = ", ".join(f"{k}={v}" for k, v in sorted(s["by_category"].items())) or "none"
            self.stdout.write(self.style.SUCCESS(
                f"{s['site']}: {s['pages']} pages ({cats}), {s['new_links']} new links, {len(s['errors'])} skipped"))
            for url, reason in s["errors"][:10]:
                self.stdout.write(f"    skipped {url}: {reason}")
