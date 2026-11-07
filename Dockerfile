FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir '.[live]'
RUN mkdir /data && chown 65534:65534 /data
USER 65534:65534
ENTRYPOINT ["ahead-live"]
