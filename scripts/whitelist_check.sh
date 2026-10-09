#!/usr/bin/env bash
# Проверка адреса против публичного «белого списка»: может ли этот IP вообще проходить.
#
# Это ФИЛЬТР, а не доказательство. Скрипт отвечает на вопрос «стоит ли тратить время
# на замер этого адреса», и только. Настоящий ответ даёт лишь замер с телефона
# (scripts/lte_probe.sh) в сети оператора под включённым режимом ограничений.
#
# Почему нельзя верить файлу на 100 %:
#   * это снимок ОДНОГО оператора, ОДНОГО города и ОДНОЙ даты (МегаФон, Иваново, 06.08.2026);
#   * состав списка у каждого оператора свой и различается по региону, вышке и MVNO;
#   * файл статичен и не обновляется;
#   * «подсеть в списке» != «ваш адрес в списке»: зависит от того, куда вас посадят.
#
# Примеры:
#   ./scripts/whitelist_check.sh 146.255.188.12
#   ./scripts/whitelist_check.sh 77.221.147.230 150.241.106.75
#   ./scripts/whitelist_check.sh --file my-nodes.txt      # по адресу в строке, # — комментарий
#   ./scripts/whitelist_check.sh --refresh --file nodes.txt   # перекачать базы
#   ./scripts/whitelist_check.sh --ip 203.0.113.7 --json
#
# Откуда данные (ДВЕ независимые базы, они расходятся — и это важно):
#   * CIDR-список (30 228 подсетей): hxehex/russia-mobile-internet-whitelist.
#     Это ЗАЯВЛЕННЫЙ список подсетей.
#   * датасет rewl (72 076 ПОДТВЕРЖДЁННЫХ живых адресов): openlibrecommunity/rewl.
#     Это ИЗМЕРЕННАЯ реальность: адреса, реально ответившие под ограничениями.
#   Базы НЕ совпадают: например 176.57.66.0/24 подтверждён почти целиком (250 из 256),
#   но в CIDR-файл не входит. Поэтому проверяем обе и показываем, где совпало.
#
# Код возврата: 0 — хотя бы один адрес признан перспективным; 1 — ни один; 2 — ошибка.

set -u

CACHE_DIR="${WHITELIST_CACHE:-${HOME}/.cache/kometa-whitelist}"
CIDR_URL="https://raw.githubusercontent.com/hxehex/russia-mobile-internet-whitelist/main/cidrwhitelist.txt"
REWL_URL="https://raw.githubusercontent.com/openlibrecommunity/rewl/master/data/pub/ru.verified.yaml"
CIDR_FILE="${CACHE_DIR}/cidrwhitelist.txt"
REWL_FILE="${CACHE_DIR}/ru.verified.yaml"

IP_ARGS=()
FILE=""
REFRESH=0
JSON=0

if [[ -t 1 ]]; then
    C_RED=$'\033[0;31m'; C_GREEN=$'\033[0;32m'; C_YELLOW=$'\033[0;33m'
    C_BLUE=$'\033[0;34m'; C_DIM=$'\033[2m'; C_OFF=$'\033[0m'
else
    C_RED=""; C_GREEN=""; C_YELLOW=""; C_BLUE=""; C_DIM=""; C_OFF=""
fi
log()  { printf '%s[•]%s %s\n' "$C_BLUE" "$C_OFF" "$*" >&2; }
die()  { printf '%s[✗]%s %s\n' "$C_RED" "$C_OFF" "$*" >&2; exit 2; }

usage() {
    sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'
    exit 0
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --ip)      IP_ARGS+=("${2:?}"); shift 2 ;;
        --file)    FILE="${2:?}";       shift 2 ;;
        --refresh) REFRESH=1;           shift ;;
        --json)    JSON=1;              shift ;;
        -h|--help) usage ;;
        -*)        die "Неизвестный флаг: $1 (см. --help)" ;;
        *)         IP_ARGS+=("$1");     shift ;;
    esac
done

