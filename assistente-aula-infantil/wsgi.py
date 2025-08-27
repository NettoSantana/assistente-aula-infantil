# wsgi.py — carrega a app mesmo com pasta com hífen
import importlib.util
from pathlib import Path

server_path = Path(__file__).parent / "assistente-aula-infantil" / "server.py"
spec = importlib.util.spec_from_file_location("server_dyn", server_path)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)  # type: ignore[attr-defined]

# a variável "app" é exportada por server.py
app = mod.app
