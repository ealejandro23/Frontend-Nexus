# nexo-front

Front del sistema Nexo (Caso 1 · Nexus Retail), hecho solo con FastAPI. FastAPI arma la página en Python y, al enviar el formulario, llama al dominio a través del gateway (`http://gateway:8080/api/dominio/...`).

- Imagen: `ghcr.io/ealejandro23/front:ep2`
- Puerto interno: 8000 (no se publica; solo el gateway publica puerto)
- Rutas: `GET /` (formulario), `POST /predecir` (resultado e historial), `GET /health`

## Probar en local

```bash
pip install -r requirements-dev.txt
pytest -q
uvicorn main:app --port 8000
```

## Lo que el front espera del dominio

| Petición | Respuesta |
|---|---|
| `GET /usuarios` | `[{"usuario_id": "U103808", "region": "Coquimbo"}]` |
| `GET /categorias` | `["Almacenamiento", "Audio", ...]` |
| `GET /usuarios/{id}/historial` | lista de compras (se muestra como tabla) |
| `POST /predicciones` con `{"usuario_id": "U103808", "categoria": "Notebooks"}` | `{"prediccion_id": 42, "probabilidad_compra": 0.73, "prediccion": 1, "version_modelo": "v1", "instancia": "dominio1"}` |

Si la IA no responde, el dominio devuelve `503` con `{"detail": "mensaje"}` y el front lo muestra.

## Pipeline

PR hacia `dev` o `main`: tests y build sin publicar. Push a `main`: publica `sha-…` y `latest`. Tag `ep2`: publica `:ep2`.

## EP3: recomendaciones, LLM y modo degradado

- **Productos (4 a 8):** `GET /api/dominio/api/recomendaciones?usuario_id=...&limite=8` a través del gateway.
- **Lista de reserva (más vendidos):** la misma ruta sin usuario. Se usa si el recomendador falla, si el cliente no tiene historial para personalizar o si llegan menos de 4 productos. Se guarda en memoria 5 minutos para seguir funcionando aunque el dominio falle.
- **Texto del LLM:** `POST /api/campanas/{usuario}` devuelve un `job_id`; la página consulta `/llm/{job_id}` cada 2 s (hasta `LLM_ESPERA_MAX_S`, 30 s por defecto). El front reenvía a `GET /api/predicciones/{job_id}` con la cabecera `X-API-Key`, así la clave nunca llega al navegador.
- **Modo degradado:** si la campaña no se puede encolar, no llega a tiempo o el worker la marca como `degradado`, se muestra el aviso y las recomendaciones siguen funcionando.
- **Dashboard:** recomendaciones entregadas, cuántas usaron la lista de reserva y su proporción, mensajes del LLM entregados y respuestas en modo degradado (contadores en memoria desde el último reinicio del front).
- **Variables:** `GATEWAY_URL`, `API_KEY` (la misma del gateway, desde el `.env` del servidor), `LLM_ESPERA_MAX_S` (opcional).
