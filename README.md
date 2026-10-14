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
