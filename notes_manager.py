"""notes_manager.py — Gestión avanzada de notas.

Mejoras (Apple Notes / Notion / Todoist / Evernote):
- Pin/favorito (fijar arriba)
- Archivar (sin eliminar)
- Estados: pending, done, sent_to_claude
- Importancia: alta/media/baja (verde/naranja/rojo)
- Tags con color
- Búsqueda en título/contenido/tags
- Export markdown
- Plantilla automática para envío a Claude
"""
import uuid
from datetime import datetime
import config
import json_atomic


IMPORTANCE = {"verde", "naranja", "rojo"}
STATUSES = {"pending", "done", "sent_to_claude", "archived"}

CLAUDE_TEMPLATE_SUFFIX = """

---
📋 **IMPORTANTE — tareas automáticas para Claude:**

1. **Comprueba bien lo que hiciste**: revisa línea por línea el código modificado, que no haya errores de sintaxis ni lógica.
2. **Dame un reporte de lo hecho**: enumera cambios en backend y frontend, archivos tocados, líneas aproximadas.
3. **Comprueba que funciona**: realiza las pruebas necesarias (curl, imports, lanzar servidor, verificar endpoints). Muéstrame los resultados.
4. **Aconséjame mejoras**: busca implementaciones de otros bots de venta de entradas/tickets (Ticketmaster, AXS, SeatGeek, StubHub, Ticombo, Viagogo, etc.) y recomienda 3-5 mejoras concretas con justificación.
"""


def _path():
    return config.ruta("notes.json")


def _now():
    return datetime.now().isoformat()


def _migrate(n):
    """Retrocompatibilidad: añade campos nuevos a notas antiguas."""
    n.setdefault("importance", "naranja")
    n.setdefault("status", "pending")
    n.setdefault("pinned", False)
    n.setdefault("archived", False)
    n.setdefault("tags", [])
    n.setdefault("sent_to_claude_at", None)
    return n


def _load():
    d = json_atomic.load(_path(), default={"notes": []})
    d["notes"] = [_migrate(n) for n in d.get("notes", [])]
    return d


def list_notes(search="", importance=None, status=None,
               pinned=None, archived=False, include_archived=False,
               sort="pinned_recent"):
    """Lista y filtra notas. Por defecto excluye archivadas."""
    d = _load()
    items = d["notes"]

    if not include_archived and archived is False:
        items = [n for n in items if not n.get("archived")]
    elif archived is True:
        items = [n for n in items if n.get("archived")]

    if pinned is True:
        items = [n for n in items if n.get("pinned")]

    if importance and importance in IMPORTANCE:
        items = [n for n in items if n.get("importance") == importance]

    if status and status in STATUSES:
        items = [n for n in items if n.get("status") == status]

    q = (search or "").lower().strip()
    if q:
        def matches(n):
            blob = (n.get("title", "") + " " + n.get("content", "") + " " +
                    " ".join(n.get("tags", []))).lower()
            return q in blob
        items = [n for n in items if matches(n)]

    if sort == "pinned_recent":
        items.sort(key=lambda n: (
            0 if n.get("pinned") else 1,
            -(_to_ts(n.get("updated_at", n.get("created_at", ""))))
        ))
    elif sort == "recent":
        items.sort(key=lambda n: n.get("updated_at", ""), reverse=True)
    elif sort == "oldest":
        items.sort(key=lambda n: n.get("created_at", ""))
    elif sort == "importance":
        order = {"verde": 3, "naranja": 2, "rojo": 1}
        items.sort(key=lambda n: (
            0 if n.get("pinned") else 1,
            -order.get(n.get("importance", "naranja"), 0)
        ))
    elif sort == "alpha":
        items.sort(key=lambda n: (n.get("title") or "").lower())

    return items


def _to_ts(iso):
    try:
        return datetime.fromisoformat(iso).timestamp()
    except Exception:
        return 0


def add_note(title, content, tags=None, importance="naranja"):
    d = _load()
    if importance not in IMPORTANCE:
        importance = "naranja"
    n = {
        "id": uuid.uuid4().hex[:8],
        "title": title,
        "content": content,
        "tags": tags or [],
        "importance": importance,
        "status": "pending",
        "pinned": False,
        "archived": False,
        "created_at": _now(),
        "updated_at": _now(),
        "sent_to_claude_at": None,
    }
    d["notes"].append(n)
    json_atomic.save(_path(), d)
    return n


def get_note(nid):
    for n in _load()["notes"]:
        if n["id"] == nid:
            return n
    return None