[[ ${#IP_ARGS[@]} -gt 0 || -n "$FILE" ]] || usage

mkdir -p "$CACHE_DIR"

fetch() {  # fetch <url> <dest>
    local url="$1" dest="$2"
    if [[ -s "$dest" && "$REFRESH" -eq 0 ]]; then
        return 0
    fi
    log "Качаю $(basename "$dest")…"
    if ! curl -sL --max-time 60 -o "${dest}.tmp" "$url"; then
        [[ -s "$dest" ]] && { log "Не скачалось, беру кэш"; rm -f "${dest}.tmp"; return 0; }
        die "Не удалось скачать $url и кэша нет"
    fi
    [[ -s "${dest}.tmp" ]] || die "Пустой ответ от $url"
    mv "${dest}.tmp" "$dest"
}

fetch "$CIDR_URL" "$CIDR_FILE"
fetch "$REWL_URL" "$REWL_FILE"

# Собираем список адресов
IPS=()
for ip in "${IP_ARGS[@]:-}"; do
    [[ -n "$ip" ]] && IPS+=("$ip")
done
if [[ -n "$FILE" ]]; then
    [[ -r "$FILE" ]] || die "Не читается файл: $FILE"
    while IFS= read -r line; do
        line="${line%%#*}"
        for tok in $line; do
            [[ -n "$tok" ]] && IPS+=("$tok")
        done
    done < "$FILE"
fi
[[ ${#IPS[@]} -gt 0 ]] || die "Не передано ни одного адреса"

CIDR_FILE="$CIDR_FILE" REWL_FILE="$REWL_FILE" JSON="$JSON" \
python3 - "${IPS[@]}" <<'PY'
import ipaddress, json, os, re, sys, collections

cidr_path = os.environ["CIDR_FILE"]
rewl_path = os.environ["REWL_FILE"]
as_json = os.environ.get("JSON") == "1"

nets = []
for ln in open(cidr_path, encoding="utf-8", errors="replace"):
    ln = ln.strip()
    if not ln or ln.startswith("#"):
        continue
    try:
        nets.append(ipaddress.ip_network(ln, strict=False))
    except ValueError:
        pass
if not nets:
    sys.exit("CIDR-файл пуст или нечитаем")

# плотность: сколько ПОДТВЕРЖДЁННЫХ адресов в каждой /24 (из датасета rewl).
# В файле каждый адрес встречается по разу на каждый открытый порт (80 и 443),
# поэтому ОБЯЗАТЕЛЬНО дедуплицируем — иначе плотность удваивается и врёт.
seen_ips = set()
density = collections.Counter()
try:
    for ln in open(rewl_path, encoding="utf-8", errors="replace"):
        m = re.match(r"\s*-?\s*ip:\s*(\S+)", ln)
        if not m:
            continue
        raw_ip = m.group(1)
        if raw_ip in seen_ips:
            continue
        seen_ips.add(raw_ip)
        try:
            density[str(ipaddress.ip_network(raw_ip + "/24", strict=False))] += 1
        except ValueError:
            pass
except OSError:
    pass

# /24, в которых есть подтверждённые адреса — вторая база проверки.
verified_nets = {ipaddress.ip_network(k) for k, v in density.items() if v > 0}


def cidr_match(addr):
    """Самая длинная подсеть из CIDR-файла, содержащая адрес (или None)."""
    hit = None
    for n in nets:
        if n.version == addr.version and addr in n:
            if hit is None or n.prefixlen > hit.prefixlen:
                hit = n
    return hit


results = []
for raw in sys.argv[1:]:
    try:
        addr = ipaddress.ip_address(raw)
    except ValueError:
        results.append({"ip": raw, "valid": False, "reason": "не похоже на IP"})
        continue
    hit = cidr_match(addr)
    net24 = ipaddress.ip_network(f"{addr}/24", strict=False)
    d = density.get(str(net24), 0)
    in_verified = net24 in verified_nets
    results.append({
        "ip": raw, "valid": True,
        "in_cidr": hit is not None,
        "matched_cidr": str(hit) if hit else "",
        "in_verified_24": in_verified,
        "net24": str(net24),
        "density": d,
        # перспективным считаем адрес, который либо в CIDR-списке, либо в плотной
        # подтверждённой /24 — вторая база бывает полнее первой
        "promising": bool(hit) or d >= 50,
    })

if as_json:
    print(json.dumps(results, ensure_ascii=False, indent=2))
    raise SystemExit(0 if any(r.get("promising") for r in results) else 1)

G, R, Y, D, O = "\033[0;32m", "\033[0;31m", "\033[0;33m", "\033[2m", "\033[0m"
print(f"\n{D}База 1 (CIDR-список): {len(nets)} подсетей. "
      f"База 2 (rewl, подтверждено замером): {len(seen_ips)} адресов "
      f"в {len(verified_nets)} подсетях /24.{O}\n")

for r in results:
    if not r.get("valid"):
        print(f"  {R}✗{O} {r['ip']:20} — {r['reason']}")
        continue
    d = r["density"]
    src = []
    if r["in_cidr"]:
        src.append(f"CIDR {r['matched_cidr']}")
    if r["in_verified_24"]:
        src.append(f"{d} подтверждённых адресов в /24")
    if not src:
        print(f"  {R}✗{O} {r['ip']:20} НЕ найден ни в одной базе")
        print(f"      {D}Ни в CIDR-списке, ни среди подтверждённых замером. "
              f"Брать не стоит.{O}")
        continue
    if d >= 200:
        mark, verdict = f"{G}✓✓{O}", "плотная подтверждённая /24 — высокий шанс"
    elif d >= 50:
        mark, verdict = f"{Y}✓{O}", "умеренная плотность — замер обязателен"
    elif r["in_cidr"] and d > 0:
        mark, verdict = f"{Y}✓{O}", "в списке, но соседи почти не отвечали — шанс низкий"
    elif r["in_cidr"]:
        mark, verdict = f"{Y}?{O}", ("в CIDR-списке, но подтверждённых адресов рядом НЕТ — "
                                     "скорее всего не сработает")
    else:
        mark, verdict = f"{Y}?{O}", "рядом единичные подтверждённые адреса — шанс низкий"
    print(f"  {mark} {r['ip']:20} {' + '.join(src)}")
    print(f"      {D}{verdict}{O}")

print(f"""
{D}Что это значит и чего это не значит:{O}
  • Зелёный результат — НЕ доказательство. Это фильтр: адрес стоит проверять замером.
  • Красный результат — сильный минус, но не абсолют: базы сняты на ОДНОМ операторе
    (МегаФон) в ОДНОМ городе (Иваново) 06.08.2026. У вашего оператора список свой.
  • Базы расходятся между собой: измеренная (rewl) местами ШИРЕ заявленной (CIDR).
    Адрес, которого нет в CIDR, но рядом с которым 200+ подтверждённых соседей,
    проверять стоит — «эффект соседа» работает по /24.
  • Решает только замер с телефона:
      LTE_OPERATOR=<оператор> LTE_REGION=<город> ./scripts/lte_probe.sh \\
          --deep --host <адрес> --deep-ports 443 --repeat 3
    Wi-Fi и VPN выключены, контроль в начале и в конце.
""")

raise SystemExit(0 if any(r.get("promising") for r in results) else 1)
PY
