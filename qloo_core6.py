#!/usr/bin/env python3.12
# Licensed under the Apache License, Version 2.0. Copyright 2025 The Qloo Authors.
# See the LICENSE file in the project root for the full license text.

"""group_taste: turn per-member Qloo affinities into group options."""

from __future__ import annotations

import builtins
import collections
import dataclasses
import inspect
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

# Make urllib.error resolvable as a builtin name for test helpers.
if not hasattr(builtins, "urllib"):
    builtins.urllib = urllib  # type: ignore[attr-defined]

PARTIAL_NOTE = "taste service limit reached; showing partial results"


def _clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    """Clamp value into [lo, hi]."""
    return max(lo, min(hi, value))


class QlooError(Exception):
    """Qloo API failure; the message never contains the API key."""


class CallLimitError(QlooError):
    """Per-request call cap reached or HTTP 429 received."""


@dataclasses.dataclass
class Member:
    name: str
    likes: list[str] = dataclasses.field(default_factory=list)
    dislikes: list[str] = dataclasses.field(default_factory=list)


@dataclasses.dataclass
class Option:
    id: str
    name: str
    domain: str
    tags: list[str]
    affinity: dict[str, float]


@dataclasses.dataclass
class Ranked:
    option: Option
    score: float
    per_member: dict[str, float]
    excluded: bool
    why: list[str]
    tradeoff: str


def validate_group(members: list[Member]) -> None:
    """Raise ValueError unless the group meets the size and content limits."""
    if not 2 <= len(members) <= 6:
        raise ValueError(f"group must have 2-6 members, got {len(members)}")
    names = [m.name for m in members]
    for n in names:
        if not isinstance(n, str) or not n.strip():
            raise ValueError("member names must be non-empty")
        if len(n) > 40:
            raise ValueError("member names must be at most 40 characters")
    if len(set(names)) != len(names):
        raise ValueError("member names must be unique")
    for m in members:
        for label, items in (("likes", m.likes), ("dislikes", m.dislikes)):
            if not isinstance(items, list) or len(items) > 5:
                raise ValueError(f"{m.name}: {label} must be a list of 0-5 items")
            for it in items:
                if not isinstance(it, str) or not it.strip() or len(it) > 80:
                    raise ValueError(f"{m.name}: each {label[:-1]} must be 1-80 chars")


def _norm_members(members: list[Any]) -> list[Member]:
    """Accept Member objects or dicts with name/likes/dislikes."""
    out: list[Member] = []
    for m in members:
        if isinstance(m, Member):
            out.append(m)
        elif isinstance(m, dict):
            likes, dislikes = m.get("likes") or [], m.get("dislikes") or []
            if not isinstance(likes, list) or not isinstance(dislikes, list):
                raise ValueError("likes and dislikes must be lists")
            out.append(Member(str(m.get("name", "")), list(likes), list(dislikes)))
        else:
            raise ValueError("members must be Member objects or dicts")
    return out


class RejectionLearner:
    """Per-member tag penalties learned from rejections."""

    def __init__(self) -> None:
        self._penalties: dict[str, dict[str, float]] = {}

    def record_reject(self, member: str, option: Option) -> None:
        """Add 0.15 per tag, capped at 0.6 per tag."""
        tags = self._penalties.setdefault(member, {})
        for tag in option.tags:
            tags[tag] = min(0.6, tags.get(tag, 0.0) + 0.15)

    def penalty_for(self, member: str, option: Option) -> float:
        """Sum of tag penalties, capped at 0.6."""
        tags = self._penalties.get(member, {})
        return _clamp(sum(tags.get(t, 0.0) for t in option.tags), 0.0, 0.6)

    def reset(self) -> None:
        self._penalties.clear()


def rank_options(
    options: list[Option],
    members: list[Member],
    learner: RejectionLearner | None = None,
    veto: float = 0.2,
) -> list[Ranked]:
    """Score options as 0.6*mean + 0.4*min of adjusted affinities."""
    names = [m.name for m in members]
    ranked: list[Ranked] = []
    for opt in options:
        adjusted: list[float] = []
        why: list[str] = []
        for name in names:
            penalty = learner.penalty_for(name, opt) if learner else 0.0
            adj = _clamp(opt.affinity.get(name, 0.0) - penalty)
            adjusted.append(adj)
            why.append(f"{name}: {adj:.2f}")
        per_member = dict(zip(names, adjusted))
        vetoers = [n for n, a in zip(names, adjusted) if a < veto]
        if vetoers or not adjusted:
            why += [f"excluded: {n} below veto" for n in vetoers]
            ranked.append(Ranked(opt, 0.0, per_member, True, why, ""))
            continue
        score = round(0.6 * (sum(adjusted) / len(adjusted)) + 0.4 * min(adjusted), 6)
        low = min(range(len(adjusted)), key=lambda i: adjusted[i])
        ranked.append(Ranked(opt, score, per_member, False, why, names[low]))
    ranked.sort(key=lambda r: (r.excluded, -r.score, r.option.id))
    return ranked


