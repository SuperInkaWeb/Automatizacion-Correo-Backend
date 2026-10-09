"""
Casos de uso de control de escaneos: iniciar, cancelar y consultar.

Proposito
    Exponer el ciclo de vida de un trabajo aplicando cuotas, idempotencia
    y permisos antes de tocar la cola.

Dependencias
    Puertos del dominio de ingesta y del dominio de buzones.

Decisiones de diseño
    1. Idempotencia por `Idempotency-Key`. Un doble clic o un reintento de
       red no deben lanzar dos escaneos: si la clave ya existe, se
       devuelve el trabajo original en lugar de crear otro.

    2. Limite de trabajos concurrentes por tenant. Sin el, un tenant
       encola cien escaneos y monopoliza los workers compartidos: un
       problema de equidad que acaba siendo una denegacion de servicio
       para los demas.

    3. La cancelacion es cooperativa (una señal que el worker consulta)
       y no una interrupcion. Matar el job dejaria adjuntos a medio subir
       y contadores sin cuadrar.
"""

from __future__ import annotations

from uuid import UUID

from mailauto.modules.ingestion.domain.entities import (
    ErrorDeProcesamiento,
    ParametrosDeEscaneo,
    TrabajoDeEscaneo,
)
from mailauto.modules.ingestion.domain.ports import (
    ColaDeTrabajos,
    OrigenDeAdjunto,
    RepositorioDeIngesta,
)
from mailauto.shared.errors import LimiteExcedido, RecursoNoEncontrado
from mailauto.shared.pagination import Pagina, SolicitudDePagina
from mailauto.shared.security.context import Permiso, TenantContext


class IniciarEscaneo:
    def __init__(
        self,
        repositorio: RepositorioDeIngesta,
        cola: ColaDeTrabajos,
        *,
        maximo_mensajes: int,
        maximo_dias: int,
        maximo_concurrentes: int,
    ) -> None:
        self._repositorio = repositorio
        self._cola = cola
        self._maximo_mensajes = maximo_mensajes
        self._maximo_dias = maximo_dias
        self._maximo_concurrentes = maximo_concurrentes

    async def ejecutar(
        self,
        ctx: TenantContext,
        *,
        conexion_id: UUID,
        parametros: ParametrosDeEscaneo,
        clave_de_idempotencia: str | None = None,
    ) -> TrabajoDeEscaneo:
        ctx.exigir(Permiso.SCAN_RUN)

        # La idempotencia se comprueba antes que la cuota: reintentar una
        # peticion ya atendida no debe consumir cupo ni fallar por limite.
        if clave_de_idempotencia:
            existente = await self._repositorio.buscar_por_idempotencia(ctx, clave_de_idempotencia)
            if existente is not None:
                return existente

        parametros.validar(maximo_mensajes=self._maximo_mensajes, maximo_dias=self._maximo_dias)

        activos = await self._repositorio.contar_trabajos_activos(ctx.tenant_id)
        if activos >= self._maximo_concurrentes:
            raise LimiteExcedido(
                f"Ya hay {activos} escaneos en curso. Espera a que terminen.",
                reintentar_en_segundos=60,
            )

        trabajo = TrabajoDeEscaneo(
            tenant_id=ctx.tenant_id,
            solicitado_por=ctx.user_id,
            conexion_id=conexion_id,
            parametros=parametros,
            clave_de_idempotencia=clave_de_idempotencia,
        )
        creado = await self._repositorio.crear_trabajo(ctx, trabajo)

        # Se encola despues de persistir: si la cola fallara, queda un
        # trabajo visible en estado "en cola" que el cron puede reintentar.
        # Al reves, un job encolado sin fila en la base de datos es un
        # mensaje huerfano que el worker no sabria resolver.
        await self._cola.encolar_escaneo(tenant_id=ctx.tenant_id, trabajo_id=creado.id)
        return creado


class CancelarEscaneo:
    def __init__(self, repositorio: RepositorioDeIngesta, cola: ColaDeTrabajos) -> None:
        self._repositorio = repositorio
        self._cola = cola

    async def ejecutar(self, ctx: TenantContext, trabajo_id: UUID) -> TrabajoDeEscaneo:
        ctx.exigir(Permiso.SCAN_RUN)

        trabajo = await self._repositorio.obtener_trabajo(ctx, trabajo_id)
        if trabajo is None:
            raise RecursoNoEncontrado("El escaneo no existe.")
        ctx.exigir_mismo_tenant(trabajo.tenant_id)

        trabajo.cancelar()  # valida la transicion; lanza si ya era terminal
        await self._repositorio.actualizar_trabajo(ctx.tenant_id, trabajo)
        await self._cola.solicitar_cancelacion(trabajo_id)
        return trabajo


class ConsultarEscaneo:
    def __init__(self, repositorio: RepositorioDeIngesta) -> None:
        self._repositorio = repositorio

    async def obtener(self, ctx: TenantContext, trabajo_id: UUID) -> TrabajoDeEscaneo:
        ctx.exigir(Permiso.SCAN_READ)
        trabajo = await self._repositorio.obtener_trabajo(ctx, trabajo_id)
        if trabajo is None:
            raise RecursoNoEncontrado("El escaneo no existe.")
        ctx.exigir_mismo_tenant(trabajo.tenant_id)
        return trabajo

    async def listar(
        self, ctx: TenantContext, pagina: SolicitudDePagina
    ) -> Pagina[TrabajoDeEscaneo]:
        ctx.exigir(Permiso.SCAN_READ)
        return await self._repositorio.listar_trabajos(ctx, pagina)

    async def listar_errores(
        self, ctx: TenantContext, pagina: SolicitudDePagina, trabajo_id: UUID | None = None
    ) -> Pagina[ErrorDeProcesamiento]:
        """
        Errores de procesamiento, opcionalmente de un trabajo concreto.

        Cuando se filtra por trabajo se comprueba primero que pertenece al
        tenant: sin esa comprobacion, el filtro seria un parametro con el
        que sondear identificadores ajenos.
        """
        ctx.exigir(Permiso.SCAN_READ)
        if trabajo_id is not None:
            await self.obtener(ctx, trabajo_id)
        return await self._repositorio.listar_errores(ctx, pagina, trabajo_id)


class ConsultarOrigenDeAdjuntos:
    """
    Procedencia (correo y nombre de archivo) de un lote de adjuntos.

    La usa la capa API para enriquecer los registros extraidos con el
    correo del que salieron, sin que el modulo de extraccion tenga que
    conocer las tablas de ingesta: el composition root conecta ambos.
    """

    def __init__(self, repositorio: RepositorioDeIngesta) -> None:
        self._repositorio = repositorio

    async def de_adjuntos(
        self, ctx: TenantContext, adjunto_ids: list[UUID]
    ) -> dict[UUID, OrigenDeAdjunto]:
        ctx.exigir(Permiso.RECORD_READ)
        return await self._repositorio.origen_de_adjuntos(ctx, adjunto_ids)
