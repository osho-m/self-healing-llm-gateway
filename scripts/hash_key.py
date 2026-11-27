"""Print the SHA-256 hash of an API key for `api_key_sha256` in the policy file.

python scripts/hash_key.py                # generates a new random key and its hash
python scripts/hash_key.py <existing-key>
"""

from __future__ import annotations

import hashlib
import secrets
import sys

key = sys.argv[1] if len(sys.argv) > 1 else "gw-" + secrets.token_urlsafe(32)
if len(sys.argv) == 1:
    print(f"key:    {key}  (give this to the team; do not commit it)")
print(f"sha256: {hashlib.sha256(key.encode()).hexdigest()}")
