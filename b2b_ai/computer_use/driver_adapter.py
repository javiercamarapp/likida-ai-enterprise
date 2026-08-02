# -*- coding: utf-8 -*-
"""Canonical sync adapter for the legacy async Playwright browser drivers."""
from __future__ import annotations

import asyncio
import threading
from typing import Any, Dict

from b2b_ai.computer_use.interface import (
    ComputerUseDriver,
    DriverResult,
    DriverResultStatus,
)


class AsyncPlaywrightDriverAdapter(ComputerUseDriver):
    """Expose one truthful, synchronous ``ComputerUseDriver`` contract."""

    def __init__(self, legacy: Any, provider: str):
        self._legacy = legacy
        self._provider = provider
        # Playwright objects are bound to the event loop where they were
        # created.  Keep one loop alive on one thread for the whole driver
        # lifetime; creating a temporary loop from a running FastAPI loop is
        # invalid and can also move Playwright objects across loops.
        self._loop = asyncio.new_event_loop()
        self._loop_ready = threading.Event()
        self._call_lock = threading.RLock()
        self._closed = False
        self._thread = threading.Thread(
            target=self._serve_loop,
            name=f"{provider}-playwright-loop",
            daemon=True,
        )
        self._thread.start()
        if not self._loop_ready.wait(timeout=5):
            raise RuntimeError(f"Timed out starting {provider} Playwright loop")

    def _serve_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop_ready.set()
        self._loop.run_forever()

    def _submit(self, awaitable):
        if self._closed:
            raise RuntimeError(f"{self._provider} driver is closed")
        future = asyncio.run_coroutine_threadsafe(awaitable, self._loop)
        return future.result()

    @property
    def provider(self) -> str:
        return self._provider

    @property
    def mode(self) -> str:
        return "playwright"

    @property
    def session(self):
        return self._legacy.session

    @staticmethod
    def _coerce(value: Any, default_message: str = "") -> DriverResult:
        if isinstance(value, DriverResult):
            return value
        if isinstance(value, list):
            return DriverResult.success(
                default_message or f"Extracted {len(value)} records",
                invoices=value,
            )
        if not isinstance(value, dict):
            return DriverResult.failed(default_message or str(value))

        payload: Dict[str, Any] = dict(value)
        ok = bool(payload.pop("ok", False))
        message = str(payload.pop("message", default_message) or default_message)
        raw_status = payload.pop("status", None)
        if ok:
            return DriverResult.success(message, **payload)
        if raw_status:
            try:
                return DriverResult(
                    status=DriverResultStatus(raw_status), message=message, data=payload
                )
            except ValueError:
                pass
        return DriverResult.failed(message, **payload)

    def _run(self, method, *args) -> DriverResult:
        with self._call_lock:
            return self._coerce(self._submit(method(*args)), method.__name__)

    def connect(self) -> DriverResult:
        return self._run(self._legacy.connect)

    def login(self, credentials: Dict[str, Any]) -> DriverResult:
        return self._run(self._legacy.login, credentials)

    def verify_authenticated(self) -> DriverResult:
        if not self._legacy.session:
            return DriverResult.session_expired("No active verified session.")
        return DriverResult.success("Verified browser session is active.")

    def logout(self) -> DriverResult:
        if not self._legacy.session:
            return DriverResult.session_expired("No active session.")
        user = self._legacy.session.get("usuario", "")
        self._legacy.session = None
        self._legacy._current_module = None
        return DriverResult.success(f"Logged out {user}.")

    def close(self) -> None:
        with self._call_lock:
            if self._closed:
                return
            try:
                self._submit(self._legacy.desktop.close())
            finally:
                self._closed = True
                self._loop.call_soon_threadsafe(self._loop.stop)
                self._thread.join(timeout=5)
                self._loop.close()
                # Close the unused compatibility loop created by the legacy
                # implementation as well.
                self._legacy.close()

    def navigate_menu(self, module: str) -> DriverResult:
        return self._run(self._legacy.navigate_menu, module)

    def extract_invoices(self) -> DriverResult:
        return self._run(self._legacy.extract_invoices)

    def capture_invoice_grid(self) -> DriverResult:
        return self._run(self._legacy.capture_invoice_grid)

    def register_invoice(self, data: Dict[str, Any]) -> DriverResult:
        return self._run(self._legacy.register_invoice, data)

    def register_poliza(self, data: Dict[str, Any]) -> DriverResult:
        return DriverResult.configuration_error(
            "This ERP browser profile has no verified póliza form mapping."
        )

    def verify_invoice_registered(self, folio_fiscal: str) -> DriverResult:
        extracted = self.extract_invoices()
        if not extracted.ok:
            return DriverResult.verification_failed(extracted.message)
        found = any(
            str(row.get("folio") or row.get("folio_fiscal") or "")
            == str(folio_fiscal)
            for row in extracted.data.get("invoices", [])
        )
        if found:
            return DriverResult.success(
                f"Invoice {folio_fiscal} verified in ERP grid.",
                folio_fiscal=folio_fiscal,
            )
        return DriverResult.verification_failed(
            f"Invoice {folio_fiscal} was not found in the ERP grid."
        )

    def verify_poliza_registered(self, poliza_id: str) -> DriverResult:
        return DriverResult.configuration_error(
            "This ERP browser profile has no verified póliza grid mapping.",
            poliza_id=poliza_id,
        )

    def health(self) -> DriverResult:
        return self._coerce(self._legacy.health(), "Driver health")

    def recover_from_error(self) -> DriverResult:
        return self._run(self._legacy.recover_from_error)
