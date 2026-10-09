#!/usr/bin/env node
/**
 * Kometa · иконочный набор бота — генератор артефактов.
 *
 * Единственный источник правды по геометрии иконок. Из него собираются:
 *   icons/<name>.svg        — 24×24, stroke="currentColor" (работает на тёмном и светлом)
 *   icons/<name>-mono.svg   — та же геометрия, жёсткий цвет #45E698 (--brand-300)
 *   icons/sheet.html        — лист превью на фоне #10171E
 *   emoji/<name>.svg        — 100×100 «медальон» для premium-эмодзи (концепт-заготовка)
 *   emoji/sheet.html        — лист превью на фоне чата #17212B (32/64/100 px)
 *
 * PNG не хранятся в репозитории как «ручной» артефакт: mono-SVG растеризуется
 * cairosvg'ом, листы — экспортом Open Design (см. assets/README.md).
 *
 * Запуск:  node design/bot/assets/tools/build-icons.mjs
 * Проверка: node design/bot/assets/tools/build-icons.mjs --check
 *
 * Стиль (не менять без пересмотра набора):
 *   сетка 24×24, оптический шаг 2px, крайние точки 2.5–3 (видимый край ≈2),
 *   stroke 1.75 (2 для плотных иконок), round caps/joins, никакой заливки
 *   кроме брендовых акцентов, деталей тоньше 0.75px нет.
 */

