import os

# Gateway inexistente: así se prueba que la página no se cae aunque el backend no responda
os.environ["GATEWAY_URL"] = "http://127.0.0.1:9"

from fastapi.testclient import TestClient  # noqa: E402

from main import app  # noqa: E402

cliente = TestClient(app)


def test_health():
    r = cliente.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_pagina_carga_aunque_el_backend_no_responda():
    r = cliente.get("/")
    assert r.status_code == 200
    assert "Recomendador de compras" in r.text
    assert "No hay conexión con el gateway" in r.text


def test_prediccion_con_backend_caido_muestra_error():
    r = cliente.post("/predecir", data={"usuario_id": "U103808", "categoria": "Notebooks"})
    assert r.status_code == 200
    assert "No hay conexión con el gateway" in r.text
