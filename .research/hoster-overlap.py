#!/usr/bin/env python3
"""Сколько /24 у хостера реально попадает в белый список.

Зачем. Вход в белые списки — это лотерея при покупке VPS: IP выдаёт хостер, и
шанс попасть в разрешённую /24 зависит от того, сколько у этого хостера таких
/24 вообще есть. До сих пор это оценивалось на глаз («у Selectel 258 /24»).
Здесь считается точно, по обеим базам, до того как заплачены деньги.

Источник префиксов — RIPEstat announced-prefixes (то, что реально анонсируется,
а не заявлено в whois). Базы берутся из кэша scripts/whitelist_check.sh.

Запуск:
    .venv/bin/python .research/hoster-overlap.py
    .venv/bin/python .research/hoster-overlap.py --asn AS49505 --show 12
"""

from __future__ import annotations

import argparse
import bisect
import ipaddress
import json
import os
import urllib.request

CACHE = os.path.expanduser("~/.cache/kometa-whitelist")
RIPESTAT = "https://stat.ripe.net/data/announced-prefixes/data.json?resource={asn}"
RIPESTAT_CACHE = "/tmp/ripestat-cache"

#: Хостеры, которые реально продают VPS с безлимитом (см. вводную 5).
HOSTERS: list[tuple[str, str, str]] = [
    ("AS49505", "Selectel (и Vscale/VDS)", "200 ₽ + IP 189,57 ₽, безлимит c оговоркой 5–20 ТБ"),
    ("AS197695", "REG.RU / Рег.облако", "560,57 ₽ + IP 187,38 ₽, безлимит письменно"),
    ("AS57724", "DDoS-Guard", "продают VPS, 7 плотных /24"),
    ("AS210079", "EuroByte", "от 290 ₽, безлимит"),
    ("AS9123", "TimeWeb", "900 ₽ — НАШ СЕРВЕР, проверено: 0 /24"),
    ("AS210756", "EdgeCenter", "CDN + VPS"),
    ("AS201706", "ServicePipe", "8 плотных /24"),
    ("AS34879", "Совр. сетевые технологии", "9 плотных /24"),
    ("AS204601", "RUVDS", "от 130 ₽, безлимит"),
]


def load_cidr_ranges() -> list[tuple[int, int]]:
    """CIDR-база → отсортированный список слитых диапазонов целых чисел."""
    ranges: list[tuple[int, int]] = []
    with open(f"{CACHE}/cidrwhitelist.txt") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                net = ipaddress.ip_network(line, strict=False)
            except ValueError:
                continue
            if net.version != 4:
                continue
            ranges.append((int(net.network_address), int(net.broadcast_address)))
    ranges.sort()
    merged: list[tuple[int, int]] = []
    for lo, hi in ranges:
        if merged and lo <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], hi))
        else:
            merged.append((lo, hi))
    return merged


def load_rewl_map() -> dict[str, int]:
    """rewl → {сеть /24: сколько подтверждённых адресов в ней}."""
    counts: dict[str, int] = {}
    with open(f"{CACHE}/ru.verified.yaml") as fh:
        for line in fh:
            line = line.strip()
            if not line.startswith("- ip: "):
                continue
            ip = line[6:].strip()
            try:
                net24 = str(ipaddress.ip_network(f"{ip}/24", strict=False))
            except ValueError:
                continue
            counts[net24] = counts.get(net24, 0) + 1
    return counts


def in_cidr(starts: list[int], merged: list[tuple[int, int]], lo: int, hi: int) -> bool:
    """Целиком ли диапазон /24 внутри слитого разрешённого диапазона."""
    if not merged:
        return False
    idx = bisect.bisect_right(starts, lo) - 1
    if idx < 0:
        return False
    return merged[idx][1] >= hi


def announced_prefixes(asn: str) -> list[str]:
    os.makedirs(RIPESTAT_CACHE, exist_ok=True)
    path = os.path.join(RIPESTAT_CACHE, f"{asn}.json")
    if not os.path.exists(path):
        url = RIPESTAT.format(asn=asn)
        with urllib.request.urlopen(url, timeout=60) as resp:
            data = resp.read()
        with open(path, "wb") as fh:
            fh.write(data)
    with open(path) as fh:
        payload = json.load(fh)
    return [p["prefix"] for p in payload["data"]["prefixes"]]


def to_24(prefixes: list[str]) -> list[str]:
    out: set[str] = set()
    for p in prefixes:
        try:
            net = ipaddress.ip_network(p, strict=False)
        except ValueError:
            continue
        if net.version != 4 or net.prefixlen > 24:
            continue
        for sub in net.subnets(new_prefix=24):
            out.add(str(sub))
    return sorted(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--asn", action="append", default=[], help="проверить только этот ASN")
    ap.add_argument("--show", type=int, default=5, help="сколько плотных /24 показать на хостер")
    args = ap.parse_args()

    merged = load_cidr_ranges()
    starts = [m[0] for m in merged]
    rewl = load_rewl_map()
    print(f"CIDR-база: {len(merged)} слитых диапазонов. rewl: {len(rewl)} подтверждённых /24.")
    print()

    targets = [h for h in HOSTERS if not args.asn or h[0] in args.asn]
    header = f"{'AS':<10} {'Хостер':<28} {'/24':>5} {'CIDR':>5} {'rewl':>5} {'в любой':>8} {'доля':>7}"
    print(header)
    print("-" * len(header))
    rows = []
    for asn, name, note in targets:
        try:
            prefixes = announced_prefixes(asn)
        except Exception as exc:  # noqa: BLE001 — исследовательский скрипт
            print(f"{asn:<10} {name:<28} ошибка RIPEstat: {exc}")
            continue
        nets24 = to_24(prefixes)
        cidr_hits, rewl_hits, any_hits = [], [], []
        for n in nets24:
            net = ipaddress.ip_network(n)
            lo, hi = int(net.network_address), int(net.broadcast_address)
            c = in_cidr(starts, merged, lo, hi)
            r = n in rewl
            if c:
                cidr_hits.append(n)
            if r:
                rewl_hits.append(n)
            if c or r:
                any_hits.append(n)
        share = (100.0 * len(any_hits) / len(nets24)) if nets24 else 0.0
        print(
            f"{asn:<10} {name:<28} {len(nets24):>5} {len(cidr_hits):>5} {len(rewl_hits):>5} "
            f"{len(any_hits):>8} {share:>6.1f}%"
        )
        rows.append((share, asn, name, note, any_hits, rewl))

    print()
    print("Топ по доле (чем выше — тем вероятнее случайно попасть в разрешённую /24):")
    for share, asn, name, note, any_hits, rewl in sorted(rows, reverse=True):
        print(f"  {share:5.1f}%  {asn} {name} — {note}")
        best = sorted(any_hits, key=lambda n: -rewl.get(n, 0))[: args.show]
        for n in best:
            confirmed = rewl.get(n, 0)
            # В rewl один и тот же адрес встречается дважды (порты 80 и 443),
            # поэтому «подтверждено N записей», а не «N из 256 адресов».
            tag = f"{confirmed} подтверждённых записей замером" if confirmed else "есть только в CIDR"
            print(f"        {n:<18} {tag}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
