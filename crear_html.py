# -*- coding: utf-8 -*-
"""Genera reporte_precios_competencia.html: comparativa SYSCOM vs competencia
con filtros por marca (proveedor) y por distribuidor (competencia).

Datos: analisis_precios.parquet (por par) + venta por código del panel.
HTML autocontenido (JS vanilla, sin CDN).
"""
import json
import os

import pandas as pd

BASE = os.path.dirname(os.path.abspath(__file__))
COMP = os.path.join(BASE, "data", "competencia")

a = pd.read_parquet(os.path.join(COMP, "analisis_precios.parquet"))
a["marca"] = a.marca.fillna("")

# venta SYSCOM por código
pan = pd.read_parquet(os.path.join(BASE, "data", "panel.parquet"))
pan["sem_ts"] = pd.to_datetime(pan.semana)
ult = pan.sem_ts.max()
rec = pan[pan.sem_ts >= ult - pd.Timedelta(weeks=8)]
vtotal = pan.groupby("codigo").unidades.sum().rename("unidades_total")
v8 = rec.groupby("codigo").unidades.sum().rename("unidades_8s")
venta = vtotal.to_frame().join(v8).fillna(0)

a = a.join(venta, on="modelo_syscom")
a["unidades_total"] = a.unidades_total.fillna(0).astype(int)
a["unidades_8s"] = a.unidades_8s.fillna(0).astype(int)

# columnas para el HTML
cols = ["distribuidor", "modelo_distribuidor", "marca", "modelo_syscom",
        "descripcion_syscom", "unidades_8s", "unidades_total",
        "precio_lista_usd_sys", "subtotal_usd_sys", "precio_lista_usd",
        "precio_venta_usd", "vs_lista", "vs_sub", "fecha_dist", "tipo_precio"]
a = a[cols].dropna(subset=["precio_venta_usd", "precio_lista_usd_sys"])
a["vs_lista"] = a.vs_lista.round(1)
a["vs_sub"] = a.vs_sub.round(1)
a["fecha_dist"] = a.fecha_dist.astype(str)

filas = a.sort_values(["unidades_8s", "unidades_total"], ascending=False).to_dict("records")
datos = json.dumps(filas, ensure_ascii=False)

