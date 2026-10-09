#!/usr/bin/env bash
# Замер на Билайне с той же точки. Запускать с Mac, подключённого к точке доступа
# телефона с SIM Билайна. Пишет всё на диск и НЕ зависит от сессии: если у Билайна
# включён режим ограничений, связь с моделью оборвётся, а данные сохранятся.
set -u
cd "$(dirname "$0")/.." || exit 2
echo "=== ОПРЕДЕЛЕНИЕ ОПЕРАТОРА ПО ВНЕШНЕМУ АДРЕСУ ==="
for svc in "https://ipinfo.io/json" "https://ifconfig.co/json" "https://api.myip.com" "https://2ip.ru/json/"; do
    out="$(curl -s -m 8 "$svc" 2>/dev/null | tr -d '\n' | head -c 220)"
    if [ -n "$out" ]; then echo "  $svc → $out"; fi
done
echo
./scripts/lte_batch.sh \
    --targets .research/gate0-targets-engels.txt \
    --operator BEELINE --region "Саратов (та же точка)" \
    --label "Билайн Саратов (та же точка)" \
    --log ".research/batch-beeline-$(date +%Y-%m-%d).log" \
    --report .research/probes.csv
