"""
Orquestador del pipeline de ingesta.

Proposito
    Ejecutar un trabajo de escaneo de principio a fin: autenticar, listar
    correos, descargar y validar adjuntos, almacenarlos y dejar el estado
    y los contadores persistidos y publicados.

Flujo
    credencial -> listar mensajes -> descartar ya procesados ->
    por cada mensaje: listar adjuntos -> descargar en streaming ->
    validar (magic bytes, tamaño, hash) -> deduplicar -> subir a storage
    -> persistir -> publicar progreso

Dependencias
    Solo puertos. Este fichero es el que se testea con dobles para cubrir
    el pipeline completo sin red ni base de datos.

Decisiones de diseño
    1. Un error en un adjunto no aborta el escaneo. Se registra y se
       continua: en un buzon de quinientos correos, un PDF corrupto no
       puede invalidar los cuatrocientos noventa y nueve restantes. El
       trabajo acaba como "completado con errores", que es informacion
       util, no un fallo.

    2. La cancelacion se comprueba entre mensajes, no entre bytes.
       Granularidad suficiente para responder en segundos sin convertir
       cada iteracion en una consulta a Redis.

    3. El progreso se persiste y se publica. Persistir permite recuperar
       el estado tras un reinicio; publicar alimenta el SSE. Hacer solo lo
       segundo es lo que convertia el progreso del sistema de referencia
       en algo que se evaporaba al reiniciar.
"""

from __future__ import annotations

from uuid import UUID

from mailauto.modules.ingestion.domain.entities import (
    Adjunto,
    ErrorDeProcesamiento,
    EtapaDeError,
    FaseDeEscaneo,
    MensajeDeCorreo,
    TrabajoDeEscaneo,
)
from mailauto.modules.ingestion.domain.ports import (
    AlmacenDeObjetos,
    CanalDeProgreso,
    ColaDeTrabajos,
    CredencialDeBuzon,
    ProveedorDeCorreo,
    ProveedorDeCredenciales,
    ReferenciaDeAdjunto,
    RepositorioDeIngesta,
    ResumenDeMensaje,
    ValidadorDeAdjuntos,
)
from mailauto.shared.errors import ErrorDeDominio, ErrorDeProveedor
from mailauto.shared.observability.logging import obtener_logger
from mailauto.shared.types import uuid7

logger = obtener_logger(__name__)

# Lote con el que se consulta que mensajes ya se procesaron. Equilibra
# entre muchas consultas pequeñas y una sentencia IN desmesurada.
_TAMANO_DE_LOTE = 200


