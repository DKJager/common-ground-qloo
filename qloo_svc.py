"""qloo_svc: Lambda Function URL service for the Common Ground demo."""
from __future__ import annotations

import base64
import collections
import copy
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import uuid

from qloo_core6 import (QlooClient, QlooError, CallLimitError, Member,  # noqa: E402
                        RejectionLearner, counterfactual, rank_options, _build)

KINDS = ("movie", "tv_show", "book", "artist", "place", "destination", "podcast", "video_game")
NOTE = "taste service limit reached; showing partial results"
MODELS = ("qwen.qwen3-32b-v1:0", "mistral.mistral-large-3-675b-instruct", "amazon.nova-pro-v1:0")
JSON_CT = "application/json"
_CACHE: collections.OrderedDict = collections.OrderedDict()
_BRK = {"fails": 0, "open_until": 0.0}


def _log(obj: dict) -> None:
    """One JSON line to stdout (never personal data or the key)."""
    print(json.dumps(obj, default=str), flush=True)


# ---------------------------------------------------------------- cache / breaker
def _cache_get(key):
    """LRU+TTL (15 min) lookup; returns None on miss."""
    ent = _CACHE.get(key)
    if ent is None:
        return None
    if time.monotonic() - ent[0] > 900:
        del _CACHE[key]
        return None
    _CACHE.move_to_end(key)
    return ent[1]


def _cache_put(key, val) -> None:
    _CACHE[key] = (time.monotonic(), val)
    _CACHE.move_to_end(key)
    while len(_CACHE) > 256:
        _CACHE.popitem(last=False)


def _breaker_fail() -> None:
    _BRK["fails"] += 1
    if _BRK["fails"] >= 5:
        _BRK["open_until"] = time.monotonic() + 30


# ---------------------------------------------------------------- Bedrock
def bedrock_client_factory():
    """Real Bedrock runtime client (us-east-1, 6 s timeouts)."""
    import boto3
    from botocore.config import Config
    return boto3.client("bedrock-runtime", region_name="us-east-1",
                        config=Config(connect_timeout=6, read_timeout=6, retries={"max_attempts": 1}))


def _text(resp) -> str:
    parts = []
    for c in resp["output"]["message"]["content"]:
        if isinstance(c, str):
            parts.append(c)
        elif isinstance(c, dict) and isinstance(c.get("text"), str):
            parts.append(c["text"])
    return re.sub(r"<think>.*?</think>", "", "\n".join(parts), flags=re.S).strip()


def _converse(client, prompt, accept, deadline=None):
    """Try models in order; return accept(text) of the first usable answer, else None."""
    reasons = []
    try:
        client = client or bedrock_client_factory()
    except Exception as exc:  # no boto3 / credentials: logged, caller falls back
        _log({"event": "llm_unavailable", "reason": type(exc).__name__})
        return None
    for model in MODELS:
        if deadline is not None and time.monotonic() > deadline:
            reasons.append("deadline")
            break
        try:
            r = client.converse(modelId=model,
                                messages=[{"role": "user", "content": [{"text": prompt}]}],
                                inferenceConfig={"maxTokens": 700, "temperature": 0.3})
            out = accept(_text(r))
        except Exception as exc:  # one model failing is expected; try the next
            reasons.append(type(exc).__name__)
            continue
        if out:
            return out
        reasons.append("unusable")
    _log({"event": "llm_fallback", "reasons": reasons})
    return None


def _template(picks, least, members) -> str:
    if not picks:
        return "No option fits everyone in this group; try different likes."
    top = picks[0]
    pm = top.get("per_member") if isinstance(top.get("per_member"), dict) else {}
    s = f"{top.get('name')} is the best fit for {', '.join(members)}."
    nums = {k: v for k, v in pm.items() if isinstance(v, (int, float))}
    if nums:
        s += " Scores: " + ", ".join(f"{k} {v:.2f}" for k, v in nums.items()) + "."
        least = least or min(nums, key=nums.get)
    if least:
        s += f" {least} gives up the most."
    if len(picks) > 1:
        s += f" The best alternative is {picks[1].get('name')}."
    return s


