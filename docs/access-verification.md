# Access verification evidence

The tests use generated RSA keys and signatures, not captured bearer credentials.
Human claims use an audience array, subject and email; service claims use a string
audience, empty subject, no email and common_name. These shapes are recorded in
Mission Control/service/tests/test_access_auth.py and access_auth.py, including
the live service-token observation dated 2026-08-07. The 2026-10-09 Access spike
records one successful human sign-in. Neither source retains the signed JWT.

The owner delegated JWT evidence decisions. We preserve those observed shapes in
synthetic fixtures and exercise the real FastMCP HTTP chain, including tool caller
identity, while avoiding reuse of potentially exposed credentials. We do not
claim that a synthetic signature proves Cloudflare currently issues these claims.
Fresh human and service-token captures and a public JWKS snapshot remain a live
verification gate before moving a server. Capture into protected temporary
storage, never logs or Git; retain only a redacted claim-shape result in records.
The later fleet gate still requires two Beta users and both client surfaces.

Proof-first: the initial tests failed collection because access.py did not exist.
After implementation the signature, issuer/audience/expiry, refresh, allowlist,
exact-health and mixed-settings tests passed. Full tests and lint run before release.

Documentation reconciliation: origin/main at 4ea2aea already contains the fleet
plan and spike result via PRs #5 and #7. The docs/access-test-result tree is
identical to that main. No redundant merge, main write or reset was performed.
v0.2.0 remains the existing dependency-security release; the owner authorized
v0.3.0 for Access mode.

Code review ce-code-review-u11-c02lskk7 completed with nine lenses and a local
adversarial pass. Three confirmed findings were fixed inline: a five-second
fail-closed JWKS outage cooldown now shares failure across queued requests;
Pydantic startup errors hide rejected input values; transport-backed tests cover
timeout, HTTP error, malformed JSON/keys and oversized JWKS without reaching a
handler. Regression tests reproduced the first two defects before fixes. No
implementation-fix attempt failed; expected red regressions are separate evidence.

Final local validation: 54 tests passed on Python 3.12 and 3.13; Ruff passed.
The real HTTP-chain tests include the existing Google service-token mode.
Starlette emits one test-client httpx deprecation warning. No live gate was run.
