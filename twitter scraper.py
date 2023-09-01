import requests
from bs4 import BeautifulSoup

def scrape_tweets(username):
    # Define the URL of the Twitter page you want to scrape
    url = f"https://twitter.com/{username}"

    try:
        # Send a GET request to the URL
        response = requests.get(url)
        response.raise_for_status()

        # Parse the HTML content of the page using Beautiful Soup
        soup = BeautifulSoup(response.content, "html.parser")

        # Find all the tweets on the page
        tweets = soup.find_all("div", class_="tweet")

        # Create a list to store tweet data
        tweet_data = []

        # Iterate through the tweets and extract data for each one
        for tweet in tweets:
            tweet_text = tweet.find("p", class_="tweet-text")
            username = tweet.find("span", class_="username")
            date = tweet.find("span", class_="_timestamp")
            like_count = tweet.find("span", class_="ProfileTweet-action--favorite")
            retweet_count = tweet.find("span", class_="ProfileTweet-action--retweet")

            # Check if all information is present before adding to the list
            if tweet_text and username and date and like_count and retweet_count:
                tweet_data.append({
                    "text": tweet_text.text,
                    "username": username.text,
                    "date": date["data-time"],
                    "likes": like_count.find("span", class_="ProfileTweet-actionCount")["data-tweet-stat-count"],
                    "retweets": retweet_count.find("span", class_="ProfileTweet-actionCount")["data-tweet-stat-count"]
                })

        return tweet_data

    except requests.exceptions.RequestException as e:
        print(f"An error occurred: {e}")
        return []

if __name__ == "__main__":
    username = "your_username_here"  # Replace with the Twitter username you want to scrape
    tweets = scrape_tweets(username)

    if tweets:
        print(f"Latest tweets from @{username}:")
        for i, tweet in enumerate(tweets, start=1):
            print(f"{i}. Text: {tweet['text']}")
            print(f"   Username: {tweet['username']}")
            print(f"   Date: {tweet['date']}")
            print(f"   Likes: {tweet['likes']}")
            print(f"   Retweets: {tweet['retweets']}")
            print("-" * 40)
    else:
        print("No tweets found or an error occurred while scraping.")
