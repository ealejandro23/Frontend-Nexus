"""Front de Nexo (Caso 1 - Nexus Retail), hecho solo con FastAPI.
FastAPI arma la pagina en Python y llama al dominio a traves del gateway.
"""
import os
import re
import threading
import time
from html import escape
from urllib.parse import quote

import httpx
from fastapi import FastAPI, Form
from fastapi.responses import HTMLResponse, JSONResponse

GATEWAY_URL = os.getenv("GATEWAY_URL", "http://gateway:8080")
DOMINIO = f"{GATEWAY_URL}/api/dominio"
CAMPANAS = f"{GATEWAY_URL}/api/campanas"          # POST encola el texto del LLM (worker + RabbitMQ)
PREDICCIONES = f"{GATEWAY_URL}/api/predicciones"  # GET /{job_id} devuelve el texto cuando esta listo
MIN_PRODUCTOS, MAX_PRODUCTOS = 4, 8
RESERVA_TTL_S = 300                               # la lista de mas vendidos se refresca cada 5 min
LLM_ESPERA_MAX_S = int(os.getenv("LLM_ESPERA_MAX_S", "30"))
LLM_INTERVALO_S = 2
JOB_ID_VALIDO = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
TIMEOUT_S = float(os.getenv("TIMEOUT_S", "5"))
# Clave que exige el gateway en la cabecera X-API-Key (se lee del .env del servidor, nunca del repo).
API_KEY = os.getenv("API_KEY", "")
CATEGORIAS_POR_DEFECTO = ["Almacenamiento", "Audio", "Cables y adaptadores", "Componentes", "Gaming",
                          "Monitores", "Notebooks", "Perifericos", "Redes", "Smartphones"]
RESPUESTA_INESPERADA = "El servicio de dominio respondio en un formato inesperado. Avisa al equipo."

app = FastAPI(title="front", docs_url=None, redoc_url=None, openapi_url=None)


class ErrorServicio(Exception):
    def __init__(self, mensaje: str, status: int | None = None):
        super().__init__(mensaje)
        self.status = status


# Contadores para el dashboard (en memoria: se reinician si se reinicia el contenedor del front).
_estadisticas = {"entregadas": 0, "con_reserva": 0, "llm_con_texto": 0, "llm_degradado": 0}
_jobs_contados: set[str] = set()
_reserva = {"productos": [], "ts": 0.0}
_lock = threading.Lock()


def contar(clave: str, job_id: str | None = None) -> None:
    with _lock:
        if job_id is not None:
            if job_id in _jobs_contados:
                return
            _jobs_contados.add(job_id)
            if len(_jobs_contados) > 5000:
                _jobs_contados.clear()
        _estadisticas[clave] += 1


def llamar(metodo: str, url: str, **kwargs):
    inicio = time.perf_counter()
    try:
        headers = {**kwargs.pop("headers", {}), "X-API-Key": API_KEY} if API_KEY else kwargs.pop("headers", None)
        r = httpx.request(metodo, url, timeout=TIMEOUT_S, headers=headers, **kwargs)
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
    if r.status_code == 401:
        raise ErrorServicio("El gateway rechazo la clave de acceso (X-API-Key). Revisa API_KEY en el .env.", 401)
    if r.status_code == 429:
        raise ErrorServicio("Demasiadas peticiones seguidas. Espera unos segundos e intenta de nuevo.", 429)
    if r.status_code in (502, 503, 504):
        raise ErrorServicio(detalle or "Un servicio del sistema no esta disponible.", r.status_code)
    if r.is_error:
        raise ErrorServicio(f"Error {r.status_code}: {detalle or 'respuesta inesperada'}", r.status_code)
    return datos, ms


# ---------------------------------------------------------------------------
# Recomendaciones: entre 4 y 8 productos, con lista de reserva (mas vendidos)
# ---------------------------------------------------------------------------

