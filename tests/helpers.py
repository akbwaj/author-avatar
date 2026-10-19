"""Baseline generation probes each use a fresh authenticated conversation.

The memory API tests use ordinary TestClient and explicit IDs instead, to test
the actual multi-turn request contract without this convenience wrapper.
"""
import os
from uuid import uuid4

from fastapi.testclient import TestClient


class FirstTurnClient(TestClient):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.headers["Authorization"] = "Bearer " + os.environ["DEMO_TOKEN_A"]

    def post(self, url, **kwargs):
        if url == "/chat":
            created = super().post("/conversations")
            if created.status_code != 201:
                return created
            kwargs["json"] = {"conversation_id": created.json()["conversation_id"],
                              "request_id": str(uuid4()), **kwargs.get("json", {})}
        return super().post(url, **kwargs)
