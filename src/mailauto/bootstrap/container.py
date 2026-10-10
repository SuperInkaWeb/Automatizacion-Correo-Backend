"""
Composition root: el unico lugar que conoce todas las capas.

Proposito
    Construir el grafo de dependencias una sola vez al arrancar y
    entregarlo ya cableado a la API y a los workers.

Flujo
    Settings -> recursos (BD, Redis, storage) -> adaptadores ->
    casos de uso -> Contenedor

Dependencias
    Todas. Es la excepcion explicita a las reglas de capas de
    importlinter: alguien tiene que conectar los puertos con sus
    adaptadores, y concentrarlo aqui es lo que mantiene limpio el resto.

Decision de diseño
    Contenedor propio en lugar de una libreria de inyeccion de
    dependencias. Son unas pocas docenas de objetos con un grafo que cabe
    en una pantalla; `dependency-injector` añadiria una dependencia, una
    sintaxis que aprender y errores de cableado en tiempo de ejecucion a
    cambio de nada. Si el grafo creciera hasta hacerse inmanejable, este
    fichero seria el unico que habria que cambiar (regla 10 de las
    normas del proyecto: no añadir dependencias sin justificacion).
"""

from __future__ import annotations

from dataclasses import dataclass

from arq import create_pool
from arq.connections import ArqRedis, RedisSettings
from redis.asyncio import Redis

from mailauto.bootstrap.adaptadores import (
    AdaptadorDeCredenciales,
    AdaptadorDeDestinoDeReportes,
    AdaptadorDeFilasDeReporte,
    AdaptadorDeLecturaDeAdjuntos,
)
from mailauto.bootstrap.settings import Settings
from mailauto.modules.audit.domain.ports import RegistroDeAuditoria
from mailauto.modules.audit.infrastructure.repository import RegistroDeAuditoriaPostgres
from mailauto.modules.extraction.application.extraer_documento import (
    ControlDePresupuesto,
    ExtraerDocumento,
)
from mailauto.modules.extraction.application.revisar_registros import (
    ConsultarRegistros,
    RevisarRegistro,
)
from mailauto.modules.extraction.domain.ports import EstrategiaDeExtraccion
from mailauto.modules.extraction.infrastructure.persistence.repository import (
    RepositorioDeRegistrosPostgres,
)
from mailauto.modules.extraction.infrastructure.profiles.sunat_arrendamiento import (
    PerfilSunatArrendamiento,
)
from mailauto.modules.extraction.infrastructure.strategies.ocr import OcrLocal
from mailauto.modules.extraction.infrastructure.strategies.pdf import (
    TablasDePdf,
    TextoNativoDePdf,
)
from mailauto.modules.extraction.infrastructure.strategies.vision import (
    VisionIA,
    crear_cliente as crear_cliente_anthropic,
)
from mailauto.modules.extraction.infrastructure.strategies.vision_groq import (
    VisionIAGroq,
    crear_cliente as crear_cliente_groq,
)
from mailauto.modules.identity.application.resolver_identidad import ResolverIdentidad
from mailauto.modules.identity.infrastructure.repository import (
    RepositorioDeIdentidadPostgres,
)
from mailauto.modules.ingestion.application.ejecutar_escaneo import EjecutarEscaneo
from mailauto.modules.ingestion.application.gestionar_escaneos import (
    CancelarEscaneo,
    ConsultarEscaneo,
    ConsultarOrigenDeAdjuntos,
    IniciarEscaneo,
    ObtenerUrlDeAdjunto,
)
from mailauto.modules.ingestion.domain.ports import ProveedorDeCorreo
from mailauto.modules.ingestion.infrastructure.almacen_s3 import AlmacenDeObjetosS3
from mailauto.modules.ingestion.infrastructure.cola_redis import (
    CanalDeProgresoRedis,
    ColaDeTrabajosRedis,
)
from mailauto.modules.ingestion.infrastructure.providers.correo import (
    ProveedorGmail,
    ProveedorMicrosoftGraph,
)
from mailauto.modules.ingestion.infrastructure.repository import (
    RepositorioDeIngestaPostgres,
)
from mailauto.modules.ingestion.infrastructure.validador import (
    ValidadorDeAdjuntosPorContenido,
)
from mailauto.modules.mailbox.application.gestionar_buzones import (
    DesvincularBuzon,
    ListarBuzones,
    ObtenerTokenVigente,
)
from mailauto.modules.mailbox.application.vincular_buzon import (
    CompletarVinculacion,
    IniciarVinculacion,
)
from mailauto.modules.mailbox.domain.entities import Proveedor
from mailauto.modules.mailbox.domain.ports import ProveedorOAuth
from mailauto.modules.mailbox.infrastructure.providers.base import (
    ProveedorGoogle,
    ProveedorMicrosoft,
)
from mailauto.modules.mailbox.infrastructure.repository import (
    RepositorioDeBuzonesPostgres,
)
from mailauto.modules.mailbox.infrastructure.state_store import (
    AlmacenDeEstadoOAuthRedis,
)
from mailauto.modules.reporting.application.exportar import (
    ConsultarExportacion,
    GenerarExportacion,
    SolicitarExportacion,
)
from mailauto.modules.reporting.domain.ports import FormatoDeReporte, GeneradorDeReporte
from mailauto.modules.reporting.infrastructure.generadores import (
    GeneradorCsv,
    GeneradorExcel,
)
from mailauto.modules.reporting.infrastructure.repository import (
    RepositorioDeExportacionesPostgres,
)
from mailauto.shared.crypto.envelope import ClaveMaestraLocal, ServicioDeCifrado
from mailauto.shared.cuotas import ControlDeCuotas, CuotasEnRedis
from mailauto.shared.db.session import FabricaDeSesiones, crear_engine
from mailauto.shared.security.jwt_verifier import VerificadorDeTokens


