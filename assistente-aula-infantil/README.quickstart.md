
# Assistente de Aula Infantil (Base)

## Rodar localmente
1) Crie e ative um venv
2) pip install -r requirements.txt
3) python -m waitress --listen=0.0.0.0:8080 server:app
4) GET http://localhost:8080/admin/ping

## Deploy Railway (passo seguinte)
- Configure o Start Command: `waitress-serve --port=$PORT server:app`
- Adicione variáveis de ambiente da `.env.example`
