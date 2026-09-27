"""Legacy entry point, kept so existing scripts keep working.

The scraper now lives in the ``xscraper`` package; run ``python -m xscraper --help``.
"""
import sys

from xscraper import Scraper


def scrape_tweets(username):
    """Return recent tweets from ``username`` as plain dicts (old return format)."""
    try:
        tweets = Scraper().user_timeline(username)
    except Exception as e:
        print(f"An error occurred: {e}")
        return []
    return [
        {"text": t.text, "username": t.user.screen_name, "date": t.created_at,
         "likes": t.like_count, "retweets": t.retweet_count}
        for t in tweets
    ]


if __name__ == "__main__":
    from xscraper.cli import main

    sys.exit(main(["user", *(sys.argv[1:] or ["NASA"])]))
