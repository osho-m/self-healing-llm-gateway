# Security policy

Do not commit provider keys, team API keys, or customer prompts. Report suspected vulnerabilities privately to the repository owner; do not open a public issue containing exploit details.

Before production use, place the admin API behind separate RBAC authentication, store secrets in a managed secret store, and use the Redis-backed policy implementation described in the README.
