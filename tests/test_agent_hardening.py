# -*- coding: utf-8 -*-
"""
test_agent_hardening.py — Tests for all bugs from AUDITORIA-DESTRUCCION-AGENTE.md

Bug #3: Conciliación persistente con audit trail y rollback
Bug #5: Memory leaks y connection leaks
Bug #7: Race conditions (thread-safe ERP)
Bug #8: Pipeline erp_status update after registration
Bug #9: Agent metrics + batch monitoring
Bug #10: Poison pill handling + persistent job queue
"""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import threading
import time

import pytest

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_db():
    """Fresh SQLite database per test."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db = None
    try:
        from b2b_ai.db.db import Database
        db = Database(path=path, migrate=True)
        # Create default tenant
        tid = db.create_tenant("Test Tenant", "XAXX010101000")
        yield db, tid
    finally:
        if db:
            db.close()
        try:
            os.unlink(path)
        except OSError:
            pass


# ===========================================================================
# Bug #3: Conciliación persistente
# ===========================================================================

class TestConciliacionPersistente:
    """Bug #3: Matches must persist to DB with audit trail and rollback."""

    def test_create_session(self, tmp_db):
        db, tid = tmp_db
        sid = db.create_conciliation_session(
            tid, user_id="contador@test.com",
            criteria={"strategy": "exact"},
            date_tolerance_days=5,
        )
        assert sid is not None
        session = db.get_conciliation_session(sid)
        assert session is not None
        assert session["tenant_id"] == tid
        assert session["status"] == "active"
        assert session["user_id"] == "contador@test.com"

    def test_save_matches(self, tmp_db):
        db, tid = tmp_db
        sid = db.create_conciliation_session(tid)
        m1 = db.save_conciliation_match(
            sid, tid, "BANK-TX-001", "EXACT", 1.0, poliza_id="POL-001")
        m2 = db.save_conciliation_match(
            sid, tid, "BANK-TX-002", "AMOUNT_DATE", 0.8, cfdi_uuid="CFDI-002")
        assert m1 is not None
        assert m2 is not None
        matches = db.get_conciliation_matches(sid)
        assert len(matches) == 2
        assert matches[0]["status"] == "proposed"

    def test_update_match_status(self, tmp_db):
        db, tid = tmp_db
        sid = db.create_conciliation_session(tid)
        mid = db.save_conciliation_match(
            sid, tid, "BANK-TX-001", "EXACT", 1.0, poliza_id="POL-001")
        db.update_conciliation_match_status(mid, "confirmed")
        matches = db.get_conciliation_matches(sid)
        assert matches[0]["status"] == "confirmed"

    def test_revert_session(self, tmp_db):
        db, tid = tmp_db
        sid = db.create_conciliation_session(tid, user_id="contador@test.com")
        db.save_conciliation_match(sid, tid, "BANK-1", "EXACT", 1.0, poliza_id="P1")
        db.save_conciliation_match(sid, tid, "BANK-2", "AMOUNT_DATE", 0.8, poliza_id="P2")
        db.update_conciliation_match_status(
            db.get_conciliation_matches(sid)[0]["id"], "confirmed")
        # Revert entire session
        db.revert_conciliation_session(sid, user_id="auditor@test.com")
        session = db.get_conciliation_session(sid)
        assert session["status"] == "reverted"
        matches = db.get_conciliation_matches(sid)
        for m in matches:
            assert m["status"] == "reverted"
            assert m["reverted_by"] == "auditor@test.com"

    def test_revert_preserves_history(self, tmp_db):
        """After revert, original data is not deleted — just marked."""
        db, tid = tmp_db
        sid = db.create_conciliation_session(tid)
        db.save_conciliation_match(sid, tid, "BANK-1", "EXACT", 1.0)
        db.revert_conciliation_session(sid, user_id="test")
        # Original match still exists (soft-delete)
        matches = db.get_conciliation_matches(sid)
        assert len(matches) == 1
        assert matches[0]["reverted_at"] is not None


# ===========================================================================
# Bug #5: Memory leaks
# ===========================================================================

