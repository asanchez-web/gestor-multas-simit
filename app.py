import os
import sqlite3
import threading
from datetime import datetime
import pandas as pd
from flask import Flask, render_template, request, jsonify, send_file
from scraper import ejecutar_scraper

app = Flask(__name__)
DB_PATH = "data/historial.db"
os.makedirs("data", exist_ok=True)

# Estado global del proceso (visible en la página web en tiempo real)
estado_proceso = {
    "activo": False,
    "total": 0,
    "procesados": 0,
    "mensaje": "Esperando archivo...",
    "errores": 0
}

# ─── Columnas esperadas del Excel de Athena ─────────────────────────────────
# El sistema busca estas columnas (case-insensitive). Si tu Excel usa nombres
# diferentes, agrega las variantes aquí.
POSIBLES_CEDULA = ['cedula', 'cédula', 'numero_documento', 'num_documento', 'documento', 'nro_documento', 'identificacion']
POSIBLES_NOMBRE = ['nombre_cliente', 'nombre', 'cliente', 'nombre_completo', 'nombres']
POSIBLES_CREDITO = ['numero_credito', 'num_credito', 'credito', 'nro_credito', 'obligacion']
POSIBLES_CELULAR = ['celular_reciente', 'celular', 'telefono', 'tel', 'client_cel',
                    'numero_celular', 'movil', 'telefono_celular', 'cel']

# ─── Reglas de reconsulta por MES CALENDARIO ────────────────────────────────
# Ya no se cuentan días: si la última consulta fue en un mes anterior al de la
# carga actual, se vuelve a consultar. Ej: consultado el 20-oct → en cualquier
# carga de noviembre (1-nov, 5-nov...) se reconsulta.
MESES_CON_MULTA = 1   # Con multa: se reconsulta cada mes
MESES_SIN_MULTA = 1   # Sin multa: pon 2 si quieres reconsultarlos cada 2 meses

# ─── Formato de carga masiva: plantilla CGEN-CargaGenerica ──────────────────
# Mismo orden y nombres de columnas que CARGA_MULTAS (hoja CGEN-CargaGenerica).
# Los valores fijos se pueden ajustar aquí si cambia la tipificación.
COLUMNAS_CGEN = ['Resultado de Gestion', 'Usuario', 'Fecha', 'Tipo gestionable',
                 'Numero de Gestionable', 'Gestion', 'Tipo Contacto', 'Gestion Con',
                 'Punto de Atencion', 'Forma Contacto', 'Informacion de contacto',
                 'Indicativo', 'tipo', 'Ciudad', 'Barrio', 'Nombre Contacto',
                 'Relacion con el Credito', 'Relacion con el Cliente', 'Estado', 'Observacion']
VALORES_FIJOS_CGEN = {
    'Resultado de Gestion': 'CSIM',
    'Usuario': 'asesorcartera',
    'Tipo gestionable': 'Obl-Credito',
    'Gestion': '1-COBRANZAS ',   # con espacio final, igual que la plantilla original
    'Tipo Contacto': 'WEB-web',
    'Gestion Con': 'CLIE-Cliente',
    'Forma Contacto': 'cel-celular',
    'tipo': 'PER-Personal',
    'Estado': 'ACT',
}


def limpiar_celular(valor):
    """Deja el celular solo con dígitos: quita '.0', espacios, +57, NaN."""
    if valor is None:
        return ''
    texto = str(valor).strip()
    if texto.lower() in ('nan', 'none', ''):
        return ''
    if texto.endswith('.0'):
        texto = texto[:-2]
    digitos = ''.join(ch for ch in texto if ch.isdigit())
    if len(digitos) == 12 and digitos.startswith('57'):
        digitos = digitos[2:]
    return digitos


def meses_entre(fecha, hoy):
    """Diferencia en meses calendario (oct→nov = 1, sin importar el día)."""
    return (hoy.year - fecha.year) * 12 + (hoy.month - fecha.month)


def buscar_columna(columnas_df, posibles_nombres):
    """Busca una columna en el DataFrame ignorando mayúsculas, tildes y espacios."""
    columnas_lower = {c.lower().strip(): c for c in columnas_df}
    for nombre in posibles_nombres:
        if nombre.lower() in columnas_lower:
            return columnas_lower[nombre.lower()]
    return None


