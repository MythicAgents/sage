# syntax=docker/dockerfile:1.7

FROM --platform=$BUILDPLATFORM debian:bookworm-slim@sha256:7c7b2c966bc9ee8cedfeef67e0e279108992c77681fa595db4a9d65c06ccc587 AS fixtures
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        gcc-mingw-w64-x86-64=12.2.0-14+25.2 \
        mono-mcs=6.8.0.105+dfsg-3.3+deb12u1 \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /src
COPY fixtures/ ./
RUN mkdir -p /out \
    && x86_64-w64-mingw32-gcc -O2 -s -o /out/native.exe native.c \
    && x86_64-w64-mingw32-gcc -O2 -s -o /out/network-attempt.exe network-attempt.c -lws2_32 \
    && x86_64-w64-mingw32-gcc -O2 -s -o /out/output-flood.exe output-flood.c \
    && x86_64-w64-mingw32-gcc -O2 -s -o /out/hang.exe hang.c \
    && x86_64-w64-mingw32-gcc -O2 -s -o /out/child-hang.exe child-hang.c \
    && x86_64-w64-mingw32-gcc -O2 -s -o /out/argv-contract.exe argv-contract.c -lshell32 \
    && mcs -nologo -optimize+ -platform:anycpu -out:/out/managed-com.exe managed-com.cs \
    && test "$(find /out -type f -name '*.exe' | wc -l)" = 7

FROM debian:bookworm-slim@sha256:7c7b2c966bc9ee8cedfeef67e0e279108992c77681fa595db4a9d65c06ccc587 AS static
COPY requirements-static.txt /tmp/requirements-static.txt
RUN apt-get update \
    && apt-get install -y --no-install-recommends python3=3.11.2-1+b1 python3-pip=23.0.1+dfsg-1 \
    && python3 -m pip install --break-system-packages --no-cache-dir --require-hashes \
        -r /tmp/requirements-static.txt \
    && rm /tmp/requirements-static.txt \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 10001 --create-home --home-dir /home/inspector inspector
COPY requirements-static.txt /opt/inspection/requirements-static.txt
COPY entrypoint.py static_metadata.py /opt/inspection/
WORKDIR /inspection/work
USER 10001:10001
ENV HOME=/inspection/work PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

FROM --platform=linux/amd64 debian:bookworm-slim@sha256:a4672c0cb26fbdde88e38fa2dfb6c681942306680e41e4378b28770b6e79ee91 AS wine-download
ADD --checksum=sha256:6413ff328ebbf7ec7689c648feb3546d8102ded865079d1fbf0331b14b3ab0ec \
    https://dl.winehq.org/wine/wine-mono/7.4.0/wine-mono-7.4.0-x86.msi /wine-mono.msi

FROM --platform=linux/amd64 debian:bookworm-slim@sha256:a4672c0cb26fbdde88e38fa2dfb6c681942306680e41e4378b28770b6e79ee91 AS wine
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        python3=3.11.2-1+b1 \
        wine64=8.0~repack-4 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 10001 --create-home --home-dir /home/inspector inspector
COPY --from=wine-download /wine-mono.msi /tmp/wine-mono.msi
COPY bootstrap-wine.sh /tmp/bootstrap-wine.sh
RUN /bin/sh /tmp/bootstrap-wine.sh /tmp/wine-mono.msi \
    && rm /tmp/bootstrap-wine.sh /tmp/wine-mono.msi
COPY --from=fixtures /out/ /opt/inspection/fixtures/
COPY entrypoint.py static_metadata.py /opt/inspection/
WORKDIR /inspection/work
USER 10001:10001
ENV HOME=/inspection/work PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    SAGE_INSPECTION_WINE=/usr/lib/wine/wine64 WINEARCH=win64