HTML = """<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Precios SYSCOM vs Competencia</title>
<style>
:root{--g:#0f766e;--border:#d1d5db;--h:#111827}
*{box-sizing:border-box}
body{font:12px/1.35 -apple-system,Segoe UI,Roboto,Arial,sans-serif;margin:0;background:#f3f4f6;color:var(--h)}
header{background:var(--g);color:#fff;padding:10px 16px}
h1{margin:0;font-size:16px}
.wrap{padding:12px 14px;max-width:100%;margin:0 auto}
.filtros{display:flex;gap:10px;flex-wrap:wrap;margin:10px 0;background:#fff;border:1px solid var(--border);border-radius:8px;padding:10px}
.filtros label{font-size:11px;color:#4b5563;display:block;margin-bottom:2px}
.filtros select,.filtros input{padding:5px 7px;border:1px solid var(--border);border-radius:6px;min-width:150px;font-size:12px}
.met{display:flex;gap:12px;flex-wrap:wrap;margin:10px 0}
.met div{flex:1;min-width:170px;background:#fff;border:1px solid var(--border);border-radius:8px;padding:8px 12px}
.met b{font-size:18px;color:var(--g)}
table{width:auto;max-width:100%;border-collapse:collapse;background:#fff;border:1px solid var(--border);font-size:11px}
th{background:#e5e7eb;position:sticky;top:0;left:0;text-align:left;padding:4px 7px;cursor:pointer;user-select:none;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;border-right:1px dashed var(--border)}
td{padding:3px 7px;border-top:1px solid #f3f4f6;border-right:1px dashed var(--border);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:230px}
tr:hover{background:#f0fdfa}
.num{text-align:right;font-variant-numeric:tabular-nums}
.pos{color:#dc2626}.neg{color:#059669}
.tabla-scroll{max-height:75vh;overflow:auto;border-radius:8px;border:1px solid var(--border)}
.muted{color:#6b7280;font-size:11px}
.foot{padding:12px 20px;color:#6b7280;font-size:11px;text-align:center}
</style>
</head>
<body>
<header><h1>Precios SYSCOM vs Competencia (match exacto)</h1></header>
<div class="wrap">
  <div class="filtros">
    <div><label>Proveedor / Marca</label><select id="fMarca"><option value="">Todas</option></select></div>
    <div><label>Competencia (distribuidor)</label><select id="fDist"><option value="">Todos</option></select></div>
    <div><label>Buscar modelo/descripción</label><input id="fQ" placeholder="ej. TLWR850N"></div>
    <div><label>Ordenar por</label>
      <select id="fOrd">
        <option value="unidades_8s">Venta SYSCOM (8s)</option>
        <option value="unidades_total">Venta SYSCOM (total)</option>
        <option value="vs_lista">Diff% vs lista</option>
        <option value="precio_venta_usd">Precio venta USD</option>
      </select>
    </div>
  </div>
  <div class="met">
    <div><b id="mPar">0</b><br><span class="muted">pares</span></div>
    <div><b id="mSys">0</b><br><span class="muted">modelos SYSCOM</span></div>
    <div><b id="mBajos">0</b><br><span class="muted">pares donde competidor vende &lt; nuestra lista</span></div>
    <div><b id="mMed">—</b><br><span class="muted">diff% media venta vs lista</span></div>
  </div>
  <div class="tabla-scroll">
  <table>
    <thead><tr>
      <th>Distribuidor</th><th>Modelo</th><th>Marca</th><th>SYSCOM</th><th>Descripción</th>
      <th>Venta 8s</th><th>Venta tot</th><th>LiSys</th><th>SubSys</th>
      <th>LiComp</th><th>VComp</th><th>%lista</th><th>%sub</th><th>Fecha</th>
    </tr></thead>
    <tbody id="tb"></tbody>
  </table>
  </div>
  <div class="foot">Generado el """ + pd.Timestamp.now().strftime("%Y-%m-%d %H:%M") + """ — datos del panel y feed de competencia.</div>
</div>
<script>
const DATA = __DATA__;
const q = (s)=>document.querySelector(s);
const fmt = (v)=> v==null||isNaN(v) ? '—' : Number(v).toLocaleString('en-US',{maximumFractionDigits:2});
const pct = (v)=> v==null||isNaN(v) ? '—' : '<span class="'+(v<0?'neg':'pos')+'">'+ (v>0?'+':'') + v.toFixed(1)+'%</span>';

const marcas = [...new Set(DATA.map(d=>d.marca).filter(Boolean))].sort();
marcas.forEach(m=>{ const o=document.createElement('option'); o.value=m; o.textContent=m; q('#fMarca').appendChild(o); });
const dists = [...new Set(DATA.map(d=>d.distribuidor))].sort();
dists.forEach(d=>{ const o=document.createElement('option'); o.value=d; o.textContent=d; q('#fDist').appendChild(o); });

function apply(){
  const marca = q('#fMarca').value, dist = q('#fDist').value, qq = q('#fQ').value.trim().toLowerCase();
  const ord = q('#fOrd').value;
  let rows = DATA.filter(d=>{
    if(marca && d.marca!==marca) return false;
    if(dist && d.distribuidor!==dist) return false;
    if(qq && !(String(d.modelo_syscom).toLowerCase().includes(qq)||String(d.descripcion_syscom).toLowerCase().includes(qq)||String(d.modelo_distribuidor).toLowerCase().includes(qq))) return false;
    return true;
  });
  rows.sort((x,y)=>(y[ord]??-Infinity)-(x[ord]??-Infinity));
  const tb=q('#tb'); tb.innerHTML='';
  rows.forEach(r=>{
    const tr=document.createElement('tr');
    const desc=(r.descripcion_syscom||'');
    const dcell=desc.length>38 ? '<td title="'+desc.replace(/"/g,'&quot;')+'" style="cursor:help">'+desc.substring(0,38)+'…</td>' : '<td>'+desc+'</td>';
    tr.innerHTML='<td title="'+r.distribuidor+'">'+r.distribuidor+'</td><td title="'+r.modelo_distribuidor+'">'+r.modelo_distribuidor+'</td><td title="'+r.marca+'">'+r.marca+'</td>'
      +'<td><b>'+r.modelo_syscom+'</b></td>'+dcell
      +'<td class="num">'+fmt(r.unidades_8s)+'</td><td class="num">'+fmt(r.unidades_total)+'</td>'
      +'<td class="num">'+fmt(r.precio_lista_usd_sys)+'</td><td class="num">'+fmt(r.subtotal_usd_sys)+'</td>'
      +'<td class="num">'+fmt(r.precio_lista_usd)+'</td><td class="num"><b>'+fmt(r.precio_venta_usd)+'</b></td>'
      +'<td class="num">'+pct(r.vs_lista)+'</td><td class="num">'+pct(r.vs_sub)+'</td>'
      +'<td class="num">'+(r.fecha_dist||'').split(' ')[0].slice(5)+'</td>';
    tb.appendChild(tr);
  });
  q('#mPar').textContent=rows.length;
  q('#mSys').textContent=new Set(rows.map(r=>r.modelo_syscom)).size;
  q('#mBajos').textContent=rows.filter(r=>(r.vs_lista??0)<0).length;
  const m=rows.filter(r=>r.vs_lista!=null).map(r=>r.vs_lista);
  q('#mMed').textContent=m.length? (m.reduce((a,b)=>a+b,0)/m.length).toFixed(1)+'%' : '—';
}
['fMarca','fDist','fQ','fOrd'].forEach(id=>q('#'+id).addEventListener('input',apply));
apply();
</script>
</body>
</html>
"""
HTML = HTML.replace("__DATA__", datos)
ruta = os.path.join(BASE, "out", "reporte_precios_competencia.html")
with open(ruta, "w", encoding="utf-8") as f:
    f.write(HTML)
print(f"generado: {ruta} ({os.path.getsize(ruta)/1e6:.1f} MB, {len(filas):,} filas)")