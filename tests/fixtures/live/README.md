# Live tweet-result responses (contract fixtures)

Real public responses from X's embed endpoint, saved unmodified on
2026-10-07 for the contract tests in `tests/test_contract_live.py`.

- Endpoint: `GET https://cdn.syndication.twimg.com/tweet-result?id=<id>&lang=en&token=<token>`
  (the request `xscraper.scraper.tweet_params` builds), browser User-Agent,
  no cookies.
- Every file is the raw response body of an HTTP 200, named
  `tweet-result-<id>.json`. Two of them are tombstones (a deleted post and an
  unavailable post), which X also answers with 200.
- Counts (likes, replies) are whatever they were on that day. Tests check
  their type, not their value.

| File | What it is |
|---|---|
| `tweet-result-1745294979403014244.json` | plain text |
| `tweet-result-1874918027982172626.json` | 3 photos |
| `tweet-result-1812256998588662068.json` | video |
| `tweet-result-1273770669214490626.json` | media only (`lang` `zxx`), video withheld (DMCA) |
| `tweet-result-1594131768298315777.json` | quote tweet |
| `tweet-result-1603190155107794944.json` | reply (with `parent`) |
| `tweet-result-88618213008621568.json` | tombstone: deleted by the author |
| `tweet-result-1347684877634838528.json` | tombstone: unavailable |
