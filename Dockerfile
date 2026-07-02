FROM python:3.10-slim
# Instalar dependencias del sistema y Google Chrome
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget gnupg2 unzip xvfb curl ca-certificates \
    && wget -q https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb \
    && apt-get install -y --no-install-recommends ./google-chrome-stable_current_amd64.deb \
    && rm google-chrome-stable_current_amd64.deb \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
# Copiar e instalar dependencias Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# Copiar el resto del código fuente
COPY . .
# Crear carpeta de datos persistente
RUN mkdir -p /app/data
# Exponer el puerto de la web
EXPOSE 5000
# Arrancar el servidor Flask
CMD ["python", "app.py"]
