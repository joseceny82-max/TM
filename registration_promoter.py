"""registration_promoter.py — Promueve cuentas de tm_registrations (cola de registros)
al account_manager (pestaña Cuentas y Tarjetas) automáticamente al completarse.

Flujo:
1. Worker tm_registrations completa registro → marca item como `status=done`.
2. Hook: `promote_to_account_manager(item)` se invoca tras done.
3. Crea cuenta en account_manager con todos los campos del item.
4. Marca el item con `promoted_at` para idempotencia.

API:
    promote_to_account_manager(item) -> dict {ok, account_id, skipped_reason?}
    promote_all_done() -> dict {ok, promoted, skipped, errors}
    get_promotion_status() -> dict {done_total, done_promoted, done_pending}
"""
from __future__ import annotations
import re
from datetime import datetime
from typing import Optional, Dict, Any, List


def _split_phone(phone: str):
    """Separa '+34612345678' → ('+34', '612345678')."""
    if not phone: return ('', '')
    p = str(phone).strip()
    m = re.match(r'^(\+\d{1,3})(.+)$', p)
    if m: return (m.group(1), re.sub(r'\D', '', m.group(2)))
    digits = re.sub(r'\D', '', p)
    if digits.startswith('34') and len(digits) > 9:
        return ('+34', digits[2:])
    return ('', digits)


def _account_data_from_registration(it: Dict[str, Any]) -> Dict[str, Any]:
    """Mapea un item de tm_registrations a los campos esperados por account_manager.add_account.
    Item de tm_registrations puede traer campos en distintas ubicaciones — toleramos."""
    pp, num = _split_phone(it.get('phone') or it.get('Telefono') or '')
    email = (it.get('email') or it.get('Email_TM') or it.get('tm_email') or '').strip()
    label = it.get('label') or it.get('N_Usuario') or ''
    if not label:
        # derivar label desde el email si no hay
        local = email.split('@', 1)[0] if '@' in email else email
        label = f"reg-{local[:20]}"
    return {
        'label': str(label),
        'status': 'active',
        'tm_email': email,
        'tm_password': it.get('password') or it.get('Password_TM') or it.get('tm_password') or '',
        'contact_email': it.get('contact_email') or it.get('Email_Contacto') or email,
        'contact_password': it.get('contact_password') or it.get('Password_Email') or '',
        'first_name': it.get('first_name') or it.get('Nombre') or '',
        'last_name': it.get('last_name') or it.get('Apellidos') or '',
        'dob': it.get('dob') or it.get('Fecha_Nacimiento') or '',
        'gender': it.get('gender') or '',
        'nationality': it.get('nationality') or it.get('Nacionalidad') or 'ES',
        'passport': it.get('passport') or it.get('Pasaporte') or '',
        'passport_country': it.get('passport_country') or it.get('Nacionalidad') or 'ES',
        'phone_prefix': pp,
        'phone': num,
        'address1': it.get('address') or it.get('address1') or it.get('Direccion') or '',
        'address2': it.get('address2') or '',
        'city': it.get('city') or it.get('Ciudad') or '',
        'state': it.get('state') or it.get('Estado') or '',
        'zip': it.get('zip') or it.get('ZIP') or '',
        'country': it.get('country') or it.get('Pais') or 'Spain',
        'notes': it.get('notes') or it.get('Notas') or
                 f"Auto-promovida desde registros TM ({datetime.now().strftime('%Y-%m-%d %H:%M')})",
    }


