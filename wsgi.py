# wsgi.py (RAIZ do repo)
import os, sys, importlib.util

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
APP_DIR = os.path.join(BASE_DIR, "assistente-aula-infantil")

# garante que a pasta do app está no sys.path
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

try:
    # import normal: carrega assistente-aula-infantil/server.py
    from server import app  # type: ignore
except ModuleNotFoundError:
    # fallback por caminho absoluto
    server_path = os.path.join(APP_DIR, "server.py")
    if not os.path.exists(server_path):
        raise
    spec = importlib.util.spec_from_file_location("server", server_path)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    assert spec.loader is not None
    spec.loader.exec_module(mod)  # type: ignore[attr-defined]
    app = mod.app  # type: ignore[attr-defined]
