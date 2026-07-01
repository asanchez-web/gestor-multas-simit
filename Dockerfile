FROM python:3.10-slim

# Instalar dependencias del sistema y Google Chrome para Selenium headless
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget gnupg2 unzip xvfb curl \
    && wget -q -O - https://dl-ssl.google.com/linux/linux_signing_key.pub | apt-key add - \
    && sh -c 'echo "deb [arch=amd64] http://dl.google.com/linux/chrome/deb/ stable main" >> /etc/apt/sources.list.d/google-chrome.list' \
    && apt-get update \
    && apt-get install -y --no-install-recommends google-chrome-stable \
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
