"""instance_manager.py — Lanzador de instancias de navegador."""

INSTANCE_MODES = [
    {"id": "light",    "name": "Ligero",     "max_instances": 10, "ram_per_instance_mb": 150},
    {"id": "balanced", "name": "Balanceado", "max_instances": 5,  "ram_per_instance_mb": 400},
    {"id": "heavy",    "name": "Pesado",     "max_instances": 3,  "ram_per_instance_mb": 800},
]


def calculate_resources(mode, count):
    m = next((x for x in INSTANCE_MODES if x["id"] == mode), INSTANCE_MODES[1])
    return {
        "ram_mb": m["ram_per_instance_mb"] * count,
        "ram_gb": round(m["ram_per_instance_mb"] * count / 1024, 2),
        "max_allowed": m["max_instances"],
        "mode": m["name"],
    }


class InstanceLauncher:
    def launch(self, mode="balanced", count=1):
        resources = calculate_resources(mode, count)
        return {"ok": True, "launched": count, "resources": resources}


instance_launcher = InstanceLauncher()
