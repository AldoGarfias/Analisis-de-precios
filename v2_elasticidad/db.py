# -*- coding: utf-8 -*-
"""Conexiones para el pipeline de precio óptimo.

Credenciales: se leen de un archivo `.env.local` junto a este script (no se copian aquí).
- Lectura: réplica (ERP_MYSQL_HOST_READ) — SELECT/SHOW/DESCRIBE.
- Escritura: primario (ERP_MYSQL_HOST) — ACOTADA a tablas `precio_optimo_*`.
  El pipeline NUNCA escribe cat_articulos2 ni otras tablas; los precios reales
  se cambian por el flujo auditado de erp-nextjs2 (cambiar-precios).
"""
import os
import re

import pymysql
from dotenv import load_dotenv

# Credenciales fuera del código: crea un archivo `.env.local` junto a este db.py
# con estas variables (SIN comillas). No lo subas a git.
#   ERP_MYSQL_HOST_READ=<host réplica de lectura>
#   ERP_MYSQL_HOST=<host primario de escritura>
#   ERP_MYSQL_PORT=3306
#   ERP_MYSQL_USER=<usuario>
#   ERP_MYSQL_PASSWORD=<password>
#   ERP_MYSQL_DATABASE=<schema>
ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env.local")
load_dotenv(ENV_PATH)


def _conectar(host_env):
    con = pymysql.connect(
        host=os.environ[host_env],
        port=int(os.environ.get("ERP_MYSQL_PORT", 3306)),
        user=os.environ["ERP_MYSQL_USER"],
        password=os.environ["ERP_MYSQL_PASSWORD"],
        database=os.environ.get("ERP_MYSQL_DATABASE", "2012iniciar"),
        charset="latin1",
        autocommit=False,
        read_timeout=300,
        cursorclass=pymysql.cursors.Cursor,
    )
    # pymysql mapea el charset MySQL latin1 al códec cp1252, que tiene bytes
    # indefinidos (0x8d, etc.); iso-8859-1 mapea los 256 bytes sin fallar.
    con.encoding = "iso-8859-1"
    return con


def conectar_erp():
    """MySQL ERP (2012iniciar), réplica de lectura."""
    return _conectar("ERP_MYSQL_HOST_READ")


def query(sql, params=None, con=None):
    """Ejecuta un SELECT y regresa (columnas, filas)."""
    verbo = sql.strip().split()[0].upper()
    if verbo not in ("SELECT", "SHOW", "DESCRIBE", "DESC", "EXPLAIN"):
        raise ValueError(f"Solo lectura: verbo no permitido: {verbo}")
    cerrar = con is None
    if con is None:
        con = conectar_erp()
    try:
        with con.cursor() as cur:
            cur.execute(sql, params if params else None)
            cols = [d[0] for d in cur.description] if cur.description else []
            filas = cur.fetchall()
        return cols, filas
    finally:
        if cerrar:
            con.close()


def conectar_erp_escritura():
    """MySQL ERP primario para ESCRITURA acotada a tablas precio_optimo_*."""
    return _conectar("ERP_MYSQL_HOST")


# candado: solo se permite escribir a estas tablas del proyecto
_TABLA_OBJETIVO = re.compile(
    r"^\s*(?:INSERT\s+INTO|UPDATE|DELETE\s+FROM|CREATE\s+TABLE(?:\s+IF\s+NOT\s+EXISTS)?"
    r"|TRUNCATE(?:\s+TABLE)?|REPLACE\s+INTO)\s+`?(precio_optimo_[a-z_]+)`?",
    re.IGNORECASE,
)


def ejecutar(sql, params=None, con=None):
    """DML/DDL restringido a tablas precio_optimo_*. Devuelve filas afectadas.

    No hace commit (lo controla el llamador para agrupar en transacción).
    """
    m = _TABLA_OBJETIVO.match(sql)
    if not m:
        raise ValueError("Escritura no permitida: la sentencia no apunta a una tabla precio_optimo_*")
    cerrar = con is None
    if con is None:
        con = conectar_erp_escritura()
    try:
        with con.cursor() as cur:
            n = cur.executemany(sql, params) if _es_lote(params) else cur.execute(sql, params)
        return n
    finally:
        if cerrar:
            con.commit()
            con.close()


def _es_lote(params):
    return isinstance(params, (list, tuple)) and params and isinstance(params[0], (list, tuple, dict))
