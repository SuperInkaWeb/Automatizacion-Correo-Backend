"""
Cifrado sobre envolvente (envelope encryption) para secretos en reposo.

Proposito
    Proteger los tokens OAuth de los buzones vinculados, que son la
    credencial mas sensible del sistema: quien los obtiene lee el correo
    del usuario.

Flujo
    KEK (clave maestra, en KMS)
        └─ envuelve ─> DEK por tenant (almacenada cifrada en la BD)
                           └─ cifra ─> tokens de ese tenant

Dependencias
    cryptography (AES-GCM). `ProveedorDeClaveMaestra` abstrae de donde sale
    la KEK, para que pasar de `.env` a AWS KMS sea cambiar un adaptador.

Decisiones de diseño
    1. Una DEK por tenant, no una clave global. Comprometer la DEK de un
       tenant no expone a los demas, y permite el derecho de supresion
       criptografico: destruir la DEK inutiliza todos sus datos cifrados.

    2. AAD (datos autenticados adicionales) = tenant|proposito|sujeto.
       GCM autentica la AAD sin cifrarla, asi que un ciphertext copiado a
       otra fila falla al descifrar. Sin AAD, un atacante con acceso de
       escritura a la BD podria mover el token cifrado del tenant A a la
       fila del tenant B y hacer que el sistema lo use en su nombre.

    3. `key_version` viaja en la envoltura. Permite rotar la KEK sin
       downtime: lo nuevo se cifra con la version actual y un job de cron
       recifra lo viejo en segundo plano.

    4. Nonce de 96 bits aleatorio por operacion. Es el tamaño nativo de
       GCM (evita el rehash interno) y nunca se reutiliza con la misma
       clave, que es la unica condicion que rompe GCM catastroficamente.
"""

from __future__ import annotations

import os
import struct
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Final

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from mailauto.shared.errors import ErrorDeCifrado

LONGITUD_CLAVE: Final = 32  # AES-256
LONGITUD_NONCE: Final = 12  # 96 bits, tamaño nativo de GCM
_MAGIC: Final = b"ME1"  # marca de formato: Mailauto Envelope v1
_CABECERA = struct.Struct(">3sBH")  # magic, version_formato, key_version


# ─────────────────────────────────────────────────────────────────────
# Proveedor de clave maestra (KEK)
# ─────────────────────────────────────────────────────────────────────


class ProveedorDeClaveMaestra(ABC):
    """
    Origen de la clave de cifrado de claves (KEK).

    Abstraer esto es lo que permite que desarrollo use una clave de entorno
    y produccion use KMS sin que el resto del codigo se entere.
    """

    @abstractmethod
    def obtener(self, version: int) -> bytes:
        """Devuelve la KEK de la version indicada."""

    @property
    @abstractmethod
    def version_actual(self) -> int:
        """Version con la que se cifran los datos nuevos."""


class ClaveMaestraLocal(ProveedorDeClaveMaestra):
    """
    KEK desde configuracion (`MASTER_KEY_B64`).

    Es el unico proveedor implementado. En produccion `Settings` solo deja
    usarlo si el operador acepta el compromiso de forma explicita
    (`KMS_LOCAL_EN_PRODUCCION_ACEPTADO=true`): sin un KMS gestionado, la
    clave vive en un secreto del entorno, lo que es aceptable para empezar
    pero debe ser una decision consciente. Los adaptadores `aws`/`vault`
    quedan como trabajo futuro; mientras no existan, el arranque los
    rechaza en lugar de usar esta clave fingiendo que es un KMS.
    """

    def __init__(self, clave: bytes, version: int = 1) -> None:
        if len(clave) != LONGITUD_CLAVE:
            raise ValueError(f"La clave maestra debe tener {LONGITUD_CLAVE} bytes")
        self._clave = clave
        self._version = version

    def obtener(self, version: int) -> bytes:
        if version != self._version:
            raise ErrorDeCifrado(contexto={"version_solicitada": version})
        return self._clave

    @property
    def version_actual(self) -> int:
        return self._version


# ─────────────────────────────────────────────────────────────────────
# Contexto criptografico (AAD)
# ─────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ContextoCripto:
    """
    Identifica inequivocamente *para que* y *para quien* es un ciphertext.

    Se serializa como AAD, de modo que el descifrado falla si alguno de
    estos campos no coincide con el del cifrado original.
    """

    tenant_id: str
    proposito: str  # p.ej. "oauth_access_token"
    sujeto: str  # p.ej. "google:usuario@dominio.com"

    def como_aad(self) -> bytes:
        return f"{self.tenant_id}|{self.proposito}|{self.sujeto}".encode()


# ─────────────────────────────────────────────────────────────────────
# Servicio de cifrado
# ─────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class DekEnvuelta:
    """Clave de datos cifrada con la KEK, tal como se guarda en la BD."""

    material: bytes
    version_kek: int