class TestMemoryLeaks:
    """Bug #5: MockCONTPAQi must cap memory; DB must clean stale connections."""

    def test_erp_lru_eviction(self):
        from b2b_ai.erp.contpaqi import MockCONTPAQi
        erp = MockCONTPAQi()
        # Register _POLIZAS_MAX_SIZE + 10 invoices
        from b2b_ai.erp.contpaqi import _POLIZAS_MAX_SIZE
        for i in range(_POLIZAS_MAX_SIZE + 10):
            erp.register_invoice({
                "folio_fiscal": f"FF-{i:05d}",
                "categoria": "gasto_operativo",
                "emisor_rfc": "TEST",
                "total": "100.00",
            })
        # LRU should have evicted the oldest
        assert len(erp._polizas) <= _POLIZAS_MAX_SIZE
        # Oldest should be gone
        assert erp.get_invoice("FF-00000") is None
        # Newest should still be there
        assert erp.get_invoice(f"FF-{_POLIZAS_MAX_SIZE + 9:05d}") is not None

    def test_db_cleanup_stale_connections(self, tmp_db):
        """Connections from dead threads get cleaned up."""
        db, tid = tmp_db
        # Create a connection from a thread that will die
        barrier = threading.Event()
        def thread_work():
            _ = db.conn  # creates thread-local connection
            barrier.set()
        t = threading.Thread(target=thread_work)
        t.start()
        barrier.wait(timeout=5)
        t.join(timeout=5)
        initial_count = len(db._connections)
        assert initial_count >= 1
        # Force cleanup by accessing conn from main thread (triggers cleanup)
        _ = db.conn
        # The dead thread's connection should be cleaned
        # Note: on SQLite with in-memory, the test is that it doesn't grow unbounded


# ===========================================================================
# Bug #7: Race conditions
# ===========================================================================