def narrate(picks, least, members, client=None, deadline=None) -> str:
    """3-5 sentence explanation from Bedrock, or a deterministic template."""
    data = json.dumps({"members": members, "least_happy": least,
                       "picks": [{"name": p.get("name"), "per_member": p.get("per_member")}
                                 for p in picks[:5]]})
    prompt = ("Given these ranked group picks with per-member scores (0-1) and who is least happy, "
              "write 3-5 plain sentences explaining why the top pick fits the group, who gives up "
              "most and the best alternative for that person. Only mention names present in the "
              "data.\n" + data)
    out = _converse(client, prompt, lambda t: t if len(t) > 20 else "", deadline)
    return out or _template(picks, least, members)


def parse_titles(text: str) -> list[str]:
    """Parse up to 5 titles from a JSON array (code fences allowed) or a bullet list."""
    t = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S)
    t = re.sub(r"```[a-zA-Z]*", "", t).strip()
    items = None
    m = re.search(r"\[.*\]", t, re.S)
    if m:
        try:
            v = json.loads(m.group(0))
            items = [x for x in v if isinstance(x, str)] if isinstance(v, list) else None
        except ValueError:
            items = None
    if items is None:
        items = [g.group(1) for g in
                 (re.match(r"^\s*(?:[-*\u2022]|\d+[.)])\s+(.+)$", ln) for ln in t.splitlines()) if g]
        items = [x.strip(' "\'`,') for x in items]
    out, seen = [], set()
    for x in items:
        x = x.strip()
        if x and len(x) <= 120 and x.lower() not in seen:
            seen.add(x.lower())
            out.append(x)
    return out[:5]


def baseline(members, kind, client=None, city=None, deadline=None) -> list[str]:
    """Ask one LLM for exactly 5 picks WITHOUT Qloo data; [] if every model fails."""
    lines = [f"- {m['name']}: likes {', '.join(m['likes'])}; dislikes "
             f"{', '.join(m.get('dislikes') or []) or 'none'}" for m in members]
    prompt = (f"Category: {kind}" + (f" in {city}" if city else "") + ".\nGroup members:\n" +
              "\n".join(lines) + "\nPick exactly 5 titles this whole group can enjoy together. "
              "Answer with a JSON array of 5 strings and nothing else.")
    return _converse(client, prompt, parse_titles, deadline) or []


# ---------------------------------------------------------------- Qloo glue
def _extra(req):
    ex = {}
    if req.get("city") and req["kind"] in ("place", "destination"):
        ex["filter.location.query"] = req["city"]
    if req.get("price_max") and req["kind"] == "place":
        ex["filter.price_level.max"] = req["price_max"]
    return ex or None


def _first_id(results):
    for e in results:
        if isinstance(e, dict) and isinstance(e.get("entity_id"), str) and e["entity_id"]:
            return e["entity_id"]
    return None


def _affinity(ent) -> float:
    q = ent.get("query")
    a = q.get("affinity") if isinstance(q, dict) else None
    if isinstance(a, (int, float)) and not isinstance(a, bool):
        return max(0.0, min(1.0, float(a)))
    return 0.5


def _score_candidates(client, members, kind, titles, extra):
    """Score a fixed title list for every member with filter.results.entities."""
    cand, scores, partial = {}, {}, False
    try:
        for t in titles:
            eid = _first_id(client.search_entity(t, types=kind))
            if eid:
                cand[t] = eid
                scores[t] = {}
        for m in members if cand else []:
            seeds = [i for i in (_first_id(client.search_entity(x, types=kind)) for x in m.likes) if i]
            if not seeds:
                for t in cand:
                    scores[t][m.name] = 0.5
                continue
            ents = client.recommend(seeds, kind, take=len(cand), results_filter=list(cand.values()),
                                    extra_params=extra)
            by_id = {e.get("entity_id"): _affinity(e) for e in ents if isinstance(e, dict)}
            for t, eid in cand.items():
                scores[t][m.name] = by_id.get(eid, 0.0)
    except CallLimitError:
        partial = True
    return {"picks": [{"name": t, "matched_id": cand[t], "per_member": scores[t]} for t in cand],
            "partial": partial}


