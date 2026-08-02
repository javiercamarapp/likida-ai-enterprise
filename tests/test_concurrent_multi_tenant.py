# -*- coding: utf-8 -*-
"""Tests for multi-tenant concurrency isolation.

Validates that concurrent operations across multiple tenants do not
leak data between tenants (race conditions, database locks, etc.).
"""
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

import pytest

from b2b_ai.db.db import Database


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_db():
    """Create a fresh in-memory DB for testing."""
    return Database()


def _sample_datos(i, emisor="EKU9003173C9", archivo=None):
    """Build a minimal datos dict for insert_invoice."""
    return {
        "archivo": archivo or f"invoice_{i}.xml",
        "fecha": "2024-07-15",
        "tipo": "egreso",
        "serie": "A",
        "folio": str(i),
        "folio_fiscal": f"UUID-{i:04d}",
        "emisor_rfc": emisor,
        "emisor_nombre": f"Empresa {i}",
        "receptor_rfc": "XAXX010101000",
        "subtotal": 100.0 + i,
        "iva": 16.0,
        "total": 116.0 + i,
        "moneda": "MXN",
    }


_CLASIF = {"categoria": "gasto_operativo", "confianza": 0.95}
_VALIDACION_OK = {"ok": True, "issues": [], "requires_human_review": False}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestConcurrentTenantCreation:
    """Multiple threads creating tenants simultaneously."""

    def test_concurrent_tenant_creation_no_crash(self):
        """Creating N tenants in parallel should not crash or duplicate."""
        db = _make_db()
        errors = []
        tenant_ids = []
        lock = threading.Lock()

        def create_tenant(i):
            try:
                tid = db.create_tenant(name=f"tenant_{i}", rfc=f"XAXX01010100{i % 10}")
                with lock:
                    tenant_ids.append(tid)
            except Exception as e:
                with lock:
                    errors.append(str(e))

        with ThreadPoolExecutor(max_workers=10) as pool:
            futures = [pool.submit(create_tenant, i) for i in range(20)]
            for f in as_completed(futures):
                f.result()

        assert len(errors) == 0, f"Errors during concurrent creation: {errors}"
        assert len(tenant_ids) == 20
        # All IDs should be unique
        assert len(set(tenant_ids)) == 20


class TestConcurrentInvoiceInsert:
    """Multiple threads inserting invoices for the same tenant."""

    def test_concurrent_insert_same_tenant(self):
        """N threads inserting invoices to the same tenant should not lose data."""
        db = _make_db()
        tid = db.create_tenant(name="shared_tenant", rfc="XAXX010101000")

        errors = []
        inserted = []
        lock = threading.Lock()

        def insert_invoice(i):
            try:
                inv_id, was_new = db.insert_invoice(
                    tenant_id=tid,
                    datos=_sample_datos(i),
                    clasif=_CLASIF,
                    validacion=_VALIDACION_OK,
                )
                with lock:
                    inserted.append(inv_id)
            except Exception as e:
                with lock:
                    errors.append(str(e))

        with ThreadPoolExecutor(max_workers=10) as pool:
            futures = [pool.submit(insert_invoice, i) for i in range(30)]
            for f in as_completed(futures):
                f.result()

        assert len(errors) == 0, f"Errors: {errors}"
        assert len(inserted) == 30


