#!/usr/bin/env python3
"""Разбор .research/probes.csv: есть ли в точке режим ограничений.

Зачем. Сырой CSV — это десятки строк rc, которые легко прочитать неправильно
(именно так рождаются выводы «работает», не подкреплённые данными). Скрипт
применяет ОДНО правило, сформулированное до замера:

    режим ограничений ЕСТЬ в точке  ⟺  адреса ВНЕ белого списка не проходят,
                                        а адреса В белом списке проходят.

Правило проверяется на контрольной паре и на подтверждении выхода: если выход
не был мобильным или контроль не сработал, вердикт помечается недействительным,
а не «на всякий случай принимается».

Запуск:
    .venv/bin/python .research/analyze-probes.py
    .venv/bin/python .research/analyze-probes.py --csv .research/probes.csv
"""

from __future__ import annotations

import argparse
import collections
import csv
import ipaddress
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: Цели, размеченные как «в белом списке» / «вне списка», берём из файлов батчей,
#: чтобы разметка была ровно та, что использовалась при замере.
TARGET_FILES = sorted((ROOT / ".research").glob("gate0-targets-*.txt"))

PASS_VERDICTS = {"ПРОХОДИТ"}
FAIL_VERDICTS = {"БЛОК", "ТИШИНА"}


def load_groups() -> dict[str, str]:
    """{IP: 'wl' | 'not-wl'} из файлов целей (метка содержит -NOT-wl)."""
    groups: dict[str, str] = {}
    for path in TARGET_FILES:
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.split("#")[0].strip()
            if not line:
                continue
            parts = line.split()
            if len(parts) < 2:
                continue
            label, target = parts[0], parts[1]
            if "/" in target:
                continue
            groups[target] = "not-wl" if "-NOT-wl" in label else "wl"
    return groups


