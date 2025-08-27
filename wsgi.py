import os, sys, importlib.util

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
APP_DIR = os.path.join(BASE_DIR, "assistente-aula-infantil")
SERVER_FILE = os.path.join(APP_DIR, "server.py")

if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

if not os.path.isfile(SERVER_FILE):
    raise RuntimeError(f"server.py não encontrado em {SERVER_FILE}")

spec = importlib.util.spec_from_file_location("app_server", SERVER_FILE)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

app = getattr(module, "app", None)
if app is None:
    raise RuntimeError("Variável 'app' não encontrada dentro de server.py")
