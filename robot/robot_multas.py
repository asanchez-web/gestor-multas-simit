"""
ROBOT DE MULTAS SIMIT — corre en GitHub Actions y guarda todo en un Google Sheet.

Pestañas del Sheet:
  Base        → la pegas tú cada mes (resultado de Athena): cedula, numero_credito,
                nombre_cliente, celular. Puede traer más columnas, se ignoran.
  Clientes    → estado actual de cada cliente (la llena el robot).
  Historial   → una fila por cada consulta hecha (la llena el robot).
  Carga_CGEN  → formato CGEN-CargaGenerica listo para la carga masiva (multados del mes).
  Revision    → multados del mes sin celular válido o sin número de crédito.
  Estado      → resumen de la última corrida.

Regla (por mes calendario): con multa → cada mes; sin multa → cada 2 meses.
Cada corrida consulta pendientes hasta que se acaba el tiempo; la siguiente sigue.

IMPORTANTE: el repositorio es público → los logs NO deben mostrar cédulas,
nombres ni celulares. Solo se imprimen conteos.
"""
import json
import os
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import gspread

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RAIZ)

TZ = ZoneInfo("America/Bogota")
LIMITE_MINUTOS = float(os.environ.get("LIMITE_MINUTOS", "320"))   # ~5h20 por corrida
LOTE_GUARDADO = 25            # cada cuántas consultas se escribe en el Sheet
MAX_ERRORES_SEGUIDOS = 15     # si el SIMIT falla tanto seguido, se para y sigue la próxima corrida
CLIENTES_ANTES_DE_REFRESCO = 20
PAUSA_ENTRE_CONSULTAS = 3

# Reconsulta por MES CALENDARIO (no por día):
MESES_CON_MULTA = 1   # con multa: cada mes
MESES_SIN_MULTA = 2   # sin multa: cada 2 meses (consultado en nov → se reconsulta en enero)

POSIBLES = {
    "cedula": ["cedula", "cédula", "numero_documento", "num_documento", "documento",
               "nro_documento", "identificacion", "client_dni"],
    "nombre_cliente": ["nombre_cliente", "nombre", "cliente", "nombre_completo", "nombres", "client_name"],
    "numero_credito": ["numero_credito", "num_credito", "credito", "nro_credito", "obligacion", "obligacion_id"],
    "celular": ["celular", "celular_cliente", "celular_reciente", "telefono", "tel", "client_cel",
                "numero_celular", "movil", "telefono_celular", "cel"],
}

COLS_CLIENTES = ["cedula", "nombre_cliente", "numero_credito", "celular",
                 "tiene_multa", "total_multa", "ultima_consulta"]
COLS_HISTORIAL = ["cedula", "tiene_multa", "total_multa", "fecha_consulta"]

COLUMNAS_CGEN = ["Resultado de Gestion", "Usuario", "Fecha", "Tipo gestionable",
                 "Numero de Gestionable", "Gestion", "Tipo Contacto", "Gestion Con",
                 "Punto de Atencion", "Forma Contacto", "Informacion de contacto",
                 "Indicativo", "tipo", "Ciudad", "Barrio", "Nombre Contacto",
                 "Relacion con el Credito", "Relacion con el Cliente", "Estado", "Observacion"]
VALORES_FIJOS_CGEN = {
    "Resultado de Gestion": "CSIM",
    "Usuario": "asesorcartera",
    "Tipo gestionable": "Obl-Credito",
    "Gestion": "1-COBRANZAS ",
    "Tipo Contacto": "WEB-web",
    "Gestion Con": "CLIE-Cliente",
    "Forma Contacto": "cel-celular",
    "tipo": "PER-Personal",
    "Estado": "ACT",
}


# ─── Utilidades ─────────────────────────────────────────────────────────────
def ahora():
    return datetime.now(TZ)