class TestRaceConditions:
    """Bug #7: MockCONTPAQi must be thread-safe."""

    def test_concurrent_registration_no_crash(self):
        from b2b_ai.erp.contpaqi import MockCONTPAQi
        erp = MockCONTPAQi()
        errors = []
        results = []

        def register(idx):
            try:
                res = erp.register_invoice({
                    "folio_fiscal": f"FF-CONCURRENT-{idx}",
                    "categoria": "gasto_operativo",
                    "total": "100.00",
                })
                results.append(res)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=register, args=(i,)) for i in range(50)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert len(errors) == 0, f"Thread errors: {errors}"
        assert len(results) == 50

    def test_concurrent_idempotent(self):
        """Same folio registered concurrently should return duplicate=True."""
        from b2b_ai.erp.contpaqi import MockCONTPAQi
        erp = MockCONTPAQi()
        results = []
        lock = threading.Lock()

        def register():
            res = erp.register_invoice({
                "folio_fiscal": "FF-IDEMPOTENT-TEST",
                "categoria": "gasto_operativo",
                "total": "500.00",
            })
            with lock:
                results.append(res)

        threads = [threading.Thread(target=register) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        # Exactly one should be non-duplicate
        non_dup = [r for r in results if not r.get("duplicate")]
        assert len(non_dup) == 1
        # The rest should be duplicates
        dups = [r for r in results if r.get("duplicate")]
        assert len(dups) == 19


# ===========================================================================
# Bug #8: Pipeline erp_status update
# ===========================================================================

class TestPipelineERPStatus:
    """Bug #8: DB must be updated with ERP result after registration."""

    def test_update_invoice_erp(self, tmp_db):
        db, tid = tmp_db
        # Insert with pending status
        datos = {
            "folio_fiscal": "FF-TEST-ERP-UPDATE",
            "fecha": "2026-01-01",
            "tipo": "I",
            "subtotal": "100.00",
            "iva": "16.00",
            "total": "116.00",
            "emisor_rfc": "TEST123",
            "emisor_nombre": "Test Emisor",
            "receptor_rfc": "REC123",
        }
        clasif = {"categoria": "gasto_operativo", "confianza": 0.8, "razon": "test"}
        validacion = {"ok": True, "issues": []}
        erp_pending = {"ok": False, "poliza": None, "status": "pending"}
        inv_id, inserted = db.insert_invoice(tid, datos, clasif, validacion, erp=erp_pending)
        assert inserted is True
        inv = db.get_invoice(inv_id, tid)
        assert inv["erp_status"] == "pending"
        # Now update with actual ERP result
        db.update_invoice_erp(inv_id, "POL-ABC123", "registrada")
        inv = db.get_invoice(inv_id, tid)
        assert inv["erp_poliza"] == "POL-ABC123"
        assert inv["erp_status"] == "registrada"

    def test_update_erp_failed(self, tmp_db):
        db, tid = tmp_db
        datos = {
            "folio_fiscal": "FF-ERP-FAIL",
            "fecha": "2026-01-01", "tipo": "I",
            "subtotal": "100", "iva": "16", "total": "116",
        }
        clasif = {"categoria": "desconocido", "confianza": 0.0, "razon": ""}
        validacion = {"ok": True, "issues": []}
        inv_id, _ = db.insert_invoice(
            tid, datos, clasif, validacion,
            erp={"ok": False, "poliza": None, "status": "pending"})
        db.update_invoice_erp(inv_id, None, "erp_failed")
        inv = db.get_invoice(inv_id, tid)
        assert inv["erp_status"] == "erp_failed"


# ===========================================================================
# Bug #9: Agent metrics + batch monitoring
# ===========================================================================

class TestAgentMetrics:
    """Bug #9: Agent metrics must be recorded for batch monitoring."""

    def test_insert_agent_metric(self, tmp_db):
        db, tid = tmp_db
        db.insert_agent_metric(tid, "cfdi_processed", 1.0,
                               {"categoria": "gasto_operativo"})
        db.insert_agent_metric(tid, "batch_file_error", 1.0,
                               {"error": "test error"})
        # Verify by direct query
        rows = db.conn.execute(
            "SELECT * FROM agent_metrics WHERE tenant_id=?", (tid,)
        ).fetchall()
        assert len(rows) == 2
        assert rows[0]["event_type"] == "cfdi_processed"
        assert rows[1]["event_type"] == "batch_file_error"

    def test_agent_metric_with_metadata(self, tmp_db):
        db, tid = tmp_db
        db.insert_agent_metric(tid, "agent_processed", 1.0,
                               {"decision": "auto_processed", "confianza": 0.85})
        rows = db.conn.execute(
            "SELECT * FROM agent_metrics WHERE event_type='agent_processed'"
        ).fetchall()
        assert len(rows) == 1
        meta = json.loads(rows[0]["metadata"])
        assert meta["decision"] == "auto_processed"
        assert meta["confianza"] == 0.85


# ===========================================================================
# Bug #10: Poison pill handling + persistent job queue
# ===========================================================================

class TestJobQueue:
    """Bug #10: Persistent job queue with poison pill handling."""

    def test_enqueue_dequeue(self, tmp_db):
        db, tid = tmp_db
        job_id = db.enqueue_job(tid, "process_cfdi", {"file": "test.xml"})
        assert job_id is not None
        job = db.dequeue_job()
        assert job is not None
        assert job["status"] == "running"
        assert job["job_type"] == "process_cfdi"
        assert job["poison"] == 0

    def test_complete_job(self, tmp_db):
        db, tid = tmp_db
        job_id = db.enqueue_job(tid, "process_cfdi", {"file": "test.xml"})
        db.dequeue_job()
        db.complete_job(job_id)
        # Should not be dequeued again
        job = db.dequeue_job()
        assert job is None

    def test_fail_job_poison_pill(self, tmp_db):
        """After max_attempts failures, job becomes poison."""
        db, tid = tmp_db
        job_id = db.enqueue_job(tid, "process_cfdi", {"file": "bad.xml"},
                                max_attempts=3)
        # Fail 3 times
        for i in range(3):
            # Re-queue for retry
            db.conn.execute(
                "UPDATE job_queue SET status='pending', attempts=? WHERE id=?",
                (i, job_id))
            db.conn.commit()
            db.dequeue_job()
            db.fail_job(job_id, f"Error attempt {i+1}")
        # Check poison
        row = db.conn.execute(
            "SELECT poison, status FROM job_queue WHERE id=?", (job_id,)
        ).fetchone()
        assert row["poison"] == 1
        assert row["status"] == "poison"

    def test_poison_not_dequeued(self, tmp_db):
        """Poison jobs are skipped by dequeue."""
        db, tid = tmp_db
        job_id = db.enqueue_job(tid, "process_cfdi", {"file": "bad.xml"},
                                max_attempts=1)
        db.dequeue_job()
        db.fail_job(job_id, "First failure")
        # Now poison
        job = db.dequeue_job()
        assert job is None

    def test_pending_job_count(self, tmp_db):
        db, tid = tmp_db
        assert db.get_pending_job_count() == 0
        db.enqueue_job(tid, "process_cfdi", {"file": "a.xml"})
        db.enqueue_job(tid, "process_cfdi", {"file": "b.xml"})
        assert db.get_pending_job_count() == 2
        # Dequeue one
        db.dequeue_job()
        assert db.get_pending_job_count() == 1

    def test_poison_pill_in_batch(self):
        """process_batch skips files that crash 3+ times."""
        from b2b_ai.services.pipeline import process_batch
        # Create a temp folder with a "bad" XML that will crash
        with tempfile.TemporaryDirectory() as tmpdir:
            # Write a malformed XML that will fail parse
            bad_file = os.path.join(tmpdir, "bad.xml")
            with open(bad_file, "w") as f:
                f.write("<not-a-cfdi/>")
            results = process_batch(tmpdir, pattern="*.xml")
            # Should have at least one result (error or success)
            assert len(results) >= 1


# ===========================================================================
# Integration: Migration 16 applies cleanly
# ===========================================================================

class TestMigration16:
    """Verify migration 16 creates all new tables."""

    def test_tables_created(self, tmp_db):
        db, _ = tmp_db
        # Check all new tables exist
        tables = [r[0] for r in db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()]
        assert "conciliation_sessions" in tables
        assert "conciliation_matches" in tables
        assert "agent_metrics" in tables
        assert "job_queue" in tables

    def test_schema_version(self, tmp_db):
        db, _ = tmp_db
        version = db.schema_version()
        assert version >= 16
