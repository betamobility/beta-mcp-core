# Changelog

## v0.2.0

- Lift the `starlette<1` cap: the package now requires `starlette>=1.0.1,<2`. Starlette
  1.0.1 is the first release with the fix for GHSA-86qp-5c8j-p5mr (CVE-2026-48710, missing
  Host header validation).
- Require `fastmcp>=3.4.8,<4` (was `>=3.1`). fastmcp from 3.4.1 needs `starlette>=1.0.1`,
  which the old cap made unreachable, so every server on v0.1.1 was held to fastmcp 3.4.0 or
  lower. GHSA-rww4-4w9c-7733 (CVE-2026-27124, OAuth proxy consent) is fixed from 3.2.0.
- No change to the package's own code or API. A member repo that pins `fastmcp` below 3.4.8
  or `starlette` below 1 must raise its own pin before moving to this tag, and can drop any
  `starlette` override it carried.
- Tests run on fastmcp 3.4.8 and Starlette 1.7.0. Starlette's test client warns that using it
  with `httpx` is deprecated in favour of `httpx2`; the dev dependency is unchanged here.

## v0.1.1

- Allow shared ASGI scaffold consumers to pass through FastMCP `json_response`
  and `stateless_http` options.

## v0.1.0

- Initial shared MCP auth/scaffold package.
