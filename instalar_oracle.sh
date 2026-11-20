#!/usr/bin/env bash
# Instala y levanta el Gestor de Multas en un servidor Ubuntu de Oracle Cloud.
# Uso:  curl -fsSL https://raw.githubusercontent.com/asanchez-web/gestor-multas-simit/main/instalar_oracle.sh | bash
set -euo pipefail

echo "==> 1/5 Zona horaria Bogotá"
sudo timedatectl set-timezone America/Bogota || true

echo "==> 2/5 Instalando Docker"
if ! command -v docker >/dev/null; then
  curl -fsSL https://get.docker.com | sudo sh
fi
sudo usermod -aG docker "$USER" || true

echo "==> 3/5 Abriendo el puerto 5000 en el firewall interno del servidor"
if ! sudo iptables -C INPUT -p tcp --dport 5000 -j ACCEPT 2>/dev/null; then
  sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 5000 -j ACCEPT
fi
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y iptables-persistent >/dev/null 2>&1 || true
sudo netfilter-persistent save || true

echo "==> 4/5 Descargando / actualizando el código"
if [ -d "$HOME/gestor-multas-simit/.git" ]; then
  git -C "$HOME/gestor-multas-simit" pull --ff-only
else
  git clone https://github.com/asanchez-web/gestor-multas-simit.git "$HOME/gestor-multas-simit"
fi

echo "==> 5/5 Construyendo y levantando (la primera vez tarda 5-10 min)"
cd "$HOME/gestor-multas-simit"
sudo docker compose up -d --build

IP=$(curl -fsS https://ifconfig.me || echo "TU_IP_PUBLICA")
echo ""
echo "Listo. Abre: http://$IP:5000"
echo "Para actualizar en el futuro, vuelve a correr este mismo comando."
