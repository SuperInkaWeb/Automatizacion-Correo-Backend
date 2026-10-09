"""
Modelos de persistencia de los registros extraidos.

Proposito
    Guardar lo que el pipeline leyo, con sus columnas tipadas para
    consultar e indexar y su copia cruda para diagnosticar.

Dependencias
    SQLAlchemy y el `Base` compartido.

Decision de diseño
    Columnas tipadas Y copia en JSONB, no una cosa o la otra. Las
    tipadas son las que se filtran, se ordenan y se indexan; el JSONB
    conserva exactamente lo que leyo cada motor, que es lo unico que
    permite entender despues por que un campo salio mal sin volver a
    abrir el adjunto. Guardar solo JSONB haria imposible un indice
    sobre RUC y periodo, que son los dos filtros de toda la aplicacion.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from mailauto.shared.db.base import Base, MixinDeTenant, MixinDeTimestamps


class RegistroTributarioORM(MixinDeTenant, MixinDeTimestamps, Base):
    __tablename__ = "extracted_records"
    __table_args__ = (
        # Un adjunto produce como mucho un registro. Es la barrera que
        # hace idempotente la reentrega de un job de extraccion.
        UniqueConstraint("tenant_id", "adjunto_id", name="uq_records_tenant_adjunto"),
        # Listado principal: siempre filtra por tenant y ordena por fecha.
        Index("ix_records_tenant_created", "tenant_id", "created_at"),
        # Reporte mensual por contribuyente.
        Index("ix_records_ruc_periodo", "tenant_id", "ruc_contribuyente", "periodo"),
        # Cola de revision. Indice PARCIAL: solo indexa lo pendiente,
        # que es una fraccion minima de la tabla. Un indice completo
        # sobre `estado_de_revision` ocuparia el tamaño de la tabla
        # para responder siempre la misma consulta.
        Index(
            "ix_records_revision_pendiente",
            "tenant_id",
            "created_at",
            postgresql_where="estado_de_revision = 'pending'",
        ),
        Index("ix_records_trabajo", "tenant_id", "trabajo_id"),
        CheckConstraint(
            "monto_alquiler IS NULL OR monto_alquiler >= 0",
            name="ck_records_monto_alquiler_no_negativo",
        ),
        CheckConstraint(
            "tributo_resultante IS NULL OR tributo_resultante >= 0",
            name="ck_records_tributo_no_negativo",
        ),
        CheckConstraint(
            "importe_pagado IS NULL OR importe_pagado >= 0",
            name="ck_records_importe_pagado_no_negativo",
        ),
        CheckConstraint(
            "intereses_moratorios IS NULL OR intereses_moratorios >= 0",
            name="ck_records_intereses_no_negativo",
        ),
    )

    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True)
    adjunto_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("attachments.id", ondelete="CASCADE"), nullable=False
    )
    trabajo_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("scan_jobs.id", ondelete="CASCADE"), nullable=False
    )
    perfil: Mapped[str] = mapped_column(String(64), nullable=False)

    # ── Datos tributarios (tipados para consultar) ───────────────────
    ruc_contribuyente: Mapped[str | None] = mapped_column(String(11), nullable=True)
    nombre_contribuyente: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    ruc_inquilino: Mapped[str | None] = mapped_column(String(11), nullable=True)
    nombre_inquilino: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    tipo_doc_inquilino: Mapped[str] = mapped_column(String(40), nullable=False, default="")
    tipo_de_bien: Mapped[str] = mapped_column(String(40), nullable=False, default="")
    # YYYYMM: seis caracteres fijos. Ordenar alfabeticamente equivale a
    # ordenar cronologicamente, que es justo lo que pide el reporte.
    periodo: Mapped[str | None] = mapped_column(String(6), nullable=True)
    fecha_de_pago: Mapped[date | None] = mapped_column(Date, nullable=True)
    numero_de_operacion: Mapped[str | None] = mapped_column(String(40), nullable=True)
    # Numeric y no Float: un reporte tributario tiene que cuadrar al
    # centimo y el binario flotante no representa 0.10 exactamente. Son
    # cuatro montos distintos; la moneda se comparte (un recibo no mezcla).
    monto_alquiler: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    tributo_resultante: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    importe_pagado: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    intereses_moratorios: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    moneda: Mapped[str] = mapped_column(String(3), nullable=False, default="PEN")

    # ── Calidad y trazabilidad ───────────────────────────────────────
    campos_crudos: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    confianza_por_campo: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    completitud: Mapped[str] = mapped_column(String(20), nullable=False, default="empty")
    estado_de_revision: Mapped[str] = mapped_column(
        String(20), nullable=False, default="not_required"
    )
    revisado_por: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    revisado_en: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    estrategia_usada: Mapped[str | None] = mapped_column(String(32), nullable=True)
    duracion_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
