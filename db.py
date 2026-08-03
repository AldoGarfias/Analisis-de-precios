# -*- coding: utf-8 -*-
"""Conexiones para el motor de precio óptimo v3 (nueva BD MySQL/Aurora en AWS).

Diseño de seguridad (ver reglas innegociables en CLAUDE.md):

  1. Credenciales SOLO en `.env.local` (junto a este db.py, en .gitignore).
     Nunca en el código, nunca en commits.
  2. Lectura por defecto contra la RÉPLICA (DB_HOST_READ), usuario de solo-SELECT.
     `query()` rechaza cualquier verbo que no sea SELECT/SHOW/DESCRIBE/EXPLAIN.
  3. Escritura ACOTADA a tablas propias del proyecto (`precio_optimo_*`) contra el
     PRIMARIO (DB_HOST_WRITE). A diferencia del v2, aquí NO se valida SQL arbitrario
     con regex (ese candado tenía un bypass por multi-tabla: `UPDATE precio_optimo_x
     JOIN otra SET otra.col=...` lo pasaba). En su lugar se exponen funciones que
     CONSTRUYEN el SQL internamente (`guardar_df`, `crear_tabla`, `reemplazar_tabla`)
     con el nombre de tabla validado contra un prefijo estricto y sin forma de
     inyectar un JOIN/coma en la cláusula de tabla.
  4. El pipeline NUNCA escribe precios reales al catálogo del ERP. Los cambios de
     precio se aplican solo por el flujo auditado del ERP.
"""
import os
import re

import pymysql
from dotenv import load_dotenv

# Credenciales fuera del código: crea `.env.local` junto a este db.py (ver .env.example).
ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env.local")
load_dotenv(ENV_PATH)

# El driver de este módulo es pymysql: solo soporta MySQL / Aurora MySQL.
_ENGINE = os.environ.get("DB_ENGINE", "mysql").strip().lower()
if _ENGINE not in ("mysql", "aurora-mysql", "aurora_mysql"):
    raise RuntimeError(
        f"db.py está escrito para MySQL/Aurora MySQL (pymysql); DB_ENGINE={_ENGINE!r}. "
        "Para otro motor hay que cambiar el driver, el placeholder de parámetros y el quoting."
    )


def _conectar(host_env):
    host = os.environ.get(host_env)
    if not host:
        raise RuntimeError(f"Falta {host_env} en .env.local (ver .env.example).")
    con = pymysql.connect(
        host=host,
        port=int(os.environ.get("DB_PORT", 3306)),
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
        database=os.environ.get("DB_DATABASE") or None,
        # La BD nueva en AWS debería ser utf8mb4; se deja configurable por si acaso.
        charset=os.environ.get("DB_CHARSET", "utf8mb4"),
        autocommit=False,
        read_timeout=int(os.environ.get("DB_READ_TIMEOUT", 300)),
        cursorclass=pymysql.cursors.Cursor,
    )
    return con


def conectar_lectura():
    """Conexión de SOLO LECTURA (réplica). Úsala con query()."""
    return _conectar("DB_HOST_READ")


# Alias por compatibilidad con el patrón de extract del v2.
conectar_erp = conectar_lectura


def conectar_escritura():
    """Conexión al PRIMARIO para escritura ACOTADA a tablas precio_optimo_*."""
    return _conectar("DB_HOST_WRITE")


# ---------------------------------------------------------------------------
# Lectura
# ---------------------------------------------------------------------------
_VERBOS_LECTURA = ("SELECT", "SHOW", "DESCRIBE", "DESC", "EXPLAIN", "WITH")


def query(sql, params=None, con=None):
    """Ejecuta una consulta de lectura y regresa (columnas, filas).

    Rechaza cualquier verbo que no sea de lectura. `WITH` se permite para CTEs
    que terminan en SELECT (MySQL 8 / Aurora MySQL 3).
    """
    verbo = sql.lstrip().split(None, 1)[0].upper() if sql.strip() else ""
    if verbo not in _VERBOS_LECTURA:
        raise ValueError(f"Solo lectura: verbo no permitido: {verbo!r}")
    cerrar = con is None
    if con is None:
        con = conectar_lectura()
    try:
        with con.cursor() as cur:
            cur.execute(sql, params if params else None)
            cols = [d[0] for d in cur.description] if cur.description else []
            filas = cur.fetchall()
        return cols, filas
    finally:
        if cerrar:
            con.close()


# ---------------------------------------------------------------------------
# Almacenamiento LOCAL de recomendaciones (fase de pruebas)
# ---------------------------------------------------------------------------
# En esta fase el pipeline SOLO LEE de Aurora; las recomendaciones se guardan
# localmente en out/, no en la BD. Cuando se habilite la escritura a
# precio_optimo_*, se usan las funciones de la sección de abajo (requieren
# DB_HOST_WRITE en .env.local).
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out")