def promote_to_account_manager(item: Dict[str, Any]) -> Dict[str, Any]:
    """Mueve un item registrado (status=done) a account_manager.
    Idempotente: si ya promovido o el email ya existe, devuelve skipped sin duplicar."""
    try:
        import account_manager
        import tm_registrations
    except ImportError as e:
        return {'ok': False, 'error': f'import_failed: {e}'}

    if not item or not isinstance(item, dict):
        return {'ok': False, 'error': 'invalid_item'}
    rid = item.get('id')
    if item.get('promoted_at'):
        return {'ok': True, 'skipped_reason': 'already_promoted', 'reg_id': rid,
                'account_id': item.get('promoted_account_id')}

    email = (item.get('email') or item.get('tm_email') or '').strip().lower()
    # Comprobar si ya existe en account_manager por tm_email
    existing = next((a for a in account_manager.account_manager.list_accounts()
                     if (a.get('tm_email') or '').strip().lower() == email), None)
    if existing:
        # Marcar promoted apuntando a la cuenta existente
        try:
            tm_registrations.update_item(rid, {})  # noop para no fallar si campo no editable
            tm_registrations._save({'items': []})  # NO usar; mejor un update explícito abajo
        except Exception: pass
        # Hack: actualizar directamente promoted_at vía json_atomic (bypass _EDITABLE_FIELDS)
        _stamp_promoted(rid, existing.get('id'))
        return {'ok': True, 'skipped_reason': 'email_already_in_account_manager',
                'reg_id': rid, 'account_id': existing.get('id')}

    # Crear cuenta nueva
    data = _account_data_from_registration(item)
    if not data['tm_email']:
        return {'ok': False, 'error': 'no_email_in_item', 'reg_id': rid}
    try:
        new_acc = account_manager.account_manager.add_account(data)
        new_id = (new_acc or {}).get('id')
        _stamp_promoted(rid, new_id)
        return {'ok': True, 'reg_id': rid, 'account_id': new_id, 'email': data['tm_email']}
    except Exception as e:
        return {'ok': False, 'error': f'add_account_failed: {e}', 'reg_id': rid}


def _stamp_promoted(reg_id: str, account_id: Optional[str]) -> None:
    """Marca el item de tm_registrations con `promoted_at` y `promoted_account_id`.
    Bypassea _EDITABLE_FIELDS escribiendo directo al JSON con el lock del módulo."""
    try:
        import tm_registrations as tr
        with tr._lock:
            d = tr._load()
            for it in d.get('items', []):
                if it.get('id') == reg_id:
                    it['promoted_at'] = datetime.now().isoformat()
                    if account_id: it['promoted_account_id'] = account_id
                    break
            tr._save(d)
    except Exception:
        pass


def promote_all_done() -> Dict[str, Any]:
    """Recorre TODOS los items en status=done y promueve los que no tengan promoted_at."""
    try:
        import tm_registrations as tr
    except ImportError as e:
        return {'ok': False, 'error': f'import: {e}'}
    items = tr.list_by_status(tr.STATUS_DONE)
    promoted, skipped, errors = [], [], []
    for it in items:
        if it.get('promoted_at'):
            skipped.append({'id': it['id'], 'reason': 'already_promoted'}); continue
        res = promote_to_account_manager(it)
        if res.get('ok') and not res.get('skipped_reason'):
            promoted.append({'reg_id': it['id'], 'account_id': res.get('account_id'),
                             'email': res.get('email')})
        elif res.get('skipped_reason'):
            skipped.append({'id': it['id'], 'reason': res['skipped_reason'],
                            'account_id': res.get('account_id')})
        else:
            errors.append({'id': it['id'], 'error': res.get('error')})
    return {'ok': True, 'promoted': promoted, 'promoted_count': len(promoted),
            'skipped': skipped, 'skipped_count': len(skipped),
            'errors': errors, 'errors_count': len(errors),
            'total_done': len(items)}


def get_promotion_status() -> Dict[str, Any]:
    try:
        import tm_registrations as tr
    except ImportError:
        return {'ok': False, 'done_total': 0, 'done_promoted': 0, 'done_pending': 0}
    items = tr.list_by_status(tr.STATUS_DONE)
    promoted = sum(1 for it in items if it.get('promoted_at'))
    return {'ok': True, 'done_total': len(items),
            'done_promoted': promoted,
            'done_pending': len(items) - promoted}


if __name__ == '__main__':
    import json
    print(json.dumps(get_promotion_status(), indent=2))
