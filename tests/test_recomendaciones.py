"""Recomendaciones (4 a 8 productos), lista de reserva, texto del LLM y modo degradado.
Se reemplaza httpx.request por un gateway simulado."""
import httpx
import pytest
from fastapi.testclient import TestClient

import main

cliente = TestClient(main.app)


def productos(prefijo, n):
    return [{"sku": f"{prefijo}{i}", "nombre": f"Producto {prefijo}{i}", "categoria": "Audio", "score": 1.0}
            for i in range(n)]


MAS_VENDIDOS = productos("TOP", 8)


def gateway(personales=None, falla_personal=False, campana_status=202, job=None):
    """personales: lista que devuelve el recomendador para el usuario. job: respuesta de /api/predicciones."""
    def responder(metodo, url, **kwargs):
        peticion = httpx.Request(metodo, url)
        params = kwargs.get("params") or {}
        if url.endswith("/api/recomendaciones"):
            if "usuario_id" not in params:
                return httpx.Response(200, json={"recomendaciones": MAS_VENDIDOS}, request=peticion)
            if falla_personal:
                return httpx.Response(503, json={"detail": "base de datos caida"}, request=peticion)
            return httpx.Response(200, json={"recomendaciones": personales or []}, request=peticion)
        if "/api/campanas/" in url and metodo == "POST":
            return httpx.Response(campana_status, json={"job_id": "job-123", "estado": "en_cola"}, request=peticion)
        if "/api/predicciones/" in url:
            if job is None:
                return httpx.Response(404, json={"detail": "Campaña no encontrada"}, request=peticion)
            return httpx.Response(200, json=job, request=peticion)
        if url.endswith("/predicciones"):
            return httpx.Response(200, json={"probabilidad_compra": 0.5, "prediccion": 1}, request=peticion)
        if url.endswith("/usuarios"):
            return httpx.Response(200, json=[{"usuario_id": "U1"}], request=peticion)
        return httpx.Response(200, json=[], request=peticion)
    return responder


@pytest.fixture(autouse=True)
def estado_limpio():
    main._estadisticas.update({k: 0 for k in main._estadisticas})
    main._jobs_contados.clear()
    main._reserva.update(productos=[], ts=0.0)


def predecir():
    return cliente.post("/predecir", data={"usuario_id": "U1", "categoria": "Audio"})


def test_muestra_entre_4_y_8_productos_personalizados(monkeypatch):
    monkeypatch.setattr(main.httpx, "request", gateway(personales=productos("P", 12)))
    r = predecir()
    assert "Productos recomendados (8)" in r.text
    assert "Producto P0" in r.text and "Lista de reserva" not in r.text
    assert main._estadisticas == {"entregadas": 1, "con_reserva": 0, "llm_con_texto": 0, "llm_degradado": 0}


def test_si_el_recomendador_falla_usa_los_mas_vendidos(monkeypatch):
    monkeypatch.setattr(main.httpx, "request", gateway(falla_personal=True))
    r = predecir()
    assert "Lista de reserva (mas vendidos)" in r.text
    assert "Producto TOP0" in r.text
    assert main._estadisticas["con_reserva"] == 1


def test_cliente_sin_historial_cuenta_como_lista_de_reserva(monkeypatch):
    monkeypatch.setattr(main.httpx, "request", gateway(personales=MAS_VENDIDOS))
    r = predecir()
    assert "no tiene historial suficiente" in r.text
    assert main._estadisticas["con_reserva"] == 1


def test_pocas_personalizadas_se_completan_hasta_8(monkeypatch):
    monkeypatch.setattr(main.httpx, "request", gateway(personales=productos("P", 2)))
    r = predecir()
    assert "Productos recomendados (8)" in r.text
    assert "Producto P0" in r.text and "Producto TOP0" in r.text
    assert main._estadisticas["con_reserva"] == 1


def test_llm_caido_muestra_modo_degradado_sin_romper_la_pagina(monkeypatch):
    monkeypatch.setattr(main.httpx, "request", gateway(personales=productos("P", 8), campana_status=503))
    r = predecir()
    assert r.status_code == 200
    assert "Modo degradado" in r.text
    assert "Productos recomendados (8)" in r.text
    assert main._estadisticas["llm_degradado"] == 1


def test_la_pagina_consulta_el_llm_sin_exponer_la_clave(monkeypatch):
    monkeypatch.setattr(main, "API_KEY", "clave-secreta-de-prueba-123")
    monkeypatch.setattr(main.httpx, "request", gateway(personales=productos("P", 8)))
    r = predecir()
    assert '"job-123"' in r.text and "/llm/" in r.text
    assert "clave-secreta-de-prueba-123" not in r.text


def test_llm_pendiente_listo_y_degradado(monkeypatch):
    monkeypatch.setattr(main.httpx, "request", gateway(job=None))
    assert cliente.get("/llm/job-1?intento=1").json() == {"estado": "pendiente"}
    d = cliente.get("/llm/job-1?intento=99").json()
    assert d["estado"] == "degradado" and "no llego a tiempo" in d["motivo"]

    monkeypatch.setattr(main.httpx, "request", gateway(job={"asunto": "Hola", "mensaje": "Texto del LLM", "degradado": False}))
    assert cliente.get("/llm/job-2").json() == {"estado": "listo", "asunto": "Hola", "mensaje": "Texto del LLM"}

    monkeypatch.setattr(main.httpx, "request", gateway(job={"asunto": "x", "mensaje": "generico", "degradado": True}))
    assert cliente.get("/llm/job-3").json()["estado"] == "degradado"
    assert main._estadisticas["llm_con_texto"] == 1
    assert main._estadisticas["llm_degradado"] == 2


def test_job_id_invalido_responde_422_con_detail():
    r = cliente.get("/llm/<script>")
    assert r.status_code in (404, 422)
    r = cliente.get("/llm/abc%20def")
    assert r.status_code == 422 and "detail" in r.json()


def test_dashboard_muestra_la_proporcion_con_lista_de_reserva(monkeypatch):
    monkeypatch.setattr(main.httpx, "request", gateway(falla_personal=True))
    predecir()
    monkeypatch.setattr(main.httpx, "request", gateway(personales=productos("P", 8)))
    predecir()
    r = cliente.get("/dashboard")
    assert "Recomendaciones entregadas" in r.text
    assert "50%" in r.text


def test_producto_sin_nombre_muestra_el_sku(monkeypatch):
    con_nan = [{"sku": "NX-1", "nombre": "NaN", "categoria": "gaming"}] + productos("P", 7)
    monkeypatch.setattr(main.httpx, "request", gateway(personales=con_nan))
    r = predecir()
    assert "Producto NX-1" in r.text and ">NaN<" not in r.text
