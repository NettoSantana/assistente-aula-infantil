from pathlib import Path
import sys, runpy

BASE = Path(__file__).parent
PKG_DIR = BASE / "assistente-aula-infantil"
SERVER = PKG_DIR / "server.py"

# garante que storage.py / progress.py sejam importáveis
sys.path.insert(0, str(PKG_DIR))

ns = runpy.run_path(str(SERVER))
app = ns["app"]