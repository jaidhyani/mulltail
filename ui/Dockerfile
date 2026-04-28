FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# docker CLI: the UI shells out via the mounted socket to talk to the
# exit-node container.
RUN apt-get update && apt-get install -y --no-install-recommends \
    docker.io \
    && rm -rf /var/lib/apt/lists/*

RUN pip install fastapi uvicorn[standard] httpx pydantic

WORKDIR /app
COPY server.py /app/server.py

EXPOSE 8191
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8191"]
