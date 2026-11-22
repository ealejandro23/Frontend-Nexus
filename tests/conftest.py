import sys

import pytest


@pytest.fixture(autouse=True)
def limpiar_cache_de_clientes():
    """La lista de clientes se guarda 5 minutos en memoria: cada test parte sin ella."""
    main = sys.modules.get("main")
    if main is not None and hasattr(main, "_clientes_cache"):
        main._clientes_cache.update(datos=[], ts=0.0)
    yield