def reconcile(client, req):
    """Adapter over the core library: group picks, or candidate scoring when req['candidates']."""
    members = [Member(m["name"], list(m["likes"]), list(m.get("dislikes") or [])) for m in req["members"]]
    kind, extra = req["kind"], _extra(req)
    if req.get("candidates"):
        return _score_candidates(client, members, kind, req["candidates"], extra)
    options, unresolved, _neutral, partial = _build(client, members, kind, extra)
    learner, by_id = RejectionLearner(), {o.id: o for o in options}
    for r in req.get("rejections") or []:
        if r["option_id"] in by_id:
            learner.record_reject(r["member"], by_id[r["option_id"]])
    ranked = rank_options(options, members, learner)
    fits = [r for r in ranked if not r.excluded]
    picks = [{"id": r.option.id, "name": r.option.name, "score": r.score, "per_member": r.per_member,
              "least_happy": r.tradeoff, "why": r.why} for r in fits[:5]]
    return {"picks": picks, "least_happy": picks[0]["least_happy"] if picks else "",
            "excluded_count": len(ranked) - len(fits), "counterfactual": counterfactual(ranked, members),
            "unresolved": unresolved, "partial": bool(partial)}


def _urlopen(url, headers, params):
    if params:
        url = f"{url}?{urllib.parse.urlencode(params, doseq=True)}"
    rq = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(rq, timeout=8.0) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


_URN = {"video_game": "videogame"}


def _urn_params(params):
    """Qloo wants urn:entity:<kind> for types / filter.type (live API rejects bare kinds)."""
    if not isinstance(params, dict):
        return params
    out = dict(params)
    for k in ("types", "filter.type"):
        v = out.get(k)
        if isinstance(v, str) and v and not v.startswith("urn:"):
            out[k] = "urn:entity:" + _URN.get(v, v)
    return out



def _make_client(st):
    """Build a per-request client with cached http; (None, None) if no key configured."""
    key = os.environ.get("QLOO_API_KEY")
    if not key:
        return None, None
    state = {"hits": 0, "deadline": st["t0"] + 17, "client": None}

    def http(method, url, headers, params):
        if time.monotonic() > state["deadline"]:
            raise CallLimitError("request time budget used")
        params = _urn_params(params)
        ck = ("http", url, json.dumps(params, sort_keys=True, default=str))
        hit = _cache_get(ck)
        if hit is not None:
            state["hits"] += 1
            state["client"].calls -= 1  # a cache hit is not a Qloo call
            return hit
        r = _urlopen(url, headers, params)
        if isinstance(r, dict):
            _cache_put(ck, r)
        return r

    base = os.environ.get("QLOO_BASE_URL", "https://hackathon.api.qloo.com")
    client = QlooClient(base, key, http=http, retries=2, sleep=lambda s: time.sleep(s), timeout=8.0,
                        max_calls=14, max_per_second=4)
    state["client"] = client
    return client, state


def _call(client, r):
    """reconcile() with graceful cap handling: (result, qloo_failed)."""
    try:
        res = reconcile(client, r)
    except CallLimitError:
        return {"picks": [], "partial": True}, False
    except QlooError:
        return None, True
    return (res if isinstance(res, dict) else {}), False


def _pace(client, before, t_start) -> None:
    """Keep the average rate at or below 4 Qloo calls per second."""
    used = int(getattr(client, "calls", 0)) - before
    need = (used - 1) / 4.0 - (time.monotonic() - t_start)
    if used > 1 and need > 0:
        time.sleep(need)


# ---------------------------------------------------------------- validation
def _strs(v, lo, hi, label, who):
    if not isinstance(v, list) or not lo <= len(v) <= hi:
        return f"{who}: {label} must be a list of {lo}-{hi} items"
    if any(not isinstance(x, str) or not x.strip() or len(x) > 80 for x in v):
        return f"{who}: each {label[:-1]} must be 1-80 characters"
    return None


