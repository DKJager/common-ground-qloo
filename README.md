# Common Ground (Qloo hackathon)

Common Ground finds what a whole group will enjoy. Each person lists a few things they like and dislike; the service asks the Qloo Insights API how well each candidate fits each person, ranks the options by how well the least-happy member does, shows who gives up the most, and says what the pick would be without each person (the counterfactual). A rejection learner re-ranks when someone vetoes a pick.

Built by 3Dogs Nexus for the Qloo hackathon. Taste data: Qloo Insights API.

## Layout
- `qloo_core6.py`  Qloo client, group reconciliation, ranking, counterfactuals (stdlib only)
- `qloo_svc.py`    AWS Lambda Function URL handler: `GET /`, `GET /health`, `POST /api/reconcile`, `POST /api/baseline`
- `qloo_ui.py`     the single-page UI served at `/`
- `tests/`         pytest suite (mocked Qloo, no network)

## Run the tests
    python -m pip install pytest
    python -m pytest -q tests

## Deploy (AWS Lambda, python3.12)
Zip the three modules, handler `qloo_svc.handler`, timeout 28 s, Function URL auth NONE, reserved concurrency 10.
Set the environment variable `QLOO_API_KEY` at deploy time from your own secret store. Never commit a key.
Optional: `QLOO_BASE_URL` (default `https://hackathon.api.qloo.com`).

Narration and the baseline use Amazon Bedrock Converse (us-east-1); if no model answers, a deterministic template is used. The Lambda role needs `bedrock:InvokeModel`.

## Notes
- Qloo response data is cached privately in memory only (256 entries, 15 min); nothing is stored publicly.
- Each request writes one audit JSON line (no personal data) to stdout.
- The UI labels the baseline comparison "measured with Qloo data, not a claim of ground truth".

License: Apache-2.0 (see LICENSE).
