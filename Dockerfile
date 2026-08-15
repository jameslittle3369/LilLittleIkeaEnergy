# ikea_energy.py container image.
#
# Exists because the TRADFRI gateway's DTLS layer (DTLSSocket) is published as a
# source tarball only and needs autoconf + a C compiler to build. Rather than
# putting that toolchain on your host, it is confined to the builder stage
# here, and only the resulting wheels are shipped.
#
# Python 3.14, to match the local dev venv. DTLSSocket (the one dependency that
# compiles) is untested against 3.14 upstream, but its build already got past
# the Cython/setup.py stage under cp314 in local testing on this project -- it
# only failed on missing autoconf/a C compiler, both of which this builder
# stage installs. If a Debian/arm64 build surfaces a real 3.14 incompatibility
# (rather than a missing build tool), drop both FROM lines to python:3.13-slim;
# nothing else in this file depends on the interpreter version.
FROM python:3.14-slim AS builder

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
FROM python:3.14-slim

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

# Charts fall back to matplotlib's bundled DejaVu Sans here. Add
# fonts-dejavu-core only if you strip matplotlib's copy.
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
