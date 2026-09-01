import time
import re
import sqlite3
from datetime import datetime
from seleniumbase import SB

# ═══════════════════════════════════════════════════════════════════════════════
# SCRAPER SIMIT - Consulta de multas por cédula
# ═══════════════════════════════════════════════════════════════════════════════

URL_SIMIT = "https://www.fcm.org.co/simit/#/estado-cuenta"
PAUSA_ENTRE_CONSULTAS = 3       # Segundos entre cliente y cliente
PAUSA_REFRESCO = 10             # Segundos de pausa al refrescar sesión
CLIENTES_ANTES_DE_REFRESCO = 20 # Cada N clientes se refresca la sesión
TIMEOUT_ELEMENTO = 15           # Tiempo máximo de espera por un elemento


def limpiar_dinero(texto):
    """Convierte un texto con formato de dinero colombiano a entero.
    Ejemplo: '$ 1.234.567' -> 1234567, '$ 0' -> 0
    """
    if not texto:
        return 0
    numeros = re.sub(r'[^\d]', '', texto)
    return int(numeros) if numeros else 0


def extraer_valor_multa(sb):
    """Extrae el valor total de la multa de la pantalla de resultados SIMIT.
    Espera a que la página cargue y busca valores monetarios en formato colombiano.
    Retorna el primer valor monetario mayor a cero, o 0 si no encuentra ninguno.
    """
    time.sleep(2)
    texto = sb.get_text("body")

    # Buscar valores con formato colombiano: $ 1.234.567 o $1234567
    valores = re.findall(r'\$\s?[\d\.]+', texto)

    # Filtrar: solo valores que representen dinero real (mayor a 0)
    for v in valores:
        monto = limpiar_dinero(v)
        if monto > 0:
            return monto

    return 0


def guardar_en_bd(db_path, cedula, nombre, num_credito, celular, total_multa):
    """Guarda o actualiza el registro de un cliente en la base de datos."""
    tiene_multa = 'SI' if total_multa > 0 else 'NO'
    fecha = datetime.now().strftime("%Y-%m-%d")

    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    # Actualizar estado actual
    c.execute('''INSERT OR REPLACE INTO clientes 
                 (cedula, nombre_cliente, numero_credito, celular, tiene_multa, total_multa, ultima_consulta) 
                 VALUES (?, ?, ?, ?, ?, ?, ?)''',
              (cedula, nombre, num_credito, celular, tiene_multa, total_multa, fecha))
    
    # Guardar en historial mes a mes
    c.execute('''INSERT INTO historial_consultas 
                 (cedula, tiene_multa, total_multa, fecha_consulta) 
                 VALUES (?, ?, ?, ?)''',
              (cedula, tiene_multa, total_multa, fecha))
              
    conn.commit()
    conn.close()


def navegar_a_busqueda(sb):
    """Asegura que el campo de búsqueda esté visible y listo para escribir."""
    try:
        if sb.is_element_visible("#txtBusqueda"):
            # Si hay botón de volver, hacer click para regresar al buscador
            if sb.is_element_visible("button[class*='volver'], .btn-secondary"):
                try:
                    sb.click("button[class*='volver'], .btn-secondary")
                    time.sleep(3)
                except Exception:
                    sb.refresh_page()
                    time.sleep(5)
        else:
            sb.refresh_page()
            time.sleep(5)

        sb.wait_for_element_visible("#txtBusqueda", timeout=TIMEOUT_ELEMENTO)
    except Exception:
        # Último recurso: navegar desde cero
        sb.uc_open_with_reconnect(URL_SIMIT, 4)
        time.sleep(PAUSA_REFRESCO)
        sb.wait_for_element_visible("#txtBusqueda", timeout=TIMEOUT_ELEMENTO)


def consultar_cedula(sb, cedula):
    """Escribe la cédula en el buscador y hace click en buscar."""
    sb.clear("#txtBusqueda")
    sb.type("#txtBusqueda", cedula)
    time.sleep(1)
    sb.click("#btnNumDocPlaca")
    time.sleep(4)


