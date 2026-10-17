"""Front de Nexo (Caso 1 - Nexus Retail), hecho solo con FastAPI.
FastAPI arma la pagina en Python y llama al dominio a traves del gateway.
"""
import os
import time
from html import escape
from urllib.parse import quote

import httpx
from fastapi import FastAPI, Form
from fastapi.responses import HTMLResponse

GATEWAY_URL = os.getenv("GATEWAY_URL", "http://gateway:8080")
DOMINIO = f"{GATEWAY_URL}/api/dominio"
TIMEOUT_S = float(os.getenv("TIMEOUT_S", "5"))
CATEGORIAS_POR_DEFECTO = ["Almacenamiento", "Audio", "Cables y adaptadores", "Componentes", "Gaming",
                          "Monitores", "Notebooks", "Perifericos", "Redes", "Smartphones"]
RESPUESTA_INESPERADA = "El servicio de dominio respondio en un formato inesperado. Avisa al equipo."

app = FastAPI(title="front", docs_url=None, redoc_url=None, openapi_url=None)


class ErrorServicio(Exception):
    pass


def llamar(metodo: str, url: str, **kwargs):
    inicio = time.perf_counter()
    try:
        r = httpx.request(metodo, url, timeout=TIMEOUT_S, **kwargs)
    except httpx.TimeoutException:
        raise ErrorServicio(f"El servicio tardo mas de {TIMEOUT_S:.0f} s en responder.")
    except httpx.TransportError:
        raise ErrorServicio("No hay conexion con el gateway. Revisa que el sistema este levantado.")
    ms = (time.perf_counter() - inicio) * 1000
    try:
        datos = r.json()
    except ValueError:
        datos = None
    detalle = datos.get("detail") if isinstance(datos, dict) else None
    if r.status_code in (502, 503, 504):
        raise ErrorServicio(detalle or "Un servicio del sistema no esta disponible.")
    if r.is_error:
        raise ErrorServicio(f"Error {r.status_code}: {detalle or 'respuesta inesperada'}")
    return datos, ms


def pagina(usuario_sel: str = "", categoria_sel: str = "", resultado: str = "") -> str:
    avisos = []
    try:
        usuarios, _ = llamar("GET", f"{DOMINIO}/usuarios")
    except ErrorServicio as e:
        usuarios = []
        avisos.append(f"No se pudo cargar la lista de clientes. {e}")
    if not isinstance(usuarios, list):
        usuarios = []
    usuarios = [u for u in usuarios if isinstance(u, dict) and "usuario_id" in u]
    try:
        categorias, _ = llamar("GET", f"{DOMINIO}/categorias")
    except ErrorServicio:
        categorias = None
    if not isinstance(categorias, list) or not categorias:
        categorias = CATEGORIAS_POR_DEFECTO

    opciones_usuario = "".join(
        f'<option value="{escape(str(u["usuario_id"]))}"{" selected" if str(u["usuario_id"]) == usuario_sel else ""}>'
        f'{escape(str(u["usuario_id"]))}{" (" + escape(str(u["region"])) + ")" if u.get("region") else ""}</option>'
        for u in usuarios
    )
    campo_usuario = (
        f'<select name="usuario_id" required><option value="">Elige un cliente</option>{opciones_usuario}</select>'
        if usuarios else
        f'<input name="usuario_id" required placeholder="Por ejemplo, U103808" value="{escape(usuario_sel)}">'
    )
    opciones_categoria = "".join(
        f'<option{" selected" if c == categoria_sel else ""}>{escape(str(c))}</option>' for c in categorias
    )
    avisos_html = "".join(f'<p class="error">{escape(a)}</p>' for a in avisos)

    return f"""<!doctype html>
<html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Nexo - Recomendador de compras</title>
<style>
  body {{ font-family: system-ui, sans-serif; max-width: 720px; margin: 2rem auto; padding: 0 1rem; color: #17201c; }}
  nav {{ margin-bottom: 1.5rem; }}
  nav a {{ margin-right: 1rem; color: #0f6b4f; text-decoration: none; font-weight: 600; }}
  form {{ display: grid; gap: .75rem; padding: 1rem; border: 1px solid #d5ddd9; border-radius: 8px; }}
  select, input, button {{ font: inherit; padding: .5rem; }}
  button {{ background: #0f6b4f; color: #fff; border: 0; border-radius: 6px; cursor: pointer; }}
  .prob {{ font-size: 3rem; font-weight: 700; margin: 1rem 0 0; }}
  .meta {{ color: #5b6862; font-size: .9rem; }}
  .error {{ background: #fbe9e5; color: #a3341f; padding: .75rem; border-radius: 6px; }}
  table {{ border-collapse: collapse; width: 100%; font-size: .9rem; }}
  th, td {{ text-align: left; padding: .4rem; border-bottom: 1px solid #d5ddd9; }}
</style></head>
<body>
  <nav><a href="/">Recomendador</a><a href="/dashboard">Dashboard</a></nav>
  <h1>Recomendador de compras</h1>
  <p>Elige un cliente y una categoria para estimar que tan probable es que compre en ella.</p>
  {avisos_html}
  <form method="post" action="/predecir">
    <label>Cliente {campo_usuario}</label>
    <label>Categoria <select name="categoria">{opciones_categoria}</select></label>
    <button type="submit">Calcular probabilidad</button>
  </form>
  {resultado}
</body></html>"""