def update_note(nid, patch):
    """Actualiza campos válidos de la nota (title, content, tags, importance)."""
    d = _load()
    allowed = {"title", "content", "tags", "importance", "status"}
    clean = {k: v for k, v in (patch or {}).items() if k in allowed}
    if "importance" in clean and clean["importance"] not in IMPORTANCE:
        clean.pop("importance")
    if "status" in clean and clean["status"] not in STATUSES:
        clean.pop("status")
    for n in d["notes"]:
        if n["id"] == nid:
            n.update(clean)
            n["updated_at"] = _now()
            json_atomic.save(_path(), d)
            return n
    return None


def delete_note(nid):
    d = _load()
    before = len(d["notes"])
    d["notes"] = [n for n in d["notes"] if n["id"] != nid]
    json_atomic.save(_path(), d)
    return len(d["notes"]) < before


def toggle_pin(nid):
    d = _load()
    for n in d["notes"]:
        if n["id"] == nid:
            n["pinned"] = not n.get("pinned", False)
            n["updated_at"] = _now()
            json_atomic.save(_path(), d)
            return n
    return None


def toggle_archive(nid):
    d = _load()
    for n in d["notes"]:
        if n["id"] == nid:
            n["archived"] = not n.get("archived", False)
            n["updated_at"] = _now()
            if n["archived"]:
                n["pinned"] = False
            json_atomic.save(_path(), d)
            return n
    return None


def duplicate_note(nid):
    orig = get_note(nid)
    if not orig:
        return None
    return add_note(
        title=(orig.get("title", "") or "") + " (copia)",
        content=orig.get("content", ""),
        tags=list(orig.get("tags", [])),
        importance=orig.get("importance", "naranja"),
    )


def mark_sent_to_claude(nid):
    d = _load()
    for n in d["notes"]:
        if n["id"] == nid:
            n["sent_to_claude_at"] = _now()
            n["status"] = "sent_to_claude"
            n["updated_at"] = _now()
            json_atomic.save(_path(), d)
            return n
    return None


def build_claude_prompt(nid):
    """Construye el prompt a copiar/enviar a Claude con sufijo de tareas automáticas."""
    n = get_note(nid)
    if not n:
        return None
    title = n.get("title", "").strip()
    content = n.get("content", "").strip()
    imp = n.get("importance", "naranja")
    tags = n.get("tags", []) or []
    imp_label = {"verde": "🟢 Alta", "naranja": "🟠 Media", "rojo": "🔴 Baja"}.get(imp, imp)

    header = f"# 📝 Nota: {title}" if title else "# 📝 Nota"
    meta = f"**Prioridad:** {imp_label}"
    if tags:
        meta += f"  ·  **Etiquetas:** {', '.join('#' + t for t in tags)}"

    body_parts = [header, "", meta, ""]
    if content:
        body_parts.append(content)
    body_parts.append(CLAUDE_TEMPLATE_SUFFIX)
    return "\n".join(body_parts)


def export_note_markdown(nid):
    n = get_note(nid)
    if not n:
        return None
    title = n.get("title", "").strip() or "(sin título)"
    imp = {"verde": "Alta", "naranja": "Media", "rojo": "Baja"}.get(n.get("importance", "naranja"), "")
    tags = " ".join("#" + t for t in n.get("tags", []) or [])
    head = f"# {title}\n"
    meta = []
    if imp:
        meta.append(f"**Prioridad:** {imp}")
    if tags:
        meta.append(f"**Tags:** {tags}")
    meta.append(f"**Creada:** {(n.get('created_at') or '').replace('T', ' ')[:19]}")
    meta.append(f"**Actualizada:** {(n.get('updated_at') or '').replace('T', ' ')[:19]}")
    return head + "\n" + "  \n".join(meta) + "\n\n" + (n.get("content") or "")


def get_stats():
    d = _load()
    items = d["notes"]
    active = [n for n in items if not n.get("archived")]
    return {
        "total": len(items),
        "active": len(active),
        "archived": sum(1 for n in items if n.get("archived")),
        "pinned": sum(1 for n in active if n.get("pinned")),
        "pending": sum(1 for n in active if n.get("status") == "pending"),
        "sent_to_claude": sum(1 for n in active if n.get("status") == "sent_to_claude"),
        "done": sum(1 for n in active if n.get("status") == "done"),
        "alta": sum(1 for n in active if n.get("importance") == "verde"),
        "media": sum(1 for n in active if n.get("importance") == "naranja"),
        "baja": sum(1 for n in active if n.get("importance") == "rojo"),
    }
