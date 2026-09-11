# Two stages: node builds the UI bundle, python runs everything.
# The final image carries no node, no npm and no source maps.

FROM node:22-alpine AS ui
WORKDIR /build
COPY ui/package*.json ./ui/
RUN cd ui && npm ci --no-audit --no-fund
COPY ui ./ui
RUN cd ui && npm run build

FROM python:3.12-slim
WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    INVOICE_AUDIT_DEMO=1 \
    INVOICE_AUDIT_VISION_LIMIT=25

COPY pyproject.toml README.md ./
COPY invoice_audit ./invoice_audit
COPY data ./data
COPY examples ./examples
COPY --from=ui /build/invoice_audit/ui/dist ./invoice_audit/ui/dist

# pdfplumber only. The vision SDK is installed at build time but a demo without
# a key simply never reaches it, and uploads of CSV or text-layer PDFs work
# regardless.
RUN pip install --no-cache-dir ".[pdf,gemini]"

# Never run as root: the upload endpoint writes files this process can reach.
RUN useradd --create-home --uid 10001 audit \
 && chown -R audit:audit /app
USER audit

EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=4s --start-period=10s \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:'+__import__('os').environ.get('PORT','8765')+'/api/mode').status==200 else 1)"

CMD ["python", "-m", "invoice_audit", "serve", "--demo", "--no-browser"]
