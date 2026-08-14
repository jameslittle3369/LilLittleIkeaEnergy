# ikea_energy.py container image.
#
# Exists because the TRADFRI gateway's DTLS layer (DTLSSocket) is published as a
# source tarball only and needs autoconf + a C compiler to build. Rather than
# putting that toolchain on your host -- impossible on stock Windows -- it is
# confined to the builder stage here, and only the resulting wheels are shipped.
#
# Python 3.13 rather than 3.14: DTLSSocket is a Cython extension with no wheels
# and no 3.14 testing, so the interpreter with the widest proven build surface is
# the safer floor. Bump it once you have verified a build.

# --------------------------------------------------------------------------- #
# Builder: compile every dependency to a wheel.
# --------------------------------------------------------------------------- #
FROM python:3.13-slim AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        autoconf \
        automake \
        libtool \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY requirements.txt .
RUN pip wheel --no-cache-dir --wheel-dir /wheels -r requirements.txt

# --------------------------------------------------------------------------- #
# Runtime: no compiler, no source tarballs, non-root.
# --------------------------------------------------------------------------- #
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    MPLBACKEND=Agg \
    MPLCONFIGDIR=/tmp/matplotlib \
    REPORT_OUTPUT_DIR=/app/out

# tzdata so TZ actually resolves: report timestamps are rendered in local time,
# and without the zoneinfo database a container silently stays on UTC.
RUN apt-get update && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

# Charts fall back to matplotlib's bundled DejaVu Sans here, since Segoe UI is a
# Windows font. Add fonts-dejavu-core only if you strip matplotlib's copy.
COPY --from=builder /wheels /wheels
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-index --find-links=/wheels -r /tmp/requirements.txt \
    && rm -rf /wheels /tmp/requirements.txt

RUN useradd --create-home --uid 10001 ikea
WORKDIR /app
COPY ikea_energy.py .
RUN mkdir -p /app/out && chown -R ikea:ikea /app/out

USER ikea

# Default to the JSON snapshot: safe, side-effect free, easy to pipe.
# Override at run time, e.g.  docker run ... ikea-energy --email
ENTRYPOINT ["python", "ikea_energy.py"]
CMD ["--json"]
