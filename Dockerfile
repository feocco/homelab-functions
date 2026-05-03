FROM python:3.13-alpine

WORKDIR /app

COPY pyproject.toml README.md /app/
COPY homelab /app/homelab

RUN pip install --no-cache-dir .

ENV SERVICE_HOST=0.0.0.0
ENV SERVICE_PORT=8091

EXPOSE 8091

CMD ["homelab-functions"]
