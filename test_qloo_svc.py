import sys, pathlib, json, os, time, importlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import pytest

# Import the service module *after* the test file has been loaded so we can
# reload it in fixtures when we need a clean global state.
import qloo_svc

# ----------------------------------------------------------------------
# Helper utilities
# ----------------------------------------------------------------------
def lambda_event(method: str, path: str, body: dict | None = None):
    """Construct a minimal Lambda Function URL v2 event."""
    ev = {
        "requestContext": {"http": {"method": method, "path": path}},
        "headers": {"content-type": "application/json"},
        "isBase64Encoded": False,
    }
    if body is not None:
        ev["body"] = json.dumps(body)
    else:
        ev["body"] = ""
    return ev


def invoke(event):
    """Call the handler and return (status_code, json_body)."""
    resp = qloo_svc.handler(event, None)
    status = resp.get("statusCode", resp.get("status"))
    body = resp.get("body", "{}")
    # the handler may already JSON‑encode the body
    try:
        body = json.loads(body) if isinstance(body, str) else body
    except Exception:
        body = {}
    return status, body


# ----------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------
@pytest.fixture(autouse=True)
def env_key(monkeypatch):
    """Ensure a dummy QLOO_API_KEY exists but is never emitted."""
    monkeypatch.setenv("QLOO_API_KEY", "SUPER‑SECRET‑KEY")
    yield
    # restore environment automatically


@pytest.fixture(autouse=True)
def fresh_module(monkeypatch):
    """
    Reload the service module before each test so that global caches,
    circuit‑breaker state, etc. start from a pristine condition.
    """
    importlib.reload(qloo_svc)
    yield
    importlib.reload(qloo_svc)


# ----------------------------------------------------------------------
# Fake implementations injected into the service module
# ----------------------------------------------------------------------
class FakeReconcileError(Exception):
    """Raised by the fake reconcile to simulate a Qloo failure."""


class FakeQlooClient:
    """
    Minimal stand‑in for QlooClient.  It records how many times its
    ``recommend`` method is called – this is what the service code uses
    (via ``reconcile``) to obtain picks.
    """
    calls = 0

    def __init__(self, *_, **__):
        pass

    def recommend(self, *_, **__):
        FakeQlooClient.calls += 1
        # Return a deterministic payload that the handler can work with.
        picks = [
            {
                "name": f"Option{i}",
                "per_member": {"Alice": 0.9 - i * 0.01, "Bob": 0.85 - i * 0.01},
            }
            for i in range(1, 6)
        ]
        return {
            "picks": picks,
            "least_happy": "Bob",
            "why": "Bob is the least happy",
            "counterfactual": {"Alice": "OptionX", "Bob": "OptionY"},
        }


class FakeBedrockClient:
    """Bedrock client that can be toggled to succeed or raise."""

    def __init__(self, succeed: bool = True, raw_response: str = ""):
        self.succeed = succeed
        self.raw_response = raw_response

    def converse(self, *_, **__):
        if not self.succeed:
            raise Exception("Bedrock failure")
        # Mimic the structure returned by the real Bedrock API.
        return {"output": {"message": {"content": [self.raw_response]}}}


# ----------------------------------------------------------------------
# Test cases
# ----------------------------------------------------------------------
def test_input_validation_bad_kind(monkeypatch):
    """Invalid ``kind`` must yield 400."""
    event = lambda_event(
        "POST",
        "/api/reconcile",
        {
            "kind": "invalid_kind",
            "members": [
                {"name": "Alice", "likes": ["comedy"], "dislikes": []},
                {"name": "Bob", "likes": ["drama"], "dislikes": []},
            ],
        },
    )
    # stub out the heavy work – we should never reach it.
    monkeypatch.setattr(qloo_svc, "reconcile", lambda *a, **k: {})
    status, body = invoke(event)
    assert status == 400
    assert "error" in body
    assert "kind" in body["error"]


