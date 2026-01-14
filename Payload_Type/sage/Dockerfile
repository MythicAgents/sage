FROM python:3.13-slim-bookworm AS builder

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
      g++ \
      && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

FROM python:3.13-slim-bookworm

COPY --from=builder /install /usr/local

WORKDIR /Mythic/

COPY [".", "."]

CMD ["python3", "main.py"]