class ServicioDeCifrado:
    """
    Operaciones de cifrado sobre envolvente.

    Uso tipico:
        dek_envuelta = servicio.generar_dek()               # al crear el tenant
        dek = servicio.desenvolver_dek(dek_envuelta)        # al operar
        ct  = servicio.cifrar(dek, b"token", contexto)
        pt  = servicio.descifrar(dek, ct, contexto)
    """

    def __init__(self, proveedor: ProveedorDeClaveMaestra) -> None:
        self._proveedor = proveedor

    # ── Gestion de DEK ───────────────────────────────────────────────

    def generar_dek(self) -> DekEnvuelta:
        """Crea una DEK aleatoria y la devuelve ya envuelta con la KEK vigente."""
        dek = AESGCM.generate_key(256)  # posicional: la firma no admite nombre
        return self.envolver_dek(dek)

    def envolver_dek(self, dek: bytes) -> DekEnvuelta:
        version = self._proveedor.version_actual
        kek = AESGCM(self._proveedor.obtener(version))
        nonce = os.urandom(LONGITUD_NONCE)
        # La AAD de la envoltura ata la DEK a su version de KEK: impide
        # presentar una DEK envuelta con una KEK antigua como si fuera actual.
        aad = f"dek|v{version}".encode()
        envuelta = nonce + kek.encrypt(nonce, dek, aad)
        return DekEnvuelta(material=envuelta, version_kek=version)

    def desenvolver_dek(self, envuelta: DekEnvuelta) -> bytes:
        kek = AESGCM(self._proveedor.obtener(envuelta.version_kek))
        nonce, cuerpo = envuelta.material[:LONGITUD_NONCE], envuelta.material[LONGITUD_NONCE:]
        aad = f"dek|v{envuelta.version_kek}".encode()
        try:
            return kek.decrypt(nonce, cuerpo, aad)
        except InvalidTag as exc:
            raise ErrorDeCifrado(contexto={"etapa": "desenvolver_dek"}) from exc

    # ── Cifrado de datos ─────────────────────────────────────────────

    def cifrar(self, dek: bytes, texto_plano: bytes, contexto: ContextoCripto) -> bytes:
        """
        Devuelve: MAGIC | version_formato | version_kek | nonce | ciphertext+tag

        La cabecera va en claro a proposito: es metadata de formato, no
        secreto, y forma parte de la AAD, asi que no puede manipularse.
        """
        version_kek = self._proveedor.version_actual
        cabecera = _CABECERA.pack(_MAGIC, 1, version_kek)
        nonce = os.urandom(LONGITUD_NONCE)
        aad = cabecera + contexto.como_aad()
        cuerpo = AESGCM(dek).encrypt(nonce, texto_plano, aad)
        return cabecera + nonce + cuerpo

    def descifrar(self, dek: bytes, cifrado: bytes, contexto: ContextoCripto) -> bytes:
        """Falla con ErrorDeCifrado si el contexto no coincide o el dato fue alterado."""
        if len(cifrado) < _CABECERA.size + LONGITUD_NONCE:
            raise ErrorDeCifrado(contexto={"etapa": "longitud_insuficiente"})

        cabecera = cifrado[: _CABECERA.size]
        magic, version_formato, _version_kek = _CABECERA.unpack(cabecera)
        if magic != _MAGIC or version_formato != 1:
            raise ErrorDeCifrado(contexto={"etapa": "formato_desconocido"})

        inicio_cuerpo = _CABECERA.size + LONGITUD_NONCE
        nonce = cifrado[_CABECERA.size : inicio_cuerpo]
        cuerpo = cifrado[inicio_cuerpo:]
        aad = cabecera + contexto.como_aad()

        try:
            return AESGCM(dek).decrypt(nonce, cuerpo, aad)
        except InvalidTag as exc:
            # InvalidTag cubre tres casos indistinguibles por diseño:
            # clave erronea, ciphertext manipulado o contexto equivocado.
            # No se detalla cual, para no dar señal util a un atacante.
            raise ErrorDeCifrado(contexto={"etapa": "descifrar"}) from exc

    # ── Rotacion ─────────────────────────────────────────────────────

    def necesita_rotacion(self, envuelta: DekEnvuelta) -> bool:
        """True si la DEK esta envuelta con una KEK anterior a la vigente."""
        return envuelta.version_kek < self._proveedor.version_actual

    def cifrar_texto(self, dek: bytes, texto: str, contexto: ContextoCripto) -> bytes:
        return self.cifrar(dek, texto.encode("utf-8"), contexto)

    def descifrar_texto(self, dek: bytes, cifrado: bytes, contexto: ContextoCripto) -> str:
        return self.descifrar(dek, cifrado, contexto).decode("utf-8")
