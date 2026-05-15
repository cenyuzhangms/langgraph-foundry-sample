FROM python:3.12-slim

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
        bash ca-certificates curl git \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

# Bake skill executables into /opt/skills/<name>/bin/ and put each on PATH so
# the model can invoke playbook commands via a shell-style call (matches the
# foundry-data-analyst-with-skills convention).
RUN set -eux; \
    if [ -d /app/skills ]; then \
        mkdir -p /opt/skills; \
        for d in /app/skills/*/; do \
            name="$(basename "$d")"; \
            mkdir -p "/opt/skills/$name"; \
            cp -r "$d"/. "/opt/skills/$name/"; \
            if [ -d "/opt/skills/$name/bin" ]; then \
                chmod -R +x "/opt/skills/$name/bin"; \
            fi; \
        done; \
    fi
ENV PATH="/opt/skills/research-brief/bin:${PATH}"

EXPOSE 8088

CMD ["python", "main.py"]
