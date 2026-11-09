from celery import shared_task
from crawler.models import Link, Keyword, Category, UserProfile, CrawledLinks, SocialMedia
from scheduler.models import ScrapedLink
from django.contrib.auth.models import User
from utils.crawler_spider import social_media_scrape

@shared_task
def save_models(query, cat, link, scrape_data, reschedule, username):
    no_of_links = 0
    user = User.objects.get(username=username)
    userprofile = UserProfile.objects.get(user=user)
    keyword = Keyword.objects.get(name=query)
    category = Category.objects.get(name=cat)
    scraped_link = ScrapedLink.objects.get_or_create(link=link, scrape_data=scrape_data,
                                                     schedule_day=reschedule)[0]
    scraped_link.save()

    link = Link.objects.get_or_create(keyword=keyword, category=category, link=link, scrape_data=scrape_data)[0]
    link.save()

    profile_update, created = CrawledLinks.objects.get_or_create(userprofile=userprofile,
                                                                 link=link, reschedule=reschedule)
    profile_update.save()

    if created:
        no_of_links += 1

    userprofile.crawled_links += no_of_links
    userprofile.save()


@shared_task
def scrape_social(keyword):
    key = Keyword.objects.get(name=keyword)
    scrape_data = social_media_scrape(keyword)
    for tweets in scrape_data:
        social = SocialMedia.objects.get_or_create(keyword=key, timestamp=tweets.timestamp, screen_name=tweets.screen_name,
                                          username=tweets.username, tweet_url=tweets.tweet_url, text=tweets.text,
                                          hashtags=tweets.hashtags, likes=tweets.likes, retweets=tweets.retweets)[0]
        social.save()




@shared_task
def crawl_seed_sites_task(site_names=None, username=None, max_pages=50, max_depth=2, delay=2.0, reschedule=1):
    """Celery entry point for the seed-site crawler (see crawler/seed_crawl.py)."""
    from crawler.seed_crawl import crawl_seed_sites
    summaries = crawl_seed_sites(site_names=site_names, username=username, reschedule=reschedule,
                                 max_pages=max_pages, max_depth=max_depth, delay=delay)
    # errors hold tuples; return plain data so any result backend can store it
    return [{**s, "errors": [list(e) for e in s["errors"]]} for s in summaries]
