# -*- coding: utf-8 -*-
"""
contpaqi.py — Mock de CONTPAQi (contaDIGITAL API) para el MVP.

Implementa ERPInterface en memoria (registro de pólizas). Está diseñado para
que, cuando existan credenciales reales, se sustituya el método de registro
por una llamada REST a la API de CONTPAQi/contaDIGITAL sin tocar los servicios
que lo consumen (ver `base.ERPInterface`).

Modo real (futuro, requiere credenciales):
    POST https://api.contpaqi.com/.../polizas   con Bearer token
No se toca credenciales aquí; el mock solo sirve para testear el flujo.
"""
from __future__ import annotations

import threading
import uuid
from collections import OrderedDict
from datetime import datetime

from b2b_ai.erp.base import ERPInterface

# Bug #5: LRU max size for _polizas to prevent unbounded memory growth
_POLIZAS_MAX_SIZE = 1000


class MockCONTPAQi(ERPInterface):
    """Implementación simulada de la API de CONTPAQi en memoria.

    Thread-safe con lock interno (Bug #7) y LRU eviction (Bug #5).
    """

    backend = "CONTPAQi (mock)"

    def __init__(self):
        self._polizas: OrderedDict = OrderedDict()
        self._lock = threading.Lock()  # Bug #7: thread-safe dict access

    def register_invoice(self, invoice):
        folio = invoice.get("folio_fiscal") or invoice.get("archivo")
        if not folio:
            return {"ok": False, "poliza": None, "status": "error",
                    "message": "Sin folio fiscal para registrar."}

        with self._lock:
            # [34][39] Idempotente: si ya existe, devolver la póliza existente.
            existing = self._polizas.get(folio)
            if existing:
                # Move to end for LRU
                self._polizas.move_to_end(folio)
                return {
                    "ok": True, "poliza": existing["poliza"],
                    "cuenta_cargo": existing["cuenta_cargo"],
                    "cuenta_abono": existing["cuenta_abono"],
                    "status": "registrada", "duplicate": True,
                    "message": f"Factura {folio} ya registrada (idempotente).",
                }

            poliza_id = "POL-" + uuid.uuid4().hex[:10].upper()
            categoria = invoice.get("categoria", "desconocido")
            cuenta_cargo, cuenta_abono = _cuentas_para_categoria(categoria)

            # Bug #5: LRU eviction — drop oldest entry if at capacity
            if len(self._polizas) >= _POLIZAS_MAX_SIZE:
                self._polizas.popitem(last=False)

            self._polizas[folio] = {
                "poliza": poliza_id,
                "folio_fiscal": folio,
                "emisor": invoice.get("emisor_rfc", ""),
                "total": invoice.get("total"),
                "categoria": categoria,
                "cuenta_cargo": cuenta_cargo,
                "cuenta_abono": cuenta_abono,
                "fecha_registro": datetime.now().isoformat(timespec="seconds"),
                "status": "registrada",
            }
        return {
            "ok": True,
            "poliza": poliza_id,
            "cuenta_cargo": cuenta_cargo,
            "cuenta_abono": cuenta_abono,
            "status": "registrada",
            "message": f"Póliza {poliza_id} registrada en CONTPAQi (mock).",
        }

    def get_invoice(self, folio_fiscal):
        with self._lock:
            p = self._polizas.get(folio_fiscal)
            return dict(p) if p else None

    def health(self):
        return {"ok": True, "backend": self.backend,
                "detail": "Mock CONTPAQi operativo (sin conexión real)."}


# Mapa de cuentas contables sugeridas por categoría (catálogo simplificado).
_CUENTAS = {
    "gasto_operativo": ("6131 Gastos generales", "1130 Bancos"),
    "inversion": ("1115 Gastos de instalacion", "1130 Bancos"),
    "activo_fijo": ("1210 Mobiliario y equipo", "1130 Bancos"),
    "nomina": ("6110 Sueldos y salarios", "1130 Bancos"),
    "desconocido": ("6100 Gastos por clasificar", "1130 Bancos"),
}


def _cuentas_para_categoria(categoria):
    return _CUENTAS.get(categoria, _CUENTAS["desconocido"])
