"""Twitter/X posting via tweepy v2 Client."""

import os
import tweepy

TWITTER_API_KEY = os.environ.get("TWITTER_API_KEY", "")
TWITTER_API_SECRET = os.environ.get("TWITTER_API_SECRET", "")
TWITTER_ACCESS_TOKEN = os.environ.get("TWITTER_ACCESS_TOKEN", "")
TWITTER_ACCESS_TOKEN_SECRET = os.environ.get("TWITTER_ACCESS_TOKEN_SECRET", "")


def _get_client() -> tweepy.Client | None:
    if not all([TWITTER_API_KEY, TWITTER_API_SECRET, TWITTER_ACCESS_TOKEN, TWITTER_ACCESS_TOKEN_SECRET]):
        return None
    return tweepy.Client(
        consumer_key=TWITTER_API_KEY,
        consumer_secret=TWITTER_API_SECRET,
        access_token=TWITTER_ACCESS_TOKEN,
        access_token_secret=TWITTER_ACCESS_TOKEN_SECRET,
    )


def post_tweet(text: str) -> dict:
    """Post a single tweet. Returns { tweet_id, error }."""
    client = _get_client()
    if not client:
        return {"tweet_id": None, "error": "Twitter API keys not configured"}
    try:
        resp = client.create_tweet(text=text)
        tweet_id = resp.data["id"]
        return {"tweet_id": tweet_id, "error": None}
    except Exception as e:
        return {"tweet_id": None, "error": str(e)[:200]}


def post_thread(tweets: list[str]) -> dict:
    """Post a thread (list of tweets). Returns { tweet_ids, error }."""
    client = _get_client()
    if not client:
        return {"tweet_ids": [], "error": "Twitter API keys not configured"}

    tweet_ids = []
    reply_to = None

    try:
        for text in tweets:
            if reply_to:
                resp = client.create_tweet(text=text, in_reply_to_tweet_id=reply_to)
            else:
                resp = client.create_tweet(text=text)
            tid = resp.data["id"]
            tweet_ids.append(tid)
            reply_to = tid

        return {"tweet_ids": tweet_ids, "error": None}
    except Exception as e:
        return {"tweet_ids": tweet_ids, "error": str(e)[:200]}
