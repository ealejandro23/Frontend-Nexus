"""Front de Nexo (Caso 1 · Nexus Retail), hecho solo con FastAPI.

FastAPI arma la página en Python (sin archivos HTML ni JavaScript aparte) y,
cuando el usuario envía el formulario, llama al dominio a través del gateway.
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
                          "Monitores", "Notebooks", "Periféricos", "Redes", "Smartphones"]
RESPUESTA_INESPERADA = "El servicio de dominio respondió en un formato inesperado. Avisa al equipo."

app = FastAPI(title="front", docs_url=None, redoc_url=None, openapi_url=None)


class ErrorServicio(Exception):
    """Error con un mensaje listo para mostrar al usuario."""


def llamar(metodo: str, url: str, **kwargs):
    """Llama al backend y devuelve (json, latencia_ms). Traduce caídas a ErrorServicio."""
    inicio = time.perf_counter()
    try:
        r = httpx.request(metodo, url, timeout=TIMEOUT_S, **kwargs)
    except httpx.TimeoutException:
        raise ErrorServicio(f"El servicio tardó más de {TIMEOUT_S:.0f} s en responder. Intenta de nuevo.")
    except httpx.TransportError:
        raise ErrorServicio("No hay conexión con el gateway. Revisa que el sistema esté levantado.")
    ms = (time.perf_counter() - inicio) * 1000
    try:
        datos = r.json()
    except ValueError:
        datos = None
    detalle = datos.get("detail") if isinstance(datos, dict) else None
    if r.status_code in (502, 503, 504):
        raise ErrorServicio(detalle or "Un servicio del sistema no está disponible. Intenta de nuevo en unos segundos.")
    if r.is_error:
        raise ErrorServicio(f"Error {r.status_code}: {detalle or 'respuesta inesperada'}")
    return datos, ms


def pagina(usuario_sel: str = "", categoria_sel: str = "", resultado: str = "") -> str:
    """Arma el HTML completo de la página."""
    avisos = []
    try:
        usuarios, _ = llamar("GET", f"{DOMINIO}/usuarios")
    except ErrorServicio as e:
        usuarios = []
        avisos.append(f"No se pudo cargar la lista de clientes. {e}")
    # Si el dominio responde algo fuera del contrato, se ignora en vez de romper la página
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
<title>Nexo · Recomendador de compras</title>
<style>
  body {{ font-family: system-ui, sans-serif; max-width: 720px; margin: 2rem auto; padding: 0 1rem; color: #17201c; }}
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
  <h1>Recomendador de compras</h1>
  <p>Elige un cliente y una categoría para estimar qué tan probable es que compre en ella.</p>
  {avisos_html}
  <form method="post" action="/predecir">
    <label>Cliente {campo_usuario}</label>
    <label>Categoría <select name="categoria">{opciones_categoria}</select></label>
    <button type="submit">Calcular probabilidad</button>
  </form>
  {resultado}
</body></html>"""


def historial_html(usuario_id: str) -> str:
    try:
        # safe="" codifica también "/", así el id no puede cambiar la ruta que se pide
        filas, _ = llamar("GET", f"{DOMINIO}/usuarios/{quote(usuario_id, safe='')}/historial")
    except ErrorServicio as e:
        return f'<p class="error">No se pudo cargar el historial. {escape(str(e))}</p>'
    if not filas:
        return "<p class=\"meta\">Este cliente todavía no tiene compras registradas.</p>"
    if not isinstance(filas, list) or not all(isinstance(f, dict) for f in filas):
        return f'<p class="error">No se pudo cargar el historial. {RESPUESTA_INESPERADA}</p>'
    columnas = list(filas[0].keys())
    cabecera = "".join(f"<th>{escape(str(c))}</th>" for c in columnas)
    cuerpo = "".join("<tr>" + "".join(f"<td>{escape(str(f.get(c, '')))}</td>" for c in columnas) + "</tr>" for f in filas)
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
        veredicto = "Conviene recomendarle esta categoría." if res.get("prediccion") == 1 \
            else "No conviene recomendarle esta categoría por ahora."
        detalles = [f"Modelo {res.get('version_modelo', 'desconocido')}",
                    f"predicción n.º {res.get('prediccion_id', '?')}", f"{ms:.0f} ms"]
        if res.get("instancia"):
            detalles.append(f"réplica {res['instancia']}")
        resultado = (f'<p class="prob">{prob:.0%}</p><p>{veredicto}</p>'
                     f'<p class="meta">{escape(", ".join(detalles))}</p>')
    except ErrorServicio as e:
        resultado = f'<p class="error">{escape(str(e))}</p>'
    except (KeyError, TypeError, ValueError):
        # El dominio respondió 200 pero sin el formato del contrato (por ejemplo, sin probabilidad_compra)
        resultado = f'<p class="error">{RESPUESTA_INESPERADA}</p>'
    resultado +=f"<h2>Historial de compras</h2>{historial_html(usuario_id)}"
    return pagina(usuario_id, categoria, resultado)
