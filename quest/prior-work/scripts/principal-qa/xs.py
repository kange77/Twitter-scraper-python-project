"""Run the xscraper CLI against the hostile mock on 127.0.0.1:8790."""
import sys
from xscraper import scraper, cli
scraper.TWEET_ENDPOINT = "http://127.0.0.1:8790/tweet-result"
scraper.TIMELINE_ENDPOINT = "http://127.0.0.1:8790/srv/timeline-profile/screen-name/{}"
if __name__ == "__main__":
    sys.exit(cli.main(sys.argv[1:]))