import { writeFileSync, mkdirSync, readFileSync, readdirSync, existsSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const ASSETS = join(dirname(fileURLToPath(import.meta.url)), '..');
const ICON_DIR = join(ASSETS, 'icons');
const EMOJI_DIR = join(ASSETS, 'emoji');
const CHECK = process.argv.includes('--check');

/* ------------------------------------------------------------------ палитра */
/* Скопировано из design/bot/tokens.css — единственного источника правды. */
const C = {
  brand300: '#45E698',
  brand200: '#8FF8AA',
  brand100: '#DCFF5C',
  brand400: '#2BC57F',
  brand700: '#0A5C3F',
  text1: '#F2F6F4',
  surface1: '#10171E',
  surface2: '#1B2733',
  surfaceChat: '#17212B',
  medallion: '#0B1410',
};

/* --------------------------------------------------------------- утилиты ---- */
const f = (n) => (Math.round(n * 100) / 100).toString();

function starPath(cx, cy, R, r, points = 5, rot = -90) {
  const pts = [];
  for (let i = 0; i < points * 2; i++) {
    const a = ((rot + (i * 180) / points) * Math.PI) / 180;
    const rr = i % 2 ? r : R;
    pts.push(`${f(cx + rr * Math.cos(a))} ${f(cy + rr * Math.sin(a))}`);
  }
  return `M${pts.join('L')}Z`;
}

/* ------------------------------------------------------------- 24 иконки ---- */
/* els — список SVG-элементов (path/circle/rect/ellipse). w — stroke-width. */
const ICONS = [
  {
    name: 'rocket',
    els: [
      'M12 2.75c2.6 2.05 4.1 5 4.1 8.45v4.3H7.9v-4.3c0-3.45 1.5-6.4 4.1-8.45Z',
      '<circle cx="12" cy="9.4" r="1.85"/>',
      'M7.9 12.4 5.35 15.1c-.4.42-.6.97-.6 1.54v1.86l3.15-1.65',
      'M16.1 12.4l2.55 2.7c.4.42.6.97.6 1.54v1.86l-3.15-1.65',
      'M10.6 17.9c.45.95.95 1.9 1.4 2.85.45-.95.95-1.9 1.4-2.85',
    ],
  },
  {
    name: 'compass',
    els: ['<circle cx="12" cy="12" r="9"/>', 'M15.9 8.1 10.6 10.6 8.1 15.9 13.4 13.4Z'],
  },
  {
    name: 'globe',
    els: [
      '<circle cx="12" cy="12" r="9"/>',
      '<ellipse cx="12" cy="12" rx="4.3" ry="9"/>',
      'M3 12h18',
      'M4.4 7.35c2.05 1.55 4.7 2.4 7.6 2.4s5.55-.85 7.6-2.4',
      'M4.4 16.65c2.05-1.55 4.7-2.4 7.6-2.4s5.55.85 7.6 2.4',
    ],
  },
  {
    name: 'shield-key',
    els: [
      'M12 2.75 19.15 5.4v6.05c0 4.3-2.88 8.2-7.15 9.55-4.27-1.35-7.15-5.25-7.15-9.55V5.4L12 2.75Z',
      '<circle cx="12" cy="10.4" r="1.7"/>',
      'M12 12.1v3.4',
    ],
  },
  {
    name: 'device-phone',
    els: [
      '<rect x="7.25" y="2.75" width="9.5" height="18.5" rx="2.5"/>',
      'M10.75 5.75h2.5',
      'M11 18.5h2',
    ],
  },
  {
    name: 'device-laptop',
    els: [
      '<rect x="4.25" y="4.75" width="15.5" height="10.25" rx="1.75"/>',
      'M5.1 15 3.5 18.5M18.9 15 20.5 18.5',
      'M2.5 18.5h19',
    ],
  },
  {
    name: 'device-tv',
    els: [
      '<rect x="2.75" y="4.25" width="18.5" height="12" rx="2.25"/>',
      'M9.4 20.5 10.5 16.25M14.6 20.5 13.5 16.25',
    ],
  },
  {
    name: 'gift',
    els: [
      '<rect x="3.25" y="7.75" width="17.5" height="3.5" rx="1.25"/>',
      'M4.75 11.25v7.25c0 .97.78 1.75 1.75 1.75h11c.97 0 1.75-.78 1.75-1.75v-7.25',
      'M12 7.75v12.5',
      'M12 7.6c-.75-2.3-2.4-3.2-3.6-2.35-1.2.85-.55 2.35 1.1 2.35H12',
      'M12 7.6c.75-2.3 2.4-3.2 3.6-2.35 1.2.85.55 2.35-1.1 2.35H12',
    ],
  },
  { name: 'star', els: [starPath(12, 12.4, 9.5, 3.8)] },
  {
    name: 'ticket',
    scale: 1.12, // билет плоский: доводим до оптической массы соседей
    els: [
      'M6.75 6.5h10.5c1.1 0 2 .9 2 2v1.9a1.6 1.6 0 0 0 0 3.2v1.9c0 1.1-.9 2-2 2H6.75c-1.1 0-2-.9-2-2v-1.9a1.6 1.6 0 0 0 0-3.2V8.5c0-1.1.9-2 2-2Z',
      '<path d="M9.5 7.25v9.5" stroke-dasharray="1.6 2.4"/>',
    ],
  },
  {
    name: 'wallet',
    els: [
      'M20.25 10.5V8.25c0-1.1-.9-2-2-2H5.75c-1.1 0-2 .9-2 2v7.5c0 1.1.9 2 2 2h12.5c1.1 0 2-.9 2-2V13.5',
      'M15.5 10.5h4.75v3H15.5a1.5 1.5 0 0 1 0-3Z',
    ],
  },
  {
    name: 'card-payment',
    els: [
      '<rect x="2.75" y="5.25" width="18.5" height="13.5" rx="2.5"/>',
      'M2.75 9.75h18.5',
      'M6.5 15.5h3.5M13.5 15.5h4',
    ],
  },
  {
    name: 'crown',
    els: [
      'M3.5 8.75 8.25 12.5 12 5.75l3.75 6.75L20.5 8.75l-1.4 9.05c-.1.6-.6 1.05-1.2 1.05H6.1c-.6 0-1.1-.45-1.2-1.05L3.5 8.75Z',
    ],
  },
  {
    name: 'refresh',
    els: [
      'M20.25 12a8.25 8.25 0 1 1-8.25-8.25c2.3 0 4.5.92 6.16 2.5l2.09 2.09',
      'M20.25 3.75v5h-5',
    ],
  },
  {
    name: 'signal-tower',
    els: [
      // волны концентричны маячку; разносим их, чтобы читались раздельно, а не «шапкой»
      'M7.54 5.09a6 6 0 0 1 8.92 0',
      'M8.69 7.34a3.75 3.75 0 0 1 6.62 0',
      '<circle cx="12" cy="9.1" r="1.45"/>',
      'M10.75 10.25 8.95 20.5M13.25 10.25 15.05 20.5',
      'M10.05 14.2h3.9M9.4 17.7h5.2',
    ],
  },
  {
    name: 'speed-meter',
    els: [
      'M3 16.75a9 9 0 0 1 18 0',
      'M12 16.75 15.65 11.15',
      '<circle cx="12" cy="16.75" r="1.15"/>',
    ],
  },
  {
    name: 'clock',
    els: ['<circle cx="12" cy="12" r="9"/>', 'M12 6.75V12l3.5 2.15'],
  },
  {
    name: 'calendar',
    els: [
      '<rect x="3.25" y="5.25" width="17.5" height="15.5" rx="2.25"/>',
      'M3.25 10h17.5',
      'M8 3v3.5M16 3v3.5',
      '<rect x="7.5" y="13.25" width="3.25" height="3.25" rx="1.1"/>',
    ],
  },
  {
    name: 'lock',
    els: [
      '<rect x="4.75" y="10.5" width="14.5" height="10" rx="2.5"/>',
      'M8.35 10.5V8a3.65 3.65 0 0 1 7.3 0v2.5',
      'M12 14.5v2.25',
    ],
  },
  {
    name: 'link-chain',
    scale: 1.12, // цепочка диагональная: та же причина
    // два звена-«капсулы» под 45°, перекрытие по перпендикуляру 0.8 — читается как цепь
    els: [
      '<g transform="translate(12 12) rotate(-45)"><rect x="-4.2" y="-0.4" width="8.4" height="5.2" rx="2.6"/><rect x="-4.2" y="-4.8" width="8.4" height="5.2" rx="2.6"/></g>',
    ],
  },
  {
    name: 'copy-clipboard',
    els: [
      '<rect x="8.5" y="8.5" width="12" height="12" rx="2.5"/>',
      'M4.25 16.5c-.97 0-1.75-.78-1.75-1.75V4.5c0-.97.78-1.75 1.75-1.75h10.25c.97 0 1.75.78 1.75 1.75',
    ],
  },
  {
    name: 'lifebuoy',
    els: [
      '<circle cx="12" cy="12" r="9"/>',
      '<circle cx="12" cy="12" r="3.75"/>',
      'M5.64 5.64 9.35 9.35M18.36 5.64 14.65 9.35',
      'M5.64 18.36 9.35 14.65M18.36 18.36 14.65 14.65',
    ],
  },
  {
    name: 'chart-growth',
    els: ['M3.75 20.25h16.5', 'M7.5 20.25V15.5M12 20.25V11M16.5 20.25V6.75'],
  },
  {
    name: 'users-group',
    els: [
      '<circle cx="9.5" cy="7.75" r="3.5"/>',
      'M3.25 20.5v-1.25a5 5 0 0 1 5-5h2.5a5 5 0 0 1 5 5v1.25',
      'M16.25 4.75a3.5 3.5 0 0 1 0 6',
      'M17.75 14.5a5 5 0 0 1 2.75 4.5v1.5',
    ],
  },
];

/* --------------------------------------------------- глифы, которых нет в 24 */
const EXTRA_GLYPHS = {
  fire: [
    'M12 3c2.9 4.1 6.25 6.6 6.25 10.75a6.25 6.25 0 0 1-12.5 0C5.75 9.6 9.1 7.1 12 3Z',
    'M12 20.5c-1.9 0-3.25-1.45-3.25-3.25 0-1.5 1.3-2.75 3.25-5.25 1.95 2.5 3.25 3.75 3.25 5.25 0 1.8-1.35 3.25-3.25 3.25Z',
  ],
  diamond: [
    'M12 20.75 3.5 9.5 6.6 4.75h10.8L20.5 9.5 12 20.75Z',
    'M3.5 9.5h17',
    'M8.4 9.5 12 20.75 15.6 9.5',
  ],
  party: [
    'M2.8 21.2 6.2 11.6c2.6 1.05 4.75 3.2 5.8 5.8L2.8 21.2Z',
    'M14.9 12.9c1.85.35 3.6-.35 4.85-1.8',
    '<circle cx="13.9" cy="9.3" r="0.85" fill="currentColor" stroke="none"/>',
    '<circle cx="18.2" cy="6.4" r="0.85" fill="currentColor" stroke="none"/>',
    '<circle cx="20.5" cy="12.6" r="0.85" fill="currentColor" stroke="none"/>',
    '<circle cx="16.2" cy="4.6" r="0.85" fill="currentColor" stroke="none"/>',
  ],
};

/* 12 premium-эмодзи: медальон 88px, окантовка 4px (--grad-brand), глиф из набора. */
const EMOJI = [
  { name: 'kometa', glyph: 'brand', ring: true, glow: true },
  { name: 'fire', glyph: 'fire' },
  { name: 'diamond', glyph: 'diamond', fill: true },
  { name: 'gift', glyph: 'gift' },
  { name: 'ticket', glyph: 'ticket' },
  { name: 'star', glyph: 'star', fill: true },
  { name: 'rocket', glyph: 'rocket' },
  { name: 'shield', glyph: 'shield-key' },
  { name: 'clock', glyph: 'clock' },
  { name: 'wallet', glyph: 'wallet' },
  { name: 'crown', glyph: 'crown' },
  { name: 'party', glyph: 'party' },
];

const byName = new Map(ICONS.map((i) => [i.name, i]));

/* --------------------------------------------------------------- SVG-сборка */
const HEAD = 'stroke-linecap="round" stroke-linejoin="round"';

/* Элемент набора: либо готовая разметка (<circle/>, <rect/>…), либо строка path d. */
const el = (e) => (e.trimStart().startsWith('<') ? e : `<path d="${e}"/>`);

function iconSvg(icon, { stroke, size, extra = '' } = {}) {
  const body = iconBody(icon);
  return [
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"',
    size ? ` width="${size}" height="${size}"` : ' width="24" height="24"',
    ` fill="none" stroke="${stroke}" stroke-width="${icon.w || 1.75}" ${HEAD}${extra}>`,
    ...body,
    '</svg>',
  ].join('\n');
}

/* Оптическая доводка: часть иконок (плоских/диагональных) масштабируется
 * вокруг центра, чтобы по массе не выпадать из набора. */
function iconBody(icon) {
  const inner = icon.els.map((e) => `  ${el(e)}`);
  return icon.scale
    ? [`  <g transform="translate(12 12) scale(${icon.scale}) translate(-12 -12)">`, ...inner.map((l) => `  ${l}`), '  </g>']
    : inner;
}

/* Глиф внутри медальона: та же геометрия 24×24, масштаб ×2 (сетка глифа 48px). */
function medGlyph(els, scale, attrs) {
  const inner = els.map((x) => `  ${el(x)}`);
  const body = scale
    ? [
        `  <g transform="translate(12 12) scale(${scale}) translate(-12 -12)">`,
        ...inner.map((l) => `  ${l}`),
        '  </g>',
      ]
    : inner;
  return [`<g transform="translate(26 26) scale(2)" ${HEAD}${attrs}>`, ...body, `</g>`];
}

/* Медальон: 100×100, диск #0B1410 88px, градиентная окантовка 4px, глиф ×2. */
let uidSeq = 0;
function medallionSvg(name, { size = 100, uid = null } = {}) {
  const e = EMOJI.find((x) => x.name === name);
  if (!e) throw new Error(`emoji ${name} не описан`);
  const u = uid || `m${++uidSeq}`;
  const ringId = `ring-${u}`;
  const discId = `disc-${u}`;
  const fillId = `fill-${u}`;
  const body = [];

  body.push(
    `<defs>`,
    `<linearGradient id="${ringId}" x1="0" y1="0" x2="1" y2="1">`,
    `<stop offset="0%" stop-color="${C.brand400}"/><stop offset="52%" stop-color="${C.brand300}"/><stop offset="100%" stop-color="${C.brand100}"/>`,
    `</linearGradient>`,
    `<radialGradient id="${discId}" cx="38%" cy="28%" r="88%">`,
    `<stop offset="0%" stop-color="#13301F"/><stop offset="58%" stop-color="#0E1D15"/><stop offset="100%" stop-color="${C.medallion}"/>`,
    `</radialGradient>`,
    `<linearGradient id="${fillId}" x1="0" y1="0" x2="1" y2="1">`,
    `<stop offset="0%" stop-color="${C.brand400}"/><stop offset="52%" stop-color="${C.brand300}"/><stop offset="100%" stop-color="${C.brand100}"/>`,
    `</linearGradient>`,
    `</defs>`,
  );
  body.push(`<circle cx="50" cy="50" r="44" fill="url(#${discId})"/>`);
  body.push(
    `<circle cx="50" cy="50" r="42" fill="none" stroke="url(#${ringId})" stroke-width="4"/>`,
  );
  if (e.ring) {
    body.push(
      `<circle cx="50" cy="50" r="35" fill="none" stroke="${C.brand300}" stroke-width="0.75" opacity="0.28"/>`,
    );
  }
  if (e.glow) {
    body.push(
      `<circle cx="50" cy="50" r="46.5" fill="none" stroke="${C.brand300}" stroke-width="1" opacity="0.16"/>`,
    );
  }

  if (e.glyph === 'brand') {
    // знак «комета-стрела» из brand/avatar/kometa-avatar.svg: те же 0.06 на 24-сетке,
    // здесь сетка медальона — поэтому ×2 и центр 50,50.
    body.push(
      `<g transform="translate(50 50) rotate(-42) scale(0.13) translate(-143 0)">`,
      `<path d="M 0 0 C 46 -12 108 -32 186 -40 L 186 -76 L 286 0 L 186 76 L 186 40 C 108 32 46 12 0 0 Z" fill="url(#${fillId})"/>`,
      `</g>`,
    );
  } else {
    const icon = byName.get(e.glyph);
    const els = icon ? icon.els : EXTRA_GLYPHS[e.glyph];
    const scale = icon ? icon.scale : null;
    if (e.fill) {
      // «премиум»-глиф: силуэт залит брендовым градиентом, фасетные линии — тёмный «рез»
      body.push(...medGlyph(els.slice(0, 1), scale, ` fill="url(#${fillId})" stroke="none"`));
      if (els.length > 1) {
        body.push(...medGlyph(els.slice(1), scale, ` fill="none" stroke="#08170F" stroke-width="1.6"`));
      }
    } else {
      // color задаёт currentColor для точечных акцентов внутри глифа
      body.push(...medGlyph(els, scale, ` fill="none" stroke="${C.text1}" color="${C.text1}" stroke-width="2"`));
    }
  }

  return [
    `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100" width="${size}" height="${size}">`,
    ...body.map((l) => `  ${l}`),
    `</svg>`,
  ].join('\n');
}

/* ------------------------------------------------------------ лист иконок --
 * Канва 1440px, фон #10171E (surface-1). Экспорт Open Design идёт с
 * deviceScaleFactor = 2 → PNG 2880px по ширине.
 */
function iconsSheetHtml() {
  const tile = (i) =>
    [
      `<figure class="tile">`,
      `<div class="ic">${iconSvg(i, { stroke: 'currentColor', size: 48 })}</div>`,
      `<figcaption>${i.name}</figcaption>`,
      `</figure>`,
    ].join('\n');

  const mini = (i) =>
    `<div class="mini" title="${i.name}">${iconSvg(i, { stroke: 'currentColor', size: 20 })}</div>`;

  const lightIcon = (i) => `<div class="lc">${iconSvg(i, { stroke: 'currentColor', size: 32 })}</div>`;

  return `<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<title>Kometa · иконочный набор бота</title>
<style>
  /* Значения скопированы из design/bot/tokens.css (единственный источник правды). */
  :root{
    --brand-300:#45E698; --brand-100:#DCFF5C; --brand-700:#0A5C3F; --brand-ink:#04140F;
    --surface-1:#10171E; --surface-2:#1B2733; --surface-chat:#17212B; --canvas:#0B0F12;
    --text-1:#F2F6F4; --text-2:#A8B6B0; --text-3:#6F7F7A;
    --border-1:rgba(255,255,255,.07); --border-brand:rgba(69,230,152,.35);
    --font-ui:-apple-system,"SF Pro Text","Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;
    --font-mono:ui-monospace,"SF Mono","JetBrains Mono",Menlo,Consolas,monospace;
  }
  *{box-sizing:border-box;margin:0;padding:0}
  body{
    width:1440px;background:var(--surface-1);color:var(--text-1);
    font-family:var(--font-ui);padding:56px 60px 44px;position:relative;
  }
  body::before{content:"";position:absolute;inset:0 0 auto;height:4px;background:linear-gradient(135deg,#2BC57F 0%,#45E698 52%,#DCFF5C 100%)}
  .kicker{font-size:13px;letter-spacing:.14em;text-transform:uppercase;color:var(--brand-300);font-weight:600}
  h1{font-size:34px;line-height:1.15;margin:10px 0 12px;letter-spacing:-.01em}
  .sub{font-size:15px;line-height:1.45;color:var(--text-2);max-width:900px}
  .meta{display:flex;gap:10px;flex-wrap:wrap;margin:22px 0 34px}
  .chip{
    font-family:var(--font-mono);font-size:12.5px;color:var(--text-2);
    border:1px solid var(--border-1);border-radius:999px;padding:6px 12px;background:rgba(255,255,255,.03)
  }
  .chip b{color:var(--brand-300);font-weight:600}
  .band{border-top:1px solid var(--border-1);padding-top:22px;margin-top:30px}
  .band-title{font-size:13px;letter-spacing:.06em;text-transform:uppercase;color:var(--text-3);margin-bottom:16px}
  .minis{display:flex;gap:26px;flex-wrap:wrap;align-items:center}
  .mini{color:var(--brand-300);display:flex;align-items:center}
  .grid{display:grid;grid-template-columns:repeat(6,1fr);gap:16px}
  .tile{
    background:var(--surface-2);border:1px solid var(--border-1);border-radius:18px;
    padding:20px 14px 16px;display:flex;flex-direction:column;align-items:center;gap:14px;
  }
  .tile .ic{color:var(--brand-300);display:flex;height:48px;align-items:center}
  .tile figcaption{font-family:var(--font-mono);font-size:12.5px;color:var(--text-2);white-space:nowrap}
  .light{background:var(--text-1);border-radius:18px;padding:24px 28px;display:flex;align-items:center;gap:28px}
  .light .cap{color:var(--brand-700);font-size:13.5px;line-height:1.4;max-width:220px;font-weight:600}
  .light .lc{color:var(--brand-700);display:flex}
  footer{margin-top:34px;font-size:13px;color:var(--text-3);font-family:var(--font-mono)}
  footer b{color:var(--text-2);font-weight:500}
</style>
</head>
<body>
  <div class="kicker">Kometa · Bot Design System</div>
  <h1>Иконочный набор бота</h1>
  <p class="sub">24 иконки одного языка: сетка 24×24, оптический шаг 2px, единая толщина линии,
  круглые окончания, никакой заливки кроме брендовых акцентов. Заменяют дефолтные эмодзи 🎁💎⭐📡
  в меню, кнопках и сообщениях бота.</p>
  <div class="meta">
    <span class="chip">viewBox <b>0 0 24 24</b></span>
    <span class="chip">stroke <b>1.75</b></span>
    <span class="chip">stroke-linecap <b>round</b></span>
    <span class="chip">stroke <b>currentColor</b></span>
    <span class="chip">акцент <b>#45E698</b></span>
    <span class="chip">минимум детали <b>0.75px</b></span>
    <span class="chip">читается с <b>20px</b></span>
  </div>

  <div class="band">
    <div class="band-title">Проверка читаемости · 20px · реальный размер в меню бота</div>
    <div class="minis">${ICONS.map(mini).join('')}</div>
  </div>

  <div class="band">
    <div class="band-title">Набор · 24 иконки · 48px</div>
    <div class="grid">${ICONS.map(tile).join('\n')}</div>
  </div>

  <div class="band">
    <div class="band-title">currentColor на светлом фоне · #0A5C3F</div>
    <div class="light">
      <div class="cap">Один и тот же файл работает и в тёмной, и в светлой теме: цвет задаёт контекст.</div>
      ${ICONS.slice(0, 10).map(lightIcon).join('')}
    </div>
  </div>

  <footer>файлы: <b>icons/&lt;name&gt;.svg</b> (currentColor) · <b>icons/&lt;name&gt;-mono.png</b> (96×96, #45E698) · геометрия: tools/build-icons.mjs</footer>
</body>
</html>
`;
}

/* ------------------------------------------------------------ лист эмодзи --
 * Канва 1440px, фон чата #17212B (surface-chat). Три размера: 100 / 64 / 32.
 */
function emojiSheetHtml() {
  const cell = (e, size) =>
    `<div class="cell" style="--s:${size}px"><div class="em">${medallionSvg(e.name, { size, uid: `${e.name}-${size}` })}</div><span>${e.name}</span></div>`;

  const row = (size) => `<div class="row" style="--s:${size}px">${EMOJI.map((e) => cell(e, size)).join('\n')}</div>`;
  return `<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<title>Kometa · premium-эмодзи</title>
<style>
  /* Значения скопированы из design/bot/tokens.css. */
  :root{
    --brand-300:#45E698; --brand-100:#DCFF5C; --surface-1:#10171E; --surface-2:#1B2733;
    --surface-chat:#17212B; --text-1:#F2F6F4; --text-2:#A8B6B0; --text-3:#6F7F7A;
    --border-1:rgba(255,255,255,.07);
    --font-ui:-apple-system,"SF Pro Text","Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;
    --font-mono:ui-monospace,"SF Mono","JetBrains Mono",Menlo,Consolas,monospace;
  }
  *{box-sizing:border-box;margin:0;padding:0}
  body{width:1440px;background:var(--surface-chat);color:var(--text-1);font-family:var(--font-ui);padding:56px 60px 44px;position:relative}
  body::before{content:"";position:absolute;inset:0 0 auto;height:4px;background:linear-gradient(135deg,#2BC57F 0%,#45E698 52%,#DCFF5C 100%)}
  .kicker{font-size:13px;letter-spacing:.14em;text-transform:uppercase;color:var(--brand-300);font-weight:600}
  h1{font-size:34px;line-height:1.15;margin:10px 0 12px;letter-spacing:-.01em}
  .sub{font-size:15px;line-height:1.45;color:var(--text-2);max-width:940px}
  .meta{display:flex;gap:10px;flex-wrap:wrap;margin:22px 0 8px}
  .chip{font-family:var(--font-mono);font-size:12.5px;color:var(--text-2);border:1px solid var(--border-1);border-radius:999px;padding:6px 12px;background:rgba(255,255,255,.03)}
  .chip b{color:var(--brand-300);font-weight:600}
  .band{border-top:1px solid var(--border-1);padding-top:22px;margin-top:30px}
  .band-title{font-size:13px;letter-spacing:.06em;text-transform:uppercase;color:var(--text-3);margin-bottom:18px}
  .row{display:grid;grid-template-columns:repeat(6,1fr);gap:16px;align-items:end}
  .cell{background:rgba(255,255,255,.03);border:1px solid var(--border-1);border-radius:18px;padding:14px;display:flex;flex-direction:column;align-items:center;justify-content:flex-end;gap:10px;min-height:calc(var(--s) + 56px)}
  .cell .em{display:flex}
  .cell span{font-family:var(--font-mono);font-size:12px;color:var(--text-3)}
  .chat{background:var(--surface-1);border:1px solid var(--border-1);border-radius:18px;padding:22px 26px;max-width:900px}
  .bubble{background:var(--surface-2);border-radius:14px;padding:14px 18px;font-size:17px;line-height:1.5;display:flex;align-items:center;gap:10px;flex-wrap:wrap}
  .bubble .em{display:flex;vertical-align:middle}
  .who{font-size:13px;color:var(--text-3);margin-bottom:10px}
  footer{margin-top:34px;font-size:13px;color:var(--text-3);font-family:var(--font-mono)}
  footer b{color:var(--text-2);font-weight:500}
</style>
</head>
<body>
  <div class="kicker">Kometa · Bot Design System</div>
  <h1>Premium-эмодзи · 12 медальонов</h1>
  <p class="sub">Концепт-заготовки под собственный набор custom_emoji: круглый медальон 88px,
  градиентная окантовка 4px (--grad-brand), внутри — глиф из иконочного набора.
  Фон медальона #0B1410, вокруг — прозрачность. Это PNG-эстимейт: анимацию (TGS/WebM) считает
  спецификация <b>emoji/SPEC-TGS.md</b>.</p>
  <div class="meta">
    <span class="chip">PNG <b>100×100</b></span>
    <span class="chip">медальон <b>88px</b></span>
    <span class="chip">окантовка <b>4px · --grad-brand</b></span>
    <span class="chip">фон диска <b>#0B1410</b></span>
    <span class="chip">TGS-цель <b>512×512 · ≤64 КБ</b></span>
  </div>

  <div class="band">
    <div class="band-title">100px · крупный размер (стикер, hero-сообщение)</div>
    ${row(100)}
  </div>

  <div class="band">
    <div class="band-title">64px · лента реакций, крупная строка</div>
    ${row(64)}
  </div>

  <div class="band">
    <div class="band-title">32px · базовый размер кастомного эмодзи в тексте</div>
    ${row(32)}
  </div>

  <div class="band">
    <div class="band-title">Как это выглядит в сообщении бота</div>
    <div class="chat">
      <div class="who">Kometa · бот</div>
      <div class="bubble">
        <span class="em">${medallionSvg('kometa', { size: 22, uid: 'inline-kometa' })}</span>
        Подписка активна до 12.11 · осталось 24 дня
        <span class="em">${medallionSvg('shield', { size: 22, uid: 'inline-shield' })}</span>
      </div>
    </div>
  </div>

  <footer>файлы: <b>emoji/&lt;name&gt;.png</b> (100×100, RGBA) · источник геометрии: <b>tools/build-icons.mjs</b> · анимация: <b>emoji/SPEC-TGS.md</b></footer>
</body>
</html>
`;
}

/* ------------------------------------------------------------------- запись */
function writeAll() {
  mkdirSync(ICON_DIR, { recursive: true });
  mkdirSync(EMOJI_DIR, { recursive: true });

  for (const icon of ICONS) {
    writeFileSync(
      join(ICON_DIR, `${icon.name}.svg`),
      `<!-- Kometa icon set · ${icon.name} · 24×24 · stroke ${icon.w || 1.75} · currentColor -->\n${iconSvg(icon, { stroke: 'currentColor' })}\n`,
    );
    writeFileSync(
      join(ICON_DIR, `${icon.name}-mono.svg`),
      `<!-- Kometa icon set · ${icon.name} · 24×24 · stroke ${icon.w || 1.75} · ${C.brand300} -->\n${iconSvg(icon, { stroke: C.brand300 })}\n`,
    );
  }
  writeFileSync(join(ICON_DIR, 'sheet.html'), iconsSheetHtml());
  writeFileSync(join(EMOJI_DIR, 'sheet.html'), emojiSheetHtml());
}

/* Векторные исходники медальонов пишутся в рабочий каталог (не в репозиторий):
 * в assets/emoji лежат только PNG — так набор остаётся ровно «48 svg + 12 png». */
function emitEmojiSvg(dir) {
  mkdirSync(dir, { recursive: true });
  for (const e of EMOJI) {
    writeFileSync(join(dir, `${e.name}.svg`), `${medallionSvg(e.name, { uid: e.name })}\n`);
  }
  console.log(`медальоны (svg-исходники): ${EMOJI.length} → ${dir}`);
}

/* ----------------------------------------------------------------- проверка */
function check() {
  const problems = [];
  const svgs = readdirSync(ICON_DIR).filter((x) => x.endsWith('.svg'));
  const monos = readdirSync(ICON_DIR).filter((x) => x.endsWith('-mono.png'));
  const emojis = readdirSync(EMOJI_DIR).filter((x) => x.endsWith('.png') && x !== 'sheet.png');
  if (svgs.length !== 48) problems.push(`иконных .svg: ${svgs.length}, ожидалось 48`);
  if (monos.length !== 24) problems.push(`mono .png: ${monos.length}, ожидалось 24`);
  if (emojis.length !== 12) problems.push(`эмодзи .png: ${emojis.length}, ожидалось 12`);
  for (const icon of ICONS) {
    const cur = join(ICON_DIR, `${icon.name}.svg`);
    if (!existsSync(cur)) {
      problems.push(`нет ${icon.name}.svg`);
      continue;
    }
    const s = readFileSync(cur, 'utf8');
    if (!s.includes('viewBox="0 0 24 24"')) problems.push(`${icon.name}.svg: viewBox не 24×24`);
    if (!s.includes('stroke="currentColor"')) problems.push(`${icon.name}.svg: нет stroke="currentColor"`);
    if (!s.includes('stroke-linecap="round"')) problems.push(`${icon.name}.svg: нет round cap`);
    const widths = [...s.matchAll(/stroke-width="([\d.]+)"/g)].map((m) => Number(m[1]));
    if (widths.some((w) => w < 0.75)) problems.push(`${icon.name}.svg: деталь тоньше 0.75px (${widths})`);
    if (!existsSync(join(ICON_DIR, `${icon.name}-mono.svg`))) problems.push(`нет ${icon.name}-mono.svg`);
    if (!existsSync(join(ICON_DIR, `${icon.name}-mono.png`))) problems.push(`нет ${icon.name}-mono.png`);
  }
  for (const e of EMOJI) if (!existsSync(join(EMOJI_DIR, `${e.name}.png`))) problems.push(`нет emoji/${e.name}.png`);
  for (const extra of ['icons/sheet.png', 'emoji/sheet.png']) {
    if (!existsSync(join(ASSETS, extra))) problems.push(`нет ${extra}`);
  }
  console.log(problems.length ? `ПРОБЛЕМЫ (${problems.length}):\n - ${problems.join('\n - ')}` : 'OK: набор полный, файлы на месте');
  return problems.length;
}

/* ------------------------------------------------------------------- запуск */
const emojiArg = process.argv.indexOf('--emit-emoji-svg');
if (CHECK) {
  process.exit(check());
} else if (emojiArg !== -1) {
  emitEmojiSvg(process.argv[emojiArg + 1] || '/tmp/kometa-emoji-svg');
} else {
  writeAll();
  console.log('записано: 48 svg + 2 листа HTML в design/bot/assets/{icons,emoji}');
}