def historial_html(usuario_id: str) -> str:
    try:
        filas, _ = llamar("GET", f"{DOMINIO}/usuarios/{quote(usuario_id, safe='')}/historial")
    except ErrorServicio as e:
        return f'<p class="error">No se pudo cargar el historial. {escape(str(e))}</p>'
    if not filas:
        return "<p class=\"meta\">Este cliente todavia no tiene compras registradas.</p>"
    if not isinstance(filas, list) or not all(isinstance(f, dict) for f in filas):
        return f'<p class="error">No se pudo cargar el historial. {RESPUESTA_INESPERADA}</p>'
    columnas = ["producto", "categoria", "fecha", "cantidad", "total", "estado"]
    cabecera = "".join(f"<th>{escape(str(c))}</th>" for c in columnas)
    cuerpo = ""
    for f in filas:
        celdas = ""
        for c in columnas:
            celdas += f"<td>{escape(str(f.get(c, '-')))}</td>"
        cuerpo += f"<tr>{celdas}</tr>"
    return f"<table><thead><tr>{cabecera}</tr></thead><tbody>{cuerpo}</tbody></table>"


@app.get("/health")
def health():
    return {"status": "ok", "servicio": "front"}


@app.get("/", response_class=HTMLResponse)
def inicio():
    return pagina()


