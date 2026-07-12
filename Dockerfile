FROM python:3.12-slim
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY tmisc/ ./tmisc/
COPY server/ ./server/
COPY cli.py SPEC.md ./

# `tools` por defecto: encode/decode/probe y NINGUNA ruta de streaming (los
# routers ni se registran). Servir la biblioteca es una decisión explícita, y
# además tiene poco sentido en un contenedor sin la música montada.
ENV TMISC_PROFILE=tools \
    TMISC_CACHE=/data/cache \
    TMISC_LIBRARY=/data/library

EXPOSE 8787

# Para levantar un nodo de verdad:
#   docker run -p 8787:8787 -v ~/musica:/data/library \
#     -e TMISC_PROFILE=node tmisc
CMD ["uvicorn", "server.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8787"]