def solo_digitos(valor):
    texto = str(valor or "").strip()
    if texto.lower() in ("nan", "none"):
        return ""
    if texto.endswith(".0"):
        texto = texto[:-2]
    return "".join(ch for ch in texto if ch.isdigit())


def limpiar_celular(valor):
    d = solo_digitos(valor)
    if len(d) == 12 and d.startswith("57"):
        d = d[2:]
    return d


def a_entero(valor):
    d = solo_digitos(valor)
    return int(d) if d else 0


def meses_desde(fecha_txt, hoy):
    """Meses calendario entre la fecha (YYYY-MM-DD) y hoy. Sin fecha → muy grande."""
    try:
        anio, mes = int(fecha_txt[:4]), int(fecha_txt[5:7])
    except (ValueError, TypeError):
        return 999
    return (hoy.year - anio) * 12 + (hoy.month - mes)


def toca_consultar(cliente, hoy):
    meses = meses_desde(cliente.get("ultima_consulta", ""), hoy)
    if cliente.get("tiene_multa") == "SI":
        return meses >= MESES_CON_MULTA
    if cliente.get("tiene_multa") == "NO":
        return meses >= MESES_SIN_MULTA
    return meses >= 1   # nunca consultado o sin dato


# ─── Google Sheet ───────────────────────────────────────────────────────────
def abrir_sheet():
    info = json.loads(os.environ["GOOGLE_CREDENTIALS"])
    gc = gspread.service_account_from_dict(info)
    return gc.open_by_key(os.environ["SHEET_ID"].strip())


def hoja(sh, nombre, encabezados=None):
    try:
        ws = sh.worksheet(nombre)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=nombre, rows=100, cols=max(len(encabezados or []), 5))
        if encabezados:
            ws.update(values=[encabezados], range_name="A1", value_input_option="RAW")
    return ws


def escribir_tabla(ws, encabezados, filas):
    """Reemplaza todo el contenido de la pestaña."""
    datos = [encabezados] + filas
    ws.clear()
    if ws.row_count < len(datos):
        ws.resize(rows=len(datos) + 10)
    if ws.col_count < len(encabezados):
        ws.resize(cols=len(encabezados))
    ws.update(values=datos, range_name="A1", value_input_option="RAW")


def leer_base(ws):
    valores = ws.get_all_values()
    if len(valores) < 2:
        return []
    encabezado = [c.strip().lower() for c in valores[0]]
    idx = {}
    for campo, opciones in POSIBLES.items():
        for op in opciones:
            if op in encabezado:
                idx[campo] = encabezado.index(op)
                break
    if "cedula" not in idx:
        raise SystemExit("La pestaña Base no tiene columna de cédula (cedula / client_dni ...).")

    base, vistos = [], set()
    for fila in valores[1:]:
        def tomar(campo):
            i = idx.get(campo)
            return fila[i].strip() if i is not None and i < len(fila) else ""
        ced = tomar("cedula").replace(" ", "")
        ced = ced[:-2] if ced.endswith(".0") else ced
        if not ced.isdigit() or ced in vistos:   # solo cédulas numéricas (SIMIT)
            continue
        vistos.add(ced)
        base.append({
            "cedula": ced,
            "nombre_cliente": tomar("nombre_cliente") or "Sin nombre",
            "numero_credito": solo_digitos(tomar("numero_credito")),
            "celular": limpiar_celular(tomar("celular")),
        })
    return base


def leer_clientes(ws_clientes, ws_historial):
    """Estado actual = pestaña Clientes, corregido con lo último del Historial
    (así nada se pierde si una corrida se cortó antes de guardar Clientes)."""
    clientes = {}
    for r in ws_clientes.get_all_records(expected_headers=COLS_CLIENTES, default_blank=""):
        ced = solo_digitos(r.get("cedula"))
        if ced:
            clientes[ced] = {c: str(r.get(c, "")) for c in COLS_CLIENTES}
            clientes[ced]["cedula"] = ced

    for r in ws_historial.get_all_records(expected_headers=COLS_HISTORIAL, default_blank=""):
        ced = solo_digitos(r.get("cedula"))
        fecha = str(r.get("fecha_consulta", ""))
        if not ced or not fecha:
            continue
        c = clientes.setdefault(ced, {k: "" for k in COLS_CLIENTES} | {"cedula": ced})
        if fecha >= c.get("ultima_consulta", ""):
            c["tiene_multa"] = str(r.get("tiene_multa", ""))
            c["total_multa"] = str(a_entero(r.get("total_multa")))
            c["ultima_consulta"] = fecha
    return clientes


