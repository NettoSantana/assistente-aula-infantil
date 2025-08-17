
from typing import Dict, Any

def get_today_reading_goal(user: Dict[str, Any]) -> str:
    livro = user.get("reading", {}).get("titulo", "Livro escolhido")
    pags_dia = user.get("reading", {}).get("paginas_dia", 6)
    dia = len(user["history"]["leitura"]) + 1
    return f"{livro} — leia ~{pags_dia} páginas (Dia {dia}). Ao concluir, envie: *leitura ok* com um resumo/áudio."

def check_reading_submission(user: Dict[str, Any]):
    user["history"]["leitura"].append({"ok": True})
    return True, "📖 Leitura registrada! Continue assim."
