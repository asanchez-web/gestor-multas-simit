# 🤖 Gestor Automático de Multas SIMIT

Sistema web automatizado para consultar multas de tránsito en el SIMIT por cédula, diseñado para gestión de cartera crediticia.

## ¿Qué hace?

1. **Subes** la base de datos de Athena (`.xlsx` o `.csv`) con las columnas de cédula, nombre y número de crédito.
2. **El sistema filtra** automáticamente quién necesita consulta según estas reglas:
   - 🆕 **Clientes nuevos** → Se consultan de inmediato.
   - ⚠️ **Con multa** → Se reconsultan cada **1 mes** (30 días).
   - ✅ **Sin multa** → Se reconsultan cada **2 meses** (60 días).
3. **El robot** consulta cada cédula en el SIMIT de forma invisible (Chrome headless).
4. **Si hay doble documento**, suma ambos valores de multa automáticamente.
5. **Todo se guarda** en una base de datos SQLite persistente (sobrevive a reinicios).
6. **Descargas** el reporte actualizado en Excel cuando quieras.

## Requisitos

- Docker y Docker Compose instalados (o Portainer).
- Un servidor con al menos 2GB de RAM.

## Despliegue con Docker Compose

```bash
git clone https://github.com/TU_USUARIO/gestor-multas-simit.git
cd gestor-multas-simit
docker-compose up -d --build
```

La web estará disponible en: `http://TU_IP:5000`

## Despliegue con Portainer

1. En Portainer, ve a **Stacks** → **Add Stack**.
2. Selecciona **Repository** y pega la URL de tu repositorio de GitHub.
3. Portainer detectará el `docker-compose.yml` y lo desplegará automáticamente.
4. El volumen `base_de_datos_multas` garantiza que tu historial nunca se pierda.

## Estructura del proyecto

```
gestor-multas-simit/
├── app.py                 # Servidor web Flask (rutas, lógica de negocio)
├── scraper.py             # Robot Selenium para consultar SIMIT
├── requirements.txt       # Dependencias Python
├── Dockerfile             # Imagen Docker con Chrome headless
├── docker-compose.yml     # Configuración para Portainer/Docker
├── .gitignore             # Archivos excluidos de Git
├── README.md              # Este archivo
└── templates/
    └── index.html         # Interfaz web
```

## Columnas esperadas del Excel

El sistema busca estas columnas automáticamente (no importan mayúsculas):

| Campo | Nombres aceptados |
|-------|-------------------|
| Cédula | `cedula`, `cédula`, `documento`, `numero_documento`, `identificacion` |
| Nombre | `nombre_cliente`, `nombre`, `cliente`, `nombre_completo` |
| Crédito | `numero_credito`, `num_credito`, `credito`, `obligacion` |

## Notas técnicas

- El scraper hace una pausa de refresco cada 20 consultas para evitar bloqueos del SIMIT.
- Los datos se guardan en SQLite inmediatamente después de cada consulta (resistente a caídas).
- La variable `PYTHONUNBUFFERED=1` en docker-compose asegura que los logs aparezcan en tiempo real en Portainer.
