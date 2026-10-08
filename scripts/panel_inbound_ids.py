#!/usr/bin/env python3
"""ID инбаундов верхнего уровня из ответа ``/panel/api/inbounds/list``.

Зачем отдельный файл. ``preflight.sh`` и ``healthcheck.sh`` сверяют ID из
``.env``/таблицы ``nodes`` с тем, что реально есть в панели. Раньше проверка шла
грепом по сырому JSON — ``grep -q '"id":3[,}]'``. В ответе панели есть вложенный
массив ``clientStats``, и у каждого его элемента **своё** поле ``id``
(``ClientTraffic.Id``), поэтому греп находил «инбаунд 3» там, где инбаунда с
таким ID давно нет, а вместо удалённого инбаунда стояла статистика клиента.

Здесь разбирается именно структура ответа: берём только элементы ``obj`` и
печатаем их ``id`` по одному на строку.

Использование::

    curl -s -H "Authorization: Bearer $TOKEN" "$API" | python3 scripts/panel_inbound_ids.py

Код возврата 1, если ответ не JSON или это не список/объект с ``obj``.
"""

from __future__ import annotations

import json
import sys


def parse_ids(payload: object) -> list[int]:
    """ID инбаундов из разобранного ответа панели.

    Понимает и обёртку ``{"success": true, "obj": [...]}``, и голый список:
    старые панели отвечают по-разному, а проверка ID не должна из-за этого
    врать.
    """
    if isinstance(payload, dict):
        payload = payload.get("obj")
    if not isinstance(payload, list):
        return []
    ids: list[int] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        value = item.get("id")
        if isinstance(value, bool) or value is None:
            continue
        try:
            ids.append(int(value))
        except (TypeError, ValueError):
            continue
    return ids


def main() -> int:
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw)
    except Exception:  # noqa: BLE001 - на входе может быть HTML ошибки, а не JSON
        print("ответ панели не JSON", file=sys.stderr)
        return 1
    for inbound_id in parse_ids(payload):
        print(inbound_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