def test_input_validation_member_counts(monkeypatch):
    """1 member and >6 members must be rejected."""
    for cnt in (1, 7):
        members = [
            {"name": f"U{i}", "likes": ["sci-fi"], "dislikes": []}
            for i in range(cnt)
        ]
        event = lambda_event(
            "POST",
            "/api/reconcile",
            {"kind": "movie", "members": members},
        )
        monkeypatch.setattr(qloo_svc, "reconcile", lambda *a, **k: {})
        status, body = invoke(event)
        assert status == 400
        assert "error" in body
        assert "member" in body["error"]


def test_input_validation_duplicate_names(monkeypatch):
    """Duplicate member names must be rejected."""
    event = lambda_event(
        "POST",
        "/api/reconcile",
        {
            "kind": "movie",
            "members": [
                {"name": "Alice", "likes": ["comedy"], "dislikes": []},
                {"name": "Alice", "likes": ["drama"], "dislikes": []},
            ],
        },
    )
    monkeypatch.setattr(qloo_svc, "reconcile", lambda *a, **k: {})
    status, body = invoke(event)
    assert status == 400
    assert "error" in body
    assert "duplicate" in body["error"].lower()


def test_input_validation_oversize_body(monkeypatch):
    """Body larger than 16 KB must be rejected."""
    big_str = "x" * (16 * 1024 + 1)  # 16 KB + 1 byte
    event = lambda_event(
        "POST",
        "/api/reconcile",
        {
            "kind": "movie",
            "members": [
                {"name": "Alice", "likes": [big_str], "dislikes": []},
                {"name": "Bob", "likes": ["drama"], "dislikes": []},
            ],
        },
    )
    monkeypatch.setattr(qloo_svc, "reconcile", lambda *a, **k: {})
    status, body = invoke(event)
    assert status == 400
    assert "error" in body
    assert "size" in body["error"].lower()


def test_health_never_calls_qloo(monkeypatch):
    """GET /health must not touch the Qloo service."""
    # Track whether the fake client is ever instantiated.
    instantiated = {"called": False}

    class GuardedFakeClient(FakeQlooClient):
        def __init__(self, *a, **k):
            instantiated["called"] = True
            super().__init__(*a, **k)

    monkeypatch.setattr(qloo_svc, "QlooClient", GuardedFakeClient)
    event = lambda_event("GET", "/health")
    status, body = invoke(event)
    assert status == 200
    assert body.get("ok") is True
    # Ensure the guard was never triggered.
    assert instantiated["called"] is False


def test_circuit_breaker_opens_after_failures(monkeypatch):
    """
    After 5 consecutive Qloo failures the service must return 503 with the
    prescribed error message; further calls stay blocked until the timeout.
    """
    # Make ``reconcile`` raise a QlooError each time.
    monkeypatch.setattr(qloo_svc, "QlooError", FakeReconcileError)
    monkeypatch.setattr(qloo_svc, "reconcile", lambda *a, **k: (_ for _ in ()).throw(FakeReconcileError()))

    # Perform 5 failing requests.
    for _ in range(5):
        ev = lambda_event("POST", "/api/reconcile", {"kind": "movie", "members": [
            {"name": "Alice", "likes": ["comedy"], "dislikes": []},
            {"name": "Bob", "likes": ["drama"], "dislikes": []},
        ]})
        status, body = invoke(ev)
        # First five attempts should still surface the underlying error (500)
        # – the exact code is implementation‑defined, but it must NOT be 200.
        assert status != 200

    # Sixth request should be blocked by the circuit‑breaker.
    ev = lambda_event("POST", "/api/reconcile", {"kind": "movie", "members": [
        {"name": "Alice", "likes": ["comedy"], "dislikes": []},
        {"name": "Bob", "likes": ["drama"], "dislikes": []},
    ]})
    status, body = invoke(ev)
    assert status == 503
    assert body.get("error") == "taste service busy, retry shortly"


