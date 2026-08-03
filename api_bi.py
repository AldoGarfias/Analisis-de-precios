# -*- coding: utf-8 -*-
"""Cliente de la API de BI de SYSCOM (developers.syscom.mx) — SOLO LECTURA.

Nueva fase (2026-07-29): integrar las BD del ERP vía API REST. Primero probar
conexión; después migrar base por base confirmando que los datos necesarios
están, ANTES de mover cualquier consumidor del pipeline.

Seguridad (mismas reglas del proyecto):
  - El CLIENT_SECRET vive SOLO en .env.local (gitignored): BI_CLIENT_SECRET=...
  - El token (dura 365 días) se guarda automáticamente en .env.local (BI_TOKEN)
    la primera vez; nunca en código, git ni chat.
  - Cliente de solo lectura: valida SELECT/WITH antes de mandar (además del
    candado del servidor).

Reglas del servidor (incorporadas aquí):
  - Solo SELECT (con WITH/CTE), UNA sentencia por request.
  - Solo tablas de la allow-list (GET /bi/tables).
  - LIMIT obligatorio, máx 1000 (si falta, el server pone LIMIT 100 —
    este cliente lo agrega explícito y avisa).
  - Timeout 60s por consulta; 30 requests/min (429 ⇒ respetar Retry-After).
  - Dialecto MySQL 8. Celdas >1000 chars se truncan.
  - 307 → IP no autorizada (red local SYSCOM): pedir alta a IT.

Uso:
  ./.venv/bin/python api_bi.py token      # canjea el secret por token (1 vez)
  ./.venv/bin/python api_bi.py probar     # prueba de conexión + allow-list
  ./.venv/bin/python api_bi.py tablas     # tablas disponibles
  ./.venv/bin/python api_bi.py muestra    # explora v_bi_eventos_interacciones
  from api_bi import q                    # q(sql) -> DataFrame, en el pipeline
"""
import json
import os
import re
import sys
import time

import pandas as pd
import requests

BASE = os.path.dirname(os.path.abspath(__file__))
ENV = os.path.join(BASE, ".env.local")
API = "https://developers.syscom.mx"
CLIENT_ID_DEFAULT = "bi-colab-prod"   # override con BI_CLIENT_ID en .env.local
MAX_LIMIT = 1000


def _env():
    if not os.path.exists(ENV):
        raise RuntimeError(".env.local no existe")
    out = {}
    for ln in open(ENV):
        ln = ln.strip()
        if ln and not ln.startswith("#") and "=" in ln:
            k, v = ln.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def _guardar_env(clave, valor):
    lineas = open(ENV).read().splitlines() if os.path.exists(ENV) else []
    lineas = [l for l in lineas if not l.startswith(f"{clave}=")]
    lineas.append(f"{clave}={valor}")
    with open(ENV, "w") as f:
        f.write("\n".join(lineas) + "\n")


def token():
    """Canjea BI_CLIENT_SECRET por un token (365 días) y lo guarda en .env.local."""
    env = _env()
    secret = env.get("BI_CLIENT_SECRET")
    if not secret:
        raise RuntimeError("falta BI_CLIENT_SECRET en .env.local — agrégalo ahí "
                         "(nunca en el chat) y reintenta")
    r = requests.post(f"{API}/oauth/token", data={
        "client_id": env.get("BI_CLIENT_ID", CLIENT_ID_DEFAULT), "client_secret": secret,
        "grant_type": "client_credentials"}, timeout=30, allow_redirects=False)
    if r.status_code == 307:
        raise RuntimeError("307: tu IP no está autorizada (red local SYSCOM) — "
                         "pedir a IT que la agregue")
    r.raise_for_status()
    tok = r.json()["access_token"]
    _guardar_env("BI_TOKEN", tok)
    print(f"✓ token obtenido y guardado en .env.local (expira en "
          f"{r.json().get('expires_in', 0)//86400} días)", flush=True)
    return tok


def _token():
    env = _env()
    if env.get("BI_TOKEN"):
        return env["BI_TOKEN"]
    return token()


def _valida(sql):
    limpio = re.sub(r"/\*.*?\*/", "", sql, flags=re.S).strip().rstrip(";")
    verbo = limpio.split(None, 1)[0].upper() if limpio else ""
    if verbo not in ("SELECT", "WITH"):
        raise PermissionError(f"solo lectura: '{verbo}' rechazado")
    if ";" in limpio:
        raise PermissionError("una sola sentencia por request")
    if not re.search(r"\bLIMIT\s+\d+", limpio, re.I):
        limpio += f" LIMIT {MAX_LIMIT}"
        print(f"  (sin LIMIT: agregado LIMIT {MAX_LIMIT})", flush=True)
    return limpio


