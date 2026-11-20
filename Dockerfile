# Funciona en servidores Intel/AMD y ARM (Oracle Cloud Ampere).
# Usa Chromium de Debian, que existe para ambas arquitecturas.
FROM python:3.10-slim-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends \
        chromium chromium-driver fonts-liberation ca-certificates tzdata \
    && rm -rf /var/lib/apt/lists/*

ENV TZ=America/Bogota \
    PYTHONUNBUFFERED=1 \
    CHROME_BIN=/usr/bin/chromium

WORKDIR /app
COPY requirements.txt .
# SeleniumBase usa el chromedriver del sistema (no descarga nada al arrancar)
RUN pip install --no-cache-dir -r requirements.txt \
    && SB_DRIVERS=$(python -c "import seleniumbase,os;print(os.path.join(os.path.dirname(seleniumbase.__file__),'drivers'))") \
    && cp /usr/bin/chromedriver "$SB_DRIVERS/chromedriver" \
    && cp /usr/bin/chromedriver "$SB_DRIVERS/uc_driver" \
    && chmod +x "$SB_DRIVERS/chromedriver" "$SB_DRIVERS/uc_driver"

COPY . .
RUN mkdir -p /app/data
EXPOSE 5000
CMD ["python", "app.py"]