def _validate(body):
    """Return (error, normalised request)."""
    if not isinstance(body, dict):
        return "body must be a JSON object", None
    kind = body.get("kind")
    if kind not in KINDS:
        return "kind must be one of: " + ", ".join(KINDS), None
    ms = body.get("members")
    if not isinstance(ms, list) or not 2 <= len(ms) <= 6:
        return "need 2-6 members", None
    members, names = [], []
    for m in ms:
        if not isinstance(m, dict):
            return "each member must be an object", None
        name = m.get("name")
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 40:
            return "member name must be 1-40 characters", None
        dis = m.get("dislikes") if m.get("dislikes") is not None else []
        err = _strs(m.get("likes"), 1, 5, "likes", "member") or _strs(dis, 0, 5, "dislikes", "member")
        if err:
            return err, None
        names.append(name.strip())
        members.append({"name": name.strip(), "likes": m["likes"], "dislikes": dis})
    if len(set(names)) != len(names):
        return "duplicate member names are not allowed", None
    req = {"kind": kind, "members": members, "rejections": []}
    city, pm = body.get("city"), body.get("price_max")
    if city is not None:
        if not isinstance(city, str) or len(city) > 80:
            return "city must be a string of at most 80 characters", None
        if city.strip():
            req["city"] = city.strip()
    if pm is not None:
        if isinstance(pm, bool) or not isinstance(pm, int) or not 1 <= pm <= 4:
            return "price_max must be an integer 1-4", None
        req["price_max"] = pm
    rej = body.get("rejections") or []
    if not isinstance(rej, list) or len(rej) > 50 or any(
            not isinstance(r, dict) or not isinstance(r.get("member"), str)
            or not isinstance(r.get("option_id"), str) for r in rej):
        return "rejections must be a list of {member, option_id}", None
    req["rejections"] = [{"member": r["member"], "option_id": r["option_id"]} for r in rej]
    return None, req


# ---------------------------------------------------------------- routes
def _fail(st, status, msg, outcome):
    st["outcome"] = outcome
    return status, {"error": msg}, JSON_CT


def _gscore(pm, names):
    vals = [pm.get(n) for n in names]
    if not vals or any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in vals):
        return None
    return round(0.6 * sum(vals) / len(vals) + 0.4 * min(vals), 4)


def _avg(vals):
    vals = [v for v in vals if isinstance(v, (int, float))]
    return round(sum(vals) / len(vals), 4) if vals else None


def _post(route, event, st):
    raw = event.get("body") or ""
    if event.get("isBase64Encoded"):
        raw = base64.b64decode(raw).decode("utf-8", errors="replace")
    if len(raw.encode("utf-8")) > 16 * 1024:
        return _fail(st, 400, "request body size exceeds 16 KB", "bad_request")
    try:
        body = json.loads(raw)
    except ValueError:
        return _fail(st, 400, "body must be valid JSON", "bad_request")
    err, req = _validate(body)
    if err:
        return _fail(st, 400, err, "bad_request")
    st["kind"], st["member_count"] = req["kind"], len(req["members"])
    names = [m["name"] for m in req["members"]]
    key = ("req", json.dumps(req, sort_keys=True))
    cached = _cache_get(key)
    if route == "reconcile" and cached is not None:
        st["outcome"], st["cache_hits"] = "cache_hit", 1
        out = copy.deepcopy(cached)
        out.update(request_id=st["request_id"], qloo_calls=0,
                   elapsed_ms=int((time.monotonic() - st["t0"]) * 1000))
        return 200, out, JSON_CT
    if _BRK["open_until"] > time.monotonic():
        return _fail(st, 503, "taste service busy, retry shortly", "breaker_open")
    client, state = _make_client(st)
    if client is None:
        return _fail(st, 500, "taste service is not configured", "no_key")
    before, t_start = int(getattr(client, "calls", 0)), time.monotonic()
    llm_deadline = st["t0"] + 19
    out = {"request_id": st["request_id"]}
    if route == "reconcile":
        res, failed = _call(client, req)
        if failed:
            _breaker_fail()
            return _fail(st, 502, "taste service request failed", "qloo_error")
        _pace(client, before, t_start)
        picks = [p for p in (res.get("picks") or []) if isinstance(p, dict) and not p.get("excluded")][:5]
        least = res.get("least_happy") if isinstance(res.get("least_happy"), str) else ""
        # a result that does not state its completeness is reported as partial (fail-safe)
        partial = bool(res["partial"]) if "partial" in res else True
        out.update(picks=picks, least_happy=least, why=res.get("why", ""),
                   excluded_count=int(res.get("excluded_count") or 0),
                   counterfactual=res.get("counterfactual") or {}, unresolved=res.get("unresolved") or {},
                   narrative=narrate(picks, least, names, deadline=llm_deadline), partial=partial)
        if partial:
            out["note"] = NOTE
        if res.get("partial") is not True:
            _cache_put(key, copy.deepcopy(out))
    else:
        titles = baseline(req["members"], req["kind"], city=req.get("city"), deadline=llm_deadline)
        res, partial = {"picks": []}, False
        if titles:
            res, failed = _call(client, dict(req, candidates=titles))
            if failed:
                _breaker_fail()
                return _fail(st, 502, "taste service request failed", "qloo_error")
            partial = bool(res.get("partial"))
        by = {str(p.get("name", "")).lower(): p for p in res.get("picks") or [] if isinstance(p, dict)}
        fits = []
        for t in titles:
            p = by.get(t.lower())
            pm = (p.get("per_member") or {}) if p else {}
            fits.append({"name": t, "resolved": p is not None,
                         "group_score": _gscore(pm, names) if p else None, "per_member": pm})
        gbody = _cache_get(key)
        gpicks = gbody["picks"] if gbody else (_call(client, req)[0] or {}).get("picks") or []
        gavg = _avg([p.get("score") if isinstance(p.get("score"), (int, float)) else
                     _gscore(p.get("per_member") or {}, names) for p in gpicks if isinstance(p, dict)])
        bavg = _avg([f["group_score"] for f in fits])
        _pace(client, before, t_start)
        st["extra"] = {"baseline_avg_group_score": bavg, "grounded_avg_group_score": gavg}
        out.update(llm_picks=titles, llm_picks_qloo_fit=fits, llm_avg_group_score=bavg,
                   grounded_avg_group_score=gavg, partial=partial)
        if partial:
            out["note"] = NOTE
        elif not titles:
            out["note"] = "LLM baseline unavailable; no picks to score"
    _BRK["fails"] = 0
    st["qloo_calls"], st["cache_hits"] = max(0, int(getattr(client, "calls", 0)) - before), state["hits"]
    out.update(qloo_calls=st["qloo_calls"], elapsed_ms=int((time.monotonic() - st["t0"]) * 1000))
    st["outcome"] = "partial" if out.get("partial") else "ok"
    return 200, out, JSON_CT


