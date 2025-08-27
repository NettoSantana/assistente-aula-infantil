# wsgi.py (raiz)
import os, sys, importlib.util

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
APP_DIR  = os.path.join(BASE_DIR, 'assistente-aula-infantil')
SERVER_PATH = os.path.join(APP_DIR, 'server.py')

def _load_module(name: str, path: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod  = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[attr-defined]
    sys.modules[name] = mod
    return mod

# garante que a pasta do app está no sys.path
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

# PRÉ-CARREGA módulos que o server.py importa por nome simples
for fname in ['storage.py', 'progress.py', 'notifications.py', 'activities.py', 'leitura.py']:
    fpath = os.path.join(APP_DIR, fname)
    if os.path.exists(fpath):
        _load_module(os.path.splitext(fname)[0], fpath)

# carrega o server.py e expõe "app"
if not os.path.exists(SERVER_PATH):
    raise RuntimeError(f'server.py not found at {SERVER_PATH}')

_server = _load_module('server', SERVER_PATH)
app = _server.app
