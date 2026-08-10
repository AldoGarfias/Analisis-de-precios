# -*- coding: utf-8 -*-
"""APLICADOR de cambios de precio vía el ENDPOINT AUDITADO del ERP
(POST /api/agent/cambiar-precios). Aprobado 2026-08-04.

El pipeline sigue SIN escribir precios: este módulo es un paso separado, con
confirmación humana, que toma el CSV revisado (botón ⬇ del reporte: modelo,
precio_sugerido, comentario) y aplica SOLO el precio que cambia (decisión del
usuario: el endpoint acepta mandar únicamente el campo que se mueve —
precio_nuevo1 o precio_nuevo3 según el tipo_precio del modelo; el 5 jamás).

Candados: dry-run por DEFAULT (aplicar exige --aplicar); valida contra la
corrida vigente (modelo en recos, precio = sugerido ±0.5%, corte <21 días,
paso ≤4.5%); remate siempre "no"; máx 200 por corrida; log por corrida en
out/aplicaciones/. Tras aplicar: monitoreo.aceptar(codigos) (registro →
medición 4/8/12 sem, tope acumulado) y seguimiento_frenos.registrar.

.env.local: ERP_API_URL (default http://localhost:3000), ERP_API_KEY
(obligatoria), ERP_ACTOR_EMAIL (o --actor correo).

Uso:  ./.venv/bin/python aplicar.py precios_subir_2026-08-04.csv [--aplicar] [--actor correo]
"""
import os
import sys
import time
from datetime import datetime

import pandas as pd
import requests

BASE = os.path.dirname(os.path.abspath(__file__))
MAX_POR_CORRIDA = 200
TOL_PRECIO = 0.005     # ±0.5% entre CSV y recos
MAX_PASO = 0.045       # ±4.5%: nada fuera del guardrail llega al ERP
MAX_CORTE_DIAS = 21    # recos más viejas que esto no se aplican


