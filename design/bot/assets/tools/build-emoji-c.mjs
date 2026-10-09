// Генератор боевого набора фирменных эмодзи (конструкция C).
//
// Запуск:  node design/bot/assets/tools/build-emoji-c.mjs <каталог-вывода>
// Затем каждый SVG растеризуется в PNG 100×100 через cairosvg (см. tools/README.md).
//
// Конструкция C — выбор владельца (08.10.2026, сравнение в Telegram):
//   • глиф белый, штрих 2.2 при сетке 100 (≈11 px в строке 20 px — читается);
//   • вокруг тонкое кольцо с фирменным градиентом изумруд → лайм, opacity 0.55;
//   • никакого тёмного диска: эмодзи садится на любой фон — тёмный и светлый.
//
// Почему глифы рисуются здесь, а не берутся из набора иконок «как есть»: набор
// иконок сделан для 24 px в интерфейсе, у него stroke 1.75 и тонкая детализация.
// В эмодзи кадр 100 px, а показывается он ~20 px — детали сливаются, поэтому
// геометрия та же (24-сетка), но штрих толще, а лишние детали убраны.

import { mkdirSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';

const OUT = process.argv[2] || '/tmp/kometa-emoji-c';
const BRAND = ['#2BC57F', '#45E698', '#DCFF5C'];

/* ------------------------------------------------------------------ глифы --
 * Сетка 24×24, как в наборе иконок. Строка = path d, либо готовая разметка.
 */
const GLYPHS = {
  // Товары и подписка
  gift: [
    'M3.5 8.5h17v11.5a1 1 0 0 1-1 1h-15a1 1 0 0 1-1-1V8.5Z',
    'M2.5 5.5h19v3h-19z',
    'M12 5.5v15.5',
    'M12 5.5S10.6 2 8.2 2a2.1 2.1 0 0 0 0 3.5H12Z',
    'M12 5.5S13.4 2 15.8 2a2.1 2.1 0 0 1 0 3.5H12Z',
  ],
  star: ['M12 2.6l2.9 6.05 6.6.9-4.8 4.6 1.2 6.55L12 17.6l-5.9 3.1 1.2-6.55L2.5 9.55l6.6-.9L12 2.6Z'],
  ticket: [
    'M3 9.5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2v1.6a2.4 2.4 0 0 0 0 4.8v1.6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-1.6a2.4 2.4 0 0 0 0-4.8V9.5Z',
    'M9.5 8v8',
  ],
  wallet: ['M3.5 7.5a2 2 0 0 1 2-2h13a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2h-13a2 2 0 0 1-2-2v-9Z', 'M16 11.5h4.5v3H16a1.5 1.5 0 0 1 0-3Z'],
  card: ['M2.5 7a2 2 0 0 1 2-2h15a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2h-15a2 2 0 0 1-2-2V7Z', 'M2.5 10h19'],
  crown: ['M3 8.5l3.5 3L12 5l5.5 6.5 3.5-3-1.8 9.5H4.8L3 8.5Z'],

  // Действия и состояния
  check: ['M4.5 12.8l4.6 4.6L19.5 7'],
  cross: ['M6 6l12 12', 'M18 6L6 18'],
  warn: ['M12 3.2 21 19.4H3L12 3.2Z', 'M12 10v4.2', 'M12 17.2h.01'],
  clock: ['M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18Z', 'M12 7.5V12l3.2 2'],
  refresh: ['M4 12a8 8 0 0 1 13.7-5.6L20 8.5', 'M20 4v4.5h-4.5', 'M20 12a8 8 0 0 1-13.7 5.6L4 15.5', 'M4 20v-4.5h4.5'],
  lock: ['M6 10.5h12a1.5 1.5 0 0 1 1.5 1.5v7A1.5 1.5 0 0 1 18 20.5H6A1.5 1.5 0 0 1 4.5 19v-7A1.5 1.5 0 0 1 6 10.5Z', 'M8 10.5V7.8a4 4 0 0 1 8 0v2.7'],

  // Связь, поддержка, рост
  shield: ['M12 2.8 20 5.6v6.1c0 4.6-3.2 8.3-8 9.5-4.8-1.2-8-4.9-8-9.5V5.6L12 2.8Z', 'M9 12.2l2.1 2.1L15.3 10'],
  rocket: ['M12 2.8c3.4 1.6 5 5 5 9.2l-2.4 2.3h-5.2L7 12C7 7.8 8.6 4.4 12 2.8Z', 'M9.4 14.3 7.2 19l3.2-1.1', 'M14.6 14.3 16.8 19l-3.2-1.1', 'M12 9.5h.01'],
  lifebuoy: ['M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18Z', 'M12 8.2a3.8 3.8 0 1 0 0 7.6 3.8 3.8 0 0 0 0-7.6Z', 'M5.6 5.6l3.9 3.9', 'M14.5 14.5l3.9 3.9', 'M18.4 5.6l-3.9 3.9', 'M9.5 14.5l-3.9 3.9'],
  phone: ['M7.5 2.8h9a1.7 1.7 0 0 1 1.7 1.7v15a1.7 1.7 0 0 1-1.7 1.7h-9a1.7 1.7 0 0 1-1.7-1.7v-15A1.7 1.7 0 0 1 7.5 2.8Z', 'M10.5 18.4h3'],
  growth: ['M4 19.5h16', 'M7 19.5v-5', 'M12 19.5v-9', 'M17 19.5v-13'],
  users: ['M9 11a3.6 3.6 0 1 0 0-7.2A3.6 3.6 0 0 0 9 11Z', 'M2.8 20c0-3.5 2.8-6 6.2-6s6.2 2.5 6.2 6', 'M16.5 4.4a3.4 3.4 0 0 1 0 6.6', 'M17.4 14.4c2.3.6 3.8 2.5 3.8 5.6'],
  clipboard: ['M9 4.8H7.5a1.5 1.5 0 0 0-1.5 1.5v12a1.5 1.5 0 0 0 1.5 1.5h9a1.5 1.5 0 0 0 1.5-1.5v-12a1.5 1.5 0 0 0-1.5-1.5H15', 'M9 3.2h6v3.2H9z'],
  link: ['M10.2 13.8a4 4 0 0 1 0-5.6l2.6-2.6a4 4 0 0 1 5.6 5.6l-1.4 1.4', 'M13.8 10.2a4 4 0 0 1 0 5.6l-2.6 2.6a4 4 0 0 1-5.6-5.6l1.4-1.4'],
  cart: ['M3 4.5h2.2l2.4 10.2h9.9l2.1-7.4H6', 'M9 20a1.2 1.2 0 1 0 0-2.4A1.2 1.2 0 0 0 9 20Z', 'M17 20a1.2 1.2 0 1 0 0-2.4A1.2 1.2 0 0 0 17 20Z'],
  megaphone: ['M4 10.5v3a1.5 1.5 0 0 0 1.5 1.5H8l7 4V6.5l-7 4H5.5A1.5 1.5 0 0 0 4 12v-1.5Z', 'M18.5 9.5a4 4 0 0 1 0 5', 'M8 15v4.5'],
  hourglass: ['M7 3.5h10', 'M7 20.5h10', 'M8 3.5v3.2c0 2 4 3.2 4 5.3s-4 3.3-4 5.3v3.2', 'M16 3.5v3.2c0 2-4 3.2-4 5.3s4 3.3 4 5.3v3.2'],
  compass: ['M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18Z', 'M15.5 8.5 13.4 13.4 8.5 15.5l2.1-4.9 4.9-2.1Z'],
  qmark: ['M9.2 8.8a3 3 0 1 1 4.3 2.7c-1 .5-1.5 1.2-1.5 2.3v.7', 'M12 18.4h.01'],
  // Знак «комета-стрела» из brand/avatar/kometa-avatar.svg (сетка 512 → 24).
  kometa: ['M 0 0 C 3.83 -1 9 -2.67 15.5 -3.33 L 15.5 -6.33 L 23.83 0 L 15.5 6.33 L 15.5 3.33 C 9 2.67 3.83 1 0 0 Z'],
};

//: Знак рисуется заливкой и в своей системе координат (центр 0,0), поэтому для
//: него отдельное преобразование — как в медальонах набора.
const BRAND_FILL = new Set(['kometa']);
//: Глифы, у которых часть элементов задана готовой разметкой (кружки-точки).
const RAW = new Set(['cart']);

/* --------------------------------------------------------------- отрисовка */
function glyphBounds(els) {
  // Честный разбор path: команды бывают абсолютные и относительные, поэтому
  // просто «выдернуть все числа» нельзя — глиф уезжает из кадра (проверено на
  // звезде: относительные `l` считались как абсолютные координаты).
  let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
  const push = (x, y) => {
    minX = Math.min(minX, x); maxX = Math.max(maxX, x);
    minY = Math.min(minY, y); maxY = Math.max(maxY, y);
  };
  const argsCount = { M: 2, L: 2, H: 1, V: 1, C: 6, S: 4, Q: 4, T: 2, A: 7, Z: 0 };

  for (const raw of els) {
    if (raw.trimStart().startsWith('<')) {
      // Готовая разметка: <circle cx cy r>, <rect x y width height>
      const nums = (raw.match(/-?\d+(?:\.\d+)?/g) || []).map(Number);
      if (raw.includes('<circle')) {
        const [cx, cy, r] = nums;
        push(cx - r, cy - r); push(cx + r, cy + r);
      } else if (raw.includes('<rect')) {
        const [x, y, w, h] = nums;
        push(x, y); push(x + w, y + h);
      }
      continue;
    }
    const tokens = raw.match(/[MmLlHhVvCcSsQqTtAaZz]|-?\d*\.?\d+(?:e[-+]?\d+)?/g) || [];
    let i = 0, cmd = 'M', x = 0, y = 0, startX = 0, startY = 0;
    while (i < tokens.length) {
      if (/[A-Za-z]/.test(tokens[i])) { cmd = tokens[i]; i += 1; }
      const n = argsCount[cmd.toUpperCase()] ?? 0;
      if (n === 0) { x = startX; y = startY; continue; }
      const a = tokens.slice(i, i + n).map(Number);
      if (a.length < n) break;
      i += n;
      const rel = cmd === cmd.toLowerCase();
      const C = cmd.toUpperCase();
      if (C === 'M' || C === 'L' || C === 'T') {
        x = rel ? x + a[0] : a[0];
        y = rel ? y + a[1] : a[1];
        if (C === 'M') { startX = x; startY = y; cmd = rel ? 'l' : 'L'; }
      } else if (C === 'H') { x = rel ? x + a[0] : a[0]; }
      else if (C === 'V') { y = rel ? y + a[0] : a[0]; }
      else if (C === 'C') { x = rel ? x + a[4] : a[4]; y = rel ? y + a[5] : a[5]; }
      else if (C === 'S' || C === 'Q') { x = rel ? x + a[2] : a[2]; y = rel ? y + a[3] : a[3]; }
      else if (C === 'A') { x = rel ? x + a[5] : a[5]; y = rel ? y + a[6] : a[6]; }
      push(x, y);
    }
  }
  if (!Number.isFinite(minX)) return { minX: 0, minY: 0, maxX: 24, maxY: 24 };
  return { minX, minY, maxX, maxY };
}

function emojiSvg(name) {
  const els = GLYPHS[name];
  if (!els) throw new Error(`нет глифа ${name}`);
  const uid = `e-${name}`;
  let body;

  if (BRAND_FILL.has(name)) {
    // Знак Kometa: заливка фирменным градиентом, кадр 100 → центр 50,50.
    body = `<g transform="translate(50 56) rotate(-38) scale(1.45) translate(-12 0)">` +
      els.map((d) => `<path d="${d}" fill="url(#brand-${uid})" stroke="none"/>`).join('') +
      `</g>`;
  } else {
    const b = glyphBounds(els);
    // Вписываем глиф в квадрат 76×76 (76% от диаметра 92): проверено на живом
    // клиенте — при меньшем размере рисунок в строке 20 px читается как точка.
    const w = Math.max(b.maxX - b.minX, 1);
    const h = Math.max(b.maxY - b.minY, 1);
    const scale = 76 / Math.max(w, h);
    const cx = (b.minX + b.maxX) / 2;
    const cy = (b.minY + b.maxY) / 2;
    const tx = 50 - cx * scale;
    const ty = 50 - cy * scale;
    const stroke = (2.2 / scale).toFixed(3);
    body = `<g transform="translate(${tx.toFixed(2)} ${ty.toFixed(2)}) scale(${scale.toFixed(4)})" ` +
      `fill="none" stroke="#FFFFFF" stroke-width="${stroke}" stroke-linecap="round" stroke-linejoin="round">` +
      els.map((e) => e.trimStart().startsWith('<') ? e : `<path d="${e}"/>`).join('') +
      `</g>`;
  }

  return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100" width="100" height="100">
  <defs>
    <linearGradient id="brand-${uid}" x1="0" y1="1" x2="1" y2="0">
      <stop offset="0%" stop-color="${BRAND[0]}"/><stop offset="52%" stop-color="${BRAND[1]}"/><stop offset="100%" stop-color="${BRAND[2]}"/>
    </linearGradient>
    <linearGradient id="ring-${uid}" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0%" stop-color="${BRAND[0]}"/><stop offset="52%" stop-color="${BRAND[1]}"/><stop offset="100%" stop-color="${BRAND[2]}"/>
    </linearGradient>
  </defs>
  <circle cx="50" cy="50" r="46" fill="none" stroke="url(#ring-${uid})" stroke-width="2.6" opacity="0.62"/>
  ${body}
</svg>`;
}

mkdirSync(OUT, { recursive: true });
let count = 0;
for (const name of Object.keys(GLYPHS)) {
  writeFileSync(join(OUT, `${name}.svg`), emojiSvg(name));
  count += 1;
}
console.log(`записано SVG: ${count} → ${OUT}`);
