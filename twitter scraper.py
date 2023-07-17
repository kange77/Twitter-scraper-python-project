import requests
from bs4 import BeautifulSoup

# Define the URL of the Twitter page you want to scrape
url = "https://twitter.com/username"

# Send a GET request to the URL
response = requests.get(url)

# Parse the HTML content of the page using Beautiful Soup
soup = BeautifulSoup(response.content, "html.parser")

# Find all the tweets on the page
tweets = soup.find_all("div", class_="tweet")

# Iterate through the tweets and print the text of each one
for tweet in tweets:
    tweet_text = tweet.find("p", class_="tweet-text").text
    print(tweet_text)
