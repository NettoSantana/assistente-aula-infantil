
from dataclasses import dataclass
from typing import Dict, Any
import random

@dataclass
class Activity:
    enunciado: str
    gabarito: Any
    materia: str  # "matematica" | "portugues"

def math_activity(level: int) -> Activity:
    if level <= 3:
        a, b = random.randint(1, 9), random.randint(1, 9)
        return Activity(enunciado=f"Calcule: {a} + {b} = ?", gabarito=a+b, materia="matematica")
    elif level <= 6:
        a, b = random.randint(2, 9), random.randint(1, 9)
        return Activity(enunciado=f"Calcule: {a} × {b} = ?", gabarito=a*b, materia="matematica")
    else:
        a, b = random.randint(10, 99), random.randint(10, 99)
        return Activity(enunciado=f"Calcule: {a} + {b} = ?", gabarito=a+b, materia="matematica")

def portugues_activity(level: int) -> Activity:
    if level <= 3:
        pares = [
            ("Complete com *c* ou *ç*: _a__a", "c"),  # casa
            ("Escreva o plural: *flor* → ?", "flores"),
            ("Escolha a forma correta: *mas* ou *mais* para oposição?", "mas"),
        ]
        enunciado, gab = random.choice(pares)
        return Activity(enunciado=enunciado, gabarito=gab, materia="portugues")
    elif level <= 6:
        pares = [
            ("Complete: *porque, por que, porquê ou por quê?* — 'Não fui ___ estava doente.'", "porque"),
            ("Acentue corretamente: *voce, cafe, ideia*", "você, café, ideia"),
            ("Classifique: 'O gato dorme.' — sujeito simples ou composto?", "simples"),
        ]
        enunciado, gab = random.choice(pares)
        return Activity(enunciado=enunciado, gabarito=gab, materia="portugues")
    else:
        pares = [
            ("Sinônimo de *tranquilo* (um):", "calmo"),
            ("Identifique o verbo na frase: 'Eles *brincaram* no parque.'", "brincaram"),
            ("Pontue: 'quando cheguei ela sorriu'", "Quando cheguei, ela sorriu."),
        ]
        enunciado, gab = random.choice(pares)
        return Activity(enunciado=enunciado, gabarito=gab, materia="portugues")

def build_daily_activity(user: Dict[str, Any]) -> Dict[str, Activity]:
    lvl_mat = user["levels"]["matematica"]
    lvl_por = user["levels"]["portugues"]
    act_m = math_activity(lvl_mat)
    act_p = portugues_activity(lvl_por)
    user["pending"] = {"matematica": act_m.__dict__, "portugues": act_p.__dict__}
    return {"matematica": act_m.__dict__, "portugues": act_p.__dict__}

def check_answer(user: Dict[str, Any], answer: str) -> str:
    pend = user.get("pending", {})
    if not pend:
        return "Não há exercícios pendentes. Envie *iniciar*."

    for materia in ("matematica", "portugues"):
        if materia in pend:
            gabarito = pend[materia]["gabarito"]
            correct = False
            if isinstance(gabarito, (int, float)):
                try:
                    correct = (float(answer) == float(gabarito))
                except Exception:
                    correct = False
            else:
                correct = (answer.strip().lower() == str(gabarito).strip().lower())

            if correct:
                user["history"][materia].append({"q": pend[materia]["enunciado"], "a": answer})
                user["levels"][materia] += 1
                del user["pending"][materia]
                return f"✅ *{materia.title()}* correta! Nível agora: {user['levels'][materia]}"
            else:
                return f"❌ Resposta incorreta para *{materia}*. Tente novamente."

    return "Tudo corrigido por hoje!"
