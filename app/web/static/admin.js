/* Kometa — админ-панель: минимальный ванильный JS.
   Никаких зависимостей и CDN: панель работает на localhost без интернета.
   Всё, что здесь есть, — прогрессивное улучшение: формы работают и без JS. */
(function () {
  "use strict";

  var THEME_KEY = "kometa-admin-theme";

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
