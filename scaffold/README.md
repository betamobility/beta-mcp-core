# Access-mode container scaffold

Copy `docker-entrypoint.sh` and adapt the payload and command in
`Dockerfile.template`. The container starts as appuser and uses no gosu,
FASTMCP_HOME, OAuth volume or Google credentials.

Set CF_ACCESS_TEAM_DOMAIN and exactly one CF_ACCESS_AUD. Configure
CF_ACCESS_SERVICE_TOKENS as a JSON array of allowed service-token client IDs,
from the same policy manifest used by Terraform. An empty array denies all
machine identities. Never place token secrets in that list.

Create FastMCP with `auth=build_auth_provider(settings)` and pass `settings`
to `build_asgi_app`, or use the shared `run` helper. Access mode requires
stateless HTTP. Only the literal GET/HEAD /health URL is unauthenticated.

This template is for Access migrations. Existing Google-mode deployments retain
the v0.2.0 volume/entrypoint setup until their planned auth cutover; changing only
their dependency pin does not change those deployed artifacts.