def guardar_recos_local(df, nombre="recomendaciones"):
    """Guarda recomendaciones en out/{nombre}.csv y .parquet. Devuelve la ruta del CSV."""
    if df is None:
        raise ValueError("df es None")
    os.makedirs(OUT, exist_ok=True)
    ruta_csv = os.path.join(OUT, f"{nombre}.csv")
    ruta_pq = os.path.join(OUT, f"{nombre}.parquet")
    df.to_csv(ruta_csv, index=False)
    df.to_parquet(ruta_pq, index=False)
    print(f"guardado local: {ruta_csv} y {ruta_pq} ({len(df)} filas)", flush=True)
    return ruta_csv


# ---------------------------------------------------------------------------
# Escritura acotada a tablas propias del proyecto (precio_optimo_*)  [DORMIDO]
# ---------------------------------------------------------------------------
# No se usa en la fase de pruebas (solo-lectura). Solo se activa si se llama
# explícitamente y existe DB_HOST_WRITE. Se conserva el diseño anti-bypass.
# Prefijo estricto: minúsculas, dígitos y guion bajo. Sin backtick, espacio,
# coma, punto (schema.tabla) ni nada que permita colarse a otra tabla.
_NOMBRE_TABLA_PROPIA = re.compile(r"^precio_optimo_[a-z0-9_]+$")


def _validar_tabla_propia(tabla):
    """Devuelve el nombre validado o revienta. Este es el único candado de escritura."""
    if not isinstance(tabla, str) or not _NOMBRE_TABLA_PROPIA.match(tabla):
        raise ValueError(
            f"Escritura no permitida: {tabla!r} no es una tabla propia (precio_optimo_[a-z0-9_]+). "
            "El pipeline solo escribe a sus propias tablas; los precios reales del ERP "
            "se cambian por el flujo auditado, nunca desde aquí."
        )
    return tabla


def crear_tabla(tabla, columnas_sql, con=None, si_no_existe=True):
    """CREATE TABLE en una tabla propia. `columnas_sql` es el cuerpo entre paréntesis.

    Ejemplo:
        crear_tabla("precio_optimo_recos",
                    "id_art INT PRIMARY KEY, precio_reco DECIMAL(12,4), confianza VARCHAR(8)")
    El nombre de tabla se valida; `columnas_sql` lo define el código del proyecto,
    no entra input del ERP.
    """
    tabla = _validar_tabla_propia(tabla)
    ine = "IF NOT EXISTS " if si_no_existe else ""
    sql = f"CREATE TABLE {ine}`{tabla}` ({columnas_sql})"
    return _ejecutar_interno(sql, None, con)


def guardar_df(df, tabla, con=None, modo="reemplazar", chunk=1000):
    """Inserta un DataFrame en una tabla propia construyendo el SQL internamente.

    modo="reemplazar": TRUNCATE + INSERT (la tabla debe existir).
    modo="append":     solo INSERT.
    No hace commit: lo controla el llamador para agrupar en transacción
    (se conserva el patrón del v2 que la revisión aprobó).
    Devuelve el número de filas insertadas.
    """
    tabla = _validar_tabla_propia(tabla)
    if modo not in ("reemplazar", "append"):
        raise ValueError(f"modo inválido: {modo!r}")
    if df is None or len(df) == 0:
        return 0

    cols = list(df.columns)
    # Los nombres de columna también se validan (defensa en profundidad):
    # solo identificadores simples, sin backtick ni truco de quoting.
    for c in cols:
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", str(c)):
            raise ValueError(f"Nombre de columna no permitido: {c!r}")

    cerrar = con is None
    if con is None:
        con = conectar_escritura()
    n_total = 0
    try:
        with con.cursor() as cur:
            if modo == "reemplazar":
                cur.execute(f"TRUNCATE TABLE `{tabla}`")
            col_list = ",".join(f"`{c}`" for c in cols)
            placeholders = ",".join(["%s"] * len(cols))
            sql = f"INSERT INTO `{tabla}` ({col_list}) VALUES ({placeholders})"
            filas = [tuple(None if _es_na(v) else v for v in row)
                     for row in df.itertuples(index=False, name=None)]
            for i in range(0, len(filas), chunk):
                n_total += cur.executemany(sql, filas[i:i + chunk])
        return n_total
    finally:
        if cerrar:
            con.commit()
            con.close()


def reemplazar_tabla(df, tabla, columnas_sql, con=None):
    """Conveniencia: crea (si no existe) y reemplaza el contenido en una transacción."""
    tabla = _validar_tabla_propia(tabla)
    cerrar = con is None
    if con is None:
        con = conectar_escritura()
    try:
        crear_tabla(tabla, columnas_sql, con=con)
        n = guardar_df(df, tabla, con=con, modo="reemplazar")
        return n
    finally:
        if cerrar:
            con.commit()
            con.close()


def _ejecutar_interno(sql, params, con):
    """Ejecuta SQL YA construido internamente (nunca SQL crudo del usuario)."""
    cerrar = con is None
    if con is None:
        con = conectar_escritura()
    try:
        with con.cursor() as cur:
            n = cur.execute(sql, params if params else None)
        return n
    finally:
        if cerrar:
            con.commit()
            con.close()


def _es_na(v):
    """True para None/NaN sin importar pandas aquí (evita ciclo de imports)."""
    return v is None or (isinstance(v, float) and v != v)
