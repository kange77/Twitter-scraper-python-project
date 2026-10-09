import json, random, copy, collections, traceback
from xscraper.parse import parse_tweet_result, parse_timeline_page, ParseError
import importlib.util, sys
spec = importlib.util.spec_from_file_location("srv", "/mnt/project-files/mock-x/server.py"); srv = importlib.util.module_from_spec(spec); spec.loader.exec_module(srv)
seeds = [srv.T_ROOT, srv.T_REPLY, srv.T_REPLY2] + srv.TIMELINE
vals = [None, 0, -1, 1.5, "", "x", [], {}, [1], {"a": 1}, True, 10**30, float("nan"), "Mon Jan 99", [None], [{"x": None}]]
def paths(o, p=()):
    yield p
    if isinstance(o, dict):
        for k, v in o.items(): yield from paths(v, p + (k,))
    elif isinstance(o, list):
        for i, v in enumerate(o): yield from paths(v, p + (i,))
def setp(o, p, v):
    for k in p[:-1]: o = o[k]
    o[p[-1]] = v
random.seed(1); bad = collections.Counter(); ex = {}
for n in range(30000):
    base = copy.deepcopy(random.choice(seeds)); ps = [p for p in paths(base) if p]
    for _ in range(random.randint(1, 3)):
        setp(base, random.choice(ps), copy.deepcopy(random.choice(vals))); ps = [p for p in paths(base) if p]
        if not ps: break
    try:
        parse_tweet_result(base)
        parse_timeline_page(srv.page([{"type": "tweet", "content": {"tweet": base}}]))
    except ParseError: pass
    except Exception as e:
        k = f"{type(e).__name__}: {str(e)[:80]}"; bad[k] += 1; ex.setdefault(k, traceback.format_exc().splitlines()[-4:])
print(sum(bad.values()), "escaped exceptions out of 30000")
for k, c in bad.most_common(10): print(c, k, ex[k])
