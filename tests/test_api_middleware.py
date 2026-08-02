# -*- coding: utf-8 -*-
"""Tests for b2b_ai.api.middleware — request size limit middleware."""
import os
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from b2b_ai.api.middleware import install_request_size_limit, _get_max_bytes


@pytest.fixture()
def app():
    a = FastAPI()
    install_request_size_limit(a, max_bytes=1024)  # 1 KB for testing

    @a.post("/data")
    async def receive_data():
        return {"ok": True}

    @a.get("/health")
    async def health():
        return {"status": "ok"}

    @a.get("/health/detailed")
    async def health_detail():
        return {"status": "ok", "detail": True}

    return a


@pytest.fixture()
def client(app):
    return TestClient(app)


# --- Basic functionality ---

class TestRequestSizeLimit:
    def test_small_payload_accepted(self, client):
        """A payload under the limit should be accepted."""
        r = client.post("/data", content=b"x" * 500,
                        headers={"Content-Type": "application/octet-stream"})
        assert r.status_code == 200
        assert r.json()["ok"] is True

    def test_oversized_payload_rejected_413(self, client):
        """A payload exceeding the limit should return 413."""
        r = client.post("/data", content=b"x" * 2000,
                        headers={"Content-Type": "application/octet-stream"})
        assert r.status_code == 413
        body = r.json()
        assert "too large" in body["detail"].lower()
        assert "1" in body["detail"]  # mentions 1 MB (our test limit)

    def test_exact_limit_payload_accepted(self, client):
        """A payload exactly at the limit should be accepted."""
        r = client.post("/data", content=b"x" * 1024,
                        headers={"Content-Type": "application/octet-stream"})
        assert r.status_code == 200

    def test_health_endpoint_exempt(self, client):
        """/health should be exempt from size limits."""
        r = client.get("/health")
        assert r.status_code == 200

    def test_health_detailed_endpoint_exempt(self, client):
        """/health/detailed should be exempt from size limits."""
        r = client.get("/health/detailed")
        assert r.status_code == 200

    def test_get_request_always_allowed(self, client):
        """GET requests should always pass regardless of Content-Length."""
        r = client.get("/data", headers={"Content-Length": "999999999"})
        # GET /data is not defined but middleware should not block it with 413
        # It will 404, not 413
        assert r.status_code != 413

    def test_malformed_content_length_allowed(self, client):
        """Malformed Content-Length should be passed through (not 413)."""
        r = client.post("/data", content=b"small",
                        headers={"Content-Length": "not-a-number"})
        # Should not crash; may succeed or fail downstream
        assert r.status_code in (200, 400, 422)

    def test_chunked_oversized_rejected(self, client):
        """Chunked transfer (no Content-Length) with oversized body → 413."""
        # When no Content-Length header, middleware reads body and checks size
        r = client.post("/data", content=b"x" * 2000,
                        headers={"Transfer-Encoding": "chunked"})
        assert r.status_code == 413

    def test_env_override(self, monkeypatch):
        """B2B_MAX_REQUEST_SIZE_MB env var should override the default."""
        monkeypatch.setenv("B2B_MAX_REQUEST_SIZE_MB", "1")
        assert _get_max_bytes() == 1 * 1024 * 1024

    def test_env_override_invalid_ignored(self, monkeypatch):
        """Invalid B2B_MAX_REQUEST_SIZE_MB should fall back to default."""
        monkeypatch.setenv("B2B_MAX_REQUEST_SIZE_MB", "abc")
        assert _get_max_bytes() == 10 * 1024 * 1024

    def test_env_override_zero_ignored(self, monkeypatch):
        """Zero B2B_MAX_REQUEST_SIZE_MB should fall back to default."""
        monkeypatch.setenv("B2B_MAX_REQUEST_SIZE_MB", "0")
        assert _get_max_bytes() == 10 * 1024 * 1024