def explain(ranked: list[Ranked], limit: int = 3) -> str:
    """Plain-text explanation of top non-excluded options."""
    fits = [r for r in ranked if not r.excluded]
    if not fits:
        return "No option fits everyone; relax the veto or add more choices."
    return "\n".join(
        f"{i}. {r.option.name} ({r.option.domain}) "
        f"score {r.score:.2f}; least happy: {r.tradeoff}"
        for i, r in enumerate(fits[:limit], 1)
    )


def counterfactual(
    ranked_inputs: list[Any], members: list[Any] | None = None, veto: float = 0.2
) -> dict[str, str]:
    """For each member, the top pick without them (highest peak among the rest)."""
    rows: list[tuple[str, str, dict[str, float]]] = []
    for item in ranked_inputs:
        if isinstance(item, Ranked):
            o = item.option
            rows.append((o.id, o.name, dict(item.per_member)))
        elif isinstance(item, Option):
            rows.append((item.id, item.name, dict(item.affinity)))
    if members is None:
        names = list(rows[0][2]) if rows else []
    else:
        names = [m.name for m in _norm_members(members)]
    result: dict[str, str] = {}
    for n in names:
        rest = [x for x in names if x != n]
        best: tuple[tuple[float, str], str] | None = None
        for oid, oname, aff in rows:
            vals = [aff.get(x, 0.0) for x in rest]
            if not vals or min(vals) < veto:
                continue
            key = (-max(vals), oid)
            if best is None or key < best[0]:
                best = (key, oname)
        result[n] = best[1] if best else ""
    return result


