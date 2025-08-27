import os, sys, importlib.util

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
APP_DIR  = os.path.join(BASE_DIR, 'assistente-aula-infantil')
SERVER_PATH = os.path.join(APP_DIR, 'server.py')

if not os.path.exists(SERVER_PATH):
    raise RuntimeError(f'server.py not found at {SERVER_PATH}')

spec = importlib.util.spec_from_file_location('server', SERVER_PATH)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
app = mod.app