class EjecutarEscaneo:
    """Pipeline de ingesta. Lo invoca el worker, nunca la API."""

    def __init__(
        self,
        *,
        repositorio: RepositorioDeIngesta,
        credenciales: ProveedorDeCredenciales,
        proveedores: dict[str, ProveedorDeCorreo],
        validador: ValidadorDeAdjuntos,
        almacen: AlmacenDeObjetos,
        cola: ColaDeTrabajos,
        progreso: CanalDeProgreso,
        limite_de_bytes: int,
        maximo_adjuntos_por_mensaje: int,
    ) -> None:
        self._repositorio = repositorio
        self._credenciales = credenciales
        self._proveedores = proveedores
        self._validador = validador
        self._almacen = almacen
        self._cola = cola
        self._progreso = progreso
        self._limite_de_bytes = limite_de_bytes
        self._maximo_adjuntos = maximo_adjuntos_por_mensaje

    # ── Punto de entrada ─────────────────────────────────────────────

    async def ejecutar(self, *, tenant_id: UUID, trabajo_id: UUID) -> None:
        trabajo = await self._repositorio.obtener_trabajo_por_tenant(tenant_id, trabajo_id)
        if trabajo is None:
            logger.warning("trabajo_inexistente", trabajo_id=str(trabajo_id))
            return
        if not trabajo.esta_activo:
            # Reentrega de la cola sobre un trabajo ya terminal: se ignora
            # en vez de reejecutar. Es la segunda mitad de la idempotencia.
            logger.info("trabajo_ya_finalizado", trabajo_id=str(trabajo_id))
            return

        errores: list[ErrorDeProcesamiento] = []

        try:
            trabajo.marcar_en_ejecucion()
            await self._guardar_y_publicar(trabajo)

            credencial = await self._obtener_credencial(trabajo)
            proveedor = self._resolver_proveedor(credencial.proveedor)

            trabajo.avanzar(FaseDeEscaneo.LISTANDO_CORREOS, 10)
            await self._guardar_y_publicar(trabajo)

            await self._procesar_mensajes(trabajo, credencial, proveedor, errores)

            # Solo se completa si el trabajo sigue vivo: el recorrido pudo
            # terminar porque se solicito la cancelacion, y en ese caso ya
            # esta en estado terminal. Completar desde ahi seria una
            # transicion invalida y ademas falsearia el resultado.
            if trabajo.esta_activo:
                trabajo.avanzar(FaseDeEscaneo.FINALIZANDO, 95)
                trabajo.completar()

        except ErrorDeDominio as exc:
            # Error esperado y tipado: se reporta con su codigo de dominio,
            # que es lo que la UI necesita para decirle algo util al usuario.
            # La guarda evita enmascarar una cancelacion ya registrada.
            if trabajo.esta_activo:
                trabajo.fallar(codigo=exc.codigo, mensaje=exc.mensaje_publico)
            errores.append(
                ErrorDeProcesamiento(
                    tenant_id=trabajo.tenant_id,
                    trabajo_id=trabajo.id,
                    etapa=EtapaDeError.LISTADO,
                    codigo=exc.codigo,
                    mensaje=exc.mensaje_publico,
                    reintentable=getattr(exc, "reintentable", False),
                )
            )
            logger.warning("escaneo_fallido", trabajo_id=str(trabajo.id), codigo=exc.codigo)

        except Exception as exc:
            # Error no previsto: el detalle va al log, nunca al usuario.
            if trabajo.esta_activo:
                trabajo.fallar(codigo="error_interno", mensaje="El escaneo no pudo completarse.")
            logger.exception("escaneo_error_inesperado", trabajo_id=str(trabajo.id))
            errores.append(
                ErrorDeProcesamiento(
                    tenant_id=trabajo.tenant_id,
                    trabajo_id=trabajo.id,
                    etapa=EtapaDeError.LISTADO,
                    codigo="error_interno",
                    mensaje=type(exc).__name__,
                    reintentable=True,
                )
            )

        finally:
            if errores:
                await self._repositorio.guardar_errores(trabajo.tenant_id, errores)
            await self._guardar_y_publicar(trabajo)

    # ── Etapas ───────────────────────────────────────────────────────

    async def _obtener_credencial(self, trabajo: TrabajoDeEscaneo) -> CredencialDeBuzon:
        trabajo.avanzar(FaseDeEscaneo.AUTENTICANDO, 5)
        return await self._credenciales.obtener(
            tenant_id=trabajo.tenant_id, conexion_id=trabajo.conexion_id
        )

    def _resolver_proveedor(self, nombre: str) -> ProveedorDeCorreo:
        proveedor = self._proveedores.get(nombre)
        if proveedor is None:
            raise ErrorDeProveedor(
                f"El proveedor '{nombre}' no esta disponible.",
                proveedor=nombre,
                reintentable=False,
            )
        return proveedor

    async def _procesar_mensajes(
        self,
        trabajo: TrabajoDeEscaneo,
        credencial: CredencialDeBuzon,
        proveedor: ProveedorDeCorreo,
        errores: list[ErrorDeProcesamiento],
    ) -> None:
        lote: list[ResumenDeMensaje] = []
        limite = trabajo.parametros.limite_de_mensajes

        async for resumen in proveedor.listar_mensajes(
            credencial.access_token,
            desde=trabajo.parametros.desde,
            hasta=trabajo.parametros.hasta,
            limite=limite,
            carpeta=trabajo.parametros.carpeta,
        ):
            lote.append(resumen)
            if len(lote) >= _TAMANO_DE_LOTE:
                if await self._procesar_lote(trabajo, credencial, proveedor, lote, errores):
                    return
                lote = []

        if lote:
            await self._procesar_lote(trabajo, credencial, proveedor, lote, errores)

    async def _procesar_lote(
        self,
        trabajo: TrabajoDeEscaneo,
        credencial: CredencialDeBuzon,
        proveedor: ProveedorDeCorreo,
        lote: list[ResumenDeMensaje],
        errores: list[ErrorDeProcesamiento],
    ) -> bool:
        """Procesa un lote. Devuelve True si hay que detenerse por cancelacion."""
        # Una sola consulta para todo el lote: el filtro de idempotencia
        # no puede costar una consulta por mensaje.
        ya_procesados = await self._repositorio.ids_de_mensajes_procesados(
            trabajo.tenant_id,
            credencial.proveedor,
            [r.id_del_proveedor for r in lote],
        )

        for resumen in lote:
            if await self._cola.cancelacion_solicitada(trabajo.id):
                trabajo.cancelar()
                return True

            trabajo.contadores.mensajes_revisados += 1

            if resumen.id_del_proveedor in ya_procesados:
                continue

            try:
                await self._procesar_mensaje(trabajo, credencial, proveedor, resumen, errores)
            except ErrorDeDominio as exc:
                trabajo.contadores.errores += 1
                errores.append(
                    ErrorDeProcesamiento(
                        tenant_id=trabajo.tenant_id,
                        trabajo_id=trabajo.id,
                        etapa=EtapaDeError.DESCARGA,
                        codigo=exc.codigo,
                        mensaje=exc.mensaje_publico,
                        contexto={"mensaje_id": resumen.id_del_proveedor},
                        reintentable=getattr(exc, "reintentable", False),
                    )
                )

            self._actualizar_progreso(trabajo)
            # Se publica correo a correo (solo Redis, barato): asi la barra
            # avanza de forma continua en lugar de quedarse clavada y saltar
            # al terminar el lote. El estado duradero se persiste una vez por
            # lote, mas abajo, que es lo que de verdad hace falta guardar.
            await self._publicar_progreso(trabajo)

        await self._guardar_y_publicar(trabajo)
        return False

    async def _procesar_mensaje(
        self,
        trabajo: TrabajoDeEscaneo,
        credencial: CredencialDeBuzon,
        proveedor: ProveedorDeCorreo,
        resumen: ResumenDeMensaje,
        errores: list[ErrorDeProcesamiento],
    ) -> None:
        referencias = await proveedor.listar_adjuntos(
            credencial.access_token, resumen.id_del_proveedor
        )
        if not referencias:
            return

        trabajo.contadores.mensajes_con_adjuntos += 1
        trabajo.avanzar(FaseDeEscaneo.DESCARGANDO_ADJUNTOS, trabajo.progreso_porcentaje)

        mensaje = await self._repositorio.guardar_mensaje(
            trabajo.tenant_id,
            MensajeDeCorreo(
                tenant_id=trabajo.tenant_id,
                trabajo_id=trabajo.id,
                proveedor=credencial.proveedor,
                id_del_proveedor=resumen.id_del_proveedor,
                remitente=resumen.remitente,
                asunto=resumen.asunto,
                recibido_en=resumen.recibido_en,
            ),
        )

        # Tope de adjuntos por mensaje: un correo con mil adjuntos
        # pequeños agota el worker tanto como uno con un adjunto enorme.
        for referencia in referencias[: self._maximo_adjuntos]:
            await self._procesar_adjunto(
                trabajo, credencial, proveedor, resumen, mensaje, referencia, errores
            )

    async def _procesar_adjunto(
        self,
        trabajo: TrabajoDeEscaneo,
        credencial: CredencialDeBuzon,
        proveedor: ProveedorDeCorreo,
        resumen: ResumenDeMensaje,
        mensaje: MensajeDeCorreo,
        referencia: ReferenciaDeAdjunto,
        errores: list[ErrorDeProcesamiento],
    ) -> None:
        # Descarte temprano por tamaño declarado: evita iniciar una
        # transferencia que sabemos que vamos a abortar.
        if referencia.tamano_declarado > self._limite_de_bytes:
            trabajo.contadores.adjuntos_rechazados += 1
            errores.append(
                self._error_de_adjunto(
                    trabajo, referencia.nombre, "tamano_excedido", EtapaDeError.VALIDACION
                )
            )
            return

        trozos: list[bytes] = []
        acumulado = 0
        async for trozo in proveedor.descargar_adjunto(
            credencial.access_token,
            id_del_mensaje=resumen.id_del_proveedor,
            id_del_adjunto=referencia.id_del_adjunto,
            limite_de_bytes=self._limite_de_bytes,
        ):
            acumulado += len(trozo)
            if acumulado > self._limite_de_bytes:
                # Segundo corte, por si el tamaño declarado mentia. El
                # proveedor es una fuente no confiable como cualquier otra.
                trabajo.contadores.adjuntos_rechazados += 1
                errores.append(
                    self._error_de_adjunto(
                        trabajo, referencia.nombre, "tamano_excedido", EtapaDeError.VALIDACION
                    )
                )
                return
            trozos.append(trozo)

        contenido = b"".join(trozos)
        validacion = self._validador.validar(contenido, nombre=referencia.nombre)

        if not validacion.aceptado:
            trabajo.contadores.adjuntos_rechazados += 1
            errores.append(
                self._error_de_adjunto(
                    trabajo,
                    referencia.nombre,
                    validacion.motivo_de_rechazo or "rechazado",
                    EtapaDeError.VALIDACION,
                )
            )
            return

        if await self._repositorio.existe_adjunto_con_hash(trabajo.tenant_id, validacion.sha256):
            trabajo.contadores.adjuntos_duplicados += 1
            return

        # Clave opaca: el nombre que venia en el correo no influye en
        # donde se escribe el objeto (path traversal cerrado por diseño).
        clave = f"{trabajo.tenant_id}/{uuid7()}"
        await self._almacen.guardar(
            clave=clave,
            contenido=contenido,
            tipo_mime=validacion.tipo_mime_real,
            metadatos={
                "tenant_id": str(trabajo.tenant_id),
                "trabajo_id": str(trabajo.id),
                "sha256": validacion.sha256,
            },
        )

        adjunto = await self._repositorio.guardar_adjunto(
            trabajo.tenant_id,
            Adjunto(
                tenant_id=trabajo.tenant_id,
                mensaje_id=mensaje.id,
                nombre_original=referencia.nombre[:255],
                clave_de_almacenamiento=clave,
                tipo_mime=validacion.tipo_mime_real,
                tamano_bytes=len(contenido),
                sha256=validacion.sha256,
            ),
        )
        trabajo.contadores.adjuntos_descargados += 1

        # La extraccion va en su propio job y no aqui mismo: parsear un
        # PDF puede tardar segundos y bloquearia la descarga de los
        # correos restantes. Separarlos permite ademas escalar los dos
        # workers por separado, que es lo que hace falta cuando el
        # cuello de botella es el OCR y no la red.
        await self._cola.encolar_extraccion(
            tenant_id=trabajo.tenant_id,
            trabajo_id=trabajo.id,
            adjunto_id=adjunto.id,
            clave_de_almacenamiento=clave,
            tipo_mime=validacion.tipo_mime_real,
            nombre=referencia.nombre[:255],
        )

    # ── Auxiliares ───────────────────────────────────────────────────

    @staticmethod
    def _error_de_adjunto(
        trabajo: TrabajoDeEscaneo, nombre: str, codigo: str, etapa: EtapaDeError
    ) -> ErrorDeProcesamiento:
        return ErrorDeProcesamiento(
            tenant_id=trabajo.tenant_id,
            trabajo_id=trabajo.id,
            etapa=etapa,
            codigo=codigo,
            mensaje=f"Adjunto rechazado: {codigo}",
            contexto={"archivo": nombre[:120]},
            reintentable=False,
        )

    def _actualizar_progreso(self, trabajo: TrabajoDeEscaneo) -> None:
        """
        Progreso estimado sobre el limite solicitado.

        Se acota al 90 % durante el recorrido: el 100 % se reserva para la
        finalizacion real, de modo que la barra no se quede clavada en
        "100 %" mientras todavia queda trabajo.
        """
        limite = max(1, trabajo.parametros.limite_de_mensajes)
        avance = int((trabajo.contadores.mensajes_revisados / limite) * 85) + 10
        trabajo.avanzar(trabajo.fase, min(avance, 90))

    async def _guardar_y_publicar(self, trabajo: TrabajoDeEscaneo) -> None:
        await self._repositorio.actualizar_trabajo(trabajo.tenant_id, trabajo)
        await self._progreso.publicar(trabajo.id, self._evento(trabajo))

    async def _publicar_progreso(self, trabajo: TrabajoDeEscaneo) -> None:
        """
        Publica el progreso en vivo sin tocar la base de datos.

        Persistir en cada correo multiplicaria las escrituras sin aportar
        nada: el estado duradero se guarda por lote y basta para recuperar
        el trabajo tras un reinicio. Esto solo alimenta la barra del SSE.
        """
        await self._progreso.publicar(trabajo.id, self._evento(trabajo))

    @staticmethod
    def _evento(trabajo: TrabajoDeEscaneo) -> dict[str, object]:
        return {
            "trabajo_id": str(trabajo.id),
            "estado": trabajo.estado.value,
            "fase": trabajo.fase.value,
            "progreso_porcentaje": trabajo.progreso_porcentaje,
            "contadores": trabajo.contadores.como_dict(),
            "codigo_de_error": trabajo.codigo_de_error,
        }
