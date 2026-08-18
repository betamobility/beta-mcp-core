# Deploy scaffold (Railway)

Canonical deploy artifacts for Beta MCP servers. New servers copy
`docker-entrypoint.sh` verbatim and adapt `Dockerfile.template`; existing
servers were migrated 2026-08-18 (beta-os, folio, poweroffice, radar, scout,
drop, photos; pulse runs Nixpacks/root with volume + `FASTMCP_HOME` only).

## Why

fastmcp's OAuthProxy stores dynamic client registrations and tokens in an
encrypted FileTreeStore under `settings.home` — container-local by default, so
every Railway deploy silently disconnected every connected claude.ai user.
Mounting a volume and pointing `FASTMCP_HOME` at it makes connector
authorizations survive deploys. The Fernet key derives from `JWT_SIGNING_KEY`
(a stable env var), so no migration is needed when moving the directory.

## Rollout order (the order IS the safety property)

1. **Attach the volume** — `railway service <name>` then `railway volume add
   -m /data` (restarts the service; `-s` on volume subcommands panics CLI
   ≤5.41).
2. **Ship the image** with entrypoint + gosu + appuser, `FASTMCP_HOME` still
   unset — behavior-neutral deploy.
3. **Set `FASTMCP_HOME=/data/fastmcp`** — final deploy; users reauthorize
   once, never again for deploys.

Inverting 2 and 3 is a production outage, not a failed deploy: the volume
attach has already restarted the service, and boot fails closed on the
root-owned mount.

## Acceptance test (DCR persistence probe)

`/register` is unauthenticated per RFC 7591:

```bash
curl -s https://<server>/register -H 'Content-Type: application/json' -d '{
  "client_name": "persistence-probe",
  "redirect_uris": ["https://claude.ai/api/mcp/auth_callback"],
  "grant_types": ["authorization_code", "refresh_token"],
  "token_endpoint_auth_method": "none"
}'
# → client_id. Then exchange a bogus code (PKCE verifier required):
curl -s https://<server>/token -d 'grant_type=authorization_code&code=bogus&client_id=<id>&redirect_uri=https://claude.ai/api/mcp/auth_callback&code_verifier=bogus-verifier-bogus-verifier-bogus-verifier-bogus'
```

`invalid_grant` = client known. Redeploy, re-probe the same client_id:
`invalid_grant` again = state survived; `invalid_client` = still ephemeral.
Servers that restrict DCR redirect URIs reject `example.com` — use the
claude.ai callback above.

## Traps

- Never set `RAILWAY_RUN_UID` alongside this entrypoint (gosu dies with
  "operation not permitted").
- `railway redeploy` rebuilds the deployed snapshot — it never ships new code.
- `railway variable delete --skip-deploys` silently no-ops; delete without the
  flag and re-read the list. Setting a variable to empty string is not
  deletion.

Full incident write-up: claude-config
`docs/solutions/integration-issues/railway-volume-fastmcp-oauth-persistence.md`.
