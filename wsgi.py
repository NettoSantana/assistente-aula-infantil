import os, sys
BASE_DIR = os.path.dirname(__file__)
APP_DIR = os.path.join(BASE_DIR, "assistente-aula-infantil")
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)
from server import app