def q(sql, reintentos=3):
    """Ejecuta un SELECT en la API y devuelve un DataFrame."""
    sql = _valida(sql)
    tok = _token()
    for i in range(reintentos):
        r = requests.post(f"{API}/api/v1/bi/query", json={"sql": sql},
                          headers={"Authorization": f"Bearer {tok}"},
                          timeout=90, allow_redirects=False)
        if r.status_code == 307:
            raise RuntimeError("307: IP no autorizada — pedir alta a IT")
        if r.status_code == 401:
            print("  token inválido/expirado: renovando…", flush=True)
            tok = token()
            continue
        if r.status_code == 429:
            espera = int(r.headers.get("Retry-After", 5))
            print(f"  429 rate limit: esperando {espera}s…", flush=True)
            time.sleep(espera)
            continue
        if r.status_code == 400:
            raise ValueError(f"400 {r.text[:300]}")
        if r.status_code >= 500 and i < reintentos - 1:
            espera = 5 * (i + 1)
            detalle = r.text[:120].replace("\n", " ") if r.text else ""
            print(f"  {r.status_code} del servidor: reintento en {espera}s… {detalle}", flush=True)
            time.sleep(espera)
            continue
        r.raise_for_status()
        js = r.json()
        filas_js = js.get("rows", [])
        df = (pd.DataFrame(filas_js) if filas_js and isinstance(filas_js[0], dict)
              else pd.DataFrame(filas_js, columns=js.get("columns", [])))
        if js.get("limitAdded"):
            print("  (el servidor agregó LIMIT — resultado posiblemente parcial)",
                  flush=True)
        if js.get("truncatedCells"):
            print(f"  ({js['truncatedCells']} celdas de texto truncadas)", flush=True)
        return df
    raise RuntimeError("agotados los reintentos")


def tablas():
    tok = _token()
    r = requests.get(f"{API}/api/v1/bi/tables", headers={"Authorization": f"Bearer {tok}"},
                     timeout=30, allow_redirects=False)
    if r.status_code == 307:
        raise RuntimeError("307: IP no autorizada — pedir alta a IT")
    r.raise_for_status()
    print("allow-list de tablas:", flush=True)
    print(json.dumps(r.json(), indent=2, ensure_ascii=False)[:2000], flush=True)
    return r.json()


def probar():
    t0 = time.time()
    ts = tablas()
    df = q("SELECT 1 AS ok LIMIT 1")
    print(f"✓ CONEXIÓN OK ({time.time()-t0:.1f}s) — query de prueba: {df.ok.iloc[0]}",
          flush=True)
    return ts


def muestra():
    """Exploración inicial de v_bi_eventos_interacciones."""
    print("== v_bi_eventos_interacciones ==", flush=True)
    df = q("SELECT MIN(fecha) AS desde, MAX(fecha) AS hasta, COUNT(*) AS n "
           "FROM v_bi_eventos_interacciones LIMIT 1")
    print(df.to_string(index=False), flush=True)
    df = q("SELECT tipo, COUNT(*) AS n FROM v_bi_eventos_interacciones "
           "GROUP BY tipo ORDER BY n DESC LIMIT 20")
    print("\npor tipo:", flush=True)
    print(df.to_string(index=False), flush=True)
    df = q("SELECT fuente, COUNT(*) AS n FROM v_bi_eventos_interacciones "
           "GROUP BY fuente ORDER BY n DESC LIMIT 20")
    print("\npor fuente:", flush=True)
    print(df.to_string(index=False), flush=True)
    df = q("SELECT * FROM v_bi_eventos_interacciones LIMIT 5")
    print("\nmuestra de filas:", flush=True)
    print(df.to_string(index=False), flush=True)


def vigilar():
    """Compara la allow-list contra la última conocida; notifica si hay tablas
    NUEVAS (p.ej. reporte_61 / valor_inventario al habilitarse). Corre en el
    cron diario vía seguimiento_frenos."""
    import requests as _rq
    ruta = os.path.join(BASE, "out", "bi_allowlist.json")
    try:
        tok = _token()
        r = _rq.get(f"{API}/api/v1/bi/tables", headers={"Authorization": f"Bearer {tok}"},
                    timeout=30, allow_redirects=False)
        r.raise_for_status()
        actuales = sorted(t["name"] for t in r.json()["tables"])
    except Exception as e:
        print(f"vigilante BI: sin acceso hoy ({str(e)[:60]})", flush=True)
        return
    previas = (json.load(open(ruta)) if os.path.exists(ruta) else [])
    nuevas = sorted(set(actuales) - set(previas))
    json.dump(actuales, open(ruta, "w"))
    if nuevas:
        print(f"vigilante BI: ¡TABLAS NUEVAS en la API!: {nuevas}", flush=True)
        try:
            import subprocess
            subprocess.run(["osascript", "-e",
                            f'display notification "Tablas nuevas en la API de BI: '
                            f'{", ".join(nuevas)}" with title '
                            f'"Motor de Precios — API BI" sound name "Glass"'], timeout=10)
        except Exception:
            pass
        with open(os.path.join(BASE, "out", f"ALERTA_api_bi.txt"), "w") as f:
            f.write(f"Tablas nuevas habilitadas en la API de BI: {nuevas}\n"
                    f"Siguiente paso: ./.venv/bin/python valida_migracion.py\n")
    else:
        print(f"vigilante BI: sin cambios ({len(actuales)} tablas)", flush=True)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "probar"
    {"token": token, "probar": probar, "tablas": tablas, "muestra": muestra,
     "vigilar": vigilar}.get(
        cmd, probar)()
