"""LOCAL DEMO ONLY: two opaque server-configured bearer tokens, no public auth."""
import hmac
import os

from fastapi import Header, HTTPException


def get_owner(authorization: str | None = Header(default=None)) -> str:
    tokens = [os.getenv("DEMO_TOKEN_A", ""), os.getenv("DEMO_TOKEN_B", "")]
    if (any(not token.isascii() or any(char.isspace() for char in token)
            or not 32 <= len(token) <= 256 for token in tokens)
            or tokens[0] == tokens[1]):
        raise HTTPException(503, "Local demo identity is not configured.")
    scheme, _, supplied = (authorization or "").partition(" ")
    if scheme.lower() == "bearer" and supplied:
        for token, owner in zip(tokens, ("demo-user-a", "demo-user-b")):
            if hmac.compare_digest(token.encode(), supplied.encode()):
                return owner
    raise HTTPException(401, "Valid local demo bearer token required.", headers={"WWW-Authenticate": "Bearer"})
