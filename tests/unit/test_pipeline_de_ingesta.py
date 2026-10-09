"""
Tests del pipeline de ingesta completo, con dobles de prueba.

Esto es lo que la arquitectura hexagonal compra: el pipeline entero
—autenticacion, listado, descarga, validacion, deduplicacion,
almacenamiento, contadores y estado final— se ejercita en milisegundos,
sin Gmail, sin PostgreSQL, sin Redis y sin S3.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import date
from uuid import UUID

import pytest

from mailauto.modules.ingestion.application.ejecutar_escaneo import EjecutarEscaneo
from mailauto.modules.ingestion.domain.entities import (
    Adjunto,
    ErrorDeProcesamiento,
    EstadoDeTrabajo,
    MensajeDeCorreo,
    ParametrosDeEscaneo,
    TrabajoDeEscaneo,
)
from mailauto.modules.ingestion.domain.ports import (
    AlmacenDeObjetos,
    CanalDeProgreso,
    ColaDeTrabajos,
    CredencialDeBuzon,
    OrigenDeAdjunto,
    ProveedorDeCorreo,
    ProveedorDeCredenciales,
    ReferenciaDeAdjunto,
    RepositorioDeIngesta,
    ResumenDeMensaje,
)
from mailauto.modules.ingestion.infrastructure.validador import (
    ValidadorDeAdjuntosPorContenido,
)
from mailauto.shared.errors import CredencialesRevocadas
from mailauto.shared.pagination import Pagina, SolicitudDePagina
from mailauto.shared.security.context import TenantContext
from tests.conftest import TENANT_A, pdf_valido, png_valido

# ─────────────────────────────────────────────────────────────────────
# Dobles de prueba
# ─────────────────────────────────────────────────────────────────────


class RepositorioFalso(RepositorioDeIngesta):
    def __init__(self, trabajo: TrabajoDeEscaneo) -> None:
        self.trabajo = trabajo
        self.mensajes: list[MensajeDeCorreo] = []
        self.adjuntos: list[Adjunto] = []
        self.errores: list[ErrorDeProcesamiento] = []
        self.ya_procesados: set[str] = set()
        self.hashes: set[str] = set()

    async def crear_trabajo(
        self, ctx: TenantContext, trabajo: TrabajoDeEscaneo
    ) -> TrabajoDeEscaneo:
        return trabajo

    async def actualizar_trabajo(self, tenant_id: UUID, trabajo: TrabajoDeEscaneo) -> None:
        self.trabajo = trabajo

    async def obtener_trabajo(
        self, ctx: TenantContext, trabajo_id: UUID
    ) -> TrabajoDeEscaneo | None:
        return self.trabajo

    async def obtener_trabajo_por_tenant(
        self, tenant_id: UUID, trabajo_id: UUID
    ) -> TrabajoDeEscaneo | None:
        return self.trabajo if self.trabajo.id == trabajo_id else None

    async def listar_trabajos(
        self, ctx: TenantContext, pagina: SolicitudDePagina
    ) -> Pagina[TrabajoDeEscaneo]:
        return Pagina(elementos=[self.trabajo])

    async def buscar_por_idempotencia(
        self, ctx: TenantContext, clave: str
    ) -> TrabajoDeEscaneo | None:
        return None

    async def contar_trabajos_activos(self, tenant_id: UUID) -> int:
        return 0

    async def ids_de_mensajes_procesados(
        self, tenant_id: UUID, proveedor: str, ids_candidatos: list[str]
    ) -> set[str]:
        return self.ya_procesados & set(ids_candidatos)

    async def guardar_mensaje(self, tenant_id: UUID, mensaje: MensajeDeCorreo) -> MensajeDeCorreo:
        self.mensajes.append(mensaje)
        return mensaje

    async def existe_adjunto_con_hash(self, tenant_id: UUID, sha256: str) -> bool:
        return sha256 in self.hashes

    async def guardar_adjunto(self, tenant_id: UUID, adjunto: Adjunto) -> Adjunto:
        self.adjuntos.append(adjunto)
        self.hashes.add(adjunto.sha256)
        return adjunto

    async def origen_de_adjuntos(
        self, ctx: TenantContext, adjunto_ids: list[UUID]
    ) -> dict[UUID, OrigenDeAdjunto]:
        return {}

    async def guardar_errores(self, tenant_id: UUID, errores: list[ErrorDeProcesamiento]) -> None:
        self.errores.extend(errores)

    async def listar_errores(
        self, ctx: TenantContext, pagina: SolicitudDePagina, trabajo_id: UUID | None = None
    ) -> Pagina[ErrorDeProcesamiento]:
        return Pagina(elementos=self.errores)


class CredencialesFalsas(ProveedorDeCredenciales):
    def __init__(self, *, revocada: bool = False) -> None:
        self._revocada = revocada

    async def obtener(self, *, tenant_id: UUID, conexion_id: UUID) -> CredencialDeBuzon:
        if self._revocada:
            raise CredencialesRevocadas(proveedor="google")
        return CredencialDeBuzon(
            proveedor="google", access_token="token", correo_de_la_cuenta="a@b.com"
        )


class ProveedorFalso(ProveedorDeCorreo):
    def __init__(self, mensajes: dict[str, list[tuple[str, bytes]]]) -> None:
        # id_mensaje -> [(nombre_archivo, contenido)]
        self._mensajes = mensajes

    async def listar_mensajes(
        self,
        access_token: str,
        *,
        desde: date | None,
        hasta: date | None,
        limite: int,
        carpeta: str,
    ) -> AsyncIterator[ResumenDeMensaje]:
        for identificador in list(self._mensajes)[:limite]:
            yield ResumenDeMensaje(
                id_del_proveedor=identificador,
                remitente="remitente@ejemplo.com",
                asunto="Constancia",
                recibido_en=None,
            )

    async def listar_adjuntos(
        self, access_token: str, id_del_mensaje: str
    ) -> list[ReferenciaDeAdjunto]:
        return [
            ReferenciaDeAdjunto(
                id_del_adjunto=f"{id_del_mensaje}:{i}",
                nombre=nombre,
                tipo_mime_declarado="application/pdf",
                tamano_declarado=len(contenido),
            )
            for i, (nombre, contenido) in enumerate(self._mensajes[id_del_mensaje])
        ]

    async def descargar_adjunto(
        self,
        access_token: str,
        *,
        id_del_mensaje: str,
        id_del_adjunto: str,
        limite_de_bytes: int,
    ) -> AsyncIterator[bytes]:
        indice = int(id_del_adjunto.rsplit(":", 1)[1])
        yield self._mensajes[id_del_mensaje][indice][1]


class AlmacenFalso(AlmacenDeObjetos):
    def __init__(self) -> None:
        self.objetos: dict[str, bytes] = {}

    async def guardar(
        self, *, clave: str, contenido: bytes, tipo_mime: str, metadatos: dict[str, str]
    ) -> None:
        self.objetos[clave] = contenido

    async def descargar(self, clave: str) -> bytes:
        return self.objetos[clave]

    async def url_de_descarga(self, clave: str, *, ttl_segundos: int) -> str:
        return f"https://storage/{clave}"

    async def eliminar(self, clave: str) -> None:
        self.objetos.pop(clave, None)

    async def esta_disponible(self) -> bool:
        return True


class ColaFalsa(ColaDeTrabajos):
    def __init__(self, *, cancelar_en: int | None = None) -> None:
        self._cancelar_en = cancelar_en
        self._consultas = 0
        # Cada adjunto guardado debe encolar su propia extraccion.
        self.extracciones: list[UUID] = []

    async def encolar_escaneo(self, *, tenant_id: UUID, trabajo_id: UUID) -> str:
        return "job"

    async def encolar_extraccion(
        self,
        *,
        tenant_id: UUID,
        trabajo_id: UUID,
        adjunto_id: UUID,
        clave_de_almacenamiento: str,
        tipo_mime: str,
        nombre: str,
    ) -> str:
        self.extracciones.append(adjunto_id)
        return "extract"

    async def solicitar_cancelacion(self, trabajo_id: UUID) -> None:
        self._cancelar_en = 0

    async def cancelacion_solicitada(self, trabajo_id: UUID) -> bool:
        self._consultas += 1
        return self._cancelar_en is not None and self._consultas > self._cancelar_en

    async def esta_disponible(self) -> bool:
        return True


class ProgresoFalso(CanalDeProgreso):
    def __init__(self) -> None:
        self.eventos: list[dict[str, object]] = []

    async def publicar(self, trabajo_id: UUID, evento: dict[str, object]) -> None:
        self.eventos.append(evento)

    async def suscribirse(self, trabajo_id: UUID) -> AsyncIterator[dict[str, object]]:
        for evento in self.eventos:
            yield evento


# ─────────────────────────────────────────────────────────────────────
# Montaje
# ─────────────────────────────────────────────────────────────────────


def _montar(
    mensajes: dict[str, list[tuple[str, bytes]]],
    *,
    credenciales: ProveedorDeCredenciales | None = None,
    cola: ColaDeTrabajos | None = None,
    limite_de_bytes: int = 1_000_000,
    maximo_adjuntos: int = 20,
) -> tuple[EjecutarEscaneo, RepositorioFalso, AlmacenFalso, ProgresoFalso, TrabajoDeEscaneo]:
    trabajo = TrabajoDeEscaneo(
        tenant_id=TENANT_A,
        parametros=ParametrosDeEscaneo(limite_de_mensajes=50),
    )
    repositorio = RepositorioFalso(trabajo)
    almacen = AlmacenFalso()
    progreso = ProgresoFalso()

    pipeline = EjecutarEscaneo(
        repositorio=repositorio,
        credenciales=credenciales or CredencialesFalsas(),
        proveedores={"google": ProveedorFalso(mensajes)},
        validador=ValidadorDeAdjuntosPorContenido(
            tipos_permitidos=["application/pdf", "image/png"],
            tamano_maximo=limite_de_bytes,
        ),
        almacen=almacen,
        cola=cola or ColaFalsa(),
        progreso=progreso,
        limite_de_bytes=limite_de_bytes,
        maximo_adjuntos_por_mensaje=maximo_adjuntos,
    )
    return pipeline, repositorio, almacen, progreso, trabajo


# ─────────────────────────────────────────────────────────────────────
# Casos
# ─────────────────────────────────────────────────────────────────────


async def test_procesa_correos_con_adjuntos_validos() -> None:
    pipeline, repo, almacen, _, trabajo = _montar(
        {
            "m1": [("constancia.pdf", pdf_valido())],
            "m2": [("recibo.png", png_valido())],
        }
    )
    await pipeline.ejecutar(tenant_id=TENANT_A, trabajo_id=trabajo.id)

    assert repo.trabajo.estado is EstadoDeTrabajo.COMPLETADO
    assert repo.trabajo.contadores.mensajes_revisados == 2
    assert repo.trabajo.contadores.adjuntos_descargados == 2
    assert len(almacen.objetos) == 2
    assert repo.trabajo.progreso_porcentaje == 100


async def test_la_clave_de_almacenamiento_no_usa_el_nombre_original() -> None:
    """
    Path traversal cerrado por diseño: el nombre que venia en el correo
    jamas influye en donde se escribe el objeto.
    """
    pipeline, _, almacen, _, trabajo = _montar({"m1": [("constancia.pdf", pdf_valido())]})
    await pipeline.ejecutar(tenant_id=TENANT_A, trabajo_id=trabajo.id)

    clave = next(iter(almacen.objetos))
    assert "constancia" not in clave
    assert clave.startswith(f"{TENANT_A}/")


async def test_rechaza_adjuntos_que_no_pasan_la_validacion() -> None:
    pipeline, repo, almacen, _, trabajo = _montar(
        {"m1": [("virus.pdf", b"MZ\x90\x00" + b"\x00" * 300)]}
    )
    await pipeline.ejecutar(tenant_id=TENANT_A, trabajo_id=trabajo.id)

    assert repo.trabajo.contadores.adjuntos_rechazados == 1
    assert repo.trabajo.contadores.adjuntos_descargados == 0
    assert almacen.objetos == {}


async def test_un_adjunto_rechazado_no_aborta_el_escaneo() -> None:
    """
    En un buzon de quinientos correos, un fichero corrupto no puede
    invalidar los cuatrocientos noventa y nueve restantes.
    """
    pipeline, repo, _, _, trabajo = _montar(
        {
            "m1": [("malo.pdf", b"MZ" + b"\x00" * 300)],
            "m2": [("bueno.pdf", pdf_valido())],
        }
    )
    await pipeline.ejecutar(tenant_id=TENANT_A, trabajo_id=trabajo.id)

    assert repo.trabajo.contadores.adjuntos_descargados == 1
    assert repo.trabajo.contadores.adjuntos_rechazados == 1
    assert repo.trabajo.estado is EstadoDeTrabajo.COMPLETADO


async def test_deduplica_por_contenido() -> None:
    """El mismo PDF reenviado en dos correos se almacena una sola vez."""
    identico = pdf_valido()
    pipeline, repo, almacen, _, trabajo = _montar(
        {"m1": [("a.pdf", identico)], "m2": [("b.pdf", identico)]}
    )
    await pipeline.ejecutar(tenant_id=TENANT_A, trabajo_id=trabajo.id)

    assert repo.trabajo.contadores.adjuntos_descargados == 1
    assert repo.trabajo.contadores.adjuntos_duplicados == 1
    assert len(almacen.objetos) == 1


async def test_omite_mensajes_ya_procesados() -> None:
    """Idempotencia: relanzar un escaneo no reprocesa lo que ya se vio."""
    pipeline, repo, almacen, _, trabajo = _montar(
        {"m1": [("a.pdf", pdf_valido())], "m2": [("b.png", png_valido())]}
    )
    repo.ya_procesados = {"m1"}

    await pipeline.ejecutar(tenant_id=TENANT_A, trabajo_id=trabajo.id)

    assert repo.trabajo.contadores.mensajes_revisados == 2
    assert len(almacen.objetos) == 1


async def test_respeta_el_tope_de_adjuntos_por_mensaje() -> None:
    """Un correo con muchos adjuntos pequeños agota el worker igual que uno enorme."""
    adjuntos = [(f"a{i}.pdf", pdf_valido() + bytes([i])) for i in range(10)]
    pipeline, repo, _, _, trabajo = _montar({"m1": adjuntos}, maximo_adjuntos=3)

    await pipeline.ejecutar(tenant_id=TENANT_A, trabajo_id=trabajo.id)
    assert repo.trabajo.contadores.adjuntos_descargados == 3


async def test_corta_por_tamano_antes_de_descargar() -> None:
    pipeline, repo, _, _, trabajo = _montar(
        {"m1": [("grande.pdf", pdf_valido() + b"\x20" * 10_000)]},
        limite_de_bytes=1024,
    )
    await pipeline.ejecutar(tenant_id=TENANT_A, trabajo_id=trabajo.id)

    assert repo.trabajo.contadores.adjuntos_rechazados == 1
    assert any(e.codigo == "tamano_excedido" for e in repo.errores)


async def test_las_credenciales_revocadas_fallan_el_trabajo_con_su_codigo() -> None:
    """
    El codigo de error llega a la UI para poder decir "vuelve a conectar
    tu buzon" en lugar de un fallo generico.
    """
    pipeline, repo, _, _, trabajo = _montar(
        {"m1": [("a.pdf", pdf_valido())]},
        credenciales=CredencialesFalsas(revocada=True),
    )
    await pipeline.ejecutar(tenant_id=TENANT_A, trabajo_id=trabajo.id)

    assert repo.trabajo.estado is EstadoDeTrabajo.FALLIDO
    assert repo.trabajo.codigo_de_error == "credenciales_revocadas"


async def test_la_cancelacion_detiene_el_recorrido() -> None:
    mensajes = {f"m{i}": [(f"a{i}.pdf", pdf_valido() + bytes([i]))] for i in range(10)}
    pipeline, repo, _, _, trabajo = _montar(mensajes, cola=ColaFalsa(cancelar_en=2))

    await pipeline.ejecutar(tenant_id=TENANT_A, trabajo_id=trabajo.id)

    assert repo.trabajo.estado is EstadoDeTrabajo.CANCELADO
    assert repo.trabajo.contadores.mensajes_revisados < 10


async def test_publica_progreso_durante_la_ejecucion() -> None:
    pipeline, _, _, progreso, trabajo = _montar({"m1": [("a.pdf", pdf_valido())]})
    await pipeline.ejecutar(tenant_id=TENANT_A, trabajo_id=trabajo.id)

    assert len(progreso.eventos) >= 2
    assert progreso.eventos[-1]["estado"] == "succeeded"


async def test_no_reejecuta_un_trabajo_ya_terminado() -> None:
    """
    Segunda mitad de la idempotencia: una reentrega de la cola sobre un
    trabajo terminal se ignora en lugar de volver a procesarlo todo.
    """
    pipeline, _, almacen, _, trabajo = _montar({"m1": [("a.pdf", pdf_valido())]})
    trabajo.marcar_en_ejecucion()
    trabajo.completar()

    await pipeline.ejecutar(tenant_id=TENANT_A, trabajo_id=trabajo.id)
    assert almacen.objetos == {}


async def test_un_trabajo_inexistente_no_lanza() -> None:
    """El worker no debe caerse por un job huerfano: lo registra y sigue."""
    import uuid as _uuid

    pipeline, _, _, _, _ = _montar({})
    await pipeline.ejecutar(tenant_id=TENANT_A, trabajo_id=_uuid.uuid4())


@pytest.mark.parametrize("sin_adjuntos", [{}, {"m1": []}])
async def test_un_buzon_sin_adjuntos_termina_limpio(
    sin_adjuntos: dict[str, list[tuple[str, bytes]]],
) -> None:
    pipeline, repo, _, _, trabajo = _montar(sin_adjuntos)
    await pipeline.ejecutar(tenant_id=TENANT_A, trabajo_id=trabajo.id)

    assert repo.trabajo.estado is EstadoDeTrabajo.COMPLETADO
    assert repo.trabajo.contadores.adjuntos_descargados == 0
