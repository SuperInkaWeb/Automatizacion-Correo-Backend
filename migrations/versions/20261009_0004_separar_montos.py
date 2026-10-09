"""Separar los montos del recibo y añadir tipo de bien/documento.

Revision ID: 0004_separar_montos
Revises: 0003_endurecer_aislamiento
Create Date: 2026-10-09

El registro guardaba un unico `importe`, pero una constancia 1683 tiene
cuatro montos distintos —monto de alquiler, tributo resultante, importe
pagado e intereses moratorios— y colapsarlos hacia que el motor guardara
uno cualquiera. Se renombra `importe` a `importe_pagado` (lo que de
verdad representaba) y se añaden los otros tres, mas el tipo de bien y el
tipo de documento del inquilino.

La moneda sigue siendo una sola columna: un recibo no mezcla monedas, asi
que no hace falta una por monto.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004_separar_montos"
down_revision = "0003_endurecer_aislamiento"
branch_labels = None
depends_on = None

_CHECKS_NUEVOS: tuple[tuple[str, str], ...] = (
    ("monto_alquiler", "ck_records_monto_alquiler_no_negativo"),
    ("tributo_resultante", "ck_records_tributo_no_negativo"),
    ("importe_pagado", "ck_records_importe_pagado_no_negativo"),
    ("intereses_moratorios", "ck_records_intereses_no_negativo"),
)


def upgrade() -> None:
    # Texto nuevo. server_default="" para que las filas existentes queden
    # con valor valido sin reescribirlas.
    op.add_column(
        "extracted_records",
        sa.Column("tipo_doc_inquilino", sa.String(40), nullable=False, server_default=""),
    )
    op.add_column(
        "extracted_records",
        sa.Column("tipo_de_bien", sa.String(40), nullable=False, server_default=""),
    )

    # El importe unico pasa a ser "importe pagado": conserva el dato ya
    # guardado y solo cambia de nombre.
    op.alter_column("extracted_records", "importe", new_column_name="importe_pagado")

    for columna in ("monto_alquiler", "tributo_resultante", "intereses_moratorios"):
        op.add_column(
            "extracted_records", sa.Column(columna, sa.Numeric(14, 2), nullable=True)
        )

    # El check del importe unico se sustituye por uno por monto.
    op.drop_constraint("ck_records_importe_no_negativo", "extracted_records", type_="check")
    for columna, nombre in _CHECKS_NUEVOS:
        op.create_check_constraint(
            nombre, "extracted_records", f"{columna} IS NULL OR {columna} >= 0"
        )


def downgrade() -> None:
    for _, nombre in _CHECKS_NUEVOS:
        op.drop_constraint(nombre, "extracted_records", type_="check")

    for columna in ("intereses_moratorios", "tributo_resultante", "monto_alquiler"):
        op.drop_column("extracted_records", columna)

    op.alter_column("extracted_records", "importe_pagado", new_column_name="importe")
    op.create_check_constraint(
        "ck_records_importe_no_negativo",
        "extracted_records",
        "importe IS NULL OR importe >= 0",
    )

    op.drop_column("extracted_records", "tipo_de_bien")
    op.drop_column("extracted_records", "tipo_doc_inquilino")