def _env():
    ruta = os.path.join(BASE, ".env.local")
    if not os.path.exists(ruta):
        raise SystemExit(".env.local no existe (ERP_API_KEY va ahí)")
    env = {}
    for ln in open(ruta):
        ln = ln.strip()
        if ln and not ln.startswith("#") and "=" in ln:
            k, v = ln.split("=", 1)
            env[k.strip()] = v.strip()
    return env


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    aplicar = "--aplicar" in sys.argv
    actor = None
    if "--actor" in sys.argv:
        actor = sys.argv[sys.argv.index("--actor") + 1]
    if not args:
        raise SystemExit(__doc__)
    env = _env()
    url = env.get("ERP_API_URL", "http://localhost:3000").rstrip("/")
    key = env.get("ERP_API_KEY", "")
    actor = actor or env.get("ERP_ACTOR_EMAIL", "")
    if aplicar and not key:
        raise SystemExit("falta ERP_API_KEY en .env.local")
    if aplicar and not actor:
        raise SystemExit("falta actor: --actor correo o ERP_ACTOR_EMAIL en .env.local")

    csv = pd.read_csv(args[0])
    recos = pd.read_csv(os.path.join(BASE, "out", "recomendaciones.csv"))
    corte = pd.to_datetime(recos.corte.iloc[0])
    edad = (pd.Timestamp.today().normalize() - corte).days
    if edad > MAX_CORTE_DIAS:
        raise SystemExit(f"recos con corte {corte.date()} ({edad} días): corre el motor antes de aplicar")
    r = recos.set_index("codigo")
    import glob as _g
    snaps = sorted(_g.glob(os.path.join(BASE, "data", "vigia", "snap_*.parquet")))
    snap = pd.read_parquet(snaps[-1]).set_index("codigo") if snaps else None

    listos, rechazados = [], []
    for _, x in csv.iterrows():
        cod, p_csv = str(x.modelo), float(x.precio_sugerido)
        if cod not in r.index:
            rechazados.append((cod, "no está en la corrida vigente")); continue
        rr = r.loc[cod]
        if abs(p_csv / float(rr.precio_sugerido) - 1) > TOL_PRECIO:
            rechazados.append((cod, f"precio CSV {p_csv} ≠ sugerido {rr.precio_sugerido}")); continue
        if float(rr.cambio_pct) == 0:
            rechazados.append((cod, "MANTENER: nada que aplicar")); continue
        if abs(p_csv / float(rr.precio_actual) - 1) > MAX_PASO:
            rechazados.append((cod, "paso fuera del guardrail ±4pts")); continue
        campo, marca = _campo_precio(cod, round(p_csv, 2), snap)
        if campo is None:
            rechazados.append((cod, marca)); continue
        nota = str(x.get("comentario", "")) or f"Motor de Precios v3 | ciclo {corte.date()}"
        listos.append({"modelo": cod, campo: round(p_csv, 2), "nota": nota,
                       "remate": "no", "actor_email": actor or "(pendiente)"})
    listos = listos[:MAX_POR_CORRIDA]

    print(f"{len(listos)} por aplicar | {len(rechazados)} rechazados"
          + (f" (primeros: {rechazados[:3]})" if rechazados else ""), flush=True)
    if not aplicar:
        for p in listos[:10]:
            print("  DRY-RUN →", p, flush=True)
        print("(dry-run: nada se envió — agrega --aplicar para ejecutar)", flush=True)
        return

    os.makedirs(os.path.join(BASE, "out", "aplicaciones"), exist_ok=True)
    log, exitosos = [], []
    for p in listos:
        try:
            resp = requests.post(f"{url}/api/agent/cambiar-precios", json=p,
                                 headers={"x-api-key": key,
                                          "Content-Type": "application/json"},
                                 timeout=30)
            ok = resp.status_code < 300
            log.append({**p, "http": resp.status_code, "resp": resp.text[:200]})
            if ok:
                exitosos.append(p["modelo"])
            print(f"  {p['modelo']}: {resp.status_code}", flush=True)
        except Exception as e:
            log.append({**p, "http": "ERROR", "resp": str(e)[:200]})
            print(f"  {p['modelo']}: ERROR {str(e)[:60]}", flush=True)
        time.sleep(0.5)
    ruta_log = os.path.join(BASE, "out", "aplicaciones",
                            f"aplicacion_{datetime.now():%Y%m%d_%H%M}.csv")
    pd.DataFrame(log).to_csv(ruta_log, index=False)
    print(f"aplicados {len(exitosos)}/{len(listos)} → log {ruta_log}", flush=True)
    if exitosos:
        import monitoreo
        monitoreo.aceptar(exitosos)          # registro → medición + tope acumulado
        import seguimiento_frenos
        seguimiento_frenos.registrar()       # frenos aplicados al seguimiento


def _campo_precio(cod, p_nuevo, snap):
    """REGLA P1/P3 (usuario 2026-08-04): se reemplaza el precio PUBLICADO.
    Solo P1 → cambiar P1. P1 y P3 → cambiar P3, VALIDANDO P3 < P1; si el
    nuevo P3 quedaría ≥ P1, NO se aplica: se marca REQUIERE_P1 (hay que
    actualizar también el P1 o revisar a mano)."""
    p1 = p3 = 0.0
    if snap is not None and cod in snap.index:
        p1 = float(snap.at[cod, "precio_1"] or 0)
        p3 = float(snap.at[cod, "precio_3"] or 0)
    if p3 <= 0:
        return "precio_nuevo1", ""
    if p_nuevo >= p1 > 0:
        return None, f"REQUIERE_P1: nuevo P3 {p_nuevo} ≥ P1 {p1}"
    return "precio_nuevo3", ""