def test_cache_hit_avoids_second_qloo_call(monkeypatch):
    """
    The first request populates the internal LRU cache; the second
    identical request must not increase the Qloo client call counter.
    """
    # Reset call counter.
    FakeQlooClient.calls = 0
    monkeypatch.setattr(qloo_svc, "QlooClient", FakeQlooClient)
    monkeypatch.setattr(qloo_svc, "reconcile", lambda client, req: client.recommend())

    payload = {
        "kind": "movie",
        "members": [
            {"name": "Alice", "likes": ["comedy"], "dislikes": []},
            {"name": "Bob", "likes": ["drama"], "dislikes": []},
        ],
    }

    ev = lambda_event("POST", "/api/reconcile", payload)
    status1, _ = invoke(ev)
    assert status1 == 200
    first_calls = FakeQlooClient.calls

    ev = lambda_event("POST", "/api/reconcile", payload)
    status2, _ = invoke(ev)
    assert status2 == 200
    second_calls = FakeQlooClient.calls

    # The second request must not increase the call count.
    assert first_calls == 1
    assert second_calls == 1


def test_api_key_is_not_logged(monkeypatch, capsys):
    """The QLOO_API_KEY environment variable must never appear in stdout."""
    monkeypatch.setattr(qloo_svc, "QlooClient", FakeQlooClient)
    monkeypatch.setattr(qloo_svc, "reconcile", lambda client, req: client.recommend())

    ev = lambda_event("POST", "/api/reconcile", {
        "kind": "movie",
        "members": [
            {"name": "Alice", "likes": ["comedy"], "dislikes": []},
            {"name": "Bob", "likes": ["drama"], "dislikes": []},
        ],
    })
    invoke(ev)
    captured = capsys.readouterr()
    assert "SUPER-SECRET-KEY" not in captured.out
    assert "SUPER-SECRET-KEY" not in captured.err


def test_narration_falls_back_to_template(monkeypatch):
    """
    When all Bedrock models fail, ``narrate`` must return a deterministic
    template sentence derived from the picks.
    """
    # Force the real ``narrate`` to use a Bedrock client that always raises.
    failing_bedrock = FakeBedrockClient(succeed=False)

    # Patch the internal factory – the service creates a client via a
    # ``_make_bedrock_client`` helper (if present) or directly via boto3.
    # We replace whatever attribute the module uses to obtain the client.
    # The implementation detail is not public, so we fall back to monkeypatching
    # the Bedrock client class itself.
    monkeypatch.setattr(qloo_svc, "bedrock_client_factory", lambda: failing_bedrock)

    # Use the normal fake Qloo client so the handler reaches the narration step.
    monkeypatch.setattr(qloo_svc, "QlooClient", FakeQlooClient)
    monkeypatch.setattr(qloo_svc, "reconcile", lambda client, req: client.recommend())

    ev = lambda_event("POST", "/api/reconcile", {
        "kind": "movie",
        "members": [
            {"name": "Alice", "likes": ["comedy"], "dislikes": []},
            {"name": "Bob", "likes": ["drama"], "dislikes": []},
        ],
    })
    status, body = invoke(ev)
    assert status == 200
    narrative = body.get("narrative")
    assert isinstance(narrative, str) and len(narrative) > 0
    # The deterministic template must mention the top pick name.
    assert "Option1" in narrative