def texto_valido(valor) -> str | None:
    """Texto limpio, o None si viene vacio o como "NaN"/"None" (nulos de los CSV originales)."""
    texto = str(valor).strip() if valor is not None else ""
    return None if texto.lower() in ("", "nan", "none", "null") else texto


def productos_de(datos) -> list[dict]:
    """Toma la lista "recomendaciones" del dominio y descarta lo que no tenga el formato esperado."""
    items = datos.get("recomendaciones") if isinstance(datos, dict) else None
    if not isinstance(items, list):
        return []
    return [
        {"sku": str(i["sku"]),
         "nombre": texto_valido(i.get("nombre")) or f"Producto {i['sku']}",
         "categoria": (texto_valido(i.get("categoria")) or "-").capitalize()}
        for i in items if isinstance(i, dict) and i.get("sku")
    ]


def lista_reserva() -> list[dict]:
    """Mas vendidos: la misma ruta del dominio sin usuario solo ordena por popularidad.
    Se guarda en memoria para poder usarla aunque el dominio falle despues."""
    if _reserva["productos"] and time.monotonic() - _reserva["ts"] < RESERVA_TTL_S:
        return _reserva["productos"]
    try:
        datos, _ = llamar("GET", f"{DOMINIO}/api/recomendaciones", params={"limite": MAX_PRODUCTOS})
        productos = productos_de(datos)
        if productos:
            _reserva.update(productos=productos, ts=time.monotonic())
    except ErrorServicio:
        pass
    return _reserva["productos"]


def recomendar(usuario_id: str) -> tuple[list[dict], bool, str | None]:
    """Devuelve (productos, uso_reserva, motivo). Usa la lista de reserva si el recomendador personalizado
    falla, si el cliente no tiene historial para personalizar o si entrega menos de 4 productos."""
    reserva = lista_reserva()
    motivo = None
    try:
        datos, _ = llamar("GET", f"{DOMINIO}/api/recomendaciones",
                          params={"usuario_id": usuario_id, "limite": MAX_PRODUCTOS})
        personales = productos_de(datos)
        if isinstance(datos, dict) and datos.get("origen") == "regla":
            # RN2: sin consentimiento el dominio no usa el historial; la lista son los mas vendidos.
            productos = (personales or reserva)[:MAX_PRODUCTOS]
            return productos, True, (str(datos.get("motivo") or "Sin personalizacion") + ": se muestran los mas vendidos.")
    except ErrorServicio as e:
        personales, motivo = [], f"El recomendador personalizado no respondio: {e}"
    if personales and reserva and [p["sku"] for p in personales] == [p["sku"] for p in reserva]:
        personales, motivo = [], "El cliente no tiene historial suficiente para personalizar."
    if len(personales) >= MIN_PRODUCTOS:
        return personales[:MAX_PRODUCTOS], False, None
    vistos = {p["sku"] for p in personales}
    productos = (personales + [p for p in reserva if p["sku"] not in vistos])[:MAX_PRODUCTOS]
    return productos, True, motivo or "Habia pocas recomendaciones personalizadas; se completo con los mas vendidos."


def _tono(texto: str) -> int:
    """Tono estable (0-359) por categoria, para la miniatura de cada producto."""
    return sum(ord(c) * (i + 1) for i, c in enumerate(texto.lower())) % 360