def filas_clientes(clientes):
    filas = []
    for c in sorted(clientes.values(), key=lambda x: x.get("ultima_consulta", ""), reverse=True):
        filas.append([c["cedula"], c["nombre_cliente"], c["numero_credito"], c["celular"],
                      c["tiene_multa"], a_entero(c["total_multa"]), c["ultima_consulta"]])
    return filas


def armar_cgen(clientes, mes):
    cgen, revision = [], []
    multados = [c for c in clientes.values()
                if c.get("tiene_multa") == "SI" and c.get("ultima_consulta", "").startswith(mes)]
    multados.sort(key=lambda c: (c["ultima_consulta"], -a_entero(c["total_multa"])))
    for c in multados:
        cel, cred = limpiar_celular(c["celular"]), solo_digitos(c["numero_credito"])
        if len(cel) == 10 and cred:
            fila = {col: "" for col in COLUMNAS_CGEN} | VALORES_FIJOS_CGEN
            fila.update({
                "Fecha": c["ultima_consulta"],
                "Numero de Gestionable": cred,
                "Informacion de contacto": cel,
                "Observacion": str(a_entero(c["total_multa"])),
            })
            cgen.append([fila[col] for col in COLUMNAS_CGEN])
        else:
            revision.append([c["cedula"], c["nombre_cliente"], cred, cel,
                             a_entero(c["total_multa"]), c["ultima_consulta"]])
    return cgen, revision


# ─── Consulta SIMIT ─────────────────────────────────────────────────────────
def consultar_una(sb, S, cedula):
    """Devuelve el total de multas de una cédula (lanza excepción si falla)."""
    S.navegar_a_busqueda(sb)
    S.consultar_cedula(sb, cedula)
    texto = sb.get_text("body").lower()
    if ("se han encontrado varios resultados" in texto or "múltiples" in texto
            or sb.is_element_visible(".modalMultiplesPersonas")):
        return S.manejar_doble_documento(sb, cedula)
    return S.extraer_valor_multa(sb)


