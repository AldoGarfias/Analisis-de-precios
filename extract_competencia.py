# -*- coding: utf-8 -*-
"""PRECIOS DE LA COMPETENCIA — extractor del feed diario de correo (aprobado
2026-08-07): saadclaw7@gmail.com envía cada día "CSVs de distribuidores" con
11-12 CSVs adjuntos (tvc, tecnosinergia, ct, exel, adises, cva, portenntum,
absa, alcione, luguer, fibremex, dextra) — listado de modelos con precio,
descuento y precio final de cada competidor.

Vía: IMAP solo-lectura (imap.gmail.com) con contraseña de aplicación en
.env.local (GMAIL_USER / GMAIL_APP_PASSWORD — jamás en código ni commits).
Idempotente: los adjuntos ya guardados se saltan (por nombre de archivo).

Salida: data/competencia/<archivo>.csv (crudos, tal como llegan) +
data/competencia/_manifiesto.csv (correo, fecha, archivos).

Uso:  ./.venv/bin/python extract_competencia.py          (baja lo que falte)
"""
import email
import imaplib
import os

import pandas as pd

BASE = os.path.dirname(os.path.abspath(__file__))
DEST = os.path.join(BASE, "data", "competencia")
REMITENTE = "saadclaw7@gmail.com"
ASUNTO = "CSVs de distribuidores"


def _env():
    env = {}
    for ln in open(os.path.join(BASE, ".env.local")):
        ln = ln.strip()
        if ln and not ln.startswith("#") and "=" in ln:
            k, v = ln.split("=", 1)
            env[k.strip()] = v.strip()
    return env


def correr():
    env = _env()
    user = env.get("GMAIL_USER", "")
    pwd = env.get("GMAIL_APP_PASSWORD", "").replace(" ", "")
    if not user or not pwd or "PEGA_AQUI" in pwd:
        raise SystemExit("faltan GMAIL_USER/GMAIL_APP_PASSWORD en .env.local")
    os.makedirs(DEST, exist_ok=True)
    ya = set(os.listdir(DEST))

    M = imaplib.IMAP4_SSL("imap.gmail.com")
    M.login(user, pwd)
    M.select("INBOX", readonly=True)   # SOLO lectura: jamás marca ni borra
    typ, data = M.search(None, f'(FROM "{REMITENTE}" SUBJECT "{ASUNTO}")')
    ids = data[0].split()
    print(f"feed de competencia: {len(ids)} correos", flush=True)

    manif, nuevos = [], 0
    for mid in ids:
        typ, msg_data = M.fetch(mid, "(RFC822)")
        msg = email.message_from_bytes(msg_data[0][1])
        fecha = pd.to_datetime(msg["Date"], utc=True).date().isoformat()
        for parte in msg.walk():
            fn = parte.get_filename()
            if not fn or not fn.lower().endswith(".csv"):
                continue
            manif.append({"correo_fecha": fecha, "archivo": fn})
            if fn in ya:
                continue
            contenido = parte.get_payload(decode=True)
            with open(os.path.join(DEST, fn), "wb") as f:
                f.write(contenido)
            nuevos += 1
    M.logout()
    pd.DataFrame(manif).to_csv(os.path.join(DEST, "_manifiesto.csv"), index=False)
    print(f"adjuntos nuevos guardados: {nuevos} | total en {DEST}: "
          f"{len([f for f in os.listdir(DEST) if f.endswith('.csv') and not f.startswith('_')])}",
          flush=True)


if __name__ == "__main__":
    correr()