def servir(puerto=8765):
    """Puente LOCAL para los botones Aplicar del reporte: la API key vive
    aquí (en .env.local), jamás en el HTML. POST /aplicar con
    {"modelos":[{"modelo":..,"precio":..}]} → valida contra recos + snapshot
    de vigía (regla P1/P3) y reenvía al endpoint auditado del ERP."""
    import glob as _g
    import json
    from http.server import BaseHTTPRequestHandler, HTTPServer
    env = _env()
    url = env.get("ERP_API_URL", "http://localhost:3000").rstrip("/")
    key, actor = env.get("ERP_API_KEY", ""), env.get("ERP_ACTOR_EMAIL", "")
    if not key or not actor:
        raise SystemExit("faltan ERP_API_KEY/ERP_ACTOR_EMAIL en .env.local")
    recos = pd.read_csv(os.path.join(BASE, "out", "recomendaciones.csv")).set_index("codigo")
    corte = pd.to_datetime(recos.corte.iloc[0]).date()
    snaps = sorted(_g.glob(os.path.join(BASE, "data", "vigia", "snap_*.parquet")))
    snap = pd.read_parquet(snaps[-1]).set_index("codigo") if snaps else None
    # DORMIDOS (usuario 2026-08-10): también se aplican desde el reporte —
    # se validan contra SU fuente (segunda capa), no contra recos
    ruta_d = os.path.join(BASE, "out", "segunda_capa_dormidos.csv")
    dorm = (pd.read_csv(ruta_d).dropna(subset=["precio_sugerido"])
            .drop_duplicates("codigo").set_index("codigo")
            if os.path.exists(ruta_d) else None)

    class H(BaseHTTPRequestHandler):
        def _resp(self, code, obj):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def do_OPTIONS(self):
            self._resp(200, {})

        def do_POST(self):
            try:
                datos = json.loads(self.rfile.read(
                    int(self.headers.get("Content-Length", 0))))
                res = []
                for m in datos.get("modelos", [])[:MAX_POR_CORRIDA]:
                    cod, p = str(m["modelo"]), float(m["precio"])
                    es_dormido = cod not in recos.index and dorm is not None \
                        and cod in dorm.index
                    if cod not in recos.index and not es_dormido:
                        res.append({"modelo": cod, "status": "RECHAZADO: no está en la corrida ni en dormidos"}); continue
                    if es_dormido:
                        dd = dorm.loc[cod]
                        if abs(p / float(dd.precio_sugerido) - 1) > TOL_PRECIO:
                            res.append({"modelo": cod, "status": f"RECHAZADO: precio ≠ sugerido de dormidos ({dd.precio_sugerido})"}); continue
                        nota = f"Motor de Precios v3 | dormidos {corte} | {dd.direccion}"
                    else:
                        rr = recos.loc[cod]
                        if abs(p / float(rr.precio_sugerido) - 1) > TOL_PRECIO or float(rr.cambio_pct) == 0:
                            res.append({"modelo": cod, "status": "RECHAZADO: precio ≠ sugerido o MANTENER"}); continue
                        nota = f"Motor de Precios v3 | ciclo {corte} | {rr.cambio_pct:+.0f}%"
                    campo, marca = _campo_precio(cod, round(p, 2), snap)
                    if campo is None:
                        res.append({"modelo": cod, "status": marca}); continue
                    payload = {"modelo": cod, campo: round(p, 2),
                               "nota": nota, "remate": "no", "actor_email": actor}
                    r_ = requests.post(f"{url}/api/agent/cambiar-precios", json=payload,
                                       headers={"x-api-key": key}, timeout=30)
                    res.append({"modelo": cod, "campo": campo, "http": r_.status_code,
                                "status": "APLICADO" if r_.status_code < 300 else f"ERROR {r_.text[:120]}"})
                ok = [r_["modelo"] for r_ in res if r_.get("status") == "APLICADO"]
                if ok:
                    from datetime import datetime as _dt
                    os.makedirs(os.path.join(BASE, "out", "aplicaciones"), exist_ok=True)
                    pd.DataFrame(res).to_csv(os.path.join(
                        BASE, "out", "aplicaciones",
                        f"aplicacion_{_dt.now():%Y%m%d_%H%M%S}.csv"), index=False)
                    ok_motor = [c for c in ok if c in recos.index]
                    if ok_motor:
                        import monitoreo
                        monitoreo.aceptar(ok_motor)
                self._resp(200, {"resultados": res})
            except Exception as e:
                self._resp(500, {"error": str(e)[:200]})

        def log_message(self, *a):
            pass

    print(f"puente de aplicación en http://127.0.0.1:{puerto} → {url} "
          f"(corte {corte}; Ctrl-C para detener)", flush=True)
    HTTPServer(("127.0.0.1", puerto), H).serve_forever()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "servir":
        servir()
    else:
        main()
