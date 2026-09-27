from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class User:
    id: str
    screen_name: str
    name: str = ""
    verified: bool = False
    profile_image_url: Optional[str] = None
    followers_count: Optional[int] = None

    @property
    def url(self) -> str:
        return f"https://x.com/{self.screen_name}"


@dataclass
class Media:
    type: str  # photo | video | animated_gif
    url: str  # image URL, or the thumbnail for videos
    width: Optional[int] = None
    height: Optional[int] = None
    video_url: Optional[str] = None  # highest-bitrate MP4, when there is one


@dataclass
class Tweet:
    id: str
    text: str
    created_at: Optional[str]  # ISO 8601, UTC
    user: User
    lang: Optional[str] = None
    like_count: Optional[int] = None
    retweet_count: Optional[int] = None
    reply_count: Optional[int] = None
    quote_count: Optional[int] = None
    hashtags: list[str] = field(default_factory=list)
    mentions: list[str] = field(default_factory=list)
    urls: list[str] = field(default_factory=list)
    media: list[Media] = field(default_factory=list)
    in_reply_to_id: Optional[str] = None
    quoted_tweet_id: Optional[str] = None
    retweeted_tweet_id: Optional[str] = None
    source: str = ""
    analysis: Optional[dict[str, Any]] = None

    @property
    def url(self) -> str:
        return f"https://x.com/{self.user.screen_name}/status/{self.id}"

    @property
    def is_retweet(self) -> bool:
        return self.retweeted_tweet_id is not None

    def to_dict(self) -> dict[str, Any]:
        # Same result as dataclasses.asdict (plus "url"), built directly:
        # asdict's generic deep copy was the slowest step of every export.
        d = dict(self.__dict__)
        d["user"] = dict(self.user.__dict__)
        d["media"] = [dict(m.__dict__) for m in self.media]
        for key in ("hashtags", "mentions", "urls"):
            d[key] = list(d[key])
        if self.analysis is not None:
            d["analysis"] = {k: list(v) if isinstance(v, list) else v for k, v in self.analysis.items()}
        d["url"] = self.url
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Tweet":
        d = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        d["user"] = User(**d["user"])
        d["media"] = [Media(**m) for m in d.get("media") or []]
        return cls(**d)