def recomendaciones_html(usuario_id: str) -> str:
    productos, reserva, motivo = recomendar(usuario_id)
    if len(productos) < MIN_PRODUCTOS:
        return ('<section class="bloque"><p class="error">No se pudieron cargar recomendaciones de productos '
                'en este momento. Intenta de nuevo en unos segundos.</p></section>')
    contar("entregadas")
    if reserva:
        contar("con_reserva")
    if reserva:
        aviso = (f'<p class="aviso"><strong>Lista de reserva (mas vendidos).</strong> {escape(motivo or "")}</p>')
        porque = "Entre los más vendidos de la tienda"
    else:
        aviso = '<p class="nota">Personalizadas según su historial de compras y navegación.</p>'
        porque = "Comprado junto a productos de su historial"
    tarjetas = "".join(
        f'<li class="producto" style="--tono:{_tono(p["categoria"])}">'
        f'<span class="miniatura" aria-hidden="true">{escape(p["categoria"][:2])}</span>'
        f'<span class="nombre">{escape(p["nombre"])}</span>'
        f'<span class="categoria">{escape(p["categoria"])}</span>'
        f'<span class="motivo">{porque}</span>'
        f'<span class="sku">{escape(p["sku"])}</span></li>'
        for p in productos
    )
    return (f'<section class="bloque"><div class="bloque-titulo"><h2>Productos recomendados ({len(productos)})</h2>'
            f'</div>{aviso}<ul class="productos">{tarjetas}</ul></section>')


# ---------------------------------------------------------------------------
# Texto del LLM: POST /api/campanas/{usuario} -> job_id; el navegador consulta /llm/{job_id}
# (el front reenvia a GET /api/predicciones/{job_id} con la clave: la clave nunca llega al navegador)
# ---------------------------------------------------------------------------

def iniciar_campana(usuario_id: str) -> str | None:
    try:
        datos, _ = llamar("POST", f"{CAMPANAS}/{quote(usuario_id, safe='')}", json={})
    except ErrorServicio:
        return None
    job_id = datos.get("job_id") if isinstance(datos, dict) else None
    return job_id if isinstance(job_id, str) and JOB_ID_VALIDO.match(job_id) else None


ICONO_CORREO = ('<svg class="icono" viewBox="0 0 24 24" aria-hidden="true"><path d="M4 6h16v12H4z" fill="none" '
                'stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/><path d="m4 7 8 6 8-6" fill="none" '
                'stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/></svg>')