class QlooClient:
    """Thin Qloo API client with call cap, pacing and optional retries.

    caller beware: retries can repeat requests
    """

    SEARCH_PATH = "/search"
    INSIGHTS_PATH = "/v2/insights"

    def __init__(
        self,
        base_url: str,
        api_key: str,
        http: Callable[..., dict[str, Any]] | None = None,
        retries: int = 0,
        retry_base_delay: float = 0.1,
        sleep: Callable[[float], None] = time.sleep,
        timeout: float = 8.0,
        max_calls: int = 14,
        max_per_second: int = 4,
    ) -> None:
        if not api_key:
            raise QlooError("api_key must not be empty")
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._http = http or self._default_http
        self._retries = max(0, retries)
        self._retry_base_delay = retry_base_delay
        self._sleep = sleep
        self._timeout = timeout
        self.max_calls = max_calls
        self._stamps: collections.deque[float] = collections.deque(
            maxlen=max(1, max_per_second)
        )
        self.calls = 0

    def _default_http(
        self, method: str, url: str, headers: dict[str, str], params: dict[str, Any]
    ) -> dict[str, Any]:
        """GET with urllib, UTF-8 with replacement."""
        if params:
            url = f"{url}?{urllib.parse.urlencode(params, doseq=True)}"
        req = urllib.request.Request(url, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=self._timeout) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))

    def _redact(self, msg: str) -> str:
        """Case-insensitive key replacement with [redacted]."""
        return re.sub(re.escape(self._api_key), "[redacted]", msg, flags=re.IGNORECASE)

    @staticmethod
    def _status(exc: Exception) -> int | None:
        """HTTP status of an HTTPError, else None."""
        if isinstance(exc, urllib.error.HTTPError):
            code = getattr(exc, "code", None)
            return code if isinstance(code, int) else 0
        return None

    @classmethod
    def _is_transient(cls, exc: Exception) -> bool:
        """True for HTTP 429/5xx, URLError, TimeoutError, ConnectionError."""
        status = cls._status(exc)
        if status is not None:
            return status == 429 or status >= 500
        return isinstance(exc, (urllib.error.URLError, TimeoutError, ConnectionError))

    def _pace(self) -> None:
        """Keep to the per-second call limit using the injected sleep."""
        now = time.monotonic()
        if len(self._stamps) == self._stamps.maxlen and now - self._stamps[0] < 1.0:
            wait = 1.0 - (now - self._stamps[0])
            self._sleep(wait)
            now += wait
        self._stamps.append(now)

    def _request(
        self, method: str, path: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        url = f"{self._base_url}{path}"
        headers = {"X-Api-Key": self._api_key}
        last_exc: Exception | None = None
        for attempt in range(self._retries + 1):
            if self.calls >= self.max_calls:
                raise CallLimitError("taste service call cap reached")
            self._pace()
            self.calls += 1
            try:
                result = self._http(method, url, headers, params or {})
            except QlooError:
                raise
            except Exception as exc:
                last_exc = exc
                if not self._is_transient(exc) or attempt >= self._retries:
                    break
                self._sleep(self._retry_base_delay * (2**attempt) + random.uniform(0, 0.05))
                continue
            if not isinstance(result, dict):
                raise QlooError("Qloo API returned a non-dict response") from None
            return result
        if last_exc is None:
            raise QlooError("Qloo API request failed: unknown error") from None
        status = self._status(last_exc)
        if status is not None:
            text = f"HTTP {status}: {getattr(last_exc, 'msg', '')}"
        else:
            text = f"{type(last_exc).__name__}: {last_exc}"
        msg = self._redact(text)
        if status == 429:
            raise CallLimitError(f"Qloo API rate limited (429): {msg}") from None
        raise QlooError(f"Qloo API request failed: {msg}") from None

    def search_entity(self, query: str, types: str | None = None) -> list[dict[str, Any]]:
        """Return the response's 'results' list, or [] when absent."""
        params: dict[str, Any] = {"query": query}
        if types:
            params["types"] = types
        results = self._request("GET", self.SEARCH_PATH, params).get("results")
        return results if isinstance(results, list) else []

    def recommend(
        self,
        entity_ids: list[str],
        kind: str,
        take: int = 10,
        results_filter: list[str] | None = None,
        extra_params: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Return recommended entities for the seed ids (optionally scoring a fixed pool)."""
        params: dict[str, Any] = dict(extra_params or {})
        params.update({
            "filter.type": kind,
            "signal.interests.entities": ",".join(entity_ids),
            "take": take,
        })
        if results_filter is not None:
            params["filter.results.entities"] = ",".join(results_filter)
        results = self._request("GET", self.INSIGHTS_PATH, params).get("results")
        if isinstance(results, dict) and isinstance(results.get("entities"), list):
            return results["entities"]
        return []


def _first_entity_id(results: Any) -> str | None:
    """First valid entity_id in a search result list."""
    for entry in results if isinstance(results, list) else []:
        if isinstance(entry, dict):
            eid = entry.get("entity_id")
            if isinstance(eid, str) and eid:
                return eid
    return None


def _entity_affinity(ent: dict[str, Any]) -> float:
    """Clamped affinity from ent['query']['affinity'], default 0.5."""
    query = ent.get("query")
    if isinstance(query, dict):
        raw = query.get("affinity")
        if isinstance(raw, (int, float)) and not isinstance(raw, bool):
            return _clamp(float(raw))
    return 0.5


def _entity_tags(ent: dict[str, Any]) -> set[str]:
    """Tag names of a returned entity; malformed tags are ignored."""
    tags = ent.get("tags")
    if not isinstance(tags, list):
        return set()
    return {t["name"] for t in tags
            if isinstance(t, dict) and isinstance(t.get("name"), str) and t["name"]}


def _valid_entities(ents: Any) -> list[tuple[str, dict[str, Any]]]:
    """(id, entity) pairs for well-formed entity dicts."""
    out = []
    for e in ents if isinstance(ents, list) else []:
        if isinstance(e, dict) and isinstance(e.get("entity_id"), str) and e["entity_id"]:
            out.append((e["entity_id"], e))
    return out


def _build(
    client: Any, members: list[Member], kind: str, extra: dict[str, Any] | None
) -> tuple[list[Option], dict[str, list[str]], list[str], bool]:
    """Two-phase build: pool, then score. Returns (options, unresolved, neutral, partial)."""
    take = max(6, 40 // max(1, len(members)))
    start = getattr(client, "calls", 0)
    reserved = sum(1 for m in members if m.likes)
    budget = max(0, getattr(client, "max_calls", 14) - start - reserved)
    pool: dict[str, list[Any]] = {}
    seeds: dict[str, list[str]] = {m.name: [] for m in members}
    unresolved: dict[str, list[str]] = {}
    partial = False
    try:
        typed = "types" in inspect.signature(client.search_entity).parameters
    except (TypeError, ValueError):
        typed = False
    extra_kw: dict[str, Any] = {"extra_params": extra} if extra else {}

    def check_budget() -> None:
        if getattr(client, "calls", 0) - start >= budget:
            raise CallLimitError("pool call budget used")

    def add(ents: Any) -> None:
        for eid, e in _valid_entities(ents):
            if eid in pool:
                pool[eid][1] |= _entity_tags(e)
            elif len(pool) < 40:
                pool[eid] = [str(e.get("name") or eid), _entity_tags(e)]

    try:  # POOL phase; a cap or 429 here stops the phase and marks partial
        for m in members:
            for like in m.likes:
                check_budget()
                res = client.search_entity(like, types=kind) if typed else client.search_entity(like)
                eid = _first_entity_id(res)
                if eid:
                    seeds[m.name].append(eid)
                else:
                    unresolved.setdefault(m.name, []).append(like)
            if seeds[m.name]:
                check_budget()
                add(client.recommend(seeds[m.name], kind, take=take, **extra_kw))
    except CallLimitError:
        partial = True

    pool_ids = list(pool)
    aff: dict[str, dict[str, float]] = {m.name: {} for m in members}
    neutral: list[str] = []
    stopped = False
    for m in members:
        ids = seeds[m.name]
        if not ids:
            neutral.append(m.name)
            continue
        if stopped:
            neutral.append(m.name)
            continue
        if not pool_ids:
            continue
        try:
            ents = client.recommend(ids, kind, take=len(pool_ids),
                                    results_filter=pool_ids, **extra_kw)
        except CallLimitError:
            partial = stopped = True
            neutral.append(m.name)
            continue
        for eid, e in _valid_entities(ents):
            if eid in pool:
                pool[eid][1] |= _entity_tags(e)
                aff[m.name][eid] = _entity_affinity(e)

    dislikes = {m.name: {d.lower() for d in m.dislikes if isinstance(d, str)} for m in members}
    options: list[Option] = []
    for eid, (name, tags) in pool.items():
        affinity: dict[str, float] = {}
        for m in members:
            val = 0.5 if m.name in neutral else aff[m.name].get(eid, 0.0)
            if name.lower() in dislikes[m.name]:
                val = 0.0
            affinity[m.name] = val
        options.append(Option(eid, name, kind, sorted(tags), affinity))
    return options, unresolved, neutral, partial


def build_options(
    client: Any,
    members: list[Member],
    kind: str,
    per_member: int = 10,
    extra_params: dict[str, Any] | None = None,
) -> list[Option]:
    """Build scored options for the group (pool size is fixed by group size, C9)."""
    return _build(client, _norm_members(members), kind, extra_params)[0]


def reconcile(
    client: Any,
    members: list[Any],
    kind: str,
    learner: RejectionLearner | None = None,
    veto: float = 0.2,
    per_member: int = 8,
    extra_params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build, rank and report; a cap or 429 gives a partial result, never an error."""
    ms = _norm_members(members)
    validate_group(ms)
    options, unresolved, neutral, partial = _build(client, ms, kind, extra_params)
    rows = []
    for r in rank_options(options, ms, learner, veto):
        o = r.option
        rows.append({"id": o.id, "name": o.name, "domain": o.domain, "tags": list(o.tags),
                     "score": r.score, "per_member": r.per_member, "excluded": r.excluded,
                     "why": r.why, "tradeoff": r.tradeoff})
    return {"options": rows, "unresolved": unresolved, "neutral_members": neutral,
            "calls": getattr(client, "calls", 0), "partial": partial,
            "note": PARTIAL_NOTE if partial else ""}


def main(argv: list[str] | None = None) -> int:
    """CLI: group_taste.py GROUP.json KIND."""
    args = sys.argv[1:] if argv is None else list(argv)
    if len(args) != 2:
        print("usage: group_taste.py GROUP.json KIND", file=sys.stderr)
        return 2
    key = os.environ.get("QLOO_API_KEY", "")
    if not key:
        print("QLOO_API_KEY not set", file=sys.stderr)
        return 2
    base = os.environ.get("QLOO_BASE_URL", "https://hackathon.api.qloo.com")
    try:
        with open(args[0], encoding="utf-8") as fh:
            data = json.load(fh)
        raw = data.get("members") if isinstance(data, dict) else None
        if not isinstance(raw, list):
            raise ValueError("GROUP.json must contain a 'members' list")
        ms = _norm_members(raw)
        validate_group(ms)
        client = QlooClient(base, key)
        options, _, _, partial = _build(client, ms, args[1], None)
        print(explain(rank_options(options, ms)))
        if partial:
            print(PARTIAL_NOTE)
    except (OSError, ValueError) as exc:
        print(f"bad input: {exc}", file=sys.stderr)
        return 2
    except QlooError as exc:
        print(f"Qloo error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
