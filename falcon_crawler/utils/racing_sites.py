"""
Seed sites for the FALCON seed-site crawler (horse racing).

Each entry describes one site:

* ``name``            - short id; also used as the FALCON ``Keyword`` the links are stored under
* ``start_urls``      - pages the crawl starts from
* ``allowed_domains`` - hosts the crawler may follow links to
* ``default_category``- category used when no URL rule matches
* ``category_rules``  - optional per-site ``(regex, category)`` rules, checked
                        before the generic ``CATEGORY_RULES`` below

Categories are stored in the ``Category`` table, so names must stay <= 20 chars.
"""

ENTRIES = "racing_entries"
RESULTS = "racing_results"
ODDS = "racing_odds"
NEWS = "racing_news"
GENERAL = "racing_general"

CATEGORIES = [ENTRIES, RESULTS, ODDS, NEWS, GENERAL]

# Generic URL -> category rules, applied to the lower-cased URL path and query.
# First match wins, so the more specific patterns come first.
CATEGORY_RULES = [
    (r"(odds|tote|probables|will-?pays|betting|live-?racing)", ODDS),
    (r"(results?|charts?|payouts?|replays?)(/|\.|$|-|\?)", RESULTS),
    (r"(entries|entry|race-?cards?|racecards?|cards|declarations|runners|fields)(/|\.|$|-|\?)", ENTRIES),
    (r"(news|articles?|stories|story|blog|features?|analysis|picks|tips|handicapping)(/|\.|$|-|\?)", NEWS),
]

SEED_SITES = [
    {
        "name": "equibase",
        "start_urls": [
            "https://www.equibase.com/",
            "https://www.equibase.com/static/entry/index.html",
            "https://www.equibase.com/static/chart/summary/index.html",
            "https://www.equibase.com/news/",
        ],
        "allowed_domains": ["equibase.com", "www.equibase.com"],
        "default_category": GENERAL,
        "category_rules": [
            (r"/static/entry/", ENTRIES),
            (r"/static/chart/", RESULTS),
        ],
    },
    {
        "name": "offtrackbetting",
        "start_urls": ["https://www.offtrackbetting.com/"],
        "allowed_domains": ["offtrackbetting.com", "www.offtrackbetting.com"],
        "default_category": GENERAL,
        "category_rules": [],
    },
    {
        "name": "horseracingnation_entries",
        "start_urls": ["https://entries.horseracingnation.com/"],
        "allowed_domains": ["entries.horseracingnation.com"],
        # Everything on this subdomain is an entries/results page.
        "default_category": ENTRIES,
        "category_rules": [],
    },
    {
        "name": "horseracingnation",
        "start_urls": [
            "https://www.horseracingnation.com/",
            "https://www.horseracingnation.com/news/",
        ],
        "allowed_domains": ["horseracingnation.com", "www.horseracingnation.com"],
        "default_category": GENERAL,
        "category_rules": [],
    },
    {
        "name": "drf",
        "start_urls": [
            "https://www.drf.com/",
            "https://www.drf.com/news",
            "https://www.drf.com/results",
        ],
        "allowed_domains": ["drf.com", "www.drf.com"],
        "default_category": GENERAL,
        "category_rules": [],
    },
    {
        "name": "attheraces",
        "start_urls": [
            "https://www.attheraces.com/",
            "https://www.attheraces.com/racecards",
            "https://www.attheraces.com/results",
            "https://www.attheraces.com/news",
        ],
        "allowed_domains": ["attheraces.com", "www.attheraces.com"],
        "default_category": GENERAL,
        "category_rules": [
            (r"/racecard", ENTRIES),
        ],
    },
]


def get_site(name):
    for site in SEED_SITES:
        if site["name"] == name:
            return site
    raise KeyError(f"Unknown seed site '{name}'. Known: {', '.join(s['name'] for s in SEED_SITES)}")