def init_db():
    """Crea la tabla de clientes si no existe."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS clientes (
                    cedula TEXT PRIMARY KEY,
                    nombre_cliente TEXT,
                    numero_credito TEXT,
                    tiene_multa TEXT,
                    total_multa INTEGER DEFAULT 0,
                    ultima_consulta DATE
                )''')
    c.execute('''CREATE TABLE IF NOT EXISTS historial_consultas (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    cedula TEXT,
                    tiene_multa TEXT,
                    total_multa INTEGER,
                    fecha_consulta DATE
                )''')
    try:
        c.execute("ALTER TABLE clientes ADD COLUMN celular TEXT")
    except sqlite3.OperationalError:
        pass  # La columna ya existe
    conn.commit()
    conn.close()


init_db()


@app.route('/')
def index():
    """Página principal."""
    return render_template('index.html')


@app.route('/upload', methods=['POST'])
def upload():
    """Recibe el archivo de Athena, filtra pendientes y lanza el scraper."""
    global estado_proceso

    if estado_proceso["activo"]:
        return jsonify({"error": "Ya hay un proceso ejecutándose. Espera a que termine."}), 400

    archivo = request.files.get('file')
    if not archivo:
        return jsonify({"error": "No se subió ningún archivo."}), 400

    # ── Leer archivo ────────────────────────────────────────────────────
    try:
        if archivo.filename.lower().endswith('.csv'):
            df = pd.read_csv(archivo)
        else:
            df = pd.read_excel(archivo)
    except Exception as e:
        return jsonify({"error": f"No se pudo leer el archivo: {str(e)}"}), 400

    # ── Identificar columnas automáticamente ─────────────────────────────
    col_cedula = buscar_columna(df.columns, POSIBLES_CEDULA)
    col_nombre = buscar_columna(df.columns, POSIBLES_NOMBRE)
    col_credito = buscar_columna(df.columns, POSIBLES_CREDITO)
    col_celular = buscar_columna(df.columns, POSIBLES_CELULAR)

    if not col_cedula:
        return jsonify({
            "error": f"No se encontró la columna de cédula. Columnas detectadas: {list(df.columns)}"
        }), 400

    # Normalizar nombres de columna para uso interno
    df = df.rename(columns={
        col_cedula: 'cedula',
        **(({col_nombre: 'nombre_cliente'}) if col_nombre else {}),
        **(({col_credito: 'numero_credito'}) if col_credito else {}),
        **(({col_celular: 'celular'}) if col_celular else {}),
    })

    # Asegurar que existan las columnas mínimas
    if 'nombre_cliente' not in df.columns:
        df['nombre_cliente'] = 'Sin nombre'
    if 'numero_credito' not in df.columns:
        df['numero_credito'] = ''
    if 'celular' not in df.columns:
        df['celular'] = ''

    # Limpiar cédulas: quitar decimales, espacios, NaN
    df['cedula'] = df['cedula'].astype(str).str.strip().str.split('.').str[0]
    df = df[df['cedula'].str.isnumeric()].copy()  # Solo cédulas numéricas válidas
    df = df.drop_duplicates(subset='cedula')
    df['celular'] = df['celular'].apply(limpiar_celular)
    df['numero_credito'] = (df['numero_credito'].astype(str).str.strip()
                            .str.replace(r'\.0$', '', regex=True)
                            .replace({'nan': '', 'None': ''}))
    df['nombre_cliente'] = df['nombre_cliente'].fillna('Sin nombre').astype(str).str.strip()

    if df.empty:
        return jsonify({"error": "El archivo no tiene cédulas válidas."}), 400

    # ── Filtrar según reglas de negocio ──────────────────────────────────
    hoy = datetime.now().date()
    pendientes = []

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    for _, row in df.iterrows():
        cedula = str(row['cedula']).strip()

        c.execute("SELECT tiene_multa, ultima_consulta FROM clientes WHERE cedula = ?", (cedula,))
        res = c.fetchone()

        if not res:
            # Cliente nuevo → consultar de inmediato
            pendientes.append(row.to_dict())
        else:
            tiene_multa, ultima_consulta_str = res
            try:
                ultima_consulta = datetime.strptime(ultima_consulta_str, "%Y-%m-%d").date()
                meses = meses_entre(ultima_consulta, hoy)
            except (ValueError, TypeError):
                meses = 999  # Si la fecha está corrupta, reconsultar

            # REGLAS (mes calendario, no días):
            #   - Con multa → si no se consultó en el mes actual
            #   - Sin multa → según MESES_SIN_MULTA
            if tiene_multa == 'SI' and meses >= MESES_CON_MULTA:
                pendientes.append(row.to_dict())
            elif tiene_multa != 'SI' and meses >= MESES_SIN_MULTA:
                pendientes.append(row.to_dict())
            else:
                # Ya consultado este mes: igual actualizamos celular/nombre/crédito
                # con lo que trae la base nueva, para que el reporte salga al día.
                c.execute('''UPDATE clientes SET
                                celular = CASE WHEN ? != '' THEN ? ELSE celular END,
                                nombre_cliente = CASE WHEN ? != '' THEN ? ELSE nombre_cliente END,
                                numero_credito = CASE WHEN ? != '' THEN ? ELSE numero_credito END
                             WHERE cedula = ?''',
                          (row['celular'], row['celular'],
                           row['nombre_cliente'], row['nombre_cliente'],
                           row['numero_credito'], row['numero_credito'], cedula))

    conn.commit()
    conn.close()

    if not pendientes:
        return jsonify({
            "mensaje": "✅ Todos los clientes de esta base ya fueron consultados este mes. No hay nada nuevo que consultar."
        })

    # ── Lanzar scraper en segundo plano ──────────────────────────────────
    estado_proceso["errores"] = 0
    thread = threading.Thread(
        target=ejecutar_scraper,
        args=(pendientes, estado_proceso, DB_PATH),
        daemon=True
    )
    thread.start()

    return jsonify({
        "mensaje": f"🚀 Procesamiento iniciado para {len(pendientes)} clientes (se ignoraron los que ya están al día)."
    })


@app.route('/status')
def status():
    """Devuelve el estado actual del proceso para la barra de progreso."""
    return jsonify(estado_proceso)


@app.route('/historial')
def historial():
    """Devuelve el contenido actual de la BD como JSON para la tabla en pantalla."""
    conn = sqlite3.connect(DB_PATH)
    df_bd = pd.read_sql_query(
        "SELECT cedula, nombre_cliente, numero_credito, celular, tiene_multa, total_multa, ultima_consulta FROM clientes ORDER BY ultima_consulta DESC",
        conn
    )
    conn.close()
    return jsonify({
        "total": len(df_bd),
        "con_multa": int((df_bd['tiene_multa'] == 'SI').sum()),
        "sin_multa": int((df_bd['tiene_multa'] == 'NO').sum()),
        "registros": df_bd.to_dict(orient='records')
    })


@app.route('/descargar')
def descargar():
    """Genera y descarga el histórico completo o filtrado como Excel."""
    filtro = request.args.get('filtro')
    conn = sqlite3.connect(DB_PATH)
    query = """
    WITH RankedConsultas AS (
        SELECT h.cedula, h.total_multa, h.fecha_consulta,
               ROW_NUMBER() OVER(PARTITION BY h.cedula ORDER BY h.fecha_consulta DESC) as rn
        FROM historial_consultas h
    )
    SELECT 
        c.cedula,
        c.nombre_cliente,
        c.numero_credito,
        c.celular,
        c.tiene_multa,
        c.total_multa,
        c.ultima_consulta,
        COALESCE(r1.total_multa - r2.total_multa, 0) as diferencia_vs_mes_anterior
    FROM clientes c
    LEFT JOIN RankedConsultas r1 ON c.cedula = r1.cedula AND r1.rn = 1
    LEFT JOIN RankedConsultas r2 ON c.cedula = r2.cedula AND r2.rn = 2
    """
    
    if filtro == 'con_multa':
        query += " WHERE c.tiene_multa = 'SI' ORDER BY c.ultima_consulta DESC"
        prefijo = "Solo_Multados"
    else:
        query += " ORDER BY c.ultima_consulta DESC"
        prefijo = "Historico_Total"
        
    df_bd = pd.read_sql_query(query, conn)
    conn.close()

    if df_bd.empty:
        return jsonify({"error": "No hay datos para descargar bajo este filtro. Sube un archivo primero."}), 400

    output = f"data/{prefijo}.xlsx"
    df_bd.to_excel(output, index=False)
    return send_file(
        output,
        as_attachment=True,
        download_name=f"{prefijo}_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx"
    )


def escribir_xls(df, ruta, hoja):
    """Escribe un .xls (Excel 97-2003) con todo como texto, igual que la plantilla."""
    import xlwt
    wb = xlwt.Workbook(encoding='utf-8')
    ws = wb.add_sheet(hoja)
    for j, col in enumerate(df.columns):
        ws.write(0, j, col)
    for i, fila in enumerate(df.itertuples(index=False), start=1):
        for j, valor in enumerate(fila):
            if valor is not None and str(valor) not in ('', 'nan'):
                ws.write(i, j, str(valor))
    wb.save(ruta)


@app.route('/descargar/carga_masiva')
def descargar_carga_masiva():
    """Archivo .xls en formato CGEN-CargaGenerica con los clientes CON multa
    consultados en el mes actual (o ?periodo=YYYY-MM).
    Con ?revision=1 descarga los que quedaron por fuera (sin celular válido o
    sin número de crédito) para revisarlos aparte."""
    periodo = request.args.get('periodo') or datetime.now().strftime('%Y-%m')
    revision = request.args.get('revision') == '1'
    conn = sqlite3.connect(DB_PATH)
    df = pd.read_sql_query(
        """SELECT cedula, nombre_cliente, celular, numero_credito, total_multa, ultima_consulta
           FROM clientes
           WHERE tiene_multa = 'SI' AND substr(ultima_consulta, 1, 7) = ?
           ORDER BY ultima_consulta, total_multa DESC""",
        conn, params=(periodo,)
    )
    conn.close()

    if df.empty:
        return jsonify({"error": f"No hay clientes con multa consultados en {periodo}."}), 400

    df['celular'] = df['celular'].apply(limpiar_celular)
    df['numero_credito'] = (df['numero_credito'].fillna('').astype(str).str.strip()
                            .str.replace(r'\.0$', '', regex=True))
    valido = (df['celular'].str.len() == 10) & (df['numero_credito'] != '')

    if revision:
        out = df[~valido][['cedula', 'nombre_cliente', 'numero_credito', 'celular',
                           'total_multa', 'ultima_consulta']]
        if out.empty:
            return jsonify({"mensaje": "Todos los multados del mes tienen celular y crédito. Nada que revisar."})
        ruta = f"data/Revision_Carga_Multas_{periodo}.xlsx"
        out.to_excel(ruta, index=False)
        return send_file(ruta, as_attachment=True, download_name=f"Revision_Carga_Multas_{periodo}.xlsx")

    ok = df[valido]
    cgen = pd.DataFrame('', index=ok.index, columns=COLUMNAS_CGEN)
    for col, val in VALORES_FIJOS_CGEN.items():
        cgen[col] = val
    cgen['Fecha'] = ok['ultima_consulta']
    cgen['Numero de Gestionable'] = ok['numero_credito']
    cgen['Informacion de contacto'] = ok['celular']
    cgen['Observacion'] = ok['total_multa'].astype(int).astype(str)

    nombre = f"CARGA_MULTAS_{datetime.now().strftime('%d_%m_%Y')}.xls"
    ruta = f"data/{nombre}"
    escribir_xls(cgen, ruta, 'CGEN-CargaGenerica')
    return send_file(ruta, as_attachment=True, download_name=nombre)


@app.route('/estadisticas')
def estadisticas():
    """Estadísticas generales de la BD y tendencias mes a mes."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM clientes")
    total = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM clientes WHERE tiene_multa = 'SI'")
    con_multa = c.fetchone()[0]
    c.execute("SELECT COALESCE(SUM(total_multa), 0) FROM clientes WHERE tiene_multa = 'SI'")
    monto_total = c.fetchone()[0]

    # Calcular tendencias basándose en el historial
    query_tendencia = """
    WITH RankedConsultas AS (
        SELECT cedula, total_multa, fecha_consulta,
               ROW_NUMBER() OVER(PARTITION BY cedula ORDER BY fecha_consulta DESC) as rn
        FROM historial_consultas
    )
    SELECT 
        c.cedula,
        c.total_multa as deuda_actual,
        prev.total_multa as deuda_anterior
    FROM RankedConsultas c
    JOIN RankedConsultas prev ON c.cedula = prev.cedula AND prev.rn = 2
    WHERE c.rn = 1
    """
    df_tendencia = pd.read_sql_query(query_tendencia, conn)
    conn.close()

    aumentaron = int(((df_tendencia['deuda_actual'] - df_tendencia['deuda_anterior']) > 150000).sum())
    bajaron = int((df_tendencia['deuda_actual'] < df_tendencia['deuda_anterior']).sum())
    
    # Monto total aumentado o disminuido
    diferencia_total = int((df_tendencia['deuda_actual'] - df_tendencia['deuda_anterior']).sum())

    return jsonify({
        "total_clientes": total,
        "con_multa": con_multa,
        "sin_multa": total - con_multa,
        "monto_total_multas": monto_total,
        "tendencia_aumentaron": aumentaron,
        "tendencia_bajaron": bajaron,
        "tendencia_diferencia_total": diferencia_total
    })