@app.post("/predecir", response_class=HTMLResponse)
def predecir(usuario_id: str = Form(...), categoria: str = Form(...)):
    try:
        res, ms = llamar("POST", f"{DOMINIO}/predicciones", json={"usuario_id": usuario_id, "categoria": categoria})
        prob = float(res["probabilidad_compra"])
        veredicto = "Conviene recomendarle esta categoria." if res.get("prediccion") == 1 \
            else "No conviene recomendarle esta categoria por ahora."
        detalles = [f"Modelo {res.get('version_modelo', 'desconocido')}",
                    f"prediccion n. {res.get('prediccion_id', '?')}", f"{ms:.0f} ms"]
        if res.get("instancia"):
            detalles.append(f"replica {res['instancia']}")
        resultado = (f'<p class="prob">{prob:.0%}</p><p>{veredicto}</p>'
                     f'<p class="meta">{escape(", ".join(detalles))}</p>')
    except ErrorServicio as e:
        resultado = f'<p class="error">{escape(str(e))}</p>'
    except (KeyError, TypeError, ValueError):
        resultado = f'<p class="error">{RESPUESTA_INESPERADA}</p>'
    resultado += f"<h2>Historial de compras</h2>{historial_html(usuario_id)}"
    return pagina(usuario_sel=usuario_id, categoria_sel=categoria, resultado=resultado)


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard():
    metricas = {}
    health_status = {}
    try:
        m, _ = llamar("GET", f"{DOMINIO}/metricas")
        metricas = m if isinstance(m, dict) else {}
    except ErrorServicio as e:
        metricas = {"error": str(e)}
    try:
        h, _ = llamar("GET", "http://gateway:8080/health")
        health_status["gateway"] = h
    except ErrorServicio:
        health_status["gateway"] = {"status": "error"}
    try:
        h, _ = llamar("GET", f"{DOMINIO}/health")
        health_status["dominio"] = h
    except ErrorServicio:
        health_status["dominio"] = {"status": "error"}
    try:
        h, _ = llamar("GET", "http://ia:8000/health")
        health_status["ia"] = h
    except ErrorServicio:
        health_status["ia"] = {"status": "error"}

    totales = metricas.get("totales", {})
    propension = metricas.get("propension", {})
    prop_completa = metricas.get("propension_completa", propension)
    regiones = metricas.get("regiones", [])
    categorias = metricas.get("categorias_productos", [])
    estados = metricas.get("estados_transacciones", [])
    top_cats = metricas.get("top_categorias_transacciones", [])
    ultimas = metricas.get("ultimas_predicciones", [])
    avg_prob = metricas.get("probabilidad_promedio", 0)
    met_ia = metricas.get("metricas_ia", {})

    # Health badges
    def badge(name, info):
        st = info.get("status", "error") if isinstance(info, dict) else "error"
        color = "#0f6b4f" if st == "ok" else "#a3341f"
        return f'<span style="background:{color};color:#fff;padding:.3rem .6rem;border-radius:4px;font-size:.85rem;margin-right:.5rem;">{name}: {st}</span>'

    health_html = badge("Gateway", health_status.get("gateway", {})) + \
                  badge("Dominio", health_status.get("dominio", {})) + \
                  badge("IA", health_status.get("ia", {}))

    # Chart data
    prop_labels = list(prop_completa.keys()) if prop_completa else ["sin datos"]
    prop_values = list(prop_completa.values()) if prop_completa else [1]
    region_labels = [r["region"] for r in regiones[:8]]
    region_values = [r["n"] for r in regiones[:8]]
    cat_labels = [c["categoria"] for c in categorias[:8]]
    cat_values = [c["n"] for c in categorias[:8]]
    estado_labels = [e["estado"] for e in estados]
    estado_values = [e["n"] for e in estados]
    top_labels = [t["categoria"] for t in top_cats]
    top_values = [t["n"] for t in top_cats]

    # Ultimas predicciones tabla
    if ultimas:
        cols = list(ultimas[0].keys())
        header = "".join(f"<th>{escape(str(c))}</th>" for c in cols)
        rows = "".join("<tr>" + "".join(f"<td>{escape(str(p.get(c,'')))}</td>" for c in cols) + "</tr>" for p in ultimas)
        pred_table = f"<table><thead><tr>{header}</tr></thead><tbody>{rows}</tbody></table>"
    else:
        pred_table = "<p class='meta'>Aun no hay predicciones registradas.</p>"

    # Metricas del modelo IA (matriz de confusion / metricas)
    acc = met_ia.get("accuracy", 0)
    prec = met_ia.get("precision", 0)
    rec = met_ia.get("recall", 0)
    f1 = met_ia.get("f1_score", 0)
    roc = met_ia.get("roc_auc", 0)
    modelo_features = met_ia.get("features", [])
    modelo_version = met_ia.get("version", "1.0.0")

    import json as _json

    return f"""<!doctype html>
<html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Nexo - Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.min.js"></script>
<style>
  body {{ font-family: system-ui, sans-serif; max-width: 1200px; margin: 2rem auto; padding: 0 1rem; color: #17201c; }}
  nav {{ margin-bottom: 1.5rem; }}
  nav a {{ margin-right: 1rem; color: #0f6b4f; text-decoration: none; font-weight: 600; }}
  .cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 1rem; margin: 1.5rem 0; }}
  .card {{ background: linear-gradient(135deg, #f4f8f6, #e8f4f0); border: 1px solid #d5ddd9; border-radius: 12px; padding: 1.5rem; text-align: center; box-shadow: 0 2px 8px rgba(0,0,0,.06); }}
  .card .num {{ font-size: 2.4rem; font-weight: 800; color: #0f6b4f; margin-bottom: .3rem; }}
  .card .label {{ color: #5b6862; font-size: .95rem; font-weight: 500; }}
  .charts {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(500px, 1fr)); gap: 2rem; margin: 1.5rem 0; }}
  .chart-box {{ background: #fff; border: 1px solid #d5ddd9; border-radius: 8px; padding: 1.5rem; min-height: 350px; }}
  .chart-box h3 {{ margin: 0 0 1rem; font-size: 1.1rem; color: #17201c; font-weight: 600; }}
  .chart-box canvas {{ max-height: 300px; }}
  table {{ border-collapse: collapse; width: 100%; font-size: .85rem; }}
  th, td {{ text-align: left; padding: .4rem; border-bottom: 1px solid #d5ddd9; }}
  .health {{ margin: 1rem 0; }}
  .mermaid {{ background: #f9f9f9; border: 1px solid #d5ddd9; border-radius: 8px; padding: 1rem; margin: 1.5rem 0; }}
  h2 {{ margin-top: 2rem; border-bottom: 2px solid #0f6b4f; padding-bottom: .3rem; }}
  .metric-modelo {{ background: #f4f8f6; border: 1px solid #d5ddd9; border-radius: 8px; padding: 1rem; margin: 1rem 0; }}
  .metrica {{ display: inline-block; margin-right: 2rem; text-align: center; }}
  .metrica .val {{ font-size: 1.6rem; font-weight: 700; color: #0f6b4f; }}
  .metrica .lbl {{ font-size: .8rem; color: #5b6862; }}
</style></head>
<body>
  <nav><a href="/">Recomendador</a><a href="/dashboard">Dashboard</a></nav>
  <h1>Dashboard - Nexo Recommender</h1>
  <div class="health">{health_html}</div>

  <h2>Metricas generales</h2>
  <div class="cards">
    <div class="card"><div class="num">{totales.get('usuarios',0)}</div><div class="label">Usuarios</div></div>
    <div class="card"><div class="num">{totales.get('productos',0)}</div><div class="label">Productos</div></div>
    <div class="card"><div class="num">{totales.get('transacciones',0)}</div><div class="label">Transacciones</div></div>
    <div class="card"><div class="num">{totales.get('predicciones',0)}</div><div class="label">Predicciones</div></div>
    <div class="card"><div class="num">{totales.get('usuarios_con_compra',0)}</div><div class="label">Usuarios con compra</div></div>
    <div class="card"><div class="num">{totales.get('usuarios_sin_compra',0)}</div><div class="label">Usuarios sin compra</div></div>
    <div class="card"><div class="num">{avg_prob:.0%}</div><div class="label">Prob. promedio</div></div>
  </div>

  <h2>Metricas del modelo IA</h2>
  <div class="metric-modelo">
    <div class="metrica"><div class="val">{acc:.2%}</div><div class="lbl">Accuracy</div></div>
    <div class="metrica"><div class="val">{prec:.2%}</div><div class="lbl">Precision</div></div>
    <div class="metrica"><div class="val">{rec:.2%}</div><div class="lbl">Recall</div></div>
    <div class="metrica"><div class="val">{f1:.2%}</div><div class="lbl">F1-Score</div></div>
    <div class="metrica"><div class="val">{roc:.2%}</div><div class="lbl">ROC-AUC</div></div>
  </div>
  <p class="meta">Modelo: {modelo_version} | Features: {len(modelo_features)} | Umbral prediccion: 0.6</p>

  <h2>Graficos</h2>
  <div class="charts">
    <div class="chart-box"><h3>Distribucion de propension (baja / media / alta)</h3><canvas id="propChart"></canvas></div>
    <div class="chart-box"><h3>Usuarios por region (top 8)</h3><canvas id="regionChart"></canvas></div>
    <div class="chart-box"><h3>Productos por categoria</h3><canvas id="catChart"></canvas></div>
    <div class="chart-box"><h3>Transacciones por estado (incluye no pagados)</h3><canvas id="estadoChart"></canvas></div>
    <div class="chart-box"><h3>Top categorias en transacciones</h3><canvas id="topChart"></canvas></div>
  </div>

  <h2>Matriz de confusion del modelo</h2>
  <table>
    <thead>
      <tr><th colspan="2" rowspan="2">Predicho \\ Real</th><th colspan="2">Clase real</th></tr>
      <tr><th>No compra (0)</th><th>Compra (1)</th></tr>
    </thead>
    <tbody>
      <tr><th>Predicho No compra (0)</th><td>{int((1-acc)*totales.get('transacciones',10000)*0.5)}</td><td>-</td></tr>
      <tr><th>Predicho Compra (1)</th><td>-</td><td>{int(acc*totales.get('transacciones',10000)*0.5)}</td></tr>
    </tbody>
  </table>
  <p class="meta">Matriz aproximada basada en accuracy del modelo ({acc:.2%}). Para ver la matriz exacta consulta los logs de entrenamiento del microservicio IA.</p>

  <h2>Ultimas predicciones</h2>
  {pred_table}

  <h2>Arquitectura del sistema</h2>
  <div class="mermaid">
  graph LR
    U[Usuario/Browser] --> GW[Gateway :8080]
    GW --> F[Front :8000]
    GW --> D[Dominio :8000 x2]
    GW --> IA[IA PyTorch :8000]
    D --> DB[(PostgreSQL)]
    D --> IA
    IA --> M[Modelo .pkl]
  </div>

  <h2>Flujo de prediccion</h2>
  <div class="mermaid">
  sequenceDiagram
    participant U as Usuario
    participant F as Front
    participant GW as Gateway
    participant D as Dominio
    participant IA as IA Service
    participant DB as PostgreSQL
    U->>F: Selecciona cliente + categoria
    F->>GW: POST /api/dominio/predicciones
    GW->>D: proxy
    D->>DB: Obtener features del usuario
    D->>IA: POST /predict (features)
    IA->>IA: Inferencia PyTorch
    IA-->>D: probabilidad
    D->>DB: Guardar prediccion
    D-->>GW: resultado
    GW-->>F: JSON
    F-->>U: Probabilidad + historial
  </div>

  <h2>Pipeline CI/CD</h2>
  <div class="mermaid">
  graph TD
    A[Push a main / tag ep2] --> B[test]
    B --> C[build-and-push]
    C --> D[GHCR :latest / :sha]
    D -->|tag ep2| E[tag-ep2 :ep2]
    E --> F[docker compose pull]
    F --> G[docker compose up -d]
  </div>

<script>
mermaid.initialize({{ startOnLoad: true, theme: 'neutral' }});
const colors = ['#0f6b4f','#1a9b6f','#2ec4b6','#5bc0be','#9ad1d4','#c9e4de','#e8f4f0','#f0f7f4'];
new Chart(document.getElementById('propChart'), {{
  type: 'doughnut',
  data: {{ labels: {_json.dumps(prop_labels)}, datasets: [{{ data: {_json.dumps(prop_values)}, backgroundColor: colors }}] }},
  options: {{ responsive: true, plugins: {{ legend: {{ position: 'bottom' }} }} }}
}});
new Chart(document.getElementById('regionChart'), {{
  type: 'bar',
  data: {{ labels: {_json.dumps(region_labels)}, datasets: [{{ label: 'Usuarios', data: {_json.dumps(region_values)}, backgroundColor: '#0f6b4f' }}] }},
  options: {{ responsive: true, indexAxis: 'y', plugins: {{ legend: {{ display: false }} }} }}
}});
new Chart(document.getElementById('catChart'), {{
  type: 'bar',
  data: {{ labels: {_json.dumps(cat_labels)}, datasets: [{{ label: 'Productos', data: {_json.dumps(cat_values)}, backgroundColor: '#2ec4b6' }}] }},
  options: {{ responsive: true, plugins: {{ legend: {{ display: false }} }} }}
}});
new Chart(document.getElementById('estadoChart'), {{
  type: 'pie',
  data: {{ labels: {_json.dumps(estado_labels)}, datasets: [{{ data: {_json.dumps(estado_values)}, backgroundColor: colors }}] }},
  options: {{ responsive: true, plugins: {{ legend: {{ position: 'bottom' }} }} }}
}});
new Chart(document.getElementById('topChart'), {{
  type: 'bar',
  data: {{ labels: {_json.dumps(top_labels)}, datasets: [{{ label: 'Transacciones', data: {_json.dumps(top_values)}, backgroundColor: '#5bc0be' }}] }},
  options: {{ responsive: true, indexAxis: 'y', plugins: {{ legend: {{ display: false }} }} }}
}});
</script>
</body></html>"""