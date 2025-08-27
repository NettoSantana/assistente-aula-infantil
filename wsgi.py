import importlib.util, os, sys

BASE_DIR = os.path.dirname(__file__)
SERVER_PATH = os.path.join(BASE_DIR, 'assistente-aula-infantil', 'server.py')

spec = importlib.util.spec_from_file_location('server', SERVER_PATH)
module = importlib.util.module_from_spec(spec)
sys.modules['server'] = module
spec.loader.exec_module(module)

app = getattr(module, 'app')
