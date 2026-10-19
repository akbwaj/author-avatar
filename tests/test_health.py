from fastapi.testclient import TestClient

from app.main import app


def test_health_returns_successful_json_contract():
    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert response.json() == {"status": "ok"}