@dataclass(slots=True)
class Contenedor:
    """Grafo de dependencias ya construido."""

    settings: Settings
    sesiones: FabricaDeSesiones
    redis: Redis
    arq: ArqRedis
    almacen: AlmacenDeObjetosS3
    verificador: VerificadorDeTokens
    auditoria: RegistroDeAuditoria
    cuotas: ControlDeCuotas

    # Casos de uso
    resolver_identidad: ResolverIdentidad
    iniciar_vinculacion: IniciarVinculacion
    completar_vinculacion: CompletarVinculacion
    listar_buzones: ListarBuzones
    desvincular_buzon: DesvincularBuzon
    iniciar_escaneo: IniciarEscaneo
    cancelar_escaneo: CancelarEscaneo
    consultar_escaneo: ConsultarEscaneo
    consultar_origen_de_adjuntos: ConsultarOrigenDeAdjuntos
    obtener_url_de_adjunto: ObtenerUrlDeAdjunto
    ejecutar_escaneo: EjecutarEscaneo
    extraer_documento: ExtraerDocumento
    consultar_registros: ConsultarRegistros
    revisar_registro: RevisarRegistro
    solicitar_exportacion: SolicitarExportacion
    consultar_exportacion: ConsultarExportacion
    generar_exportacion: GenerarExportacion
    lector_de_adjuntos: AdaptadorDeLecturaDeAdjuntos

    # Infraestructura que la API necesita directamente
    cola: ColaDeTrabajosRedis
    progreso: CanalDeProgresoRedis

    async def cerrar(self) -> None:
        """Libera recursos en el apagado. El orden evita cortar operaciones vivas."""
        await self.sesiones.cerrar()
        await self.arq.aclose()
        await self.redis.aclose()


