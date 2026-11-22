"""Pruebas con un gateway simulado: se reemplaza httpx.request por respuestas fijas del contrato."""
import httpx
import pytest
from fastapi.testclient import TestClient

import main

cliente = TestClient(main.app)

USUARIOS = [{"usuario_id": "U103808", "region": "Coquimbo"}]
CATEGORIAS = ["Audio", "Notebooks"]
PERFIL = {"usuario_id": "U103808", "region": "coquimbo", "comuna": "La Serena", "edad": 34,
          "acepta_marketing": True, "pedidos": 3, "total_gastado": 59970.0}
HISTORIAL = [{"producto": "Audífonos", "categoria": "Audio", "monto": 19990}]
PREDICCION = {"prediccion_id": 42, "probabilidad_compra": 0.73, "prediccion": 1,
              "version_modelo": "v1", "instancia": "dominio1"}


def gateway_falso(prediccion_status=200, prediccion_json=PREDICCION):
    """Devuelve una función que reemplaza a httpx.request y responde según la ruta pedida."""
    def responder(metodo, url, **kwargs):
        peticion = httpx.Request(metodo, url)
        if "/usuarios/" in url and not url.endswith("/historial"):
            return httpx.Response(200, json=PERFIL, request=peticion)
        if url.endswith("/usuarios"):
            return httpx.Response(200, json=USUARIOS, request=peticion)
        if url.endswith("/categorias"):
            return httpx.Response(200, json=CATEGORIAS, request=peticion)
        if url.endswith("/historial"):
            return httpx.Response(200, json=HISTORIAL, request=peticion)
        if url.endswith("/predicciones"):
            return httpx.Response(prediccion_status, json=prediccion_json, request=peticion)
        return httpx.Response(404, json={"detail": "no existe"}, request=peticion)
    return responder


def predecir():
    return cliente.post("/predecir", data={"usuario_id": "U103808", "categoria": "Audio"})


def test_prediccion_exitosa_muestra_probabilidad_version_e_historial(monkeypatch):
    monkeypatch.setattr(main.httpx, "request", gateway_falso())
    r = predecir()
    assert r.status_code == 200
    assert "73%" in r.text
    assert "Modelo v1" in r.text
    assert "replica dominio1" in r.text
    assert "Audífonos" in r.text          # historial
    assert "U103808 (Coquimbo)" in r.text  # lista de clientes


def test_ia_caida_muestra_el_detail_del_dominio(monkeypatch):
    detalle = {"detail": "El modelo de IA no está disponible"}
    monkeypatch.setattr(main.httpx, "request", gateway_falso(503, detalle))
    r = predecir()
    assert r.status_code == 200
    assert "El modelo de IA no está disponible" in r.text
    assert "Audífonos" in r.text  # el historial sigue funcionando aunque la IA esté caída


@pytest.mark.parametrize("respuesta", [{"error": "sin probabilidad"}, ["no", "es", "objeto"], None])
def test_respuesta_fuera_de_contrato_no_rompe_la_pagina(monkeypatch, respuesta):
    monkeypatch.setattr(main.httpx, "request", gateway_falso(200, respuesta))
    r = predecir()
    assert r.status_code == 200
    assert "formato inesperado" in r.text


def test_envia_la_clave_x_api_key_al_gateway(monkeypatch):
    vistas = []
    falso = gateway_falso()

    def espia(metodo, url, **kwargs):
        vistas.append((kwargs.get("headers") or {}).get("X-API-Key"))
        return falso(metodo, url, **kwargs)

    monkeypatch.setattr(main, "API_KEY", "clave-de-prueba-123456")
    monkeypatch.setattr(main.httpx, "request", espia)
    cliente.get("/")
    assert vistas and all(v == "clave-de-prueba-123456" for v in vistas)


def test_clave_rechazada_muestra_aviso_claro(monkeypatch):
    def rechaza(metodo, url, **kwargs):
        return httpx.Response(401, json={"detail": "Falta la cabecera X-API-Key"}, request=httpx.Request(metodo, url))

    monkeypatch.setattr(main.httpx, "request", rechaza)
    r = cliente.get("/")
    assert r.status_code == 200
    assert "X-API-Key" in r.text


def test_cliente_inexistente_muestra_aviso_claro(monkeypatch):
    def responder(metodo, url, **kwargs):
        peticion = httpx.Request(metodo, url)
        if "/usuarios/" in url and not url.endswith("/historial"):
            return httpx.Response(404, json={"detail": "Cliente no encontrado"}, request=peticion)
        return gateway_falso()(metodo, url, **kwargs)
    monkeypatch.setattr(main.httpx, "request", responder)
    r = predecir()
    assert "No existe un cliente con el código U103808" in r.text


def test_ficha_del_cliente_con_datos_reales(monkeypatch):
    monkeypatch.setattr(main.httpx, "request", gateway_falso())
    r = predecir()
    assert "La Serena, Coquimbo" in r.text and "$ 59.970" in r.text and "34 años" in r.text
