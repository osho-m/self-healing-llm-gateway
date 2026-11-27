# Security

## Reporting a problem

Report a suspected vulnerability privately to the repository owner. Please do not open a public issue with exploit details.

## How the gateway protects itself

- **Team keys** are stored only as SHA-256 hashes (`api_key_sha256`). Create one with `python scripts/hash_key.py`. The plain-text `api_keys` field exists only for local demos; never use it for real keys.
- **Admin routes** need either the separate `GATEWAY_ADMIN_API_KEY` (checked in constant time, so its length and content can't be guessed from timing) or a team key whose policy `role` is `admin`. Ordinary team keys get `403`. Every admin action goes into an append-only audit log.
- **Production mode** (`GATEWAY_ENVIRONMENT=production`) refuses to start if:
  - Redis is not configured;
  - the admin key is shorter than 24 characters, or looks like a documented placeholder (`demo`, `example`, `change-me`);
  - the policy file contains plain-text `api_keys`. This is checked at startup and on every reload, so only hashes are ever accepted.
- **Model API keys** are read from the environment variables named in the policy (`api_key_env`). They are never logged or returned.
- **Fault injection** on the gateway is admin-only. The mock model's `/fault` endpoint has no login at all, so keep it inside a test network.
- **`/metrics`** has no login, as is usual for Prometheus. Restrict it at the network level.

## Never commit

Model API keys, team keys, `.env` files or customer prompts. `.gitignore` already excludes `.env` and `data/` (where audit logs go).

## Before going to production

- Put admin access behind single sign-on.
- Keep secrets in a managed secret store.
- Serve the gateway over TLS.
- Ship audit logs to storage that cannot be edited.
- Change or remove the demo keys in `config/*.yml`.
