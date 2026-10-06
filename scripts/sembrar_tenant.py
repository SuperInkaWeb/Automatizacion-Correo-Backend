"""
Siembra el primer espacio de trabajo (tenant) y nombra dueño a un usuario.

Para que sirve
    Un usuario nuevo se crea solo al iniciar sesion, pero su pertenencia a un
    espacio de trabajo NO se crea automaticamente: es una decision de
    seguridad (nadie entra a un tenant sin invitacion). La consecuencia es
    que el primer usuario de una instalacion recien montada inicia sesion,
    se le crea la cuenta, y se queda sin nada que ver porque no pertenece a
    ningun tenant. Este script resuelve ese arranque: crea el primer tenant
    y la primera membresia de dueño.

    A partir de ahi, el dueño gestiona el resto de membresias desde la
    aplicacion con normalidad; este script es solo para el arranque.

Orden correcto
    1. El usuario inicia sesion UNA vez. Eso crea su fila en `users` con el
       `external_id` real que emite el proveedor de identidad.
    2. Se ejecuta este script con su correo. Busca esa fila y le cuelga el
       tenant y la membresia.

    No crea el usuario a mano a proposito: si lo hiciera con un `external_id`
    inventado, al iniciar sesion el proveedor traeria otro distinto, no
    casaria, se crearia un segundo usuario y la membresia quedaria colgada
    del usuario equivocado. Por eso exige que el usuario exista ya.

Uso
    python scripts/sembrar_tenant.py --email tu@correo.com --nombre "Mi Empresa"

    Opcional: --slug mi-empresa (por defecto se deriva del nombre).
    Opcional: --rol owner|admin|member|viewer (por defecto owner).

    Es idempotente: ejecutarlo dos veces no duplica nada y sirve para
    cambiarle el rol a una membresia existente.
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from mailauto.bootstrap.settings import get_settings
from mailauto.shared.security.context import Rol
from mailauto.shared.types import uuid7


def _slug_desde(nombre: str) -> str:
    """Convierte un nombre en un slug: minusculas, sin acentos simples, guiones."""
    base = nombre.strip().lower()
    base = re.sub(r"[^a-z0-9]+", "-", base).strip("-")
    return base[:80] or "espacio"


async def _sembrar(email: str, nombre: str, slug: str, rol: Rol) -> int:
    motor = create_async_engine(str(get_settings().database_url))
    try:
        async with motor.begin() as conexion:
            # 1. El usuario tiene que existir ya (haber iniciado sesion).
            fila = (
                await conexion.execute(
                    text("SELECT id, external_id FROM users WHERE email = :email"),
                    {"email": email},
                )
            ).first()
            if fila is None:
                print(
                    f"No existe ningun usuario con el correo {email!r}.\n"
                    "Inicia sesion una vez en la aplicacion con esa cuenta y vuelve a "
                    "ejecutar este script: el primer inicio de sesion crea el usuario.",
                    file=sys.stderr,
                )
                return 1
            user_id: UUID = fila.id

            # 2. Reutiliza el tenant si el slug ya existe; si no, lo crea.
            tenant_id = (
                await conexion.execute(
                    text("SELECT id FROM tenants WHERE slug = :slug"), {"slug": slug}
                )
            ).scalar_one_or_none()
            if tenant_id is None:
                tenant_id = uuid7()
                await conexion.execute(
                    text("INSERT INTO tenants (id, nombre, slug) VALUES (:id, :nombre, :slug)"),
                    {"id": tenant_id, "nombre": nombre, "slug": slug},
                )
                print(f"Espacio de trabajo creado: {nombre!r} (slug: {slug})")
            else:
                print(f"Ya existia un espacio con slug {slug!r}; se reutiliza.")

            # 3. Crea o actualiza la membresia. Re-ejecutar cambia el rol.
            await conexion.execute(
                text(
                    "INSERT INTO memberships (id, tenant_id, user_id, rol) "
                    "VALUES (:id, :tenant_id, :user_id, :rol) "
                    "ON CONFLICT (tenant_id, user_id) DO UPDATE SET rol = EXCLUDED.rol"
                ),
                {
                    "id": uuid7(),
                    "tenant_id": tenant_id,
                    "user_id": user_id,
                    "rol": rol.value,
                },
            )
    finally:
        await motor.dispose()

    print(f"Listo. {email} es '{rol.value}' en '{nombre}'. Ya puede operar al iniciar sesion.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Siembra el primer tenant y su duenno.")
    parser.add_argument(
        "--email", required=True, help="Correo del usuario (ya debe haber entrado una vez)."
    )
    parser.add_argument("--nombre", required=True, help="Nombre del espacio de trabajo.")
    parser.add_argument(
        "--slug", default=None, help="Slug del espacio (por defecto se deriva del nombre)."
    )
    parser.add_argument(
        "--rol",
        default=Rol.OWNER.value,
        choices=[r.value for r in Rol],
        help="Rol de la membresia (por defecto owner).",
    )
    args = parser.parse_args()

    slug = args.slug or _slug_desde(args.nombre)
    return asyncio.run(_sembrar(args.email, args.nombre, slug, Rol(args.rol)))


if __name__ == "__main__":
    raise SystemExit(main())