@app.route('/cliente/<cedula>/historial')
def historial_cliente(cedula):
    """Devuelve el historial mes a mes de un cliente específico."""
    conn = sqlite3.connect(DB_PATH)
    df_hist = pd.read_sql_query(
        "SELECT fecha_consulta, total_multa FROM historial_consultas WHERE cedula = ? ORDER BY fecha_consulta DESC", 
        conn, params=(cedula,)
    )
    conn.close()
    
    # Calcular diferencia con mes anterior para mostrar en UI
    if not df_hist.empty:
        # Invertir para calcular diff (de más antiguo a más nuevo)
        df_hist = df_hist.iloc[::-1].copy()
        df_hist['diferencia'] = df_hist['total_multa'].diff().fillna(df_hist['total_multa'])
        # Volver a invertir para mostrar más nuevo primero
        df_hist = df_hist.iloc[::-1]

    return jsonify(df_hist.to_dict(orient='records'))


@app.route('/novedades')
def novedades():
    """Devuelve las novedades de los clientes comparando su última consulta con la penúltima."""
    conn = sqlite3.connect(DB_PATH)
    query = """
    WITH RankedConsultas AS (
        SELECT h.cedula, c.nombre_cliente, h.total_multa, h.fecha_consulta,
               ROW_NUMBER() OVER(PARTITION BY h.cedula ORDER BY h.fecha_consulta DESC) as rn
        FROM historial_consultas h
        JOIN clientes c ON h.cedula = c.cedula
    )
    SELECT 
        c.cedula,
        c.nombre_cliente,
        c.total_multa as deuda_actual,
        COALESCE(prev.total_multa, 0) as deuda_anterior
    FROM RankedConsultas c
    LEFT JOIN RankedConsultas prev ON c.cedula = prev.cedula AND prev.rn = 2
    WHERE c.rn = 1 AND c.total_multa != COALESCE(prev.total_multa, 0)
    """
    df = pd.read_sql_query(query, conn)
    conn.close()

    nuevas_multas = []
    aumentos = []
    disminuciones = []

    for _, row in df.iterrows():
        dif = row['deuda_actual'] - row['deuda_anterior']
        obj = {
            "cedula": row['cedula'],
            "nombre": row['nombre_cliente'] or 'Sin Nombre',
            "actual": row['deuda_actual'],
            "anterior": row['deuda_anterior'],
            "diferencia": dif
        }
        
        if row['deuda_anterior'] == 0 and row['deuda_actual'] > 0:
            nuevas_multas.append(obj)
        elif dif > 150000:
            aumentos.append(obj)
        elif dif < 0:
            disminuciones.append(obj)

    return jsonify({
        "nuevas": nuevas_multas,
        "aumentos": aumentos,
        "disminuciones": disminuciones
    })


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)