_FALLBACK_PAGE = "<!DOCTYPE html><html><body><h1>Common Ground</h1><p>UI unavailable.</p></body></html>"


def _page() -> str:
    try:
        from qloo_ui import PAGE
        return PAGE
    except ImportError:
        return _FALLBACK_PAGE


def _dispatch(event, st):
    http = (event.get("requestContext") or {}).get("http") or {}
    method, path = str(http.get("method", "")).upper(), http.get("path", "")
    st["route"] = f"{method} {path}"
    if method == "GET" and path == "/":
        return 200, _page(), "text/html; charset=utf-8"
    if method == "GET" and path == "/health":
        return 200, {"ok": True, "version": os.environ.get("APP_VERSION", "dev"),
                     "qloo": "configured" if os.environ.get("QLOO_API_KEY") else "missing"}, JSON_CT
    if method == "POST" and path in ("/api/reconcile", "/api/baseline"):
        return _post(path.rsplit("/", 1)[1], event, st)
    return _fail(st, 404, "not found", "not_found")


def handler(event, context):
    """AWS Lambda Function URL (payload v2) entry point; never raises."""
    st = {"t0": time.monotonic(), "request_id": uuid.uuid4().hex[:12], "route": "?", "kind": None,
          "member_count": 0, "qloo_calls": 0, "cache_hits": 0, "outcome": "ok"}
    try:
        status, payload, ctype = _dispatch(event if isinstance(event, dict) else {}, st)
    except Exception as exc:  # last resort: report class name only, no message
        status, payload, ctype = 500, {"error": "internal error"}, JSON_CT
        st["outcome"] = "error:" + type(exc).__name__
    elapsed = int((time.monotonic() - st["t0"]) * 1000)
    audit = {k: st[k] for k in ("request_id", "route", "kind", "member_count", "qloo_calls",
                                "cache_hits", "outcome")}
    audit.update(elapsed_ms=elapsed, status=status, **st.get("extra", {}))
    _log(audit)
    return {"statusCode": status, "headers": {"Content-Type": ctype},
            "body": payload if isinstance(payload, str) else json.dumps(payload)}
