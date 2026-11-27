# Security policy

## Reporting

Report suspected vulnerabilities privately to the repository owner. Do not open a public issue containing exploit details.

## Model

- **Team keys** are stored only as SHA-256 hashes (`api_key_sha256`); generate them with `python scripts/hash_key.py`. The plaintext `api_keys` field exists for local demos and must not be used for real keys.
- **Admin routes** require a separate `GATEWAY_ADMIN_API_KEY` (compared in constant time) or a team key whose policy `role` is `admin`. Standard team keys receive `403`. Every admin action is written to an append-only audit log.
- **Production mode** (`GATEWAY_ENVIRONMENT=production`) refuses to start without Redis and an admin key of at least 24 characters.
- **Provider credentials** are read from environment variables named in the policy (`api_key_env`) and are never logged or returned.
- **Fault-injection endpoints** are admin-only on the gateway. The mock provider's `/fault` endpoint has no authentication and must never be exposed outside a test network.
- `/metrics` is unauthenticated, following Prometheus convention. Restrict it at the network layer.

## Do not commit

Provider keys, team API keys, `.env` files, or customer prompts. `.gitignore` excludes `.env` and `data/` (audit logs).

## Before production

- Put admin access behind SSO/OIDC.
- Store secrets in a managed secret store.
- Terminate TLS in front of the gateway.
- Ship audit logs to immutable storage.
- Rotate the default demo keys in `config/*.yml`, or remove them.
