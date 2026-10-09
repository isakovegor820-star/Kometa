/* Kometa — админ-панель: минимальный ванильный JS.
   Никаких зависимостей и CDN: панель работает на localhost без интернета.
   Всё, что здесь есть, — прогрессивное улучшение: формы работают и без JS. */
(function () {
  "use strict";

  var THEME_KEY = "kometa-admin-theme";
  var NAV_KEY = "kometa-admin-nav";
  var DENSITY_KEY = "kometa-admin-density";

  function store(key, value) {
    try {
      if (value) localStorage.setItem(key, value);
      else localStorage.removeItem(key);
    } catch (e) { /* приватный режим — просто не запоминаем */ }
  }

  /* ------------------------------------------------------------- тема */
  function applyTheme(theme) {
    document.documentElement.setAttribute("data-theme", theme === "dark" ? "dark" : "light");
    var btn = document.querySelector("[data-theme-toggle]");
    if (btn) {
      btn.setAttribute("aria-label", theme === "dark" ? "Включить светлую тему" : "Включить тёмную тему");
      var icon = btn.querySelector("[data-theme-icon]");
      if (icon) icon.textContent = theme === "dark" ? "☀" : "☾";
    }
  }

  function currentTheme() {
    try {
      return localStorage.getItem(THEME_KEY) || "light";
    } catch (e) {
      return "light";
    }
  }

  applyTheme(currentTheme());

  document.addEventListener("click", function (event) {
    var toggle = event.target.closest("[data-theme-toggle]");
    if (!toggle) return;
    var next = document.documentElement.getAttribute("data-theme") === "dark" ? "light" : "dark";
    try {
      localStorage.setItem(THEME_KEY, next);
    } catch (e) { /* приватный режим — просто не запоминаем */ }
    applyTheme(next);
  });

  /* ------------------------------------- плотность таблиц и меню-рейка */
  // Задача: на 15″ в экран должно входить больше данных. Плотность меняет
  // отступы ячеек, «рейка» — ширину меню (252 px → 68 px). Оба выбора
  // запоминаются; восстановление делает инлайн-скрипт в base.html.
  var DENSITIES = [
    { id: "", label: "Обычно", hint: "обычная плотность" },
    { id: "compact", label: "Плотно", hint: "компактные строки" },
    { id: "roomy", label: "Просторно", hint: "просторные строки" }
  ];

  function densityIndex() {
    var current = document.documentElement.getAttribute("data-density") || "";
    for (var i = 0; i < DENSITIES.length; i++) {
      if (DENSITIES[i].id === current) return i;
    }
    return 0;
  }

  function applyDensity(index) {
    var item = DENSITIES[index];
    if (item.id) document.documentElement.setAttribute("data-density", item.id);
    else document.documentElement.removeAttribute("data-density");
    store(DENSITY_KEY, item.id);
    var btn = document.querySelector("[data-density-toggle]");
    if (!btn) return;
    var label = btn.querySelector("[data-density-label]");
    if (label) label.textContent = item.label;
    btn.setAttribute("title", "Плотность таблиц: " + item.hint + " (нажми, чтобы сменить)");
  }

  applyDensity(densityIndex());

  document.addEventListener("click", function (event) {
    if (!event.target.closest("[data-density-toggle]")) return;
    applyDensity((densityIndex() + 1) % DENSITIES.length);
  });

  function applyNav(mode) {
    if (mode === "min") document.documentElement.setAttribute("data-nav", "min");
    else document.documentElement.removeAttribute("data-nav");
    var btn = document.querySelector("[data-nav-min]");
    if (!btn) return;
    var min = mode === "min";
    btn.setAttribute("title", min ? "Развернуть меню" : "Свернуть меню");
    btn.setAttribute("aria-label", min ? "Развернуть меню" : "Свернуть меню");
    btn.setAttribute("aria-pressed", min ? "true" : "false");
  }

  applyNav(document.documentElement.getAttribute("data-nav") === "min" ? "min" : "");

  document.addEventListener("click", function (event) {
    if (!event.target.closest("[data-nav-min]")) return;
    var next = document.documentElement.getAttribute("data-nav") === "min" ? "" : "min";
    store(NAV_KEY, next);
    applyNav(next);
  });

  /* -------------------------------------------- горячие клавиши списков */
  // «/» — поиск, Esc — выйти из поиска. Мелочь, но в работе экономит клик.
  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape") {
      var active = document.activeElement;
      if (active && active.tagName === "INPUT") active.blur();
      return;
    }
    if (event.key !== "/" || event.metaKey || event.ctrlKey || event.altKey) return;
    var tag = (event.target.tagName || "").toLowerCase();
    if (tag === "input" || tag === "textarea" || tag === "select") return;
    var search = document.querySelector("input[name=q], .search input, input[type=search]");
    if (!search) return;
    event.preventDefault();
    search.focus();
    search.select();
  });

  /* ------------------------------------------------ столбцы таблиц */
  // Кнопка «Столбцы» в шапке страницы: какие столбцы показывать, решает
  // пользователь. Набор хранится в localStorage по имени таблицы, поэтому
  // у модератора и владельца списки могут выглядеть по-разному.
  var COLS_KEY = "kometa-admin-cols";

  function colsStore() {
    try {
      return JSON.parse(localStorage.getItem(COLS_KEY) || "{}") || {};
    } catch (e) {
      return {};
    }
  }

  function applyCols(table) {
    var id = table.getAttribute("data-table");
    if (!id) return;
    var cfg = colsStore()[id] || {};
    var hidden = Object.keys(cfg).filter(function (key) { return cfg[key] === false; });
    if (hidden.length) table.setAttribute("data-cols-hidden", hidden.join(" "));
    else table.removeAttribute("data-cols-hidden");
    var menu = document.querySelector('[data-colmenu][data-table="' + id + '"]');
    if (menu) {
      Array.prototype.forEach.call(menu.querySelectorAll("[data-col-toggle]"), function (box) {
        box.checked = cfg[box.getAttribute("data-col-toggle")] !== false;
      });
    }
  }

  function applyAllCols() {
    Array.prototype.forEach.call(document.querySelectorAll("table.data[data-table]"), applyCols);
  }

  applyAllCols();

  document.addEventListener("change", function (event) {
    var box = event.target.closest("[data-col-toggle]");
    if (!box) return;
    var menu = box.closest("[data-colmenu]");
    if (!menu) return;
    var id = menu.getAttribute("data-table");
    var table = document.querySelector('table.data[data-table="' + id + '"]');
    if (!table) return;
    var store = colsStore();
    store[id] = store[id] || {};
    store[id][box.getAttribute("data-col-toggle")] = box.checked;
    try {
      localStorage.setItem(COLS_KEY, JSON.stringify(store));
    } catch (e) { /* приватный режим — просто не запоминаем */ }
    applyCols(table);
  });

  document.addEventListener("click", function (event) {
    Array.prototype.forEach.call(document.querySelectorAll("[data-colmenu][open]"), function (menu) {
      if (!menu.contains(event.target)) menu.removeAttribute("open");
    });
  });

  document.addEventListener("keydown", function (event) {
    if (event.key !== "Escape") return;
    Array.prototype.forEach.call(document.querySelectorAll("[data-colmenu][open]"), function (menu) {
      menu.removeAttribute("open");
    });
  });

  /* ------------------------------------------------ командная палитра ⌘K */
  // Один ввод вместо хождения по меню: разделы (из прав роли), клиенты и
  // заказы (из /admin/search). Разметка — в base.html.
  var cmdk = document.getElementById("cmdk");
  var cmdkInput = document.getElementById("cmdk-input");
  var cmdkList = document.getElementById("cmdk-list");
  var cmdkSections = [];
  try {
    var raw = document.getElementById("cmdk-sections");
    cmdkSections = raw ? JSON.parse(raw.textContent || "[]") : [];
  } catch (e) {
    cmdkSections = [];
  }
  var cmdkRows = [];
  var cmdkIndex = 0;
  var cmdkSeq = 0;
  var cmdkTimer = null;
  var cmdkKind = { user: "Клиент", order: "Заказ" };

  function cmdkEscape(text) {
    return String(text == null ? "" : text).replace(/[&<>"']/g, function (ch) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch];
    });
  }

  function cmdkRender(query, found, loading) {
    if (!cmdkList) return;
    var needle = query.trim().toLowerCase();
    var sections = cmdkSections.filter(function (item) {
      if (!needle) return true;
      return (item.title + " " + (item.keys || "")).toLowerCase().indexOf(needle) !== -1;
    }).slice(0, needle ? 4 : 8);

    var rows = sections.map(function (item) {
      return { kind: "Раздел", title: item.title, sub: "", url: item.url };
    });
    (found || []).forEach(function (item) {
      rows.push({ kind: cmdkKind[item.kind] || "", title: item.title, sub: item.sub || "", url: item.url });
    });

    if (!rows.length) {
      cmdkRows = [];
      cmdkIndex = 0;
      cmdkList.innerHTML = '<div class="cmdk-empty">' + (loading ? "Ищу…" : "Ничего не нашлось") + "</div>";
      return;
    }
    cmdkRows = rows;
    if (cmdkIndex >= rows.length) cmdkIndex = rows.length - 1;
    if (cmdkIndex < 0) cmdkIndex = 0;
    cmdkList.innerHTML = rows.map(function (row, index) {
      return '<div class="cmdk-item' + (index === cmdkIndex ? " active" : "") + '" role="option" ' +
        'data-url="' + cmdkEscape(row.url) + '">' +
        '<span class="grow"><span class="t">' + cmdkEscape(row.title) + "</span>" +
        (row.sub ? '<span class="s">' + cmdkEscape(row.sub) + "</span>" : "") + "</span>" +
        (row.kind ? '<span class="badge">' + cmdkEscape(row.kind) + "</span>" : "") +
        "</div>";
    }).join("");
  }

  function cmdkFetch(query) {
    var seq = ++cmdkSeq;
    if (query.trim().length < 2) {
      cmdkRender(query, [], false);
      return;
    }
    fetch("/admin/search?q=" + encodeURIComponent(query), { headers: { "X-Panel-Ajax": "1" } })
      .then(function (response) { return response.ok ? response.json() : { items: [] }; })
      .then(function (data) {
        if (seq === cmdkSeq) cmdkRender(query, data.items || [], false);
      })
      .catch(function () {
        if (seq === cmdkSeq) cmdkRender(query, [], false);
      });
  }

  function cmdkIsOpen() {
    return !!cmdk && cmdk.classList.contains("open");
  }

  function cmdkOpen() {
    if (!cmdk) return;
    cmdk.classList.add("open");
    cmdkIndex = 0;
    if (cmdkInput) {
      cmdkInput.value = "";
      cmdkInput.focus();
    }
    cmdkRender("", [], false);
  }

  function cmdkClose() {
    if (cmdk) cmdk.classList.remove("open");
  }

  function cmdkGo() {
    var row = cmdkRows[cmdkIndex];
    if (row) window.location.href = row.url;
  }

  function cmdkMove(step) {
    if (!cmdkRows.length) return;
    cmdkIndex = (cmdkIndex + step + cmdkRows.length) % cmdkRows.length;
    Array.prototype.forEach.call(cmdkList.querySelectorAll(".cmdk-item"), function (node, index) {
      node.classList.toggle("active", index === cmdkIndex);
    });
    var active = cmdkList.querySelector(".cmdk-item.active");
    if (active && active.scrollIntoView) active.scrollIntoView({ block: "nearest" });
  }

  document.addEventListener("click", function (event) {
    if (event.target.closest("[data-cmdk-open]")) {
      event.preventDefault();
      cmdkOpen();
      return;
    }
    if (event.target.closest("[data-cmdk-close]")) {
      cmdkClose();
      return;
    }
    var item = event.target.closest(".cmdk-item");
    if (item) window.location.href = item.getAttribute("data-url");
  });

  if (cmdkInput) {
    cmdkInput.addEventListener("input", function () {
      window.clearTimeout(cmdkTimer);
      var value = cmdkInput.value;
      cmdkTimer = window.setTimeout(function () { cmdkFetch(value); }, 160);
    });
    cmdkInput.addEventListener("keydown", function (event) {
      if (event.key === "ArrowDown") {
        event.preventDefault();
        cmdkMove(1);
      } else if (event.key === "ArrowUp") {
        event.preventDefault();
        cmdkMove(-1);
      } else if (event.key === "Enter") {
        event.preventDefault();
        cmdkGo();
      } else if (event.key === "Escape") {
        event.preventDefault();
        cmdkClose();
      }
    });
  }

  document.addEventListener("keydown", function (event) {
    var key = (event.key || "").toLowerCase();
    if ((event.metaKey || event.ctrlKey) && key === "k") {
      event.preventDefault();
      if (cmdkIsOpen()) cmdkClose();
      else cmdkOpen();
      return;
    }
    if (key === "escape" && cmdkIsOpen()) cmdkClose();
  });

  /* ------------------------------------ действия без перезагрузки страницы */
  // Любая POST-форма панели уходит через fetch: сервер (flash_redirect)
  // отвечает JSON-ом, мы показываем тост и обновляем содержимое на месте.
  // Если JS выключен или сервер ответил не JSON — обычная отправка формы.
  var toasts = document.getElementById("toasts");

  function toast(kind, text) {
    if (!toasts || !text) return;
    var el = document.createElement("div");
    el.className = "toast " + (kind === "err" ? "err" : "ok");
    el.setAttribute("role", kind === "err" ? "alert" : "status");
    var path = kind === "err"
      ? '<path d="M12 4 2.8 19.2h18.4z"/><path d="M12 10v4.5M12 17.4h.01"/>'
      : '<path d="M4.5 12.5l5 5 10-11"/>';
    el.innerHTML =
      '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" ' +
      'stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' + path + "</svg>" +
      '<span class="grow"></span>' +
      '<button class="icon-btn" type="button" aria-label="Закрыть">×</button>';
    el.querySelector(".grow").textContent = String(text);
    el.querySelector("button").addEventListener("click", function () { el.remove(); });
    toasts.appendChild(el);
    window.setTimeout(function () { el.remove(); }, kind === "err" ? 9000 : 4200);
  }

  function syncNavCounts(doc) {
    var fresh = {};
    Array.prototype.forEach.call(doc.querySelectorAll(".nav a"), function (link) {
      var badge = link.querySelector(".count");
      fresh[link.getAttribute("href")] = badge ? badge.textContent.trim() : "";
    });
    Array.prototype.forEach.call(document.querySelectorAll(".nav a"), function (link) {
      var badge = link.querySelector(".count");
      var value = fresh[link.getAttribute("href")];
      if (badge && value !== undefined) badge.textContent = value;
    });
  }

  function loadContent(url, push) {
    return fetch(url, { headers: { "X-Panel-Ajax": "1" } })
      .then(function (response) { return response.ok ? response.text() : ""; })
      .then(function (html) {
        if (!html) return;
        var doc = new DOMParser().parseFromString(html, "text/html");
        var fresh = doc.querySelector(".content");
        var current = document.querySelector(".content");
        if (!fresh || !current) return;
        var scroll = window.scrollY;
        current.replaceWith(fresh);
        syncNavCounts(doc);
        applyAllCols();
        window.scrollTo(0, scroll);
        if (push) window.history.pushState({ panel: true }, "", url);
      })
      .catch(function () { /* не смогли обновить — данные уже сохранены на сервере */ });
  }

  function refreshContent() {
    return loadContent(window.location.href, false);
  }

  // Мастер-деталь: клик по строке открывает карточку сбоку, не уводя со списка.
  document.addEventListener("click", function (event) {
    if (event.metaKey || event.ctrlKey || event.shiftKey || event.button) return;
    var closer = event.target.closest("[data-drawer-close]");
    if (closer) {
      event.preventDefault();
      loadContent(closer.getAttribute("href"), true);
      return;
    }
    var row = event.target.closest("tr[data-card-url]");
    if (!row) return;
    if (event.target.closest("button, input, select, form, label, .copy-chip")) return;
    var link = event.target.closest("a");
    // Ссылки «Карточка» и прочие ведут на отдельные страницы — их не трогаем.
    if (link && !link.classList.contains("nm")) return;
    event.preventDefault();
    loadContent(row.getAttribute("data-card-url"), true);
  });

  window.addEventListener("popstate", function () {
    loadContent(window.location.href, false);
  });

  function submitAjax(form, action) {
    var button = form.querySelector('button[type="submit"]') || form.querySelector("button");
    if (button) button.disabled = true;
    return fetch(action, {
      method: "POST",
      body: new FormData(form),
      headers: { "X-Panel-Ajax": "1" },
      redirect: "follow",
    })
      .then(function (response) {
        if (response.redirected && response.url.indexOf("/admin/login") !== -1) {
          window.location.href = response.url;
          return null;
        }
        var type = response.headers.get("content-type") || "";
        if (type.indexOf("application/json") === -1) {
          // Сервер ответил страницей (например, отказ по правам) — отдаём
          // решение ему и отправляем форму обычным способом.
          if (button) button.disabled = false;
          form.submit();
          return null;
        }
        return response.json();
      })
      .then(function (data) {
        if (!data) return;
        toast(data.ok ? "ok" : "err", data.ok ? (data.message || "Готово") : (data.error || "Не получилось"));
        if (data.ok) return refreshContent();
        return null;
      })
      .catch(function (error) {
        toast("err", "Сеть недоступна: " + error.message);
      })
      .then(function () {
        if (button) button.disabled = false;
      });
  }

  document.addEventListener("submit", function (event) {
    var form = event.target;
    if (!form || form.tagName !== "FORM") return;
    if (event.defaultPrevented) return; // пользователь отменил подтверждение
    if (form.hasAttribute("data-no-ajax")) return;
    if ((form.getAttribute("method") || "get").toLowerCase() !== "post") return;
    var action = form.getAttribute("action") || window.location.href;
    if (/\/admin\/(login|logout)\b/.test(action)) return; // вход и выход — навигацией
    event.preventDefault();
    // Отправляем после того, как отработают остальные обработчики submit:
    // массовые действия дописывают в форму выбранные id (см. «массовые
    // действия» выше), и FormData должна собираться уже с ними.
    window.setTimeout(function () { submitAjax(form, action); }, 0);
  });

  /* ---------------------------------------------------- мобильное меню */
  document.addEventListener("click", function (event) {
    if (event.target.closest("[data-nav-toggle]")) {
      document.body.classList.toggle("nav-open");
      return;
    }
    if (document.body.classList.contains("nav-open") && !event.target.closest(".sidebar")) {
      document.body.classList.remove("nav-open");
    }
  });

  /* --------------------------------------------------- копирование */
  function copyText(text) {
    if (navigator.clipboard && window.isSecureContext) {
      return navigator.clipboard.writeText(text);
    }
    var area = document.createElement("textarea");
    area.value = text;
    area.setAttribute("readonly", "");
    area.style.position = "fixed";
    area.style.left = "-9999px";
    document.body.appendChild(area);
    area.select();
    var ok = false;
    try { ok = document.execCommand("copy"); } catch (e) { ok = false; }
    document.body.removeChild(area);
    return ok ? Promise.resolve() : Promise.reject(new Error("copy failed"));
  }

  function flashButton(btn, label) {
    var old = btn.getAttribute("data-label") || btn.innerHTML;
    btn.setAttribute("data-label", old);
    btn.innerHTML = label;
    window.setTimeout(function () { btn.innerHTML = btn.getAttribute("data-label"); }, 1400);
  }

  document.addEventListener("click", function (event) {
    var trigger = event.target.closest("[data-copy]");
    if (!trigger) return;
    event.preventDefault();
    var value = trigger.getAttribute("data-copy");
    copyText(value).then(function () {
      flashButton(trigger, "✓");
    }).catch(function () {
      window.prompt("Скопируй вручную:", value);
    });
  });

  /* ------------------------------------------------ подтверждения */
  document.addEventListener("submit", function (event) {
    var form = event.target;
    var message = form.getAttribute("data-confirm");
    if (message && !window.confirm(message)) {
      event.preventDefault();
    }
  });

  document.addEventListener("click", function (event) {
    var link = event.target.closest("a[data-confirm]");
    if (!link) return;
    if (!window.confirm(link.getAttribute("data-confirm"))) event.preventDefault();
  });

  /* ------------------------------------------- массовые действия */
  function selectedBoxes(scope) {
    return Array.prototype.slice.call(scope.querySelectorAll("[data-row-check]:checked"));
  }

  function syncBulk(scope) {
    var bar = document.querySelector("[data-bulkbar]");
    var boxes = selectedBoxes(scope);
    var all = scope.querySelectorAll("[data-row-check]");
    var master = scope.querySelector("[data-check-all]");
    if (master) {
      master.checked = all.length > 0 && boxes.length === all.length;
      master.indeterminate = boxes.length > 0 && boxes.length < all.length;
    }
    if (!bar) return;
    bar.classList.toggle("on", boxes.length > 0);
    var counter = bar.querySelector("[data-bulk-count]");
    if (counter) counter.textContent = String(boxes.length);
    Array.prototype.forEach.call(scope.querySelectorAll("tr[data-row]"), function (row) {
      var box = row.querySelector("[data-row-check]");
      row.classList.toggle("is-selected", !!(box && box.checked));
    });
  }

  document.addEventListener("change", function (event) {
    var target = event.target;
    var scope = target.closest("[data-bulk-scope]") || document;
    if (target.matches("[data-row-check]")) {
      syncBulk(scope);
    } else if (target.matches("[data-check-all]")) {
      Array.prototype.forEach.call(scope.querySelectorAll("[data-row-check]"), function (box) {
        box.checked = target.checked;
      });
      syncBulk(scope);
    }
  });

  document.addEventListener("submit", function (event) {
    var form = event.target.closest("[data-bulk-form]");
    if (!form) return;
    var scope = document.querySelector("[data-bulk-scope]") || document;
    var boxes = selectedBoxes(scope);
    if (!boxes.length) {
      event.preventDefault();
      return;
    }
    // Собираем выбранные id в скрытые поля: работает и без JS-фреймворков.
    Array.prototype.forEach.call(form.querySelectorAll("input[data-generated]"), function (node) {
      node.remove();
    });
    boxes.forEach(function (box) {
      var hidden = document.createElement("input");
      hidden.type = "hidden";
      hidden.name = "ids";
      hidden.value = box.value;
      hidden.setAttribute("data-generated", "1");
      form.appendChild(hidden);
    });
  });

  /* ------------------------------------------------ автоподтверждение */
  // Кнопка «Подтвердить всё» собирает все видимые pending-заказы.
  document.addEventListener("click", function (event) {
    var btn = event.target.closest("[data-select-all-rows]");
    if (!btn) return;
    event.preventDefault();
    var scope = document.querySelector("[data-bulk-scope]") || document;
    Array.prototype.forEach.call(scope.querySelectorAll("[data-row-check]"), function (box) {
      if (!btn.hasAttribute("data-only-visible") || box.closest("tr").style.display !== "none") box.checked = true;
    });
    syncBulk(scope);
  });

  /* --------------------------------------------------- живые фильтры */
  var search = document.querySelector("[data-live-search]");
  if (search) {
    search.addEventListener("input", function () {
      var scope = document.querySelector("[data-bulk-scope]") || document;
      var needle = search.value.trim().toLowerCase();
      Array.prototype.forEach.call(scope.querySelectorAll("tr[data-row]"), function (row) {
        var hay = (row.getAttribute("data-search") || "").toLowerCase();
        row.style.display = !needle || hay.indexOf(needle) !== -1 ? "" : "none";
      });
    });
  }

  /* ------------------------------------------------ закрытие модалок */
  document.addEventListener("click", function (event) {
    var closer = event.target.closest("[data-close-dialog]");
    if (closer) {
      var dialog = closer.closest("dialog");
      if (dialog) dialog.close();
      return;
    }
    // клик по фону закрывает модальное окно
    if (event.target.tagName === "DIALOG" && event.target.hasAttribute("data-backdrop-close")) {
      event.target.close();
    }
  });

  /* --------------------------------------------------- подсказки времени */
  Array.prototype.forEach.call(document.querySelectorAll("[data-ts]"), function (node) {
    var raw = node.getAttribute("data-ts");
    if (!raw || node.title) return;
    node.title = new Date(raw).toLocaleString("ru-RU");
  });
})();
