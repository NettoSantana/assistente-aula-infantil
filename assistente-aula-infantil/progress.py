
from typing import Dict, Any

def init_user_if_needed(db: Dict[str, Any], user_id: str) -> Dict[str, Any]:
    users = db.setdefault("users", {})
    if user_id not in users:
        users[user_id] = {
            "levels": {"matematica": 1, "portugues": 1},
            "history": {"matematica": [], "portugues": [], "leitura": []},
            "pending": {},
            "reading": {"titulo": "Livro base", "paginas_dia": 6},
        }
    return users[user_id]

def next_levels_for_user(user: Dict[str, Any]):
    return user["levels"]