def test_baseline_parsing_handles_code_fences(monkeypatch):
    """Baseline must correctly parse a Bedrock response wrapped in markdown fences."""
    raw_response = "```json\n[\"Movie A\",\"Movie B\",\"Movie C\",\"Movie D\",\"Movie E\"]\n```"

    # Bedrock client that returns the above raw string.
    bedrock = FakeBedrockClient(succeed=True, raw_response=raw_response)
    monkeypatch.setattr(qloo_svc, "bedrock_client_factory", lambda: bedrock)

    # Fake Qloo scoring – simply echo a constant score for each title.
    class ScoringClient(FakeQlooClient):
        def recommend(self, *_, **__):
            # Return empty picks; the handler will call back into ``reconcile``
            # for each title to obtain a group score.  We'll stub ``reconcile``
            # directly.
            return {}

    monkeypatch.setattr(qloo_svc, "QlooClient", ScoringClient)

    # Stub ``reconcile`` to produce a deterministic group score for any title.
    def fake_reconcile(client, req):
        # The request will include a ``candidates`` field with the titles.
        titles = req.get("candidates", [])
        # Produce a per‑member score of 0.5 for each title.
        return {
            "picks": [{"name": t, "per_member": {"Alice": 0.5, "Bob": 0.5}} for t in titles[:5]],
            "least_happy": "Bob",
            "why": "Bob is the least happy",
        }

    monkeypatch.setattr(qloo_svc, "reconcile", fake_reconcile)

    ev = lambda_event("POST", "/api/baseline", {
        "kind": "movie",
        "members": [
            {"name": "Alice", "likes": ["comedy"], "dislikes": []},
            {"name": "Bob", "likes": ["drama"], "dislikes": []},
        ],
    })
    status, body = invoke(ev)
    assert status == 200
    llm_picks = body.get("llm_picks")
    assert isinstance(llm_picks, list) and len(llm_picks) == 5
    # Ensure the picks are exactly the ones we placed in the fenced JSON.
    assert llm_picks == ["Movie A", "Movie B", "Movie C", "Movie D", "Movie E"]


def test_rate_limit_cap_and_partial_response(monkeypatch):
    """
    Enforce the hard cap of 14 Qloo calls per request.
    When the cap is exceeded (simulated by a 429 response from the fake client),
    the service must return a partial result with the proper flag and note.
    """
    # A client that counts calls and raises a 429 after the 14th.
    class RateLimitedClient(FakeQlooClient):
        def recommend(self, *_, **__):
            if FakeQlooClient.calls >= 14:
                raise qloo_svc.CallLimitError("429 Too Many Requests")
            return super().recommend(*_, **__)

    monkeypatch.setattr(qloo_svc, "QlooClient", RateLimitedClient)
    # Stub ``reconcile`` to simply forward the client call.
    monkeypatch.setattr(qloo_svc, "reconcile", lambda client, req: client.recommend())

    payload = {
        "kind": "movie",
        "members": [
            {"name": "Alice", "likes": ["comedy"], "dislikes": []},
            {"name": "Bob", "likes": ["drama"], "dislikes": []},
        ],
    }

    ev = lambda_event("POST", "/api/reconcile", payload)
    status, body = invoke(ev)

    # Because our fake client raises after the 14th call, the handler should
    # produce a partial result rather than an exception.
    assert status == 200
    assert body.get("partial") is True
    assert body.get("note") == "taste service limit reached; showing partial results"
    # The response must still contain the fields that a normal response would have.
    assert "picks" in body
    assert isinstance(body["picks"], list)


def test_pacing_injects_sleep(monkeypatch):
    """
    The service must not exceed 4 Qloo calls per second.
    We replace ``time.sleep`` with a spy to confirm it is called when the
    rate‑limit logic is exercised.
    """
    sleep_calls = []

    def fake_sleep(seconds):
        sleep_calls.append(seconds)

    monkeypatch.setattr(time, "sleep", fake_sleep)

    # Use a client that pretends to make many rapid calls.
    class FastClient(FakeQlooClient):
        def recommend(self, *_, **__):
            # Simulate three quick calls per request.
            for _ in range(3):
                FakeQlooClient.calls += 1
            return super().recommend()

    monkeypatch.setattr(qloo_svc, "QlooClient", FastClient)
    monkeypatch.setattr(qloo_svc, "reconcile", lambda client, req: client.recommend())

    ev = lambda_event("POST", "/api/reconcile", {
        "kind": "movie",
        "members": [
            {"name": "Alice", "likes": ["comedy"], "dislikes": []},
            {"name": "Bob", "likes": ["drama"], "dislikes": []},
        ],
    })
    invoke(ev)

    # Verify that our fake ``sleep`` was invoked (the exact number is
    # implementation‑dependent, but at least once means pacing was applied).
    assert len(sleep_calls) >= 1
    # All sleep calls should be non‑negative.
    assert all(s >= 0 for s in sleep_calls)
