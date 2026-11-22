"""Front de Nexo (Caso 1 - Nexus Retail), hecho solo con FastAPI.
FastAPI arma la pagina en Python y llama al dominio a traves del gateway.
"""
import json
import os
import re
import unicodedata
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


def pesos(valor) -> str:
    """56804.0 -> "$ 56.804" (pesos chilenos, sin decimales)."""
    try:
        numero = float(valor)
    except (TypeError, ValueError):
        return "-"
    if numero != numero:
        return "-"
    return "$ " + f"{numero:,.0f}".replace(",", ".")


def fecha_corta(valor) -> str:
    """"2025-06-03T00:00:00" -> "03-06-2025"; vacio o None -> "Sin fecha"."""
    texto = texto_valido(valor)
    if not texto:
        return "Sin fecha"
    partes = texto[:10].split("-")
    return "-".join(reversed(partes)) if len(partes) == 3 else texto


def productos_de(datos) -> list[dict]:
    """Toma la lista "recomendaciones" del dominio y descarta lo que no tenga el formato esperado."""
    items = datos.get("recomendaciones") if isinstance(datos, dict) else None
    if not isinstance(items, list):
        return []
    return [
        {"sku": str(i["sku"]),
         "nombre": texto_valido(i.get("nombre")) or f"Producto {i['sku']}",
         "categoria": (texto_valido(i.get("categoria")) or "-").capitalize(),
         "marca": texto_valido(i.get("marca")),
         "precio": i.get("precio") if isinstance(i.get("precio"), (int, float)) else None}
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


def recomendaciones_html(usuario_id: str, categoria: str = "") -> str:
    productos, reserva, motivo = recomendar(usuario_id)
    if len(productos) < MIN_PRODUCTOS:
        return ('<section class="bloque"><p class="error">No se pudieron cargar recomendaciones de productos '
                'en este momento. Intenta de nuevo en unos segundos.</p></section>')
    contar("entregadas")
    if reserva:
        contar("con_reserva")
    elegida = categoria.strip().lower()
    productos = sorted(productos, key=lambda p: p["categoria"].lower() != elegida)  # la categoria consultada primero
    if reserva:
        aviso = f'<p class="aviso"><strong>Lista de reserva (mas vendidos).</strong> {escape(motivo or "")}</p>'
    else:
        aviso = ('<p class="nota">Personalizadas según su historial de compras y navegación. '
                 'Primero los de la categoría consultada.</p>')

    def motivo_de(p) -> str:
        if reserva:
            return "Entre los más vendidos"
        if p["categoria"].lower() == elegida:
            return "De la categoría consultada"
        return "Comprado junto a productos de su historial"

    tarjetas = ""
    for p in productos:
        detalle = " &middot; ".join(x for x in (escape(p["marca"] or ""), pesos(p["precio"]) if p["precio"] else "") if x)
        destacado = " destacado" if not reserva and p["categoria"].lower() == elegida else ""
        tarjetas += (f'<li class="producto{destacado}" style="--tono:{_tono(p["categoria"])}">'
                     f'<span class="miniatura" aria-hidden="true">{escape(p["categoria"][:2])}</span>'
                     f'<span class="nombre">{escape(p["nombre"])}</span>'
                     f'<span class="categoria">{escape(p["categoria"])}</span>'
                     + (f'<span class="precio">{detalle}</span>' if detalle else "") +
                     f'<span class="motivo">{motivo_de(p)}</span>'
                     f'<span class="sku">{escape(p["sku"])}</span></li>')
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
.rotulo-campo { display: block; font-weight: 600; font-size: .85rem; color: var(--tinta-2); margin-bottom: .35rem; }
.perfiles { display: grid; gap: .45rem; }
button.perfil { display: grid; gap: .1rem; text-align: left; background: var(--panel); color: var(--tinta); border: 1px solid var(--linea); padding: .6rem .75rem; font-weight: 400; }
button.perfil:hover { background: var(--marca-suave); border-color: #b9d8cb; }
.perfil-titulo { font-weight: 600; }
.perfil-caso { color: var(--marca-osc); font-size: .8rem; }
.perfil-detalle { color: var(--tinta-3); font-size: .78rem; }
.elegido { margin: 0; padding: .6rem .75rem; border: 1px dashed #cfd4cc; border-radius: 8px; color: var(--tinta-3); font-size: .88rem; }
.elegido.listo { border-style: solid; border-color: #b9d8cb; background: #fbfdfc; color: var(--tinta); display: grid; gap: .15rem; }
.codigo { font-family: 'IBM Plex Mono', ui-monospace, monospace; font-size: .76rem; color: var(--tinta-3); }
.filtros { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: .75rem; margin-bottom: .9rem; }
.tabla-clientes { max-height: 360px; overflow: auto; border: 1px solid var(--linea); border-radius: 8px; }
.tabla-clientes thead th { position: sticky; top: 0; background: var(--panel); z-index: 1; }
.tabla-clientes tbody tr { cursor: pointer; }
.tabla-clientes tbody tr:hover { background: var(--papel); }
.tabla-clientes tbody tr.activo { background: var(--marca-suave); }
.tabla-clientes tbody tr:focus-visible { outline: 2px solid var(--marca); outline-offset: -2px; }
details.cambiar > summary { cursor: pointer; font-weight: 600; color: var(--marca-osc); padding: .2rem 0; }
details.cambiar[open] > summary { margin-bottom: .75rem; }
.fila-campo { display: flex; gap: .5rem; }
.fila-campo input { flex: 1; min-width: 0; }
button.secundario { background: var(--panel); color: var(--marca-osc); border: 1px solid #cfd4cc; padding: .6rem .8rem; font-weight: 600; }
button.secundario:hover { background: var(--marca-suave); }
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
.producto .precio { font-weight: 600; font-size: .9rem; font-variant-numeric: tabular-nums; }
.producto.destacado { border-color: #b9d8cb; background: #fbfdfc; }
.ficha { display: grid; grid-template-columns: repeat(auto-fit, minmax(130px, 1fr)); gap: .75rem 1.25rem; margin: 0 0 1.1rem; padding: 0 0 1rem; border-bottom: 1px solid var(--linea); }
.ficha dt { color: var(--tinta-3); font-size: .76rem; text-transform: uppercase; letter-spacing: .03em; }
.ficha dd { margin: .15rem 0 0; font-weight: 600; font-variant-numeric: tabular-nums; }
td.num, th.num { text-align: right; }
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


def _sin_tildes(texto: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", texto) if unicodedata.category(c) != "Mn")


# Los datos traen la misma region escrita de varias formas ("Bio-Bio", "Biobío", "Rm", "V Region"...):
# se unifican con el nombre oficial de la region de Chile.
REGIONES_OFICIALES = {
    "biobio": "Biobío", "araucania": "La Araucanía", "laaraucania": "La Araucanía", "maule": "Maule",
    "delmaule": "Maule", "ohiggins": "O'Higgins", "libertadorbohiggins": "O'Higgins",
    "metropolitana": "Metropolitana", "rmetropolitana": "Metropolitana", "regionmetropolitana": "Metropolitana",
    "rm": "Metropolitana", "vregion": "Valparaíso", "valparaiso": "Valparaíso", "tarapaca": "Tarapacá",
    "antofagasta": "Antofagasta", "coquimbo": "Coquimbo", "loslagos": "Los Lagos",
}


def region_oficial(bruto: str) -> str:
    compacto = re.sub(r"[^a-z]", "", _sin_tildes(bruto).lower())
    return REGIONES_OFICIALES.get(compacto, bruto.strip().title())


def agrupar_regiones(usuarios: list[dict], usuario_sel: str = "") -> tuple[list, dict, str]:
    """[(clave, (region oficial, [ids]))] ordenado, {id: region} y la clave de la region del cliente elegido."""
    grupos: dict[str, list] = {}
    for u in usuarios:
        etiqueta = region_oficial(texto_valido(u.get("region")) or "Sin región")
        grupos.setdefault(etiqueta, []).append(str(u["usuario_id"]))
    regiones, region_de = [], {}
    for etiqueta, ids in grupos.items():
        clave = re.sub(r"[^a-z]", "", _sin_tildes(etiqueta).lower())
        regiones.append((clave, (etiqueta, sorted(ids))))
        for i in ids:
            region_de[i] = etiqueta
    regiones.sort(key=lambda r: r[1][0])
    region_sel = next((c for c, (_, ids) in regiones if usuario_sel in ids), "")
    return regiones, region_de, region_sel


_clientes_cache = {"datos": [], "ts": 0.0}


def clientes() -> list[dict]:
    """Clientes con datos reales para elegirlos sin depender del código (en memoria 5 minutos)."""
    if _clientes_cache["datos"] and time.monotonic() - _clientes_cache["ts"] < 300:
        return _clientes_cache["datos"]
    datos, _ = llamar("GET", f"{DOMINIO}/usuarios")
    lista = []
    for u in datos if isinstance(datos, list) else []:
        if not isinstance(u, dict) or "usuario_id" not in u:
            continue
        lista.append({
            "id": str(u["usuario_id"]),
            "region": region_oficial(texto_valido(u.get("region")) or "Sin región"),
            "comuna": texto_valido(u.get("comuna")) or "",
            "edad": u.get("edad") if isinstance(u.get("edad"), int) else None,
            "pedidos": int(u.get("pedidos") or 0) if isinstance(u.get("pedidos"), (int, float)) else 0,
            "total": float(u.get("total_gastado") or 0) if isinstance(u.get("total_gastado"), (int, float)) else 0.0,
            "marketing": bool(u.get("acepta_marketing")),
        })
    if lista:
        _clientes_cache.update(datos=lista, ts=time.monotonic())
    return lista


def describir(c: dict) -> str:
    """"Concepción, Biobío · 47 años · 3 pedidos"."""
    lugar = ", ".join(x for x in (c.get("comuna"), c.get("region")) if x)
    partes = [lugar or "Sin ubicación"]
    if c.get("edad"):
        partes.append(f'{c["edad"]} años')
    partes.append(f'{c["pedidos"]} pedido' + ("" if c["pedidos"] == 1 else "s"))
    return " · ".join(partes)


def perfiles_demo(lista: list[dict]) -> list[tuple[str, str, dict]]:
    """Clientes reales representativos de cada caso que se muestra en la demo."""
    con = [c for c in lista if c["marketing"]]
    sin = [c for c in lista if not c["marketing"]]
    perfiles = []
    frecuente = max(con, key=lambda c: (c["pedidos"], c["total"]), default=None)
    if frecuente:
        perfiles.append(("Comprador frecuente", "Recomendaciones personalizadas", frecuente))
    nuevo = next((c for c in con if c["pedidos"] == 0), None) or next((c for c in lista if c["pedidos"] == 0), None)
    if nuevo:
        perfiles.append(("Cliente nuevo", "Sin compras: lista de más vendidos", nuevo))
    sin_cons = max(sin, key=lambda c: (c["pedidos"], c["total"]), default=None)
    if sin_cons:
        perfiles.append(("Sin consentimiento", "No se usa su historial (RN2)", sin_cons))
    return perfiles


JS_ELEGIR = r"""
(function () {
  const $ = id => document.getElementById(id);
  const oculto = $("cliente"), elegido = $("cliente-elegido"), calcular = $("calcular");
  const filtros = ["f-region", "f-compras", "f-marketing", "f-buscar"].map($);
  const cuerpo = $("tabla-clientes"), contador = $("contador-clientes");
  let todos = [];
  const pesos = n => "$ " + Math.round(n).toLocaleString("es-CL");
  const describir = c => [[c.comuna, c.region].filter(Boolean).join(", ") || "Sin ubicación",
    c.edad ? c.edad + " años" : null, c.pedidos + (c.pedidos === 1 ? " pedido" : " pedidos")].filter(Boolean).join(" · ");
  function elegir(c) {
    oculto.value = c.id;
    elegido.innerHTML = "";
    const fuerte = document.createElement("strong"); fuerte.textContent = describir(c);
    const codigo = document.createElement("span"); codigo.className = "codigo"; codigo.textContent = c.id;
    elegido.append(fuerte, codigo);
    elegido.classList.add("listo");
    calcular.disabled = false;
    cuerpo.querySelectorAll("tr").forEach(tr => tr.classList.toggle("activo", tr.dataset.id === c.id));
  }
  function pintar() {
    const [region, compras, marketing, buscar] = filtros.map(f => f.value.trim().toLowerCase());
    const lista = todos.filter(c =>
      (!region || c.region === region) &&
      (!compras || (compras === "con" ? c.pedidos > 0 : c.pedidos === 0)) &&
      (!marketing || (marketing === "si" ? c.marketing : !c.marketing)) &&
      (!buscar || c.comuna.toLowerCase().includes(buscar) || c.id.toLowerCase().includes(buscar)))
      .sort((a, b) => b.pedidos - a.pedidos || b.total - a.total);
    const visibles = lista.slice(0, 40);
    cuerpo.replaceChildren(...visibles.map(c => {
      const tr = document.createElement("tr");
      tr.dataset.id = c.id; tr.tabIndex = 0;
      tr.classList.toggle("activo", c.id === oculto.value);
      [[c.comuna || "-", ""], [c.region, ""], [c.edad ? c.edad : "-", "num"], [c.pedidos, "num"],
       [pesos(c.total), "num"], [c.marketing ? "Sí" : "No", ""], [c.id, "codigo"]].forEach(([v, cls]) => {
        const td = document.createElement("td"); td.textContent = v; if (cls) td.className = cls; tr.appendChild(td);
      });
      tr.addEventListener("click", () => elegir(c));
      tr.addEventListener("keydown", e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); elegir(c); } });
      return tr;
    }));
    contador.textContent = lista.length > visibles.length
      ? "Mostrando " + visibles.length + " de " + lista.length.toLocaleString("es-CL") + " clientes. Usa los filtros para acotar."
      : lista.length ? lista.length + " clientes" : "Ningún cliente cumple los filtros.";
  }
  filtros.forEach(f => f.addEventListener(f.tagName === "INPUT" ? "input" : "change", pintar));
  calcular.disabled = !oculto.value;
  fetch("/clientes.json").then(r => r.json()).then(d => { todos = d; pintar();
    const actual = todos.find(c => c.id === oculto.value); if (actual) elegir(actual); })
    .catch(() => { contador.textContent = "No se pudo cargar la lista de clientes."; });
})();
"""


def pagina(usuario_sel: str = "", categoria_sel: str = "", resultado: str = "") -> str:
    avisos = []
    try:
        lista = clientes()
    except ErrorServicio as e:
        lista = []
        avisos.append(f"No se pudo cargar la lista de clientes. {e}")
    try:
        categorias, _ = llamar("GET", f"{DOMINIO}/categorias")
    except ErrorServicio:
        categorias = None
    if not isinstance(categorias, list) or not categorias:
        categorias = CATEGORIAS_POR_DEFECTO

    conteo: dict[str, int] = {}
    for c in lista:
        conteo[c["region"]] = conteo.get(c["region"], 0) + 1
    opciones_region = "".join(
        f'<option value="{escape(r.lower())}">{escape(r)} ({n:,})</option>'.replace(",", ".")
        for r, n in sorted(conteo.items()))
    opciones_categoria = "".join(
        f'<option value="{escape(str(c))}"{" selected" if str(c) == categoria_sel else ""}>{escape(str(c).capitalize())}</option>'
        for c in categorias)
    perfiles = "".join(
        f'<button type="submit" class="perfil" onclick="document.getElementById(\'cliente\').value=\'{escape(c["id"])}\'">'
        f'<span class="perfil-titulo">{escape(titulo)}</span><span class="perfil-caso">{escape(caso)}</span>'
        f'<span class="perfil-detalle">{escape(describir(c))}</span></button>'
        for titulo, caso, c in perfiles_demo(lista))
    actual = next((c for c in lista if c["id"] == usuario_sel), None)
    elegido = (f'<strong>{escape(describir(actual))}</strong><span class="codigo">{escape(actual["id"])}</span>'
               if actual else "Elige un cliente en la tabla o usa un perfil de demo.")
    avisos_html = "".join(f'<p class="error">{escape(a)}</p>' for a in avisos)
    total_txt = f"{len(lista):,}".replace(",", ".")

    selector = f"""<section class="bloque elegir">
  <div class="bloque-titulo"><h2>Elegir cliente</h2><span class="etiqueta">Datos reales del cliente; el código es solo referencia</span></div>
  <div class="filtros">
    <label>Región<select id="f-region"><option value="">Todas ({total_txt})</option>{opciones_region}</select></label>
    <label>Compras<select id="f-compras"><option value="">Todos</option><option value="con">Con compras</option><option value="sin">Sin compras</option></select></label>
    <label>Marketing<select id="f-marketing"><option value="">Todos</option><option value="si">Acepta</option><option value="no">No acepta</option></select></label>
    <label>Buscar<input id="f-buscar" placeholder="Comuna o código" autocomplete="off"></label>
  </div>
  <div class="tabla tabla-clientes"><table><thead><tr><th>Comuna</th><th>Región</th><th class="num">Edad</th>
    <th class="num">Pedidos</th><th class="num">Total comprado</th><th>Marketing</th><th>Código</th></tr></thead>
    <tbody id="tabla-clientes"></tbody></table></div>
  <p class="nota" id="contador-clientes" style="margin:.6rem 0 0">Cargando clientes...</p>
</section>"""
    if resultado:
        selector = f'<details class="cambiar"><summary>Cambiar de cliente</summary>{selector}</details>'

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
    <form method="post" action="/predecir" id="form-recomendar"
          onsubmit="const b=document.getElementById('calcular');b.disabled=true;b.textContent='Calculando...'">
      <input type="hidden" name="usuario_id" id="cliente" value="{escape(usuario_sel)}">
      <div class="perfiles"><span class="rotulo-campo">Perfiles de demo</span>{perfiles}</div>
      <div><span class="rotulo-campo">Cliente elegido</span><p class="elegido{" listo" if actual else ""}" id="cliente-elegido">{elegido}</p></div>
      <label>Categoría
        <select name="categoria">{opciones_categoria}</select>
      </label>
      <button type="submit" id="calcular">Calcular probabilidad</button>
    </form>
  </aside>
  <div class="principal">
    {selector}
    {resultado}
  </div>
</main>
<script>{JS_ELEGIR}</script>
</body></html>"""


def perfil_cliente(usuario_id: str) -> tuple[dict | None, str | None]:
    """(perfil, error). Datos reales del cliente desde el dominio; sin datos personales."""
    try:
        datos, _ = llamar("GET", f"{DOMINIO}/usuarios/{quote(usuario_id, safe='')}")
    except ErrorServicio as e:
        if e.status == 404:
            return None, f"No existe un cliente con el código {usuario_id}. Revisa el código e intenta de nuevo."
        return None, None
    return (datos, None) if isinstance(datos, dict) and datos.get("usuario_id") else (None, None)


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
    cabecera = ("<th>Producto</th><th>Categoría</th><th>Fecha</th>"
                "<th class='num'>Cantidad</th><th class='num'>Total</th><th>Estado</th>")
    cuerpo = ""
    for f in filas:
        cantidad = f.get("cantidad")
        cuerpo += (f"<tr><td>{escape(texto_valido(f.get('producto')) or '-')}</td>"
                   f"<td>{escape((texto_valido(f.get('categoria')) or '-').capitalize())}</td>"
                   f"<td>{escape(fecha_corta(f.get('fecha')))}</td>"
                   f"<td class='num'>{escape(str(int(cantidad)) if isinstance(cantidad, (int, float)) else '-')}</td>"
                   f"<td class='num'>{escape(pesos(f.get('total')))}</td>"
                   f"<td>{escape((texto_valido(f.get('estado')) or '-').capitalize())}</td></tr>")
    return envolver(f'<div class="tabla"><table><thead><tr>{cabecera}</tr></thead><tbody>{cuerpo}</tbody></table></div>')


@app.get("/health")
def health():
    return {"status": "ok", "servicio": "front"}


@app.get("/clientes.json")
def clientes_json():
    try:
        return clientes()
    except ErrorServicio as e:
        return JSONResponse(status_code=503, content={"detail": str(e)})


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
    perfil, error_perfil = perfil_cliente(usuario_id)
    if error_perfil:
        resultado = f'<section class="bloque"><p class="error">{escape(error_perfil)}</p></section>'
        return pagina(usuario_sel=usuario_id, categoria_sel=categoria, resultado=resultado)

    ficha = ""
    if perfil:
        region = (texto_valido(perfil.get("region")) or "").title()
        comuna = texto_valido(perfil.get("comuna")) or ""
        lugar = ", ".join(x for x in (comuna, region) if x) or "Sin región"
        edad = f'{perfil["edad"]} años' if perfil.get("edad") else "Edad no informada"
        consentimiento = ('<span class="chip">Acepta marketing</span>' if perfil.get("acepta_marketing")
                          else '<span class="chip reserva">Sin consentimiento de marketing</span>')
        ficha = (f'<dl class="ficha"><div><dt>Ubicación</dt><dd>{escape(lugar)}</dd></div>'
                 f'<div><dt>Edad</dt><dd>{escape(edad)}</dd></div>'
                 f'<div><dt>Pedidos pagados</dt><dd>{int(perfil.get("pedidos") or 0)}</dd></div>'
                 f'<div><dt>Total comprado</dt><dd>{pesos(perfil.get("total_gastado"))}</dd></div>'
                 f'<div><dt>Marketing</dt><dd>{consentimiento}</dd></div></dl>')

    try:
        res, ms = llamar("POST", f"{DOMINIO}/predicciones", json={"usuario_id": usuario_id, "categoria": categoria})
        prob = float(res["probabilidad_compra"])
        veredicto = ("Conviene recomendarle esta categoría." if res.get("prediccion") == 1
                     else "No conviene recomendarle esta categoría por ahora.")
        origen = res.get("origen")
        chip = ('<span class="chip" title="El endpoint serverless de SageMaker respondió a tiempo">Modelo en SageMaker</span>'
                if origen == "sagemaker" else
                '<span class="chip reserva" title="SageMaker no respondió en 200 ms; respondió el modelo local">'
                'Modelo de respaldo</span>' if origen == "reserva" else "")
        detalles = [f"Respondió en {ms:.0f} ms", f"predicción #{res.get('prediccion_id', '?')}",
                    f"Modelo {res.get('version_modelo', 'desconocido')}"]
        if res.get("instancia"):
            detalles.append(f"replica {res['instancia']}")
        ancho = max(0.0, min(1.0, prob)) * 100
        probabilidad = (f'<div class="resumen"><p class="prob">{prob:.0%}</p><div><p class="veredicto">{veredicto}</p>'
                        f'<p class="nota" style="margin:0">Probabilidad de que compre en '
                        f'{escape(categoria.capitalize())}.</p></div>'
                        f'<div class="barra-prob"><span style="width:{ancho:.0f}%"></span></div>'
                        f'<p class="meta">{escape(" · ".join(detalles))}</p></div>')
    except ErrorServicio as e:
        chip, probabilidad = "", f'<p class="error">{escape(str(e))}</p>'
    except (KeyError, TypeError, ValueError):
        chip, probabilidad = "", f'<p class="error">{RESPUESTA_INESPERADA}</p>'

    resultado = (f'<section class="bloque"><div class="bloque-titulo"><h2>Cliente {escape(usuario_id)} '
                 f'&middot; {escape(categoria.capitalize())}</h2>{chip}</div>{ficha}{probabilidad}</section>')
    resultado += recomendaciones_html(usuario_id, categoria)
    resultado += llm_html(usuario_id)
    resultado += historial_html(usuario_id)
    return pagina(usuario_sel=usuario_id, categoria_sel=categoria, resultado=resultado)


# Métricas del modelo medidas en el Training Job de SageMaker (22/11/2026) y en la prueba de carga.
MODELO = {"endpoint": "nexo-recomendacion", "tipo": "XGBoost binario (algoritmo integrado de SageMaker)",
          "config": "Serverless · 2048 MB · concurrencia 10", "variables": 6,
          "pr_auc": 0.967, "pr_auc_base": 0.798, "roc_auc": 0.915,
          "latencia_caliente_ms": 45, "timeout_ms": 200}


def _estado_html(nombre: str, estado: str, detalle: str) -> str:
    clase = {"ok": "ok", "degradado": "medio", "no_configurado": "medio"}.get(estado, "mal")
    texto = {"ok": "Operativo", "degradado": "Degradado", "no_configurado": "Sin configurar"}.get(estado, "Con fallas")
    return (f'<li class="estado"><span class="punto {clase}" aria-hidden="true"></span><span class="estado-nombre">{nombre}</span>'
            f'<span class="estado-texto">{texto}</span><span class="estado-detalle">{escape(detalle)}</span></li>')


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard():
    import json as _json

    metricas = {}
    try:
        m, _ = llamar("GET", f"{DOMINIO}/metricas")
        metricas = m if isinstance(m, dict) else {}
    except ErrorServicio:
        metricas = {}
    salud = {}
    try:
        h, _ = llamar("GET", f"{GATEWAY_URL}/api/health")
        salud = h if isinstance(h, dict) else {}
    except ErrorServicio as e:
        salud = {"estado": "error", "checks": {}, "_error": str(e)}

    nombres = {"base_datos": "Base de datos", "cola": "Cola RabbitMQ", "llm": "LLM (NVIDIA)", "sagemaker": "Modelo en SageMaker"}
    checks = salud.get("checks") or {}
    estados = "".join(_estado_html(nombres.get(k, k), str(v.get("estado", "error")), str(v.get("detalle", "")))
                      for k, v in checks.items() if isinstance(v, dict))
    if not estados:
        estados = _estado_html("Sistema", "error", salud.get("_error", "No se pudo consultar /api/health"))

    with _lock:
        est = dict(_estadisticas)
    pct_reserva = f"{est['con_reserva'] / est['entregadas']:.0%}" if est["entregadas"] else "-"

    totales = metricas.get("totales", {})
    prop = metricas.get("propension_completa") or metricas.get("propension") or {}
    orden = ["alta", "media", "baja"]
    prop_labels = [k.capitalize() for k in orden if k in prop] or ["Sin datos"]
    prop_values = [prop[k] for k in orden if k in prop] or [0]
    top = metricas.get("top_categorias_transacciones", [])[:8]
    regiones = metricas.get("regiones", [])[:8]
    ultimas = metricas.get("ultimas_predicciones", [])[:10]

    def fmt(n):
        try:
            return f"{int(n):,}".replace(",", ".")
        except (TypeError, ValueError):
            return "-"

    filas = ""
    for u in ultimas:
        try:
            pr = f"{float(u.get('prediccion')):.0%}"
        except (TypeError, ValueError):
            pr = "-"
        fecha = str(u.get("fecha", ""))[:16].replace("T", " ")
        etiqueta = str(u.get("propension", "")).capitalize()
        filas += (f"<tr><td>{escape(str(u.get('usuario_id', '-')))}</td>"
                  f"<td>{escape(str(u.get('categoria', '-')).capitalize())}</td>"
                  f"<td class='num'>{pr}</td><td>{escape(etiqueta)}</td><td>{escape(fecha)}</td></tr>")
    tabla = (f"<div class='tabla'><table><thead><tr><th>Cliente</th><th>Categoría</th><th class='num'>Probabilidad</th>"
             f"<th>Propensión</th><th>Fecha</th></tr></thead><tbody>{filas}</tbody></table></div>"
             if filas else "<p class='nota'>Aún no hay predicciones registradas.</p>")

    datos = _json.dumps({
        "prop": [prop_labels, prop_values],
        "top": [[str(t.get("categoria", "")).capitalize() for t in top], [t.get("n", 0) for t in top]],
        "reg": [[str(r.get("region", "")).title() for r in regiones], [r.get("n", 0) for r in regiones]],
    })
    ventaja = MODELO["pr_auc"] - MODELO["pr_auc_base"]

    return f"""<!doctype html>
<html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Nexo - Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>{ESTILOS}
.tablero {{ max-width: 1180px; margin: 0 auto; padding: 1.75rem 1.25rem 3rem; display: grid; gap: 1.25rem; }}
.cabecera h1 {{ font-size: 1.5rem; margin: 0; }}
.cabecera p {{ color: var(--tinta-2); margin: .25rem 0 0; }}
.dos {{ display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 1.25rem; }}
.estados {{ list-style: none; margin: 0; padding: 0; display: grid; gap: .1rem; }}
.estado {{ display: grid; grid-template-columns: 14px 170px 110px minmax(0, 1fr); align-items: center; gap: .6rem;
  padding: .55rem 0; border-bottom: 1px solid var(--linea); font-size: .92rem; }}
.estado:last-child {{ border-bottom: 0; }}
.punto {{ width: 10px; height: 10px; border-radius: 50%; }}
.punto.ok {{ background: #1f9d6b; }} .punto.medio {{ background: #d79a1a; }} .punto.mal {{ background: #c2412b; }}
.estado-nombre {{ font-weight: 600; }} .estado-texto {{ color: var(--tinta-2); }}
.estado-detalle {{ color: var(--tinta-3); font-size: .84rem; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
.cifras {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); border: 1px solid var(--linea);
  border-radius: 9px; overflow: hidden; }}
.cifra {{ padding: .9rem 1rem; border-right: 1px solid var(--linea); }}
.cifra:last-child {{ border-right: 0; }}
.cifra .valor {{ font-size: 1.6rem; font-weight: 700; letter-spacing: -.02em; font-variant-numeric: tabular-nums; }}
.cifra .rotulo {{ color: var(--tinta-2); font-size: .82rem; }}
.modelo {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: .9rem 1.25rem; margin: 0; }}
.modelo dt {{ color: var(--tinta-3); font-size: .76rem; text-transform: uppercase; letter-spacing: .03em; }}
.modelo dd {{ margin: .15rem 0 0; font-weight: 600; }}
.grafico {{ position: relative; height: 260px; }}
@media (max-width: 860px) {{ .dos {{ grid-template-columns: minmax(0, 1fr); }}
  .estado {{ grid-template-columns: 14px 1fr; }} .estado-texto, .estado-detalle {{ grid-column: 2; }} }}
</style></head>
<body>
{barra_superior("/dashboard")}
<main class="tablero">
  <div class="cabecera"><h1>Dashboard</h1><p>Estado del sistema, del modelo y de las recomendaciones entregadas.</p></div>

  <section class="bloque"><div class="bloque-titulo"><h2>Estado del sistema</h2>
    <span class="etiqueta">Fuente: GET /api/health</span></div><ul class="estados">{estados}</ul></section>

  <section class="bloque"><div class="bloque-titulo"><h2>Recomendaciones entregadas</h2>
    <span class="etiqueta">Desde el último reinicio del front</span></div>
    <div class="cifras">
      <div class="cifra"><div class="valor">{est["entregadas"]}</div><div class="rotulo">Recomendaciones entregadas</div></div>
      <div class="cifra"><div class="valor">{est["con_reserva"]}</div><div class="rotulo">Con lista de reserva (más vendidos)</div></div>
      <div class="cifra"><div class="valor">{pct_reserva}</div><div class="rotulo">Proporción con lista de reserva</div></div>
      <div class="cifra"><div class="valor">{est["llm_con_texto"]}</div><div class="rotulo">Mensajes del LLM entregados</div></div>
      <div class="cifra"><div class="valor">{est["llm_degradado"]}</div><div class="rotulo">Respuestas en modo degradado</div></div>
    </div></section>

  <section class="bloque"><div class="bloque-titulo"><h2>Modelo</h2>
    <span class="etiqueta">Medido en el Training Job y en la prueba de carga</span></div>
    <dl class="modelo">
      <div><dt>Endpoint</dt><dd>{MODELO["endpoint"]}</dd></div>
      <div><dt>Configuración</dt><dd>{MODELO["config"]}</dd></div>
      <div><dt>Tipo</dt><dd>{MODELO["tipo"]}</dd></div>
      <div><dt>PR-AUC (validación)</dt><dd>{MODELO["pr_auc"]:.3f} <span class="etiqueta">vs {MODELO["pr_auc_base"]:.3f} al azar (+{ventaja:.3f})</span></dd></div>
      <div><dt>ROC-AUC</dt><dd>{MODELO["roc_auc"]:.3f}</dd></div>
      <div><dt>Latencia en caliente</dt><dd>~{MODELO["latencia_caliente_ms"]} ms <span class="etiqueta">reserva si supera 200 ms</span></dd></div>
      <div><dt>Predicciones registradas</dt><dd>{fmt(totales.get("predicciones"))}</dd></div>
      <div><dt>Datos</dt><dd>{fmt(totales.get("usuarios"))} clientes · {fmt(totales.get("productos"))} productos · {fmt(totales.get("transacciones"))} transacciones</dd></div>
    </dl></section>

  <div class="dos">
    <section class="bloque"><div class="bloque-titulo"><h2>Predicciones por propensión</h2></div>
      <div class="grafico"><canvas id="g-prop"></canvas></div></section>
    <section class="bloque"><div class="bloque-titulo"><h2>Categorías más compradas</h2></div>
      <div class="grafico"><canvas id="g-top"></canvas></div></section>
  </div>

  <section class="bloque"><div class="bloque-titulo"><h2>Últimas predicciones</h2></div>{tabla}</section>

  <section class="bloque"><div class="bloque-titulo"><h2>Clientes por región</h2></div>
    <div class="grafico"><canvas id="g-reg"></canvas></div></section>
</main>
<script>
const D = {datos};
const verde = "#0f6b4f", tinta = "#4f5b55";
Chart.defaults.font.family = "'IBM Plex Sans', system-ui, sans-serif";
Chart.defaults.color = tinta;
const barras = (id, etiquetas, valores, horizontal, colores) => new Chart(document.getElementById(id), {{
  type: "bar",
  data: {{ labels: etiquetas, datasets: [{{ data: valores, backgroundColor: colores || verde, borderRadius: 4 }}] }},
  options: {{ maintainAspectRatio: false, indexAxis: horizontal ? "y" : "x",
    plugins: {{ legend: {{ display: false }} }},
    scales: {{ x: {{ grid: {{ color: "#eceee9" }} }}, y: {{ grid: {{ color: "#eceee9" }} }} }} }}
}});
barras("g-prop", D.prop[0], D.prop[1], false, ["#0f6b4f", "#5aa889", "#c9d9d1"]);
barras("g-top", D.top[0], D.top[1], true);
barras("g-reg", D.reg[0], D.reg[1], false);
</script>
</body></html>"""
