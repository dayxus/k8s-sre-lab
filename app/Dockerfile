# Slim, dependency-free, non-root runtime.
# The image tag is pinned by tools/versions.env (APP_IMAGE_TAG) and loaded into the
# kind cluster by scripts/e2e-kind.sh; there is no registry round-trip in the lab.
FROM python:3.12-slim

# Fixed uid/gid so runAsUser, runAsGroup and fsGroup in the pod spec are predictable
# and so the restricted Pod Security Standard admission passes without exceptions.
RUN groupadd --gid 10001 demo \
 && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin demo

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8080

WORKDIR /app
COPY --chown=10001:10001 server.py /app/server.py

USER 10001:10001
EXPOSE 8080

# Container-level health for docker/podman users; Kubernetes uses the probes declared
# in manifests/base/deployment.yaml instead, which are the ones that matter in-cluster.
HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=3 \
  CMD ["python3", "-c", "import sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/readyz', timeout=2).status == 200 else 1)"]

ENTRYPOINT ["python3", "/app/server.py"]
