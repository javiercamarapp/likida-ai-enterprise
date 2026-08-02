# -*- coding: utf-8 -*-
"""Regression tests for enterprise auth, ERP truthfulness and review gates."""
from __future__ import annotations

import asyncio

from b2b_ai.api.auth import APIKeyAuth
from b2b_ai.api.app import create_app
from b2b_ai.db.db import Database
from b2b_ai.features.bookkeeping import erp_registrar as erp_module
from b2b_ai.features.bookkeeping.erp_registrar import ERPRegistrar
from b2b_ai.features.bookkeeping.models import (
    ERPSystem,
    LineaPoliza,
    PolizaContable,
)
from b2b_ai.features.bookkeeping.pipeline import PipelineOrchestrator
from b2b_ai.computer_use.driver_adapter import AsyncPlaywrightDriverAdapter


def _balanced_poliza() -> PolizaContable:
    return PolizaContable(
        fecha="2026-08-02",
        tenant_id="1",
        cuadrada=True,
        total_debe=100.0,
        total_haber=100.0,
        lineas=[
            LineaPoliza(cuenta="600000", debe=100.0, haber=0.0),
            LineaPoliza(cuenta="200000", debe=0.0, haber=100.0),
        ],
    )


def test_service_key_and_db_tenant_keys_coexist(tmp_path, monkeypatch):
    monkeypatch.setenv("B2B_API_KEY", "service-master-key")
    db = Database(str(tmp_path / "auth.db"))
    tenant_id = db.create_tenant("Tenant A")
    db.create_api_key(tenant_id, "tenant", "tenant-key")

    auth = APIKeyAuth(db)
    assert auth.validate("service-master-key") is True
    assert auth.validate("tenant-key") is True
    assert auth.get_tenant_id("service-master-key") is None
    assert auth.get_tenant_id("tenant-key") == tenant_id


def test_canonical_pipeline_route_is_mounted(tmp_path, monkeypatch):
    monkeypatch.setenv("B2B_API_KEY", "service-master-key")
    app = create_app(Database(str(tmp_path / "routes.db")))
    assert "/api/v1/pipeline/run" in app.openapi()["paths"]


def test_erp_rejection_cannot_be_converted_to_fake_success(monkeypatch):
    class RejectingAdapter:
        def register_invoice(self, _payload):
            return {"ok": False, "message": "selector not found"}

    monkeypatch.setattr(
        erp_module, "_build_erp_adapter", lambda _tenant_id: RejectingAdapter()
    )
    registrar = ERPRegistrar(erp_system=ERPSystem.CONTPAQI)
    result = registrar.register(_balanced_poliza())

    assert result.success is False
    assert result.erp_reference is None
    assert "selector not found" in (result.error or "")


def test_erp_success_requires_a_verifiable_reference(monkeypatch):
    class AmbiguousAdapter:
        def register_invoice(self, _payload):
            return {"ok": True, "message": "saved maybe"}

    monkeypatch.setattr(
        erp_module, "_build_erp_adapter", lambda _tenant_id: AmbiguousAdapter()
    )
    registrar = ERPRegistrar(erp_system=ERPSystem.ASPEL)
    result = registrar.register(_balanced_poliza())

    assert result.success is False
    assert "without a verifiable reference" in (result.error or "")


def test_human_review_gate_never_calls_erp():
    class LowConfidenceClassifier:
        def predict(self, _cfdi):
            return "otros", 0.05

        def get_suggestions(self, _cfdi):
            return []

    class ERPSpy:
        def __init__(self):
            self.calls = 0

        def register_batch(self, _polizas):
            self.calls += 1
            raise AssertionError("ERP must not run before human review")

        def get_status(self):
            return {"erp_system": "spy", "registered_count": 0}

    erp = ERPSpy()
    pipeline = PipelineOrchestrator(
        classifier=LowConfidenceClassifier(),
        erp_registrar=erp,
    )
    job = pipeline.process_cfdis(
        [{
            "uuid": "review-1",
            "descripcion": "operación ambigua",
            "subtotal": 100.0,
            "iva": 16.0,
            "total": 116.0,
            "tipo": "I",
        }],
        tenant_id="tenant-a",
        auto_register_erp=True,
    )

    assert job.overrides_needed == 1
    assert job.erp_references == []
    assert erp.calls == 0


def test_sync_computer_use_adapter_works_inside_running_event_loop():
    class Desktop:
        async def close(self):
            return None

    class Legacy:
        def __init__(self):
            self.session = None
            self.desktop = Desktop()
            self.closed = False

        async def connect(self):
            return {"ok": True, "message": "connected"}

        def close(self):
            self.closed = True

    legacy = Legacy()
    adapter = AsyncPlaywrightDriverAdapter(legacy, "test-erp")

    async def call_sync_contract_from_asgi_loop():
        return adapter.connect()

    try:
        result = asyncio.run(call_sync_contract_from_asgi_loop())
        assert result.ok is True
    finally:
        adapter.close()
    assert legacy.closed is True
