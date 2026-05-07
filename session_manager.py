"""session_manager.py — Estrategias de reparto de sesiones entre workers."""

SELECTION_STRATEGIES = [
    {"id": "none",        "name": "Sin reparto",              "description": "Todos los workers compiten libremente"},
    {"id": "equal",       "name": "Equitativo",               "description": "Reparto equitativo entre todos los workers"},
    {"id": "round_robin", "name": "Round-robin",              "description": "Turnos rotativos"},
    {"id": "first_wins",  "name": "Primera disponible gana",  "description": "El primero en obtener se lleva todo"},
    {"id": "burst",       "name": "Burst paralelo",           "description": "Todos a la vez, maximo spike"},
    {"id": "cascade",     "name": "Cascada",                  "description": "Uno tras otro con delay"},
    {"id": "shuffle",     "name": "Shuffle aleatorio",        "description": "Orden aleatorio"},
    {"id": "priority",    "name": "Por prioridad de cuenta",  "description": "Por success_rate de cada cuenta"},
]


class SessionManager:
    def select(self, workers, strategy="equal"):
        if not workers:
            return []
        if strategy == "first_wins":
            return [workers[0]]
        return workers


session_manager = SessionManager()


def get_strategies():
    return SELECTION_STRATEGIES