def main():
    inicio = time.time()
    hoy = ahora()
    mes = hoy.strftime("%Y-%m")
    print(f"== Robot multas | {hoy:%Y-%m-%d %H:%M} | mes {mes}")

    sh = abrir_sheet()
    ws_base = hoja(sh, "Base", ["cedula", "numero_credito", "nombre_cliente", "celular"])
    ws_cli = hoja(sh, "Clientes", COLS_CLIENTES)
    ws_hist = hoja(sh, "Historial", COLS_HISTORIAL)
    ws_cgen = hoja(sh, "Carga_CGEN", COLUMNAS_CGEN)
    ws_rev = hoja(sh, "Revision")
    ws_est = hoja(sh, "Estado")

    base = leer_base(ws_base)
    clientes = leer_clientes(ws_cli, ws_hist)

    # Actualizar datos de contacto con la Base del mes + calcular pendientes
    pendientes = []
    for b in base:
        c = clientes.setdefault(b["cedula"], {k: "" for k in COLS_CLIENTES} | {"cedula": b["cedula"]})
        for campo in ("nombre_cliente", "numero_credito", "celular"):
            if b[campo]:
                c[campo] = b[campo]
        if toca_consultar(c, hoy):
            pendientes.append(b["cedula"])

    print(f"Base: {len(base)} clientes | pendientes este mes: {len(pendientes)}")

    consultados = errores = 0
    if pendientes:
        import scraper as S
        from seleniumbase import SB

        buffer_hist = []
        errores_seguidos = 0

        def guardar(final=False):
            nonlocal buffer_hist
            if buffer_hist:
                ws_hist.append_rows(buffer_hist, value_input_option="RAW")
                buffer_hist = []
            if final or consultados % (LOTE_GUARDADO * 4) == 0:
                escribir_tabla(ws_cli, COLS_CLIENTES, filas_clientes(clientes))

        with SB(uc=True, headless=True, incognito=True,
                binary_location=os.environ.get("CHROME_BIN") or None,
                chromium_arg="--no-sandbox,--disable-dev-shm-usage") as sb:
            sb.uc_open_with_reconnect(S.URL_SIMIT, 4)
            time.sleep(S.PAUSA_REFRESCO)

            for i, ced in enumerate(pendientes, start=1):
                if (time.time() - inicio) / 60 > LIMITE_MINUTOS:
                    print("Tiempo de esta corrida agotado; la próxima sigue con los pendientes.")
                    break
                try:
                    total = consultar_una(sb, S, ced)
                    fecha = ahora().strftime("%Y-%m-%d")
                    tiene = "SI" if total > 0 else "NO"
                    c = clientes[ced]
                    c.update(tiene_multa=tiene, total_multa=str(total), ultima_consulta=fecha)
                    buffer_hist.append([ced, tiene, total, fecha])
                    consultados += 1
                    errores_seguidos = 0
                    if consultados % LOTE_GUARDADO == 0:
                        guardar()
                        print(f"  avance: {consultados} consultados, {errores} errores")
                except Exception as e:
                    errores += 1
                    errores_seguidos += 1
                    print(f"  error #{errores}: {type(e).__name__}")
                    try:
                        sb.refresh_page()
                        time.sleep(5)
                    except Exception:
                        pass
                    if errores_seguidos >= MAX_ERRORES_SEGUIDOS:
                        print("Demasiados errores seguidos (posible bloqueo del SIMIT). Se para esta corrida.")
                        break

                if i % CLIENTES_ANTES_DE_REFRESCO == 0:
                    S.refrescar_sesion(sb, {"mensaje": ""})
                time.sleep(PAUSA_ENTRE_CONSULTAS)

        guardar(final=True)

    # Reportes del mes
    cgen, revision = armar_cgen(clientes, mes)
    escribir_tabla(ws_cgen, COLUMNAS_CGEN, cgen)
    escribir_tabla(ws_rev, ["cedula", "nombre_cliente", "numero_credito", "celular",
                            "total_multa", "ultima_consulta"], revision)

    en_base = {b["cedula"] for b in base}
    faltan = sum(1 for c in en_base if toca_consultar(clientes[c], hoy))
    hechos_mes = sum(1 for c in en_base if clientes[c].get("ultima_consulta", "").startswith(mes))
    multados_mes = sum(1 for c in en_base if clientes[c].get("ultima_consulta", "").startswith(mes)
                       and clientes[c].get("tiene_multa") == "SI")
    if not pendientes:
        escribir_tabla(ws_cli, COLS_CLIENTES, filas_clientes(clientes))
    escribir_tabla(ws_est, ["dato", "valor"], [
        ["ultima_corrida", ahora().strftime("%Y-%m-%d %H:%M")],
        ["mes", mes],
        ["clientes_en_base", len(base)],
        ["consultados_este_mes", hechos_mes],
        ["pendientes", faltan],
        ["con_multa_este_mes", multados_mes],
        ["filas_carga_cgen", len(cgen)],
        ["multados_sin_celular_o_credito", len(revision)],
        ["consultados_en_esta_corrida", consultados],
        ["errores_en_esta_corrida", errores],
    ])
    print(f"Fin: {consultados} consultados, {errores} errores, pendientes {faltan}, "
          f"CGEN {len(cgen)} filas")


if __name__ == "__main__":
    main()
