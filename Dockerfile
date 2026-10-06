# ─────────────────────────────────────────────────────────────────────
# Imagen de la API.
#
# Deliberadamente SIN Tesseract, OpenCV ni poppler: el contenedor que
# esta expuesto a Internet no debe llevar las bibliotecas nativas de
# parseo que van a procesar ficheros de terceros. Esas viven solo en
# Dockerfile.worker, que no recibe trafico entrante (hallazgo H12).
#
# Construccion en dos etapas: las herramientas de compilacion se quedan
# en la etapa de build y no viajan a la imagen final.
# ─────────────────────────────────────────────────────────────────────

FROM python:3.12-slim-bookworm AS constructor

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /build

RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential libpq-dev \
 && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
COPY src/ ./src/

# El upgrade posterior a la instalacion fuerza la version parcheada
# de dos transitivas que el resolutor podria dejar en una anterior.
#
# Y despues se desinstalan pip y wheel. No es solo por tamaño: pip
# lleva copias embebidas de urllib3 y setuptools en su directorio
# `_vendor`, con sus propios .dist-info, y los escaneres las
# reportan como paquetes vulnerables aunque las versiones reales
# esten al dia. El runtime no instala nada, asi que pip solo aporta
# superficie de ataque y ruido en los informes.
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --upgrade pip setuptools wheel \
 && /opt/venv/bin/pip install . \
 && /opt/venv/bin/pip install --no-cache-dir --upgrade 'setuptools>=84.0.0' 'urllib3>=2.8.0' \
 && /opt/venv/bin/pip uninstall -y pip wheel


# ─────────────────────────────────────────────────────────────────────
FROM python:3.12-slim-bookworm AS final

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH"

# Usuario sin privilegios. Un proceso root dentro del contenedor
# convierte cualquier escape en un compromiso del host.
RUN groupadd --gid 10001 mailauto \
 && useradd --uid 10001 --gid mailauto --no-create-home --shell /usr/sbin/nologin mailauto \
 && apt-get update \
 && apt-get upgrade -y \
 && apt-get install -y --no-install-recommends libpq5 curl \
 && /usr/local/bin/python -m pip install --no-cache-dir --upgrade 'setuptools>=84.0.0' \
 && /usr/local/bin/python -m pip uninstall -y pip \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY --from=constructor /opt/venv /opt/venv
COPY --chown=mailauto:mailauto src/ ./src/
COPY --chown=mailauto:mailauto migrations/ ./migrations/
COPY --chown=mailauto:mailauto alembic.ini ./

USER mailauto

EXPOSE 8000

# La sonda usa /health/live y no /health/ready: Docker reinicia el
# contenedor cuando la sonda falla, y no se quiere reiniciar la API
# porque PostgreSQL este temporalmente caido.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS "http://localhost:${PORT:-8000}/health/live" || exit 1

# Se liga a ${PORT:-8000}: en local y en compose la variable no existe y
# vale 8000; las plataformas que asignan un puerto dinamico (Railway,
# Render, Cloud Run) lo inyectan en PORT y el contenedor se liga a el sin
# tocar nada. `sh -c exec` es necesario para expandir la variable, y el
# `exec` deja a uvicorn como PID 1 para que reciba SIGTERM y apague limpio.
CMD ["sh", "-c", \
     "exec uvicorn mailauto.bootstrap.app:crear_app --factory \
      --host 0.0.0.0 --port ${PORT:-8000} \
      --proxy-headers --forwarded-allow-ips '*' --no-server-header"]
