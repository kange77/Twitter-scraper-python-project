import sys
from xscraper import scraper, cli
scraper.TWEET_ENDPOINT = "http://127.0.0.1:8765/tweet-result"
scraper.TIMELINE_ENDPOINT = "http://127.0.0.1:8765/srv/timeline-profile/screen-name/{}"
sys.exit(cli.main(sys.argv[1:]))