async def construir_contenedor(settings: Settings) -> Contenedor:
    """Crea y cablea todo. Se invoca una vez, en el arranque."""

    # ── Recursos ─────────────────────────────────────────────────────
    engine = crear_engine(
        str(settings.database_url),
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        command_timeout=settings.db_command_timeout_seconds,
        echo=settings.db_echo,
    )
    sesiones = FabricaDeSesiones(engine)

    redis: Redis = Redis.from_url(str(settings.redis_url), decode_responses=True, socket_timeout=5)
    arq = await create_pool(RedisSettings.from_dsn(str(settings.redis_url)))

    almacen = AlmacenDeObjetosS3(
        bucket=settings.storage_bucket,
        region=settings.storage_region,
        access_key=settings.storage_access_key,
        secret_key=settings.storage_secret_key,
        endpoint_url=settings.storage_endpoint_url,
        ttl_presigned=settings.storage_presign_ttl_seconds,
    )

    # ── Criptografia ─────────────────────────────────────────────────
    # `Settings` ya impide KMS_PROVIDER='local' en produccion, asi que
    # este adaptador solo puede alcanzarse en desarrollo y pruebas.
    cifrado = ServicioDeCifrado(ClaveMaestraLocal(settings.master_key))

    # ── Identidad ────────────────────────────────────────────────────
    verificador = VerificadorDeTokens(
        issuer=settings.oidc_issuer,
        audience=settings.oidc_audience,
        roles_claim=settings.oidc_roles_claim,
        jwks_cache_seconds=settings.oidc_jwks_cache_seconds,
    )
    repo_identidad = RepositorioDeIdentidadPostgres(sesiones)
    auditoria = RegistroDeAuditoriaPostgres(sesiones)

    # ── Buzones ──────────────────────────────────────────────────────
    proveedores_oauth: dict[Proveedor, ProveedorOAuth] = {}
    proveedores_correo: dict[str, ProveedorDeCorreo] = {}

    if settings.google_habilitado:
        proveedores_oauth[Proveedor.GOOGLE] = ProveedorGoogle(
            client_id=settings.google_client_id or "",
            client_secret=settings.google_client_secret or "",
        )
        proveedores_correo["google"] = ProveedorGmail()

    if settings.microsoft_habilitado:
        proveedores_oauth[Proveedor.MICROSOFT] = ProveedorMicrosoft(
            client_id=settings.microsoft_client_id or "",
            client_secret=settings.microsoft_client_secret or "",
            tenant_id=settings.microsoft_tenant_id,
        )
        proveedores_correo["microsoft"] = ProveedorMicrosoftGraph()

    repo_buzones = RepositorioDeBuzonesPostgres(sesiones, cifrado)
    almacen_estado = AlmacenDeEstadoOAuthRedis(redis)
    obtener_token = ObtenerTokenVigente(repo_buzones, proveedores_oauth)

    # ── Ingesta ──────────────────────────────────────────────────────
    repo_ingesta = RepositorioDeIngestaPostgres(sesiones)
    cola = ColaDeTrabajosRedis(arq, redis)
    progreso = CanalDeProgresoRedis(redis)
    validador = ValidadorDeAdjuntosPorContenido(
        tipos_permitidos=settings.allowed_mime_types,
        tamano_maximo=settings.max_attachment_bytes,
    )

    # ── Extraccion ───────────────────────────────────────────────────
    repo_registros = RepositorioDeRegistrosPostgres(sesiones)
    perfil = PerfilSunatArrendamiento()

    # Los motores gratuitos siempre; el de pago solo si hay credencial
    # y esta habilitado. Sin el, el pipeline sigue funcionando y los
    # documentos ilegibles van a revision humana.
    estrategias: list[EstrategiaDeExtraccion] = [
        TextoNativoDePdf(),
        TablasDePdf(),
        OcrLocal(),
    ]
    # Vision IA: un solo proveedor activo, elegido por configuracion
    # (Groq por defecto). El otro queda disponible sin tocar codigo. Sin
    # la credencial del elegido no se registra nada y el pipeline sigue
    # con los tres motores gratuitos.
    estrategia_vision: EstrategiaDeExtraccion | None = None
    if settings.vision_ai_habilitada:
        if settings.vision_proveedor == "groq":
            cliente_groq = crear_cliente_groq(
                settings.groq_api_key, base_url=settings.groq_base_url
            )
            if cliente_groq is not None:
                estrategia_vision = VisionIAGroq(
                    cliente_groq, modelo=settings.groq_vision_modelo
                )
        else:
            cliente_anthropic = crear_cliente_anthropic(settings.anthropic_api_key)
            if cliente_anthropic is not None:
                estrategia_vision = VisionIA(
                    cliente_anthropic,
                    modelo=settings.vision_modelo,
                    esfuerzo=settings.vision_esfuerzo,
                )
    if estrategia_vision is not None:
        estrategias.append(estrategia_vision)

    # ── Reportes ─────────────────────────────────────────────────────
    repo_exportaciones = RepositorioDeExportacionesPostgres(sesiones)
    destino_reportes = AdaptadorDeDestinoDeReportes(almacen, settings.storage_presign_ttl_seconds)
    generadores: dict[FormatoDeReporte, GeneradorDeReporte] = {
        FormatoDeReporte.EXCEL: GeneradorExcel(),
        FormatoDeReporte.CSV: GeneradorCsv(),
    }

    return Contenedor(
        settings=settings,
        sesiones=sesiones,
        redis=redis,
        arq=arq,
        almacen=almacen,
        verificador=verificador,
        auditoria=auditoria,
        cuotas=CuotasEnRedis(redis),
        resolver_identidad=ResolverIdentidad(
            repo_identidad, auto_aprovisionar=settings.auto_aprovisionar_tenant
        ),
        iniciar_vinculacion=IniciarVinculacion(
            proveedores_oauth,
            almacen_estado,
            redirect_uris_permitidos=settings.oauth_redirect_uris,
            ttl_estado_segundos=settings.oauth_state_ttl_seconds,
        ),
        completar_vinculacion=CompletarVinculacion(proveedores_oauth, almacen_estado, repo_buzones),
        listar_buzones=ListarBuzones(repo_buzones),
        desvincular_buzon=DesvincularBuzon(repo_buzones, proveedores_oauth),
        iniciar_escaneo=IniciarEscaneo(
            repo_ingesta,
            cola,
            maximo_mensajes=settings.max_messages_per_scan,
            maximo_dias=settings.max_scan_date_range_days,
            maximo_concurrentes=settings.tenant_max_concurrent_scans,
        ),
        cancelar_escaneo=CancelarEscaneo(repo_ingesta, cola),
        consultar_escaneo=ConsultarEscaneo(repo_ingesta),
        consultar_origen_de_adjuntos=ConsultarOrigenDeAdjuntos(repo_ingesta),
        obtener_url_de_adjunto=ObtenerUrlDeAdjunto(
            repo_ingesta, almacen, ttl_segundos=settings.storage_presign_ttl_seconds
        ),
        ejecutar_escaneo=EjecutarEscaneo(
            repositorio=repo_ingesta,
            credenciales=AdaptadorDeCredenciales(obtener_token),
            proveedores=proveedores_correo,
            validador=validador,
            almacen=almacen,
            cola=cola,
            progreso=progreso,
            limite_de_bytes=settings.max_attachment_bytes,
            maximo_adjuntos_por_mensaje=settings.max_attachments_per_message,
        ),
        extraer_documento=ExtraerDocumento(
            estrategias=estrategias,
            perfil=perfil,
            repositorio=repo_registros,
            presupuesto=ControlDePresupuesto(
                ia_habilitada=estrategia_vision is not None,
                maximo_llamadas_por_trabajo=settings.vision_maximo_llamadas_por_trabajo,
            ),
        ),
        consultar_registros=ConsultarRegistros(repo_registros),
        revisar_registro=RevisarRegistro(repo_registros),
        solicitar_exportacion=SolicitarExportacion(repo_exportaciones, cola.encolar_exportacion),
        consultar_exportacion=ConsultarExportacion(repo_exportaciones, destino_reportes),
        generar_exportacion=GenerarExportacion(
            repositorio=repo_exportaciones,
            fuente=AdaptadorDeFilasDeReporte(repo_registros),
            generadores=generadores,
            destino=destino_reportes,
            limite_de_filas=settings.reporte_maximo_filas,
        ),
        lector_de_adjuntos=AdaptadorDeLecturaDeAdjuntos(almacen),
        cola=cola,
        progreso=progreso,
    )