def parse_dt(value: str):
    """«2026-10-09 14:02» → datetime; иначе None."""
    from datetime import datetime

    value = (value or "").strip()
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def ip_of(target: str) -> str:
    """«91.215.42.5:443» → «91.215.42.5»; IPv6 в скобках сохраняем как есть."""
    raw = target.strip()
    if raw.startswith("["):
        return raw[1:raw.index("]")] if "]" in raw else raw
    if raw.count(":") == 1:  # host:port, но не IPv6
        host, _, port = raw.rpartition(":")
        if port.isdigit():
            raw = host
    try:
        ipaddress.ip_address(raw)
        return raw
    except ValueError:
        return target.strip()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default=str(ROOT / ".research" / "probes.csv"))
    args = ap.parse_args()

    path = Path(args.csv)
    if not path.exists():
        print(f"Нет файла {path} — замер ещё не проводился.", file=sys.stderr)
        return 2

    groups = load_groups()
    rows = list(csv.DictReader(path.open(encoding="utf-8")))

    # Батчи режем ПОСЛЕДОВАТЕЛЬНО, а не по метке: 09.10.2026 часть строк с меткой
    # «МТС Саратов» была снята уже через домашний Wi-Fi после отвала точки доступа.
    # Границы батча: строка egress (пишет lte_batch.sh) либо control-end (пишет
    # lte_gate0.sh) либо пауза больше 15 минут.
    batches: list[list[dict]] = []
    cur: list[dict] = []
    prev_dt = None
    for r in rows:
        dt = parse_dt(r.get("время") or "")
        if cur and prev_dt and dt and (dt - prev_dt).total_seconds() > 900:
            batches.append(cur)
            cur = []
        cur.append(r)
        if (r.get("режим") or "") in {"egress", "control-end"}:
            batches.append(cur)
            cur = []
        prev_dt = dt or prev_dt
    if cur:
        batches.append(cur)

    if not batches:
        print("CSV пуст.")
        return 2

    for items in batches:
        label = next(((r.get("метка") or "").strip() for r in items if (r.get("метка") or "").strip()), "")
        date = (items[0].get("время") or "")[:16]
        print("=" * 74)
        print(f"Точка: {label or '(без метки)'}   время: {date}   строк: {len(items)}")
        print("-" * 74)

        # 1. Подтверждение выхода: строка egress с вердиктом ДЕЙСТВИТЕЛЕН
        egress_rows = [r for r in items if r.get("режим") == "egress"]
        start_e = next((r for r in egress_rows if str(r.get("цель", "")).startswith("start")), None)
        end_e = next((r for r in egress_rows if str(r.get("цель", "")).startswith("end")), None)
        if start_e:
            print(f"  выход начало: {start_e.get('цель')} · {start_e.get('вердикт')}")
        if end_e:
            print(f"  выход конец : {end_e.get('цель')} · {end_e.get('вердикт')}")
        if not egress_rows:
            print("  ⚠️  выход в CSV не зафиксирован (старый формат) — сверять с логом батча")
        invalid = any("НЕДЕЙСТВИТЕЛЕН" in (r.get("вердикт") or "") for r in egress_rows)

        # 2. Контроль
        controls = [r for r in items if (r.get("режим") or "").startswith("control")]
        allowed_controls = [r for r in controls if "allowed" in (r.get("группа") or "")]
        bad_controls = [r for r in allowed_controls if (r.get("вердикт") or "") != "ПРОХОДИТ"]
        last_control_ok = bool(allowed_controls) and (allowed_controls[-1].get("вердикт") or "") == "ПРОХОДИТ"
        if controls:
            print(f"  контроль: строк {len(controls)} (разрешённых {len(allowed_controls)}, "
                  f"не прошли {len(bad_controls)})")
            if bad_controls and last_control_ok:
                print("  ⚠️  часть контроля не прошла, но контроль в КОНЦЕ батча прошёл —")
                print("      сверить с логом: внутри каждого --deep свой контроль до и после")
        # 3. Цели
        wl_pass = wl_fail = nwl_pass = nwl_fail = unknown = 0
        details = []
        for r in items:
            if r.get("режим") != "deep":
                continue
            addr = ip_of(r.get("цель") or "")
            grp = groups.get(addr)
            verdict = (r.get("вердикт") or "").strip()
            if grp == "wl":
                if verdict in PASS_VERDICTS:
                    wl_pass += 1
                else:
                    wl_fail += 1
                mark = "в списке    "
            elif grp == "not-wl":
                if verdict in PASS_VERDICTS:
                    nwl_pass += 1
                else:
                    nwl_fail += 1
                mark = "ВНЕ списка  "
            else:
                unknown += 1
                mark = "не размечен "
            details.append(f"    {mark} {addr:<16} → {verdict}")

        if details:
            print("  цели:")
            for d in details:
                print(d)

        print("-" * 74)
        # 4. Вердикт по правилу
        if not allowed_controls:
            print("  ⚠️  контроль в CSV не зафиксирован — сверять с логом батча")
        if invalid:
            verdict = "НЕДЕЙСТВИТЕЛЕН — выход сменился во время батча"
        elif allowed_controls and not last_control_ok:
            verdict = ("НЕДЕЙСТВИТЕЛЕН — КОНТРОЛЬ В КОНЦЕ батча не прошёл: "
                       "сеть могла умереть во время замера, повторить")
        elif not (wl_pass or wl_fail):
            verdict = "НЕТ ДАННЫХ по адресам в белом списке"
        elif nwl_pass and wl_pass:
            verdict = ("РЕЖИМА НЕТ в этой точке — адреса ВНЕ списка проходят "
                       f"({nwl_pass} из {nwl_pass + nwl_fail})")
        elif nwl_fail and wl_pass:
            verdict = ("РЕЖИМ ЕСТЬ в этой точке — адреса вне списка закрыты, "
                       "адреса в списке проходят")
        elif nwl_pass and wl_fail:
            verdict = "ФИЛЬТР ДРУГОЙ ПРИРОДЫ — закрыты как раз адреса В списке"
        else:
            verdict = "СМЕШАННО — разбирать вручную"
        print(f"  ВЕРДИКТ: {verdict}")
        print(f"  счёт: в списке {wl_pass}✅/{wl_fail}❌ · вне списка {nwl_pass}✅/{nwl_fail}❌"
              + (f" · без разметки {unknown}" if unknown else ""))
    print("=" * 74)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
