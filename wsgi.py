import os, sys

BASE = os.path.dirname(__file__)
SYS_PATH = os.path.join(BASE, "assistente-aula-infantil")
if SYS_PATH not in sys.path:
    sys.path.insert(0, SYS_PATH)

from server import app  # <- ESTA ? a WSGI app