def llm_html(usuario_id: str) -> str:
    cabecera = (f'<div class="bloque-titulo"><h2>Mensaje personalizado</h2>'
                f'<span class="etiqueta">Redactado por IA (NVIDIA) &middot; llega por la cola</span></div>')
    job_id = iniciar_campana(usuario_id)
    if not job_id:
        contar("llm_degradado")
        return (f'<section class="bloque">{cabecera}<div class="correo aviso"><strong>Modo degradado:</strong> '
                'el servicio de mensajes personalizados no esta disponible. Las recomendaciones siguen funcionando.'
                '</div></section>')
    intentos = max(1, LLM_ESPERA_MAX_S // LLM_INTERVALO_S)
    # job_id ya paso por JOB_ID_VALIDO (solo letras, numeros, _ y -): es seguro dentro del script.
    return f"""<section class="bloque">{cabecera}
<div id="llm" class="correo esperando" aria-live="polite">
  <div class="correo-cabecera">{ICONO_CORREO}<span>Para: cliente seudonimizado</span></div>
  <p class="correo-asunto">Generando mensaje personalizado...</p>
  <span class="linea-carga"></span><span class="linea-carga corta"></span>
</div></section>
<script>
(function () {{
  const caja = document.getElementById("llm");
  const job = "{job_id}";
  let intento = 0;
  function mostrar(estado, titulo, texto) {{
    caja.className = "correo " + estado;
    caja.querySelectorAll(".linea-carga").forEach(e => e.remove());
    const asunto = caja.querySelector(".correo-asunto");
    asunto.textContent = titulo;
    const cuerpo = document.createElement("p");
    cuerpo.className = "correo-cuerpo";
    cuerpo.textContent = texto;
    caja.appendChild(cuerpo);
  }}
  function consultar() {{
    intento += 1;
    fetch("/llm/" + encodeURIComponent(job) + "?intento=" + intento)
      .then(r => r.json())
      .then(d => {{
        if (d.estado === "pendiente" && intento < {intentos}) {{ setTimeout(consultar, {LLM_INTERVALO_S * 1000}); return; }}
        if (d.estado === "listo") {{ mostrar("ok", d.asunto || "Mensaje para el cliente", d.mensaje || ""); return; }}
        mostrar("aviso", "Modo degradado", d.motivo || "El mensaje personalizado no llego a tiempo.");
      }})
      .catch(() => mostrar("aviso", "Modo degradado", "No se pudo consultar el mensaje personalizado."));
  }}
  setTimeout(consultar, {LLM_INTERVALO_S * 1000});
}})();
</script>"""


ESTILOS = """
@import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600;700&family=IBM+Plex+Mono:wght@500&display=swap');
:root {
  --papel: #f4f3ef; --panel: #ffffff; --tinta: #1a201d; --tinta-2: #4f5b55; --tinta-3: #6d7872;
  --linea: #e1e3dd; --marca: #0f6b4f; --marca-osc: #0b523c; --marca-suave: #e5f1ec;
  --aviso: #fff4dc; --aviso-tinta: #6a4a00; --error: #fbe9e5; --error-tinta: #9c2f1b;
  --radio: 10px; --sombra: 0 1px 2px rgba(26,32,29,.06), 0 6px 18px rgba(26,32,29,.06);
}
* { box-sizing: border-box; }
html { -webkit-text-size-adjust: 100%; }
body { margin: 0; background: var(--papel); color: var(--tinta); font: 15px/1.55 'IBM Plex Sans', system-ui, sans-serif; }
::selection { background: var(--marca); color: #fff; }
a { color: var(--marca); text-underline-offset: 3px; }
:focus-visible { outline: 2px solid var(--marca); outline-offset: 2px; border-radius: 4px; }
.barra { background: var(--panel); border-bottom: 1px solid var(--linea); }
.barra-interior { max-width: 1180px; margin: 0 auto; padding: .85rem 1.25rem; display: flex; align-items: center; gap: 2rem; }
.marca { font-weight: 700; font-size: 1.15rem; letter-spacing: -.01em; color: var(--tinta); text-decoration: none; }
.marca span { color: var(--marca); }
.barra nav { display: flex; gap: .25rem; }
.barra nav a { color: var(--tinta-2); text-decoration: none; font-weight: 500; padding: .4rem .75rem; border-radius: 6px; }
.barra nav a:hover { background: var(--papel); color: var(--tinta); }
.barra nav a[aria-current="page"] { background: var(--marca-suave); color: var(--marca-osc); }
.contenido { max-width: 1180px; margin: 0 auto; padding: 1.75rem 1.25rem 3rem; display: grid;
  grid-template-columns: 300px minmax(0, 1fr); gap: 1.75rem; align-items: start; }
.lateral { position: sticky; top: 1.25rem; background: var(--panel); border: 1px solid var(--linea);
  border-radius: var(--radio); box-shadow: var(--sombra); padding: 1.25rem; }
h1 { font-size: 1.3rem; line-height: 1.25; margin: 0 0 .35rem; letter-spacing: -.015em; }
.lateral > p { color: var(--tinta-2); margin: 0 0 1.25rem; font-size: .92rem; }
form { display: grid; gap: 1rem; }
label { display: grid; gap: .35rem; font-weight: 600; font-size: .85rem; color: var(--tinta-2); }
input, select { font: inherit; color: var(--tinta); background: var(--panel); border: 1px solid #cfd4cc;
  border-radius: 8px; padding: .6rem .7rem; width: 100%; }
input:focus, select:focus { outline: none; border-color: var(--marca); box-shadow: 0 0 0 3px var(--marca-suave); }
input::placeholder { color: var(--tinta-3); }
.ayuda { font-weight: 400; color: var(--tinta-3); font-size: .8rem; }
button { font: inherit; font-weight: 600; background: var(--marca); color: #fff; border: 0; border-radius: 8px;
  padding: .7rem 1rem; cursor: pointer; transition: background .15s ease-out; }
button:hover { background: var(--marca-osc); }
button:disabled { opacity: .6; cursor: progress; }
.principal { display: grid; gap: 1.25rem; min-width: 0; }
.bloque { background: var(--panel); border: 1px solid var(--linea); border-radius: var(--radio);
  box-shadow: var(--sombra); padding: 1.25rem 1.35rem; }
.bloque-titulo { display: flex; align-items: baseline; justify-content: space-between; gap: 1rem; flex-wrap: wrap; margin-bottom: .75rem; }
h2 { font-size: 1.02rem; margin: 0; letter-spacing: -.005em; }
.etiqueta { font-size: .8rem; color: var(--tinta-3); }
.nota { color: var(--tinta-2); font-size: .9rem; margin: 0 0 1rem; }
.aviso { background: var(--aviso); color: var(--aviso-tinta); padding: .7rem .85rem; border-radius: 8px; margin: 0 0 1rem; font-size: .92rem; }
.error { background: var(--error); color: var(--error-tinta); padding: .7rem .85rem; border-radius: 8px; margin: 0; font-size: .92rem; }
.vacio { padding: 2.5rem 1.5rem; text-align: left; }
.vacio h2 { font-size: 1.15rem; margin-bottom: .5rem; }
.vacio p { color: var(--tinta-2); max-width: 60ch; margin: 0 0 .5rem; }
.resumen { display: grid; grid-template-columns: auto minmax(0, 1fr); gap: .25rem 1.5rem; align-items: center; }
.prob { font-size: 2.6rem; font-weight: 700; letter-spacing: -.03em; line-height: 1; font-variant-numeric: tabular-nums; margin: 0; color: var(--marca-osc); }
.veredicto { font-weight: 600; margin: 0; }
.barra-prob { height: 8px; border-radius: 99px; background: var(--papel); overflow: hidden; grid-column: 1 / -1; margin-top: .9rem; }
.barra-prob span { display: block; height: 100%; background: var(--marca); border-radius: inherit; }
.meta { color: var(--tinta-3); font-size: .84rem; margin: .75rem 0 0; grid-column: 1 / -1; }
.chip { display: inline-block; font-size: .78rem; font-weight: 600; padding: .12rem .5rem; border-radius: 99px;
  background: var(--marca-suave); color: var(--marca-osc); }
.chip.reserva { background: var(--aviso); color: var(--aviso-tinta); }
.productos { list-style: none; padding: 0; margin: 0; display: grid; grid-template-columns: repeat(auto-fill, minmax(min(190px, 100%), 1fr)); gap: .85rem; }
.producto { border: 1px solid var(--linea); border-radius: 9px; padding: .8rem; display: grid; gap: .2rem; align-content: start;
  transition: border-color .15s ease-out, box-shadow .15s ease-out; }
.producto:hover { border-color: #c9cfc6; box-shadow: var(--sombra); }
.miniatura { height: 64px; border-radius: 7px; display: grid; place-items: center; margin-bottom: .45rem;
  background: hsl(var(--tono) 45% 93%); color: hsl(var(--tono) 40% 32%); font-weight: 700; font-size: 1.05rem;
  text-transform: uppercase; letter-spacing: .04em; }
.producto .nombre { font-weight: 600; line-height: 1.3; }
.producto .categoria { color: var(--tinta-2); font-size: .85rem; }
.producto .motivo { color: var(--marca-osc); font-size: .8rem; }
.producto .sku { font-family: 'IBM Plex Mono', ui-monospace, monospace; font-size: .75rem; color: var(--tinta-3); }
.correo { border: 1px solid var(--linea); border-radius: 9px; padding: 1rem 1.1rem; }
.correo-cabecera { display: flex; align-items: center; gap: .45rem; color: var(--tinta-3); font-size: .82rem;
  padding-bottom: .6rem; border-bottom: 1px solid var(--linea); margin-bottom: .7rem; }
.icono { width: 18px; height: 18px; }
.correo-asunto { font-weight: 600; margin: 0 0 .35rem; }
.correo-cuerpo { margin: 0; white-space: pre-line; color: var(--tinta-2); max-width: 70ch; }
.correo.ok { background: #fbfdfc; }
.correo.aviso { background: var(--aviso); color: var(--aviso-tinta); border-color: #f0dfb3; }
.correo.aviso .correo-cuerpo { color: var(--aviso-tinta); }
.linea-carga { display: block; height: 10px; border-radius: 99px; margin-top: .55rem; width: 92%;
  background: linear-gradient(90deg, var(--papel) 0%, #e9ebe6 50%, var(--papel) 100%); background-size: 200% 100%;
  animation: carga 1.2s ease-out infinite; }
.linea-carga.corta { width: 60%; }
@keyframes carga { from { background-position: 100% 0; } to { background-position: -100% 0; } }
table { border-collapse: collapse; width: 100%; font-size: .9rem; font-variant-numeric: tabular-nums; }
th { text-align: left; font-weight: 600; color: var(--tinta-3); font-size: .78rem; text-transform: uppercase; letter-spacing: .03em; }
th, td { padding: .55rem .5rem; border-bottom: 1px solid var(--linea); }
tbody tr:last-child td { border-bottom: 0; }
.tabla { overflow-x: auto; }
@media (max-width: 860px) { .contenido { grid-template-columns: minmax(0, 1fr); padding: 1rem 1rem 2rem; gap: 1rem; } .lateral { position: static; } .barra-interior { gap: 1rem; } .prob { font-size: 2.1rem; } }
@media (prefers-reduced-motion: reduce) { .linea-carga { animation: none; } * { transition: none !important; } }
"""


def barra_superior(activa: str) -> str:
    def enlace(href, texto):
        actual = ' aria-current="page"' if href == activa else ""
        return f'<a href="{href}"{actual}>{texto}</a>'
    return (f'<header class="barra"><div class="barra-interior"><a class="marca" href="/">Nexo<span>.</span></a>'
            f'<nav>{enlace("/", "Recomendador")}{enlace("/dashboard", "Dashboard")}</nav></div></header>')


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

    # Buscador con sugerencias: con miles de clientes, un <select> no se puede recorrer.
    sugerencias = "".join(
        f'<option value="{escape(str(u["usuario_id"]))}">'
        f'{escape(str(u["usuario_id"]))}{" (" + escape(str(u["region"]).title()) + ")" if u.get("region") else ""}</option>'
        for u in usuarios
    )
    opciones_categoria = "".join(
        f'<option value="{escape(str(c))}"{" selected" if str(c) == categoria_sel else ""}>{escape(str(c).capitalize())}</option>'
        for c in categorias
    )
    avisos_html = "".join(f'<p class="error">{escape(a)}</p>' for a in avisos)
    total = f"{len(usuarios):,}".replace(",", ".") if usuarios else ""
    ayuda = f'<span class="ayuda">Escribe el código; hay {total} clientes.</span>' if usuarios else ""
    if not resultado:
        resultado = ('<section class="bloque vacio"><h2>Elige un cliente para ver su recomendación</h2>'
                     '<p>El modelo calcula la probabilidad de que compre en la categoría elegida y el sistema arma '
                     'entre 4 y 8 productos para mostrarle. En paralelo, la IA redacta el mensaje de la campaña.</p>'
                     '<p>Prueba con U100000 o U103814.</p></section>')

    return f"""<!doctype html>
<html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Nexo - Recomendador de compras</title>
<style>{ESTILOS}</style></head>
<body>
{barra_superior("/")}
<main class="contenido">
  <aside class="lateral">
    <h1>Recomendador de compras</h1>
    <p>Qué productos mostrarle a cada cliente y qué mensaje enviarle.</p>
    {avisos_html}
    <form method="post" action="/predecir" onsubmit="this.querySelector('button').disabled=true;this.querySelector('button').textContent='Calculando...'">
      <label>Cliente
        <input name="usuario_id" list="clientes" required autocomplete="off" spellcheck="false"
               placeholder="Por ejemplo, U103808" value="{escape(usuario_sel)}">
        {ayuda}
      </label>
      <datalist id="clientes">{sugerencias}</datalist>
      <label>Categoría
        <select name="categoria">{opciones_categoria}</select>
      </label>
      <button type="submit">Calcular probabilidad</button>
    </form>
  </aside>
  <div class="principal">
    {resultado}
  </div>
</main>
</body></html>"""


def historial_html(usuario_id: str) -> str:
    def envolver(contenido: str) -> str:
        return f'<section class="bloque"><div class="bloque-titulo"><h2>Historial de compras</h2></div>{contenido}</section>'
    try:
        filas, _ = llamar("GET", f"{DOMINIO}/usuarios/{quote(usuario_id, safe='')}/historial")
    except ErrorServicio as e:
        return envolver(f'<p class="error">No se pudo cargar el historial. {escape(str(e))}</p>')
    if not filas:
        return envolver('<p class="nota">Este cliente todavia no tiene compras registradas.</p>')
    if not isinstance(filas, list) or not all(isinstance(f, dict) for f in filas):
        return envolver(f'<p class="error">No se pudo cargar el historial. {RESPUESTA_INESPERADA}</p>')
    columnas = ["producto", "categoria", "fecha", "cantidad", "total", "estado"]
    cabecera = "".join(f"<th>{escape(str(c))}</th>" for c in columnas)
    cuerpo = ""
    for f in filas:
        celdas = ""
        for c in columnas:
            celdas += f"<td>{escape(str(f.get(c, '-')))}</td>"
        cuerpo += f"<tr>{celdas}</tr>"
    return envolver(f'<div class="tabla"><table><thead><tr>{cabecera}</tr></thead><tbody>{cuerpo}</tbody></table></div>')


@app.get("/health")
def health():
    return {"status": "ok", "servicio": "front"}


@app.get("/", response_class=HTMLResponse)
def inicio():
    return pagina()


@app.get("/llm/{job_id}")
def estado_llm(job_id: str, intento: int = 1):
    """El navegador consulta aqui; el front pregunta al gateway con la clave X-API-Key."""
    if not JOB_ID_VALIDO.match(job_id):
        return JSONResponse(status_code=422, content={"detail": "job_id invalido"})
    ultimo = intento >= max(1, LLM_ESPERA_MAX_S // LLM_INTERVALO_S)
    try:
        datos, _ = llamar("GET", f"{PREDICCIONES}/{job_id}")
    except ErrorServicio as e:
        if e.status == 404 and not ultimo:
            return {"estado": "pendiente"}
        contar("llm_degradado", job_id)
        motivo = "El mensaje personalizado no llego a tiempo." if e.status == 404 else str(e)
        return {"estado": "degradado", "motivo": motivo}
    if not isinstance(datos, dict) or not datos.get("mensaje"):
        contar("llm_degradado", job_id)
        return {"estado": "degradado", "motivo": RESPUESTA_INESPERADA}
    if datos.get("degradado"):
        contar("llm_degradado", job_id)
        return {"estado": "degradado",
                "motivo": "El LLM no respondio; se muestra un mensaje generico. " + str(datos.get("mensaje"))[:500]}
    contar("llm_con_texto", job_id)
    return {"estado": "listo", "asunto": str(datos.get("asunto") or ""), "mensaje": str(datos["mensaje"])[:2000]}


@app.post("/predecir", response_class=HTMLResponse)
def predecir(usuario_id: str = Form(...), categoria: str = Form(...)):
    usuario_id = usuario_id.strip()
    try:
        res, ms = llamar("POST", f"{DOMINIO}/predicciones", json={"usuario_id": usuario_id, "categoria": categoria})
        prob = float(res["probabilidad_compra"])
        veredicto = "Conviene recomendarle esta categoría." if res.get("prediccion") == 1 \
            else "No conviene recomendarle esta categoría por ahora."
        origen = res.get("origen")
        chip = ('<span class="chip">SageMaker</span>' if origen == "sagemaker" else
                '<span class="chip reserva">Reserva: modelo local</span>' if origen == "reserva" else "")
        detalles = [f"Modelo {res.get('version_modelo', 'desconocido')}",
                    f"prediccion n. {res.get('prediccion_id', '?')}", f"{ms:.0f} ms"]
        if res.get("instancia"):
            detalles.append(f"replica {res['instancia']}")
        ancho = max(0.0, min(1.0, prob)) * 100
        resultado = (f'<section class="bloque"><div class="bloque-titulo"><h2>Cliente {escape(usuario_id)} '
                     f'&middot; {escape(categoria.capitalize())}</h2>{chip}</div>'
                     f'<div class="resumen"><p class="prob">{prob:.0%}</p><div><p class="veredicto">{veredicto}</p>'
                     f'<p class="nota" style="margin:0">Probabilidad de que compre en esta categoría.</p></div>'
                     f'<div class="barra-prob"><span style="width:{ancho:.0f}%"></span></div>'
                     f'<p class="meta">{escape(", ".join(detalles))}</p></div></section>')
    except ErrorServicio as e:
        resultado = f'<section class="bloque"><p class="error">{escape(str(e))}</p></section>'
    except (KeyError, TypeError, ValueError):
        resultado = f'<section class="bloque"><p class="error">{RESPUESTA_INESPERADA}</p></section>'
    resultado += recomendaciones_html(usuario_id)
    resultado += llm_html(usuario_id)
    resultado += historial_html(usuario_id)
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

    with _lock:
        est = dict(_estadisticas)
    pct_reserva = f"{est['con_reserva'] / est['entregadas']:.0%}" if est["entregadas"] else "-"

    return f"""<!doctype html>
<html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Nexo - Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.min.js"></script>
<style>
  @import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600;700&display=swap');
  body {{ font-family: 'IBM Plex Sans', system-ui, sans-serif; margin: 0; background: #f4f3ef; color: #1a201d; }}
  .tablero {{ max-width: 1200px; margin: 0 auto; padding: 1.75rem 1.25rem 3rem; }}
  .barra {{ background: #fff; border-bottom: 1px solid #e1e3dd; }}
  .barra-interior {{ max-width: 1200px; margin: 0 auto; padding: .85rem 1.25rem; display: flex; align-items: center; gap: 2rem; }}
  .marca {{ font-weight: 700; font-size: 1.15rem; color: #1a201d; text-decoration: none; }}
  .marca span {{ color: #0f6b4f; }}
  .barra nav {{ display: flex; gap: .25rem; margin: 0; }}
  .barra nav a {{ color: #4f5b55; text-decoration: none; font-weight: 500; padding: .4rem .75rem; border-radius: 6px; margin: 0; }}
  .barra nav a[aria-current="page"] {{ background: #e5f1ec; color: #0b523c; }}
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
{barra_superior("/dashboard")}
<div class="tablero">
  <h1>Dashboard - Nexo Recommender</h1>
  <div class="health">{health_html}</div>

  <h2>Recomendaciones entregadas</h2>
  <div class="cards">
    <div class="card"><div class="num">{est["entregadas"]}</div><div class="label">Recomendaciones entregadas</div></div>
    <div class="card"><div class="num">{est["con_reserva"]}</div><div class="label">Con lista de reserva (mas vendidos)</div></div>
    <div class="card"><div class="num">{pct_reserva}</div><div class="label">Proporcion con lista de reserva</div></div>
    <div class="card"><div class="num">{est["llm_con_texto"]}</div><div class="label">Mensajes del LLM entregados</div></div>
    <div class="card"><div class="num">{est["llm_degradado"]}</div><div class="label">Respuestas en modo degradado</div></div>
  </div>
  <p class="meta">Contadores del front desde su ultimo reinicio.</p>

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
</div>
</body></html>"""