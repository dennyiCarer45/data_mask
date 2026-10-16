FROM python:3.13-slim@sha256:70729b46c69b4f1e97c4822c1af3df53a1476cf5ddc6c087c0c10bc3a5678c2f
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 LITELLM_LOCAL_MODEL_COST_MAP=True \
    OTEL_SDK_DISABLED=true HF_HUB_DISABLE_TELEMETRY=1 PYTHONPATH=/app
COPY requirements-tested-linux.txt /app/
RUN pip install --index-url https://download.pytorch.org/whl/cpu 'torch==2.14.1' \
    && pip install -r requirements-tested-linux.txt
# Local OCR for images and image regions in PDFs. English plus Hindi (Aadhaar and other Indian
# IDs are bilingual). Language data is installed at build time; nothing is fetched per request.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       tesseract-ocr=5.5.0-1+b1 tesseract-ocr-eng=1:4.1.0-2 tesseract-ocr-hin=1:4.1.0-2 \
    && rm -rf /var/lib/apt/lists/*
ENV OMP_THREAD_LIMIT=1
COPY pyproject.toml /app/
COPY LICENSE README.md /app/
COPY privacy_gateway /app/privacy_gateway
RUN pip install --no-deps .
COPY config /app/config
COPY scripts /app/scripts
COPY tests /app/tests
RUN useradd --create-home --uid 10001 gateway && chown -R gateway:gateway /app
USER gateway
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "privacy_gateway.api:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]