class TestMultiTenantDataIsolation:
    """Verify that concurrent reads/writes don't leak data across tenants."""

    def test_no_data_leak_between_tenants(self):
        """Invoices from tenant A should never appear in tenant B's results."""
        db = _make_db()
        tA = db.create_tenant(name="tenant_A", rfc="AAA010101AAA")
        tB = db.create_tenant(name="tenant_B", rfc="BBB010101BBB")

        # Insert 10 invoices for each tenant
        for i in range(10):
            db.insert_invoice(tenant_id=tA,
                              datos=_sample_datos(i, emisor="AAA010101AAA", archivo=f"A_{i}.xml"),
                              clasif=_CLASIF, validacion=_VALIDACION_OK)
            db.insert_invoice(tenant_id=tB,
                              datos=_sample_datos(i, emisor="BBB010101BBB", archivo=f"B_{i}.xml"),
                              clasif=_CLASIF, validacion=_VALIDACION_OK)

        isolation_errors = []

        def read_and_verify(tenant_id, expected_prefix, iterations=20):
            for _ in range(iterations):
                invoices = db.list_invoices(tenant_id=tenant_id)
                for inv in invoices:
                    archivo = inv.get("archivo", "")
                    if not archivo.startswith(expected_prefix):
                        isolation_errors.append(
                            f"Tenant {tenant_id} saw foreign invoice: {archivo}"
                        )

        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [
                pool.submit(read_and_verify, tA, "A_"),
                pool.submit(read_and_verify, tB, "B_"),
                pool.submit(lambda: [
                    db.insert_invoice(
                        tenant_id=tA,
                        datos=_sample_datos(100 + i, emisor="AAA010101AAA", archivo=f"A_new_{i}.xml"),
                        clasif=_CLASIF, validacion=_VALIDACION_OK,
                    ) for i in range(5)
                ]),
                pool.submit(lambda: [
                    db.insert_invoice(
                        tenant_id=tB,
                        datos=_sample_datos(200 + i, emisor="BBB010101BBB", archivo=f"B_new_{i}.xml"),
                        clasif=_CLASIF, validacion=_VALIDACION_OK,
                    ) for i in range(5)
                ]),
            ]
            for f in as_completed(futures):
                try:
                    f.result()
                except Exception:
                    pass

        assert len(isolation_errors) == 0, "\n".join(isolation_errors)

    def test_concurrent_stats_no_crash(self):
        """Concurrent stats calls should not crash or return mixed data."""
        db = _make_db()
        tA = db.create_tenant(name="stats_A", rfc="SSA010101SSA")
        tB = db.create_tenant(name="stats_B", rfc="SSB010101SSB")

        # Insert different amounts
        for i in range(5):
            db.insert_invoice(tenant_id=tA,
                              datos=_sample_datos(i, emisor="SSA010101SSA", archivo=f"a_{i}.xml"),
                              clasif=_CLASIF, validacion=_VALIDACION_OK)
        for i in range(3):
            db.insert_invoice(tenant_id=tB,
                              datos=_sample_datos(i, emisor="SSB010101SSB", archivo=f"b_{i}.xml"),
                              clasif=_CLASIF, validacion=_VALIDACION_OK)

        errors = []

        def get_stats(tenant_id, expected_count):
            try:
                stats = db.invoice_stats(tenant_id=tenant_id)
                if stats["total_facturas"] != expected_count:
                    errors.append(
                        f"Tenant {tenant_id}: expected {expected_count}, "
                        f"got {stats['total_facturas']}"
                    )
            except Exception as e:
                errors.append(f"Stats error for {tenant_id}: {e}")

        with ThreadPoolExecutor(max_workers=6) as pool:
            futures = []
            for _ in range(10):
                futures.append(pool.submit(get_stats, tA, 5))
                futures.append(pool.submit(get_stats, tB, 3))
            for f in as_completed(futures):
                f.result()

        assert len(errors) == 0, "\n".join(errors)


class TestNoSQLiteLocked:
    """Verify that heavy concurrent writes don't cause 'database is locked'."""

    def test_no_database_locked_under_load(self):
        """50 concurrent writes should not cause SQLite lock errors."""
        db = _make_db()
        tid = db.create_tenant(name="load_tenant", rfc="LOA010101LOA")

        lock_errors = []
        success_count = []
        lock = threading.Lock()

        def write(i):
            try:
                db.insert_invoice(
                    tenant_id=tid,
                    datos=_sample_datos(i, archivo=f"load_{i}.xml"),
                    clasif=_CLASIF,
                    validacion=_VALIDACION_OK,
                )
                with lock:
                    success_count.append(i)
            except Exception as e:
                err_msg = str(e).lower()
                if "locked" in err_msg:
                    with lock:
                        lock_errors.append(f"iteration {i}: {e}")

        with ThreadPoolExecutor(max_workers=20) as pool:
            futures = [pool.submit(write, i) for i in range(50)]
            for f in as_completed(futures):
                f.result()

        assert len(lock_errors) == 0, f"Database locked errors: {lock_errors}"
        assert len(success_count) == 50