def manejar_doble_documento(sb, cedula):
    """Cuando SIMIT muestra 'varios resultados' (doble documento):
    - Selecciona el primer documento, extrae el valor
    - Vuelve atrás, selecciona el segundo documento, extrae el valor
    - Suma ambos valores y retorna el total
    """
    selector_doc_1 = "(//div[@id='modal-multiples-personas']//label)[1]"
    selector_doc_2 = "(//div[@id='modal-multiples-personas']//label)[2]"
    boton_continuar = "//div[@id='modal-multiples-personas']//button[contains(text(), 'Continuar') or contains(@class, 'btn-primary')]"

    total = 0

    try:
        # ── Primer documento ─────────────────────────────────────────────
        sb.wait_for_element_visible(selector_doc_1, timeout=TIMEOUT_ELEMENTO)
        sb.js_click(selector_doc_1)
        time.sleep(1)
        sb.js_click(boton_continuar)
        valor_1 = extraer_valor_multa(sb)
        total += valor_1

        # ── Volver a buscar para el segundo documento ────────────────────
        sb.refresh_page()
        sb.wait_for_element_visible("#txtBusqueda", timeout=TIMEOUT_ELEMENTO)
        consultar_cedula(sb, cedula)

        # ── Segundo documento ────────────────────────────────────────────
        sb.wait_for_element_visible(selector_doc_2, timeout=TIMEOUT_ELEMENTO)
        sb.js_click(selector_doc_2)
        time.sleep(1)
        sb.js_click(boton_continuar)
        valor_2 = extraer_valor_multa(sb)
        total += valor_2

        # ── Dejar el buscador listo para el siguiente ────────────────────
        sb.refresh_page()
        sb.wait_for_element_visible("#txtBusqueda", timeout=TIMEOUT_ELEMENTO)

    except Exception as e:
        print(f"[DOBLE DOC] Error procesando doble documento para {cedula}: {e}")
        try:
            sb.refresh_page()
            time.sleep(5)
        except Exception:
            pass

    return total


def refrescar_sesion(sb, estado):
    """Borra cookies y recarga SIMIT para evitar bloqueos por muchas consultas seguidas."""
    estado["mensaje"] = "⏸️ Pausa técnica de refresco (SIMIT)..."
    try:
        sb.delete_all_cookies()
        sb.uc_open_with_reconnect(URL_SIMIT, 4)
        time.sleep(PAUSA_REFRESCO)
    except Exception as e:
        print(f"[REFRESCO] Error al refrescar sesión: {e}")


def ejecutar_scraper(pendientes, estado, db_path):
    """Función principal del scraper. Recibe la lista de clientes pendientes,
    abre Chrome headless, y consulta cada cédula en SIMIT.
    
    Args:
        pendientes: Lista de dicts con datos de clientes ({cedula, nombre_cliente, numero_credito})
        estado: Dict compartido con la UI para mostrar progreso en vivo
        db_path: Ruta a la base de datos SQLite
    """
    estado["activo"] = True
    estado["total"] = len(pendientes)
    estado["procesados"] = 0
    estado["errores"] = 0
    estado["mensaje"] = "🔄 Abriendo Chrome en segundo plano..."

    try:
        # no_sandbox=True es VITAL para que Chrome funcione dentro de Docker
        with SB(uc=True, headless=True, incognito=True, chromium_arg="--no-sandbox") as sb:
            sb.uc_open_with_reconnect(URL_SIMIT, 4)
            time.sleep(PAUSA_REFRESCO)

            for i, cliente in enumerate(pendientes):
                cedula = str(cliente['cedula']).strip().split('.')[0]
                nombre = cliente.get('nombre_cliente', 'Sin nombre')
                num_credito = str(cliente.get('numero_credito', ''))
                celular = str(cliente.get('celular', ''))

                estado["mensaje"] = f"🔍 Consultando: {nombre} ({cedula}) — {i + 1}/{len(pendientes)}"
                total_multa = 0
                hubo_error = False

                try:
                    # Asegurar que el buscador esté disponible
                    navegar_a_busqueda(sb)

                    # Buscar la cédula
                    consultar_cedula(sb, cedula)

                    # Revisar si hay resultado de doble documento
                    texto_pantalla = sb.get_text("body").lower()

                    if ("se han encontrado varios resultados" in texto_pantalla
                            or "múltiples" in texto_pantalla
                            or sb.is_element_visible(".modalMultiplesPersonas")):
                        # CASO DOBLE DOCUMENTO: sumar ambos valores
                        total_multa = manejar_doble_documento(sb, cedula)
                    else:
                        # CASO NORMAL: un solo resultado
                        total_multa = extraer_valor_multa(sb)

                except Exception as e:
                    print(f"[ERROR] Cliente {cedula} ({nombre}): {e}")
                    estado["errores"] += 1
                    hubo_error = True
                    try:
                        sb.refresh_page()
                        time.sleep(5)
                    except Exception:
                        pass

                # GUARDAR EN BD SOLO SI NO HUBO ERROR
                if not hubo_error:
                    guardar_en_bd(db_path, cedula, nombre, num_credito, celular, total_multa)

                estado["procesados"] += 1

                # Pausa técnica cada N clientes para evitar baneos del SIMIT
                if estado["procesados"] % CLIENTES_ANTES_DE_REFRESCO == 0 and estado["procesados"] < len(pendientes):
                    refrescar_sesion(sb, estado)

                # Pausa corta entre consultas
                time.sleep(PAUSA_ENTRE_CONSULTAS)

    except Exception as e:
        estado["mensaje"] = f"❌ Error crítico del navegador: {str(e)}"
        print(f"[CRÍTICO] Error en el scraper: {e}")
    finally:
        errores = estado["errores"]
        procesados = estado["procesados"]
        estado["activo"] = False
        estado["mensaje"] = (
            f"✅ Proceso finalizado. {procesados} consultados"
            + (f", {errores} con error" if errores > 0 else "")
            + ". Puedes descargar el histórico."
        )
