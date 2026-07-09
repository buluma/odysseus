"""get_embedding_client() and the /endpoint save route must not write the
decrypted (or plaintext) embedding API key into the process environment —
os.environ is process-wide and outlives the request, so a secret placed
there is readable via /proc/self/environ or by any subprocess the app
spawns for the rest of the process's life. URL/model aren't secret and are
still allowed in env; only the key is scoped out.
"""
import json
import os

import pytest

from src.secret_storage import encrypt


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("EMBEDDING_API_KEY", raising=False)
    monkeypatch.delenv("EMBEDDING_URL", raising=False)
    monkeypatch.delenv("EMBEDDING_MODEL", raising=False)
    yield
    monkeypatch.delenv("EMBEDDING_API_KEY", raising=False)


def test_get_embedding_client_does_not_leak_key_into_env(tmp_path, monkeypatch):
    import src.embeddings as embeddings

    endpoint_file = tmp_path / "embedding_endpoint.json"
    endpoint_file.write_text(json.dumps({
        "url": "http://localhost:11434/v1/embeddings",
        "model": "test-model",
        "api_key": encrypt("super-secret-key"),
    }))
    monkeypatch.setattr(embeddings, "EMBEDDING_ENDPOINT_FILE", str(endpoint_file))
    embeddings.reset_http_embed_state()

    captured = {}

    class _FakeHttpClient:
        def __init__(self, url=None, model=None, api_key=None):
            captured["api_key"] = api_key
            self.url = url
            self.model = model

        def get_sentence_embedding_dimension(self):
            return 8

    monkeypatch.setattr(embeddings, "EmbeddingClient", _FakeHttpClient)

    client = embeddings.get_embedding_client()

    assert captured["api_key"] == "super-secret-key"
    assert os.environ.get("EMBEDDING_API_KEY") is None
    # URL/model aren't secret — fine for other code to read them from env.
    assert os.environ.get("EMBEDDING_URL") == "http://localhost:11434/v1/embeddings"


def test_set_endpoint_route_does_not_leak_key_into_env(monkeypatch, tmp_path):
    import httpx
    import routes.embedding_routes as embedding_routes

    monkeypatch.setattr(embedding_routes, "_ENDPOINT_FILE", str(tmp_path / "embedding_endpoint.json"))
    monkeypatch.setattr(embedding_routes, "require_admin", lambda r: None)

    class _FakeResp:
        def raise_for_status(self):
            pass

    monkeypatch.setattr(httpx, "post", lambda *a, **kw: _FakeResp())

    router = embedding_routes.setup_embedding_routes()
    route = next(r for r in router.routes if r.path == "/api/embeddings/endpoint" and "POST" in r.methods)

    result = route.endpoint(url="http://127.0.0.1:11434/v1/embeddings", model="", api_key="super-secret-key")

    assert result["success"] is True
    assert os.environ.get("EMBEDDING_API_KEY") is None
