import sys, os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
APP_DIR  = os.path.join(BASE_DIR, "assistente-aula-infantil")
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

from server import app  # server.py dentro de assistente-aula-infantil