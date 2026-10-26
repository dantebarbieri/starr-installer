FROM python:3.14-alpine

LABEL org.opencontainers.image.source="https://github.com/dantebarbieri/starr-installer" \
      org.opencontainers.image.description="Read-only portable Starr installer download host"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SOURCE_ROOT=/source \
    PUBLIC_URL=https://starr.danteb.com
WORKDIR /source
COPY scripts/starr/ /source/scripts/starr/
COPY recyclarr-configs/ /source/recyclarr-configs/
RUN python3 scripts/starr/profiles.py verify
USER 65534:65534
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD ["python3", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3).close()"]
CMD ["python3", "-B", "/source/scripts/starr/serve.py"]
