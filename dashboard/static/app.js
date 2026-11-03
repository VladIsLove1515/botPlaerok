const PAGES = [
  ["overview", "Обзор"],
  ["bump", "Поднятие"],
  ["snipe", "Перебив"],
  ["restore", "Восстановление"],
  ["accounts", "Аккаунты"],
  ["errors", "Ошибки"],
  ["deals", "Сделки"],
  ["messages", "Сообщения"],
  ["notifications", "Уведомления"],
  ["general", "Общее"],
];

const EVENTS = [
  ["new_deal", "Новая сделка", "Покупатель оплатил товар"],
  ["new_user_message", "Сообщение покупателя", "Обычное сообщение в чате"],
  ["new_problem", "Проблема в сделке", "Покупатель открыл спор"],
  ["new_review", "Новый отзыв", "Покупатель оставил оценку"],
  ["deal_status_changed", "Статус сделки", "Сделка подтверждена, отправлена или возвращена"],
  ["item_bumped", "Товар поднят", "Сработало автоподнятие"],
  ["item_restored", "Товар восстановлен", "Лот снова выставлен"],
  ["withdrawal_requested", "Запрос вывода", "Бот отправил вывод средств"],
  ["new_system_message", "Системное сообщение", "Служебные сообщения Playerok"],
];

const MESSAGES = [
  ["first_message", "Если лот не привязан ни к одному своему приветствию — уйдёт этот текст на каждую сделку"],
  ["new_deal", "Сразу после оплаты"],
  ["deal_sent", "Когда заказ отмечен выполненным"],
  ["deal_confirmed", "Когда покупатель подтвердил сделку"],
  ["deal_refunded", "Когда заказ возвращён"],
  ["deal_has_problem", "Когда открыта проблема"],
  ["new_review", "Ответ на отзыв"],
  ["cmd_seller", "Ответ на команду !ошибка"],
  ["cmd_error", "Если команда написана с ошибкой"],
];

const SNIPE_EVENTS = {
  detected: "Заметили чужой лот",
  raising: "Пора поднимать",
  bumped: "Свой лот поднят",
  failed: "Ошибка поднятия",
  missing: "Чужой лот пропал",
};

let state = null;
let page = "overview";
let query = "";
let settingsQuery = "";
let selectedError = "";
let dragging = false;
let editing = null;
let toastTimer = 0;
let findTimer = 0;
let snipeQuery = "";
let snipeDraft = null;
let snipeSelected = {};
let snipeMyPick = {};
let snipeMyLots = [];
let snipeMyLotsLoading = false;

const main = document.getElementById("main");
const nav = document.getElementById("nav");
const topStatus = document.getElementById("top-status");

function toast(text, bad) {
  const node = document.getElementById("toast");
  node.textContent = text;
  node.classList.toggle("bad", !!bad);
  node.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => node.classList.remove("show"), 1800);
}

function clock(iso) {
  if (!iso) return "—";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return String(iso);
  return date.toLocaleString("ru-RU", {
    day: "2-digit",
    month: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function duration(seconds) {
  const sec = Math.max(0, Math.round(Number(seconds) || 0));
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = sec % 60;
  const parts = [];
  if (h) parts.push(h + " ч");
  if (m) parts.push(m + " мин");
  if (s || !parts.length) parts.push(s + " сек");
  return parts.join(" ");
}

function hourLabel(value) {
  const hour = String(value || "00:00").slice(0, 2);
  return hour + ":00";
}

function paint(input) {
  const min = Number(input.min);
  const max = Number(input.max);
  const value = Number(input.value);
  const percent = max === min ? 0 : ((value - min) / (max - min)) * 100;
  input.style.setProperty("--p", percent + "%");
}

function esc(text) {
  return String(text ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function switchHtml(checked) {
  return `<span class="switch"><input type="checkbox" ${checked ? "checked" : ""}><i></i></span>`;
}

function row(title, hint, checked, attrs) {
  return `<label class="row">${switchHtml(checked)}<span><b>${esc(title)}</b><span>${esc(hint)}</span></span></label>`
    .replace("<span><b>", `<span data-bind="${attrs}"><b>`);
}

function slider(title, hint, value, min, max, step, label, attrs, find) {
  const mark = find ? ` data-find="${find}"` : "";
  return `
    <div class="slider"${mark}>
      <div class="slider-head"><b>${esc(title)}</b><strong>${esc(label)}</strong></div>
      <input type="range" min="${min}" max="${max}" step="${step}" value="${value}" ${attrs}>
      <p>${esc(hint)}</p>
    </div>`;
}

function seg(options, current, attrs, find) {
  const buttons = options.map(([value, label]) =>
    `<button type="button" class="${value === current ? "on" : ""}" data-seg="${esc(value)}" ${attrs}>${esc(label)}</button>`
  ).join("");
  const mark = find ? ` data-find="${find}"` : "";
  return `<div class="seg"${mark}>${buttons}</div>`;
}

async function post(url, body, rerender) {
  const response = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "не сохранилось");
  state = data;
  toast("Сохранено");
  paintStatus();
  if (rerender) render();
}

async function saveSetting(path, value, rerender = true) {
  try {
    await post("/api/setting", { path, value }, rerender);
  } catch (error) {
    toast(error.message, true);
    await reload(true);
  }
}

function catalogItem(id) {
  return (state.catalog || []).find((item) => item.id === id);
}

function participating(kind, id) {
  const list = state.lists[kind];
  const all = kind === "bump" ? state.settings.bump.all
    : kind === "restore" ? state.settings.restore.all
      : state.settings.complete.all;
  if (all) return !list.excluded.includes(id);
  return list.included.includes(id);
}

async function toggleItem(kind, id, on) {
  const list = state.lists[kind];
  const all = kind === "bump" ? state.settings.bump.all
    : kind === "restore" ? state.settings.restore.all
      : state.settings.complete.all;
  let included = list.included.slice();
  let excluded = list.excluded.slice();
  const drop = (arr) => arr.filter((item) => item !== id);
  included = drop(included);
  excluded = drop(excluded);
  if (all) {
    if (!on) excluded.push(id);
  } else if (on) {
    included.push(id);
  }
  try {
    await post("/api/list", { kind, included, excluded }, true);
  } catch (error) {
    toast(error.message, true);
    await reload(true);
  }
}

function itemsBlock(kind) {
  const all = kind === "bump" ? state.settings.bump.all
    : kind === "restore" ? state.settings.restore.all
      : state.settings.complete.all;
  const needle = query.trim().toLowerCase();
  const items = (state.catalog || []).filter((item) =>
    !needle || (item.name || "").toLowerCase().includes(needle)
  );
  const hint = all
    ? "Сейчас берутся все товары. Выключите ползунок у тех, которые трогать не нужно."
    : "Сейчас берутся только включённые товары.";
  const rows = items.map((item) => {
    const on = participating(kind, item.id);
    const price = item.price == null ? "" : item.price + " ₽";
    const badge = [price, item.priority].filter(Boolean).join(" · ");
    const image = item.image
      ? `<img src="${esc(item.image)}" alt="">`
      : `<div class="ph"></div>`;
    return `
      <label class="item">
        ${image}
        <span class="grow">
          <div class="name">${esc(item.name)}</div>
          <div class="meta">${esc(badge || "товар")}</div>
        </span>
        ${switchHtml(on).replace("<input", `<input data-item="${esc(item.id)}" data-kind="${kind}"`)}
      </label>`;
  }).join("");
  return `
    <p class="sub">${esc(hint)}</p>
    <input class="search" placeholder="Найти товар" value="${esc(query)}">
    ${rows || `<div class="empty">Товаров пока нет. Они появятся, когда бот увидит ваши лоты.</div>`}
  `;
}

function bumpPage() {
  const bump = state.settings.bump;
  const night = bump.night;
  const byPosition = bump.mode === "position";
  const positionMax = Math.max(200, bump.position, night.position);
  const day = byPosition
    ? slider("Позиция днём", "Товар поднимается, когда опускается ниже этого места.", bump.position, 1, positionMax, 1, bump.position + " место", `data-range="playerok.auto_bump_items.position" data-label="place"`, "bump-position")
    : slider("Интервал днём", "Полный круг для всех лотов. Они делят это время по очереди.", bump.interval, 300, 86400, 300, duration(bump.interval), `data-range="playerok.auto_bump_items.interval" data-label="time"`, "bump-interval");
  const nightControl = byPosition
    ? slider("Позиция ночью", "Ночью можно держать товар ниже и реже тратить баланс.", night.position, 1, positionMax, 1, night.position + " место", `data-range="playerok.auto_bump_items.night.position" data-label="place"`, "bump-night-position")
    : slider("Интервал ночью", "Ночью поднятие идёт реже.", night.interval, 300, 86400, 300, duration(night.interval), `data-range="playerok.auto_bump_items.night.interval" data-label="time"`, "bump-night-interval");
  return `
    <h1 data-find="bump-page">Поднятие</h1>
    <p class="lead">Поднимаются только отмеченные премиум-лоты за баланс Playerok. Обычные (бесплатные) лоты бот не трогает.</p>
    <section class="card" data-find="bump-card">
      <h2>Автоподнятие</h2>
      <label class="row" data-find="bump-enabled">
        ${switchHtml(bump.enabled).replace("<input", `<input data-path="playerok.auto_bump_items.enabled"`)}
        <span><b>Поднимать премиум</b><span>Списание с баланса за каждый подъём отмеченного премиум-лота.</span></span>
      </label>
      ${seg([["position", "По позиции"], ["interval", "По времени"]], bump.mode, `data-path="playerok.auto_bump_items.mode"`, "bump-mode")}
      ${day}
      ${byPosition ? slider("Пауза одного товара", "Минимум между двумя поднятиями одного и того же лота.", bump.cooldown, 60, 7200, 10, duration(bump.cooldown), `data-range="playerok.auto_bump_items.cooldown" data-label="time"`, "bump-cooldown") : ""}
      ${byPosition ? slider("Проверка позиций", "Как часто смотреть, не выпал ли товар.", bump.check_interval, 30, 600, 10, duration(bump.check_interval), `data-range="playerok.auto_bump_items.check_interval" data-label="time"`, "bump-check") : ""}
    </section>
    <section class="card">
      <h2>Ночь</h2>
      <label class="row" data-find="bump-night">
        ${switchHtml(night.enabled).replace("<input", `<input data-path="playerok.auto_bump_items.night.enabled"`)}
        <span><b>Ночной режим</b><span>В эти часы действуют другие цифры.</span></span>
      </label>
      ${night.enabled ? nightControl : ""}
      ${night.enabled ? slider("Ночь начинается", "Час, с которого включаются ночные правила.", Number(night.time_from.slice(0, 2)), 0, 23, 1, hourLabel(night.time_from), `data-range="playerok.auto_bump_items.night.time_from" data-label="hour"`, "bump-night-from") : ""}
      ${night.enabled ? slider("Ночь заканчивается", "Час, после которого снова дневные правила.", Number(night.time_to.slice(0, 2)), 0, 23, 1, hourLabel(night.time_to), `data-range="playerok.auto_bump_items.night.time_to" data-label="hour"`, "bump-night-to") : ""}
    </section>
    <section class="card">
      <h2>Какие товары</h2>
      <p class="sub">Отметьте премиум-позиции. Обычные лоты в списке игнорируются. Режим «все» = все активные премиум-лоты.</p>
      ${seg([["all", "Все премиум"], ["picked", "Только выбранные"]], bump.all ? "all" : "picked", `data-flag="playerok.auto_bump_items.all"`, "bump-which")}
      ${itemsBlock("bump")}
    </section>
    ${bumpJournal()}`;
}

function snipeMineLabel(mine) {
  const price = mine.price == null ? "" : `${mine.price}₽`;
  const priority = mine.priority === "PREMIUM" ? "премиум" : (mine.priority || "");
  const badge = [price, priority].filter(Boolean).join(" · ");
  return badge ? `${mine.name || mine.id} (${badge})` : (mine.name || mine.id);
}

function snipeMineOptions(pick) {
  if (snipeMyLotsLoading && !snipeMyLots.length) {
    return `<option value="">Загрузка лотов…</option>`;
  }
  if (!snipeMyLots.length) {
    return `<option value="">Нет лотов в продаже</option>`;
  }
  return `<option value="">Мой лот…</option>` + snipeMyLots.map((mine) =>
    `<option value="${esc(mine.id)}" ${mine.id === pick ? "selected" : ""}>${esc(snipeMineLabel(mine))}</option>`
  ).join("");
}

async function loadSnipeMyLots(force) {
  if (snipeMyLotsLoading) return;
  snipeMyLotsLoading = true;
  try {
    const response = await fetch("/api/snipe/my-items", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ force: !!force }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "не удалось загрузить свои лоты");
    snipeMyLots = Array.isArray(data.my_items) ? data.my_items : [];
  } catch (error) {
    toast(error.message, true);
  } finally {
    snipeMyLotsLoading = false;
  }
}

function snipePage() {
  const snipe = state.settings.snipe || {
    enabled: false,
    delay_seconds: 60,
    check_interval: 45,
    max_pay_price: 50,
  };
  const sellers = ((state.snipe && state.snipe.sellers) || []);
  const activeRows = sellers.flatMap((seller) =>
    (seller.links || []).map((link) => `
      <div class="snipe-link">
        <div class="grow">
          <div class="name">${esc(link.competitor_name || link.competitor_item_id)}</div>
          <div class="meta">продавец @${esc(seller.username || "?")} → мой «${esc(link.my_name || link.my_item_id)}»</div>
        </div>
        <button type="button" class="mini" data-snipe-remove
          data-competitor="${esc(link.competitor_item_id)}"
          data-mine="${esc(link.my_item_id)}">Удалить</button>
      </div>`).join("")
  ).join("");

  let draftBlock = `<div class="empty">Вставьте ссылку на продавца и нажмите «Найти».</div>`;
  if (snipeDraft) {
    const rows = (snipeDraft.items || []).map((item) => {
      const on = !!snipeSelected[item.id];
      const pick = snipeMyPick[item.id] || "";
      const price = item.price == null ? "" : item.price + " ₽";
      const badge = [price, item.priority].filter(Boolean).join(" · ");
      return `
        <div class="snipe-row">
          <label class="item snipe-item">
            <span class="grow">
              <div class="name">${esc(item.name)}</div>
              <div class="meta">${esc(badge || "лот")}</div>
            </span>
            ${switchHtml(on).replace("<input", `<input data-snipe-comp="${esc(item.id)}"`)}
          </label>
          <select class="textin snipe-select" data-snipe-mine="${esc(item.id)}" ${on ? "" : "disabled"}>
            ${snipeMineOptions(pick)}
          </select>
        </div>`;
    }).join("");
    draftBlock = `
      <p class="sub">Продавец <b>@${esc(snipeDraft.username)}</b> · ${esc(String((snipeDraft.items || []).length))} лот(ов) в продаже. Свои лоты: только сейчас в продаже (${esc(String(snipeMyLots.length))})</p>
      ${rows || `<div class="empty">У продавца нет активных лотов.</div>`}
      <button type="button" class="btn wide" data-snipe-add>Прикрепить выбранные</button>`;
  }

  return `
    <h1 data-find="snipe-page">Перебив</h1>
    <p class="lead">Следим за лотами конкурента. Когда он перевыставит объявление, через паузу бот платно поднимет ваш привязанный премиум-лот.</p>
    <section class="card" data-find="snipe-card">
      <h2>Режим перебива</h2>
      <label class="row" data-find="snipe-enabled">
        ${switchHtml(snipe.enabled).replace("<input", `<input data-path="playerok.auto_snipe.enabled"`)}
        <span><b>Включить перебив</b><span>Мониторинг чужих лотов и отложенный подъём своих.</span></span>
      </label>
      ${slider("Пауза после выставления", "Сколько ждать после перевыставления чужого лота, прежде чем поднять свой.", snipe.delay_seconds, 5, 600, 5, duration(snipe.delay_seconds), `data-range="playerok.auto_snipe.delay_seconds" data-label="time"`, "snipe-delay")}
      ${slider("Как часто проверять", "Интервал опроса лотов продавца.", snipe.check_interval, 15, 600, 5, duration(snipe.check_interval), `data-range="playerok.auto_snipe.check_interval" data-label="time"`, "snipe-check")}
      ${slider("Лимит оплаты", "Не поднимать, если цена премиум-поднятия выше этой суммы.", snipe.max_pay_price, 1, 500, 1, snipe.max_pay_price + " ₽", `data-range="playerok.auto_snipe.max_pay_price" data-label="rub"`, "snipe-pay")}
    </section>
    <section class="card" data-find="snipe-add">
      <h2>Привязать лоты продавца</h2>
      <p class="sub">Ссылка вида https://playerok.com/profile/ник или https://playerok.com/user/ник — либо просто ник.</p>
      <div class="snipe-form">
        <input class="textin" data-snipe-query placeholder="https://playerok.com/profile/..." value="${esc(snipeQuery)}">
        <button type="button" class="btn" data-snipe-resolve>Найти</button>
      </div>
      ${draftBlock}
    </section>
    <section class="card" data-find="snipe-links">
      <h2>Активные связки</h2>
      <p class="sub">Каждая строка: чужой лот → ваш лот, который поднимется после их перевыставления.</p>
      ${activeRows || `<div class="empty">Связок пока нет.</div>`}
    </section>
    ${snipeJournal()}`;
}

function snipeJournal() {
  const entries = ((state.snipe_journal && state.snipe_journal.entries) || []);
  const rows = entries.map((row) => {
    const title = SNIPE_EVENTS[row.event] || row.event || "Событие";
    const who = [row.competitor_name && `чужой «${row.competitor_name}»`, row.my_name && `мой «${row.my_name}»`]
      .filter(Boolean).join(" → ");
    const headline = who ? `${title}: ${who}` : title;
    return `
      <div class="journal-row">
        <span>${esc(clock(row.at))}</span>
        <b title="${esc(headline)}">${esc(headline)}</b>
        <em class="${row.ok ? "ok" : "bad"}">${esc(row.detail || (row.ok ? "ок" : "ошибка"))}</em>
      </div>`;
  }).join("");
  return `
    <section class="card" data-find="snipe-journal">
      <h2>История перебива</h2>
      <p class="sub">Заметили чужой лот → таймер → подняли свой. Последние события сверху.</p>
      <div class="journal">${rows || `<div class="empty">Пока пусто — события появятся после первой сработки.</div>`}</div>
    </section>`;
}

function bumpJournal() {
  const journal = state.journal || { entries: [], plan: [], slot: 0 };
  const step = journal.slot ? ` Шаг очереди: ${duration(journal.slot)}.` : "";
  const plan = (journal.plan || []).map((row) => `
    <div class="journal-row">
      <span>${esc(clock(row.next_at))}</span>
      <b>${esc(row.name)}</b>
      <em>${row.kind === "premium" ? "премиум" : "бесплатно"}</em>
    </div>`).join("");
  const rows = (journal.entries || []).map((row) => `
    <div class="journal-row">
      <span>${esc(clock(row.at))}</span>
      <b>${esc(row.name)}</b>
      <em class="${row.ok ? "ok" : "bad"}">${esc(row.detail || (row.ok ? "обновлён" : "ошибка"))}</em>
    </div>`).join("");
  return `
    <section class="card" data-find="bump-journal">
      <h2>Журнал обновлений</h2>
      <p class="sub">Каждая запись — момент, когда лот обновляли. Лоты не поднимаются разом: интервал делится между ними поровну.${esc(step)}</p>
      <p class="sub">Очередь</p>
      <div class="journal">${plan || `<div class="empty">Очередь появится, когда бот начнёт цикл.</div>`}</div>
      <p class="sub">История</p>
      <div class="journal">${rows || `<div class="empty">Обновлений ещё не было.</div>`}</div>
    </section>`;
}

function restorePage() {
  const restore = state.settings.restore;
  return `
    <h1 data-find="restore-page">Восстановление</h1>
    <p class="lead">После продажи или истечения лот можно сразу выставить снова.</p>
    <section class="card">
      <h2>Когда восстанавливать</h2>
      <label class="row" data-find="restore-sold">${switchHtml(restore.sold).replace("<input", `<input data-path="playerok.auto_restore_items.sold"`)}<span><b>Проданные</b><span>Товар купили — бот выставляет его ещё раз.</span></span></label>
      <label class="row" data-find="restore-expired">${switchHtml(restore.expired).replace("<input", `<input data-path="playerok.auto_restore_items.expired"`)}<span><b>Истёкшие</b><span>Срок лота кончился — бот публикует его снова.</span></span></label>
      <label class="row" data-find="restore-free">${switchHtml(restore.free).replace("<input", `<input data-path="playerok.auto_restore_items.free"`)}<span><b>Бесплатные</b><span>Восстанавливать лоты без премиума.</span></span></label>
      <label class="row" data-find="restore-premium">${switchHtml(restore.premium).replace("<input", `<input data-path="playerok.auto_restore_items.premium"`)}<span><b>Премиум</b><span>Восстанавливать лоты с платным статусом.</span></span></label>
    </section>
    <section class="card">
      <h2>Как публиковать</h2>
      <p class="sub">Премиум-товары Playerok всё равно восстанавливает премиумом, за это списывается баланс.</p>
      ${seg([["last", "Как был"], ["premium", "Премиум"], ["free", "Бесплатный"]], restore.publish_status, `data-path="playerok.auto_restore_items.publish_status"`, "restore-publish")}
      <label class="row" data-find="restore-recreate">${switchHtml(restore.recreate).replace("<input", `<input data-path="playerok.auto_restore_items.recreate"`)}<span><b>Пересоздавать</b><span>Для категорий, которые нельзя выставить повторно. Бот создаёт новый лот и удаляет старый.</span></span></label>
      <p class="warn">Пересоздание создаёт и удаляет товары автоматически. Правила Playerok это ограничивают, включайте только если обычное восстановление не срабатывает.</p>
    </section>
    <section class="card">
      <h2>Какие товары</h2>
      ${seg([["all", "Все товары"], ["picked", "Только выбранные"]], restore.all ? "all" : "picked", `data-flag="playerok.auto_restore_items.all"`, "restore-which")}
      ${itemsBlock("restore")}
    </section>`;
}

function dealsPage() {
  const complete = state.settings.complete;
  return `
    <h1 data-find="deals-page">Сделки</h1>
    <p class="lead">Бот может сам подтверждать выполнение заказа.</p>
    <section class="card">
      <label class="row" data-find="deals-enabled">${switchHtml(complete.enabled).replace("<input", `<input data-path="playerok.auto_complete_deals.enabled"`)}<span><b>Подтверждать сделки</b><span>После выдачи бот отмечает заказ выполненным.</span></span></label>
      ${seg([["all", "Все товары"], ["picked", "Только выбранные"]], complete.all ? "all" : "picked", `data-flag="playerok.auto_complete_deals.all"`, "deals-which")}
      ${itemsBlock("complete")}
    </section>`;
}

function greetingLotsBlock(greeting) {
  const selected = new Set((greeting.lots || []).map((lot) => lot.id));
  const pool = snipeMyLots.length ? snipeMyLots : (state.catalog || []);
  if (!pool.length) {
    return `<div class="empty">Лоты подгрузятся с Playerok. Откройте вкладку ещё раз или нажмите «Обновить лоты».</div>`;
  }
  return pool.map((item) => {
    const on = selected.has(item.id);
    const price = item.price == null ? "" : item.price + " ₽";
    const badge = [price, item.priority].filter(Boolean).join(" · ");
    return `
      <label class="item">
        <span class="grow">
          <div class="name">${esc(item.name || item.id)}</div>
          <div class="meta">${esc(badge || "лот")}</div>
        </span>
        ${switchHtml(on).replace("<input", `<input data-greet-lot="${esc(greeting.id)}" data-item-id="${esc(item.id)}" data-item-name="${esc(item.name || "")}" data-item-slug="${esc(item.slug || "")}"`)}
      </label>`;
  }).join("");
}

function greetingsBlock() {
  const greetings = state.greetings || [];
  const cards = greetings.map((greeting) => {
    const lotCount = (greeting.lots || []).length;
    return `
    <section class="card" data-find="greet-${esc(greeting.id)}">
      <div class="snipe-form">
        <input class="textin" data-greet-title="${esc(greeting.id)}" value="${esc(greeting.title)}" placeholder="Название приветствия">
        <button type="button" class="mini" data-greet-delete="${esc(greeting.id)}">Удалить</button>
      </div>
      <label class="row">${switchHtml(greeting.enabled).replace("<input", `<input data-greet-enabled="${esc(greeting.id)}"`)}<span><b>Отправлять</b><span>На каждую сделку по отмеченным лотам.</span></span></label>
      <textarea data-greet-text="${esc(greeting.id)}">${esc(greeting.text)}</textarea>
      <p class="sub">Лоты для этого приветствия · выбрано ${esc(String(lotCount))}</p>
      ${greetingLotsBlock(greeting)}
    </section>`;
  }).join("");
  return `
    <section class="card" data-find="greetings-page">
      <h2>Приветствия по лотам</h2>
      <p class="sub">Несколько текстов — у каждого свои лоты галочками. На сделку уйдёт приветствие того лота, который купили. Если лот никуда не привязан — сработает обычное «Приветствие» (только новым покупателям).</p>
      <button type="button" class="btn" data-greet-add>Добавить приветствие</button>
      <button type="button" class="mini" data-greet-refresh>Обновить лоты</button>
    </section>
    ${cards || `<div class="empty">Пока нет отдельных приветствий — используется «Приветствие» по умолчанию.</div>`}`;
}

function messagesPage() {
  const cards = MESSAGES.map(([id, hint]) => {
    const message = state.messages[id];
    if (!message) return "";
    return `
      <section class="card" data-find="msg-${id}">
        <h2>${esc(message.label)}</h2>
        <p class="sub">${esc(hint)}</p>
        <label class="row">${switchHtml(message.enabled).replace("<input", `<input data-message="${id}"`)}<span><b>Отправлять</b><span>Выключенное сообщение бот не пишет.</span></span></label>
        <textarea data-text="${id}">${esc(message.text)}</textarea>
      </section>`;
  }).join("");
  return `<h1 data-find="messages-page">Сообщения</h1><p class="lead">Текст уходит покупателю в чат Playerok. Правка сохраняется, когда вы уходите из поля.</p>${greetingsBlock()}${cards}`;
}

function notificationsPage() {
  const notes = state.settings.notifications;
  const rows = EVENTS.map(([id, title, hint]) => `
    <label class="row" data-find="event-${id}">${switchHtml(notes.events[id]).replace("<input", `<input data-path="playerok.notifications.events.${id}"`)}<span><b>${esc(title)}</b><span>${esc(hint)}</span></span></label>
  `).join("");
  return `
    <h1 data-find="notes-page">Уведомления</h1>
    <p class="lead">Сообщения приходят в Telegram. Здесь только то, о чём писать.</p>
    <section class="card">
      <label class="row" data-find="notes-enabled">${switchHtml(notes.enabled).replace("<input", `<input data-path="playerok.notifications.enabled"`)}<span><b>Писать в Telegram</b><span>Общий выключатель всех уведомлений.</span></span></label>
      ${rows}
    </section>`;
}

function generalPage() {
  const s = state.settings;
  return `
    <h1 data-find="general-page">Общее</h1>
    <p class="lead">Подпись, связь с чатом и редкие служебные параметры.</p>
    <section class="card">
      <h2>Чат</h2>
      <label class="row" data-find="general-read">${switchHtml(s.read_chat).replace("<input", `<input data-path="playerok.read_chat"`)}<span><b>Отмечать чат прочитанным</b><span>После ответа бота чат не висит непрочитанным.</span></span></label>
      <label class="row" data-find="general-watermark">${switchHtml(s.watermark.enabled).replace("<input", `<input data-path="playerok.watermark.enabled"`)}<span><b>Подпись внизу сообщения</b><span>Добавляется к ответам бота.</span></span></label>
      <input class="textin" data-watermark data-find="general-watermark-text" value="${esc(s.watermark.value)}" placeholder="Текст подписи">
    </section>
    <section class="card">
      <h2>Вывод</h2>
      <p class="sub">Карта, телефон и адрес здесь не показываются. Меняется только ритм.</p>
      <label class="row" data-find="general-withdraw">${switchHtml(s.withdrawal.enabled).replace("<input", `<input data-path="playerok.auto_withdrawal.enabled"`)}<span><b>Выводить автоматически</b><span>${s.withdrawal.ready ? "Реквизиты уже заданы." : "Сначала укажите реквизиты в Telegram."}</span></span></label>
      ${slider("Как часто", "Пауза между запросами вывода.", s.withdrawal.interval, 3600, 604800, 3600, duration(s.withdrawal.interval), `data-range="playerok.auto_withdrawal.interval" data-label="time"`, "general-withdraw-interval")}
    </section>
    <section class="card">
      <h2>Связь и журнал</h2>
      ${slider("Ожидание ответа Playerok", "Сколько ждать сайт, прежде чем считать запрос неудачным.", s.timeout, 5, 120, 5, s.timeout + " сек", `data-range="playerok.api.requests_timeout" data-label="sec"`, "general-timeout")}
      ${slider("Размер журнала", "Когда файл лога достигнет этого размера, он начнётся заново.", s.logs.max_file_size, 64, 4096, 64, s.logs.max_file_size + " КБ", `data-range="logs.max_file_size" data-label="kb"`, "general-logs")}
      <label class="row" data-find="general-update">${switchHtml(s.updates.auto_update).replace("<input", `<input data-path="updates.auto_update"`)}<span><b>Ставить обновления бота</b><span>Официальное обновление может заменить ваши доработки в основных файлах.</span></span></label>
      <label class="row" data-find="general-update-notify">${switchHtml(s.updates.notify).replace("<input", `<input data-path="updates.notify"`)}<span><b>Сообщать об обновлении</b><span>Уведомление в Telegram, когда вышла новая версия.</span></span></label>
    </section>`;
}

function overviewPage() {
  const a = state.account;
  const s = state.settings;
  const name = a.username || "Магазин";
  const money = a.balance || "—";
  return `
    <section class="hero" data-find="overview-page">
      <p class="who">${esc(name)}</p>
      <div class="money">${esc(money)}</div>
      <div class="stats">
        <div class="stat"><b>${esc(a.available || "—")}</b><span>доступно</span></div>
        <div class="stat"><b>${esc(a.pending || "—")}</b><span>в ожидании</span></div>
        <div class="stat"><b>${esc(a.frozen || "—")}</b><span>заморожено</span></div>
        <div class="stat"><b>${esc(a.sales || "0")}</b><span>продажи</span></div>
      </div>
    </section>
    <div class="grid">
      <section class="card">
        <h2>Быстрые ползунки</h2>
        <p class="sub">То же самое, что в остальных разделах. Бот подхватывает правку примерно за 3 секунды.</p>
        <label class="row" data-find="overview-bump">${switchHtml(s.bump.enabled).replace("<input", `<input data-path="playerok.auto_bump_items.enabled"`)}<span><b>Поднятие</b><span>${s.bump.mode === "position" ? "Держать около " + s.bump.position + " места" : "Каждые " + duration(s.bump.interval)}</span></span></label>
        <label class="row" data-find="overview-restore">${switchHtml(s.restore.sold).replace("<input", `<input data-path="playerok.auto_restore_items.sold"`)}<span><b>Восстанавливать продажи</b><span>Сразу выставлять купленный товар снова.</span></span></label>
        <label class="row" data-find="overview-complete">${switchHtml(s.complete.enabled).replace("<input", `<input data-path="playerok.auto_complete_deals.enabled"`)}<span><b>Закрывать сделки</b><span>Отмечать заказ выполненным.</span></span></label>
        <label class="row" data-find="overview-notes">${switchHtml(s.notifications.enabled).replace("<input", `<input data-path="playerok.notifications.enabled"`)}<span><b>Уведомления</b><span>${a.telegram ? "@" + a.telegram : "Telegram"}</span></span></label>
      </section>
      <section class="card">
        <h2>Сейчас</h2>
        <p class="sub">Версия бота ${esc(state.version || "1.22.6")}. Цифры баланса берутся из последнего запуска.</p>
        <label class="row" data-find="overview-read">${switchHtml(s.read_chat).replace("<input", `<input data-path="playerok.read_chat"`)}<span><b>Читать чаты</b><span>Отмечать диалог прочитанным после ответа.</span></span></label>
        <label class="row" data-find="overview-watermark">${switchHtml(s.watermark.enabled).replace("<input", `<input data-path="playerok.watermark.enabled"`)}<span><b>Подпись сообщений</b><span>${s.watermark.enabled ? s.watermark.value : "Скрыта"}</span></span></label>
        <label class="row" data-find="overview-withdraw">${switchHtml(s.withdrawal.enabled).replace("<input", `<input data-path="playerok.auto_withdrawal.enabled"`)}<span><b>Автовывод</b><span>${s.withdrawal.enabled ? "Каждые " + duration(s.withdrawal.interval) : "Выключен"}</span></span></label>
      </section>
    </div>`;
}

const STOCK_STATUS = {
  free: "в очереди",
  listed: "на лоте",
  sold: "продан",
};

function stockItemsBlock() {
  const needle = query.trim().toLowerCase();
  const picked = new Set((state.stock && state.stock.items) || []);
  const items = (state.catalog || []).filter((item) =>
    !needle || (item.name || "").toLowerCase().includes(needle)
  );
  const rows = items.map((item) => {
    const on = picked.has(item.id);
    const price = item.price == null ? "" : item.price + " ₽";
    const badge = [price, item.priority].filter(Boolean).join(" · ");
    const image = item.image
      ? `<img src="${esc(item.image)}" alt="">`
      : `<div class="ph"></div>`;
    return `
      <label class="item">
        ${image}
        <span class="grow">
          <div class="name">${esc(item.name)}</div>
          <div class="meta">${esc(badge || "товар")}</div>
        </span>
        ${switchHtml(on).replace("<input", `<input data-stock-item="${esc(item.id)}"`)}
      </label>`;
  }).join("");
  return `
    <p class="sub">Общий список лотов. У каждого аккаунта в строке выбран свой лот: бот выдаёт его только туда.</p>
    <input class="search" data-kind="stock" placeholder="Найти товар" value="${esc(query)}">
    ${rows || `<div class="empty">Товаров пока нет. Они появятся, когда бот увидит ваши лоты. Отметьте лот здесь, когда он появится.</div>`}
  `;
}

function stockCounts(accounts) {
  const counts = { free: 0, listed: 0, sold: 0 };
  for (const account of accounts || []) {
    if (counts[account.status] != null) counts[account.status] += 1;
  }
  return counts;
}

function lotKind(item) {
  const priority = String(item.priority || "").toUpperCase();
  if (priority === "PREMIUM") return "премиум";
  if (priority === "DEFAULT" || priority === "FREE") return "обычный";
  return priority ? priority.toLowerCase() : "";
}

function lotLabel(item) {
  const price = item.price == null || item.price === "" ? "" : item.price + " ₽";
  return [lotKind(item), price, item.name].filter(Boolean).join(" · ");
}

function lotSelect(account, index, role) {
  const items = state.catalog || [];
  const current = account.item_id || "";
  const known = items.some((item) => item.id === current);
  const options = [`<option value="">Лот не выбран</option>`];
  if (current && !known) {
    options.push(`<option value="${esc(current)}" selected>Сохранённый лот</option>`);
  }
  options.push(...items.map((item) =>
    `<option value="${esc(item.id)}" ${item.id === current ? "selected" : ""}>${esc(lotLabel(item))}</option>`
  ));
  const empty = items.length ? "" : `<p class="meta">Товары появятся, когда бот увидит лоты. Привязка сохранится.</p>`;
  return `${empty}<select class="lot" data-stock-lot data-index="${index}" data-role="${role}" title="Лот аккаунта">${options.join("")}</select>`;
}

function markSelect(account, index, role) {
  const options = Object.entries(STOCK_STATUS).map(([value, label]) =>
    `<option value="${value}" ${account.status === value ? "selected" : ""}>${label}</option>`
  ).join("");
  return `<select class="mark" data-stock-status data-index="${index}" data-role="${role}">${options}</select>`;
}

function editorBlock(account, index, role) {
  const open = editing && editing.role === role && editing.index === index;
  if (!open) return "";
  const fields = [
    ["login", "Логин", account.login],
    ["password", "Пароль", account.password],
    ["email", "Почта", account.email],
    ["email_password", "Пароль почты", account.email_password],
  ];
  const inputs = fields.map(([key, placeholder, value]) =>
    `<input class="textin" data-edit="${key}" placeholder="${placeholder}" value="${esc(value || "")}" autocomplete="off" spellcheck="false">`
  ).join("");
  return `
    <div class="stock-editor">
      ${inputs}
      <input class="textin wide" data-edit="mail_url" placeholder="Сайт почты" value="${esc(account.mail_url || "")}" autocomplete="off" spellcheck="false">
      <div class="actions">
        <button type="button" class="btn" data-stock-save data-index="${index}" data-role="${role}">Сохранить</button>
        <button type="button" class="mini" data-stock-cancel>Отмена</button>
      </div>
    </div>`;
}

function queueBlock(queue) {
  const accounts = queue.accounts || [];
  const counts = stockCounts(accounts);
  const role = esc(queue.role || "primary");
  const rows = accounts.map((account, index) => {
    const mail = account.mail_url || "без сайта";
    return `
      <div class="stock-item" data-index="${index}" data-role="${role}">
        <div class="stock-line">
          <button type="button" class="grip" data-drag aria-label="Перетащить">⋮⋮</button>
          <span class="grow">
            <div class="name">${esc(account.login)}</div>
            <div class="meta">${esc(account.email || "без почты")} · ${esc(mail)}</div>
          </span>
          ${lotSelect(account, index, role)}
          ${markSelect(account, index, role)}
          <span class="stock-actions">
            <button type="button" class="mini" data-stock-edit="${index}" data-role="${role}">Изменить</button>
            <button type="button" class="mini danger" data-stock-delete="${index}" data-role="${role}">Удалить</button>
          </span>
        </div>
        ${editorBlock(account, index, queue.role || "primary")}
      </div>`;
  }).join("");
  const pending = queue.pending_login
    ? `<p class="warn">Незавершённая выдача запомнила логин ${esc(queue.pending_login)}. Пока отметка висит, бот снова возьмёт его. <button type="button" class="mini" data-stock-clear data-role="${role}">Снять отметку</button></p>`
    : "";
  const active = queue.active_login
    ? `<p class="sub">Сейчас на лоте отмечен ${esc(queue.active_login)}.</p>`
    : "";
  return `
    <section class="card" data-find="stock-pool-${role}">
      <h2>${esc(queue.title || "Очередь")}</h2>
      <p class="note">Перетащите строку, чтобы изменить порядок выдачи внутри лота. «На лоте» может быть только один аккаунт на каждый лот.</p>
      <div class="counts">
        <div class="count"><b>${counts.free}</b><span>в очереди</span></div>
        <div class="count"><b>${counts.listed}</b><span>на лоте</span></div>
        <div class="count"><b>${counts.sold}</b><span>продано</span></div>
      </div>
      ${active}
      ${pending}
      <div class="stock-form">
        <input class="textin" data-account="login" placeholder="Логин" autocomplete="off" spellcheck="false">
        <input class="textin" data-account="password" placeholder="Пароль" autocomplete="off" spellcheck="false">
        <input class="textin" data-account="email" placeholder="Почта" autocomplete="off" spellcheck="false">
        <input class="textin" data-account="email_password" placeholder="Пароль почты" autocomplete="off" spellcheck="false">
        <input class="textin wide" data-account="mail_url" placeholder="Сайт почты" autocomplete="off" spellcheck="false">
        <button type="button" class="btn wide" data-stock-add data-role="${role}">Добавить</button>
      </div>
      ${rows || `<div class="empty">В этой очереди пока нет аккаунтов.</div>`}
    </section>`;
}

function accountsPage() {
  const stock = state.stock || {};
  const queues = stock.queues || [];
  const primary = queues.find((queue) => queue.role === "primary") || { role: "primary", title: "Дешёвые", accounts: [] };
  const reserve = queues.find((queue) => queue.role === "reserve") || { role: "reserve", title: "Дорогие", accounts: [] };
  const note = stock.need_reserve
    ? `<p class="warn">Следующая выдача возьмёт один аккаунт из очереди «Дорогие», затем снова пойдут дешёвые.</p>`
    : "";
  return `
    <h1 data-find="accounts-page">Аккаунты</h1>
    <p class="lead" data-find="stock-intro">Обычный и премиум лот берут аккаунты из одной очереди. На лотах сейчас стоят свои аккаунты; после продажи любого лота на него встаёт следующий свободный (3-й, если занят — 4-й и дальше). У выданного аккаунта остаётся пометка лота.</p>
    <p class="note" data-find="stock-marks">«На лоте» — сейчас на товаре. «Продан» откреплён и больше не выдаётся. «В очереди» ждёт своей очереди. Порядок строк меняют перетаскиванием.</p>
    ${note}
    <section class="card">
      <label class="row" data-find="stock-reserve">${switchHtml(!!stock.need_reserve).replace("<input", `<input data-stock-reserve`)}<span><b>Следующий аккаунт из дорогих</b><span>Один раз после проблемы по лоту. Потом очередь снова берёт дешёвые.</span></span></label>
    </section>
    <section class="card" data-find="stock-lot">
      <h2>Лот</h2>
      ${stockItemsBlock()}
    </section>
    ${queueBlock(primary)}
    ${queueBlock(reserve)}`;
}

function errorsPage() {
  const cases = state.errors || [];
  const selected = cases.find((row) => row.id === selectedError) || null;
  if (selected) {
    return `
      <h1 data-find="errors-page">Ошибки</h1>
      <p class="lead">Клиент <b>${esc(selected.username)}</b> написал !ошибка. Раздел будем расширять.</p>
      <section class="card">
        <h2>👤 ${esc(selected.username)}</h2>
        <p class="sub">Чат открыт с ${esc(selected.created_at || "—")}. ${selected.item_name ? "Товар: " + esc(selected.item_name) + "." : ""}</p>
        <button type="button" class="btn wide" data-error-issue="${esc(selected.id)}">Выдать доп аккаунт</button>
        <p class="note">Аккаунт уйдёт из очереди «Дорогие» и будет отправлен покупателю в чат Playerok.</p>
        <button type="button" class="mini" data-error-back>Назад к списку</button>
      </section>`;
  }
  const rows = cases.map((row) => `
    <button type="button" class="error-btn" data-error-open="${esc(row.id)}">
      <b>👤 ${esc(row.username || "клиент")}</b>
      <span>${esc(row.created_at || "")}</span>
    </button>
  `).join("");
  return `
    <h1 data-find="errors-page">Ошибки</h1>
    <p class="lead" data-find="errors-intro">Здесь появляются покупатели, которые написали <b>!ошибка</b> или <b>ошибка</b>. Нажмите на ник — откроется решение.</p>
    <section class="card" data-find="errors-list">
      ${rows || `<div class="empty">Активных ошибок нет.</div>`}
    </section>`;
}

const VIEWS = {
  overview: overviewPage,
  bump: bumpPage,
  snipe: snipePage,
  restore: restorePage,
  accounts: accountsPage,
  errors: errorsPage,
  deals: dealsPage,
  messages: messagesPage,
  notifications: notificationsPage,
  general: generalPage,
};

function paintStatus() {
  topStatus.textContent = state && state.alive ? "Бот на связи" : "Нет свежих записей";
  topStatus.classList.toggle("on", !!(state && state.alive));
}

function renderNav() {
  nav.innerHTML = PAGES.map(([id, label]) =>
    `<button type="button" class="nav-btn ${id === page ? "on" : ""}" data-page="${id}">${label}</button>`
  ).join("");
}

function render() {
  if (!state) return;
  renderNav();
  paintStatus();
  main.innerHTML = VIEWS[page]();
  main.querySelectorAll('input[type="range"]').forEach(paint);
}

function rangeValue(input) {
  if (input.dataset.label === "hour") {
    return String(input.value).padStart(2, "0") + ":00";
  }
  return Number(input.value);
}

function rangeLabel(input) {
  if (input.dataset.label === "hour") return hourLabel(String(input.value).padStart(2, "0") + ":00");
  if (input.dataset.label === "place") return input.value + " место";
  if (input.dataset.label === "sec") return input.value + " сек";
  if (input.dataset.label === "kb") return input.value + " КБ";
  if (input.dataset.label === "rub") return input.value + " ₽";
  return duration(input.value);
}

main.addEventListener("input", (event) => {
  const input = event.target;
  if (input.matches("[data-settings-search]")) return;
  if (input.matches("[type=range]")) {
    dragging = true;
    paint(input);
    const strong = input.parentElement.querySelector("strong");
    if (strong) strong.textContent = rangeLabel(input);
  }
  if (input.matches(".search")) {
    query = input.value;
    const card = input.closest(".card");
    const start = input.selectionStart;
    const kind = input.dataset.kind || pageKind();
    const html = kind === "stock" ? stockItemsBlock() : itemsBlock(kind);
    const holder = document.createElement("div");
    holder.innerHTML = html;
    const fresh = holder.querySelector(".search");
    input.parentElement.querySelectorAll(".item, .empty, .sub").forEach((node) => {
      if (node.parentElement === input.parentElement || node.closest(".card") === card) {
        /* keep */
      }
    });
    card.querySelectorAll(".item, .empty").forEach((node) => node.remove());
    card.querySelector(".sub").textContent = holder.querySelector(".sub").textContent;
    card.append(...holder.querySelectorAll(".item, .empty"));
    input.focus();
    input.setSelectionRange(start, start);
  }
});

function pageKind() {
  if (page === "restore") return "restore";
  if (page === "deals") return "complete";
  if (page === "accounts") return "stock";
  return "bump";
}

main.addEventListener("change", async (event) => {
  const input = event.target;
  if (input.matches("[type=range]")) {
    dragging = false;
    const value = rangeValue(input);
    await saveSetting(input.dataset.range, value, false);
    return;
  }
  if (input.matches("[data-path]")) {
    await saveSetting(input.dataset.path, input.checked, true);
    return;
  }
  if (input.matches("[data-item]")) {
    await toggleItem(input.dataset.kind, input.dataset.item, input.checked);
    return;
  }
  if (input.matches("[data-stock-reserve]")) {
    try {
      await post("/api/stock", { action: "reserve", enabled: input.checked }, true);
    } catch (error) {
      toast(error.message, true);
      await reload(true);
    }
    return;
  }
  if (input.matches("[data-stock-status]")) {
    try {
      await post("/api/stock", {
        action: "status",
        role: input.dataset.role || "primary",
        index: Number(input.dataset.index),
        status: input.value,
      }, true);
    } catch (error) {
      toast(error.message, true);
      await reload(true);
    }
    return;
  }
  if (input.matches("[data-stock-lot]")) {
    try {
      await post("/api/stock", {
        action: "lot",
        role: input.dataset.role || "primary",
        index: Number(input.dataset.index),
        item_id: input.value,
      }, true);
    } catch (error) {
      toast(error.message, true);
      await reload(true);
    }
    return;
  }
  if (input.matches("[data-stock-item]")) {
    const items = new Set((state.stock && state.stock.items) || []);
    if (input.checked) items.add(input.dataset.stockItem);
    else items.delete(input.dataset.stockItem);
    try {
      await post("/api/stock", { action: "bind", items: [...items] }, true);
    } catch (error) {
      toast(error.message, true);
      await reload(true);
    }
  }
  if (input.matches("[data-message]")) {
    const id = input.dataset.message;
    const text = main.querySelector(`[data-text="${id}"]`).value;
    try {
      await post("/api/message", { id, enabled: input.checked, text }, true);
    } catch (error) {
      toast(error.message, true);
      await reload(true);
    }
  }
  if (input.matches("[data-snipe-comp]")) {
    const id = input.dataset.snipeComp;
    if (input.checked) snipeSelected[id] = true;
    else delete snipeSelected[id];
    render();
    return;
  }
  if (input.matches("[data-snipe-mine]")) {
    const id = input.dataset.snipeMine;
    if (input.value) snipeMyPick[id] = input.value;
    else delete snipeMyPick[id];
  }
  if (input.matches("[data-greet-enabled]")) {
    try {
      await post("/api/greetings", {
        action: "edit",
        id: input.dataset.greetEnabled,
        enabled: input.checked,
      }, true);
    } catch (error) {
      toast(error.message, true);
      await reload(true);
    }
    return;
  }
  if (input.matches("[data-greet-lot]")) {
    const gid = input.dataset.greetLot;
    const greeting = (state.greetings || []).find((row) => row.id === gid);
    if (!greeting) return;
    const lots = (greeting.lots || []).slice();
    const itemId = input.dataset.itemId;
    const next = input.checked
      ? [...lots.filter((lot) => lot.id !== itemId), {
          id: itemId,
          name: input.dataset.itemName || "",
          slug: input.dataset.itemSlug || "",
        }]
      : lots.filter((lot) => lot.id !== itemId);
    try {
      await post("/api/greetings", { action: "edit", id: gid, lots: next }, true);
    } catch (error) {
      toast(error.message, true);
      await reload(true);
    }
  }
});

let stockDrag = null;
let suppressStockClick = false;

function placeStockDrag(clientY) {
  const dy = clientY - stockDrag.startY;
  if (Math.abs(dy) > 4) stockDrag.moved = true;
  const shift = Math.round(dy / stockDrag.height);
  let over = stockDrag.index + shift;
  over = Math.max(0, Math.min(stockDrag.rows.length - 1, over));
  stockDrag.over = over;
  const { rows, index, item, height } = stockDrag;
  rows.forEach((row, i) => {
    if (row === item) {
      row.style.transform = `translateY(${dy}px)`;
      return;
    }
    let offset = 0;
    if (index < over && i > index && i <= over) offset = -height;
    else if (index > over && i >= over && i < index) offset = height;
    row.style.transform = offset ? `translateY(${offset}px)` : "";
  });
}

function finishStockDrag() {
  if (!stockDrag) return null;
  const drag = stockDrag;
  stockDrag = null;
  const delta = (drag.over - drag.index) * drag.height;
  drag.item.classList.remove("dragging");
  drag.item.classList.add("settling");
  void drag.item.offsetWidth;
  drag.item.style.transform = `translateY(${delta}px)`;
  return drag;
}

main.addEventListener("pointerdown", (event) => {
  if (event.button !== 0) return;
  if (event.target.closest("button, select, input, textarea, a, option") && !event.target.closest("[data-drag]")) return;
  const line = event.target.closest(".stock-line");
  if (!line || !main.contains(line)) return;
  const item = line.closest(".stock-item");
  if (!item) return;
  const rows = [...item.parentElement.querySelectorAll(":scope > .stock-item")];
  const index = rows.indexOf(item);
  if (index < 0) return;
  stockDrag = {
    pointer: event.pointerId,
    item,
    rows,
    index,
    over: index,
    startY: event.clientY,
    height: Math.max(item.getBoundingClientRect().height, 1),
    role: item.dataset.role || "primary",
    moved: false,
  };
  item.classList.add("dragging");
  try { item.setPointerCapture(event.pointerId); } catch (error) { /* synthetic pointer */ }
  event.preventDefault();
});

main.addEventListener("pointermove", (event) => {
  if (!stockDrag || event.pointerId !== stockDrag.pointer) return;
  placeStockDrag(event.clientY);
});

main.addEventListener("pointerup", async (event) => {
  if (!stockDrag || event.pointerId !== stockDrag.pointer) return;
  const drag = finishStockDrag();
  if (!drag) return;
  if (drag.moved) suppressStockClick = true;
  window.setTimeout(async () => {
    drag.rows.forEach((row) => {
      row.style.transform = "";
      row.classList.remove("dragging", "settling");
    });
    if (!drag.moved || drag.index === drag.over) return;
    try {
      editing = null;
      await post("/api/stock", {
        action: "reorder",
        role: drag.role,
        index: drag.index,
        to: drag.over,
      }, true);
    } catch (error) {
      toast(error.message, true);
      await reload(true);
    }
  }, 180);
});

main.addEventListener("pointercancel", (event) => {
  if (!stockDrag || event.pointerId !== stockDrag.pointer) return;
  const drag = finishStockDrag();
  if (!drag) return;
  drag.rows.forEach((row) => {
    row.style.transform = "";
    row.classList.remove("dragging", "settling");
  });
});

main.addEventListener("click", (event) => {
  if (!suppressStockClick) return;
  suppressStockClick = false;
  event.preventDefault();
  event.stopPropagation();
}, true);

main.addEventListener("click", async (event) => {
  const greetAdd = event.target.closest("[data-greet-add]");
  if (greetAdd) {
    try {
      if (!snipeMyLots.length) await loadSnipeMyLots(true);
      await post("/api/greetings", {
        action: "add",
        title: "Новое приветствие",
        text: "Здравствуйте, {user.username}!\n\nСпасибо за покупку «{item.name}».",
        lots: [],
      }, true);
    } catch (error) {
      toast(error.message, true);
      await reload(true);
    }
    return;
  }
  const greetDelete = event.target.closest("[data-greet-delete]");
  if (greetDelete) {
    try {
      await post("/api/greetings", { action: "delete", id: greetDelete.dataset.greetDelete }, true);
    } catch (error) {
      toast(error.message, true);
      await reload(true);
    }
    return;
  }
  const greetRefresh = event.target.closest("[data-greet-refresh]");
  if (greetRefresh) {
    await loadSnipeMyLots(true);
    render();
    toast("Лоты обновлены");
    return;
  }
  const snipeResolve = event.target.closest("[data-snipe-resolve]");
  if (snipeResolve) {
    const input = main.querySelector("[data-snipe-query]");
    snipeQuery = input ? input.value.trim() : snipeQuery;
    try {
      const response = await fetch("/api/snipe/resolve", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ query: snipeQuery }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || "не удалось найти продавца");
      snipeDraft = data;
      snipeSelected = {};
      snipeMyPick = {};
      if (Array.isArray(data.my_items)) snipeMyLots = data.my_items;
      else await loadSnipeMyLots(true);
      toast("Продавец найден");
      render();
    } catch (error) {
      toast(error.message, true);
    }
    return;
  }
  const snipeAdd = event.target.closest("[data-snipe-add]");
  if (snipeAdd) {
    if (!snipeDraft) return;
    const links = (snipeDraft.items || [])
      .filter((item) => snipeSelected[item.id] && snipeMyPick[item.id])
      .map((item) => {
        const mine = snipeMyLots.find((row) => row.id === snipeMyPick[item.id])
          || catalogItem(snipeMyPick[item.id]);
        return {
          competitor_item_id: item.id,
          competitor_slug: item.slug || "",
          competitor_name: item.name || "",
          my_item_id: snipeMyPick[item.id],
          my_name: (mine && mine.name) || snipeMyPick[item.id],
        };
      });
    if (!links.length) {
      toast("Отметьте лоты и выберите свои", true);
      return;
    }
    try {
      await post("/api/snipe/targets", {
        action: "add",
        username: snipeDraft.username,
        user_id: snipeDraft.user_id,
        links,
      }, true);
      snipeSelected = {};
      snipeMyPick = {};
    } catch (error) {
      toast(error.message, true);
      await reload(true);
    }
    return;
  }
  const snipeRemove = event.target.closest("[data-snipe-remove]");
  if (snipeRemove) {
    try {
      await post("/api/snipe/targets", {
        action: "remove",
        competitor_item_id: snipeRemove.dataset.competitor,
        my_item_id: snipeRemove.dataset.mine,
      }, true);
    } catch (error) {
      toast(error.message, true);
      await reload(true);
    }
    return;
  }
  const stockButton = event.target.closest("[data-stock-add], [data-stock-delete], [data-stock-move], [data-stock-edit], [data-stock-save], [data-stock-cancel], [data-stock-transfer], [data-stock-clear]");
  if (stockButton) {
    try {
      const role = stockButton.dataset.role || "primary";
      if (stockButton.hasAttribute("data-stock-edit")) {
        const index = Number(stockButton.dataset.stockEdit);
        editing = editing && editing.role === role && editing.index === index ? null : { role, index };
        render();
        return;
      }
      if (stockButton.hasAttribute("data-stock-cancel")) {
        editing = null;
        render();
        return;
      }
      if (stockButton.hasAttribute("data-stock-save")) {
        const editor = stockButton.closest(".stock-editor");
        const body = {
          action: "edit",
          role,
          index: Number(stockButton.dataset.index),
        };
        for (const key of ["login", "password", "email", "email_password", "mail_url"]) {
          const node = editor ? editor.querySelector(`[data-edit="${key}"]`) : null;
          body[key] = node ? node.value : "";
        }
        await post("/api/stock", body, false);
        editing = null;
        render();
        return;
      }
      if (stockButton.hasAttribute("data-stock-transfer")) {
        editing = null;
        await post("/api/stock", {
          action: "transfer",
          role,
          index: Number(stockButton.dataset.index),
        }, true);
        return;
      }
      if (stockButton.hasAttribute("data-stock-clear")) {
        editing = null;
        await post("/api/stock", { action: "clear_pending", role }, true);
        return;
      }
      if (stockButton.hasAttribute("data-stock-add")) {
        const card = stockButton.closest(".card");
        const body = { action: "add", role };
        for (const key of ["login", "password", "email", "email_password", "mail_url"]) {
          const node = card ? card.querySelector(`[data-account="${key}"]`) : null;
          body[key] = node ? node.value : "";
        }
        editing = null;
        await post("/api/stock", body, true);
        return;
      }
      if (stockButton.hasAttribute("data-stock-delete")) {
        editing = null;
        await post("/api/stock", {
          action: "delete",
          role,
          index: Number(stockButton.dataset.stockDelete),
        }, true);
        return;
      }
      editing = null;
      await post("/api/stock", {
        action: "move",
        role,
        index: Number(stockButton.dataset.index),
        direction: Number(stockButton.dataset.stockMove),
      }, true);
    } catch (error) {
      toast(error.message, true);
      await reload(true);
    }
    return;
  }
  const button = event.target.closest("[data-seg], [data-flag]");
  if (!button) return;
  if (button.dataset.seg && button.dataset.path) {
    await saveSetting(button.dataset.path, button.dataset.seg, true);
    return;
  }
  if (button.dataset.flag) {
    await saveSetting(button.dataset.flag, button.dataset.seg === "all", true);
  }
});

main.addEventListener("focusout", async (event) => {
  const input = event.target;
  if (input.matches("[data-snipe-query]")) {
    snipeQuery = input.value;
    return;
  }
  if (input.matches("[data-greet-title]")) {
    const gid = input.dataset.greetTitle;
    const greeting = (state.greetings || []).find((row) => row.id === gid);
    if (!greeting || input.value.trim() === greeting.title) return;
    try {
      await post("/api/greetings", { action: "edit", id: gid, title: input.value }, false);
    } catch (error) {
      toast(error.message, true);
    }
    return;
  }
  if (input.matches("[data-greet-text]")) {
    const gid = input.dataset.greetText;
    const greeting = (state.greetings || []).find((row) => row.id === gid);
    if (!greeting || input.value === greeting.text) return;
    try {
      await post("/api/greetings", { action: "edit", id: gid, text: input.value }, false);
    } catch (error) {
      toast(error.message, true);
    }
    return;
  }
  if (input.matches("[data-text]")) {
    const id = input.dataset.text;
    const enabled = main.querySelector(`[data-message="${id}"]`).checked;
    if (input.value === state.messages[id].text) return;
    try {
      await post("/api/message", { id, enabled, text: input.value }, false);
    } catch (error) {
      toast(error.message, true);
    }
    return;
  }
  if (input.matches("[data-watermark]")) {
    if (input.value.trim() === state.settings.watermark.value) return;
    await saveSetting("playerok.watermark.value", input.value, false);
  }
});

document.body.addEventListener("click", (event) => {
  const errorOpen = event.target.closest("[data-error-open]");
  if (errorOpen) {
    selectedError = errorOpen.dataset.errorOpen || "";
    render();
    return;
  }
  if (event.target.closest("[data-error-back]")) {
    selectedError = "";
    render();
    return;
  }
  const errorIssue = event.target.closest("[data-error-issue]");
  if (errorIssue) {
    (async () => {
      try {
        await post("/api/errors", { action: "issue", id: errorIssue.dataset.errorIssue }, false);
        selectedError = "";
        render();
      } catch (error) {
        toast(error.message, true);
      }
    })();
    return;
  }
  const button = event.target.closest("[data-page]");
  if (!button) return;
  page = button.dataset.page;
  query = "";
  selectedError = "";
  editing = null;
  if (page !== "snipe") {
    snipeDraft = null;
    snipeSelected = {};
    snipeMyPick = {};
  }
  if ((page === "snipe" || page === "messages") && !snipeMyLots.length && !snipeMyLotsLoading) {
    loadSnipeMyLots(true).then(() => {
      if (page === "snipe" || page === "messages") render();
    });
  }
  closeSettingsSearch();
  render();
});

const FIND = [
  ["overview-page", "overview", "Обзор", "Обзор", "Баланс магазина и быстрые ползунки."],
  ["overview-bump", "overview", "Обзор", "Поднятие на обзоре", "Быстрый ползунок автоподнятия товаров."],
  ["overview-restore", "overview", "Обзор", "Восстанавливать продажи", "Сразу выставлять купленный товар снова."],
  ["overview-complete", "overview", "Обзор", "Закрывать сделки", "Отмечать заказ выполненным."],
  ["overview-notes", "overview", "Обзор", "Уведомления на обзоре", "Писать события в Telegram."],
  ["overview-read", "overview", "Обзор", "Читать чаты", "Отмечать диалог прочитанным после ответа."],
  ["overview-watermark", "overview", "Обзор", "Подпись сообщений", "Подпись внизу ответов бота."],
  ["overview-withdraw", "overview", "Обзор", "Автовывод на обзоре", "Быстрый ползунок автоматического вывода."],
  ["bump-page", "bump", "Поднятие", "Поднятие", "Бот сам возвращает товары в верх выдачи."],
  ["bump-enabled", "bump", "Поднятие", "Поднимать премиум", "Только отмеченные премиум-лоты. Выключенный ползунок останавливает траты баланса.", "bump-card"],
  ["bump-mode", "bump", "Поднятие", "Режим поднятия", "По позиции или по времени.", "bump-card"],
  ["bump-position", "bump", "Поднятие", "Позиция днём", "Товар поднимается, когда опускается ниже этого места.", "bump-card"],
  ["bump-interval", "bump", "Поднятие", "Интервал днём", "Полный круг для всех лотов. Они делят это время по очереди.", "bump-card"],
  ["bump-journal", "bump", "Поднятие", "Журнал обновлений", "Когда обновлялся каждый лот и чья очередь следующая."],
  ["bump-cooldown", "bump", "Поднятие", "Пауза одного товара", "Минимум между двумя поднятиями одного и того же лота.", "bump-card"],
  ["bump-check", "bump", "Поднятие", "Проверка позиций", "Как часто смотреть, не выпал ли товар.", "bump-card"],
  ["bump-night", "bump", "Поднятие", "Ночной режим", "В эти часы действуют другие цифры."],
  ["bump-night-position", "bump", "Поднятие", "Позиция ночью", "Ночью можно держать товар ниже и реже тратить баланс.", "bump-night"],
  ["bump-night-interval", "bump", "Поднятие", "Интервал ночью", "Ночью поднятие идёт реже.", "bump-night"],
  ["bump-night-from", "bump", "Поднятие", "Ночь начинается", "Час, с которого включаются ночные правила.", "bump-night"],
  ["bump-night-to", "bump", "Поднятие", "Ночь заканчивается", "Час, после которого снова дневные правила.", "bump-night"],
  ["bump-which", "bump", "Поднятие", "Какие товары поднимать", "Все товары или только выбранные."],
  ["snipe-page", "snipe", "Перебив", "Перебив", "Следить за лотами конкурента и поднимать свои после их перевыставления."],
  ["snipe-enabled", "snipe", "Перебив", "Включить перебив", "Мониторинг чужих лотов и отложенный подъём своих.", "snipe-card"],
  ["snipe-delay", "snipe", "Перебив", "Пауза после выставления", "Сколько ждать перед поднятием своего лота.", "snipe-card"],
  ["snipe-check", "snipe", "Перебив", "Как часто проверять", "Интервал опроса лотов продавца.", "snipe-card"],
  ["snipe-pay", "snipe", "Перебив", "Лимит оплаты", "Максимальная цена платного поднятия.", "snipe-card"],
  ["snipe-add", "snipe", "Перебив", "Привязать лоты продавца", "Найти продавца и закрепить свои лоты за его объявлениями."],
  ["snipe-links", "snipe", "Перебив", "Активные связки", "Список чужой лот → мой лот."],
  ["snipe-journal", "snipe", "Перебив", "История перебива", "Когда заметили чужой лот и подняли свой."],
  ["greetings-page", "messages", "Сообщения", "Приветствия по лотам", "Разные тексты на разные лоты с галочками."],
  ["restore-page", "restore", "Восстановление", "Восстановление", "После продажи или истечения лот можно сразу выставить снова."],
  ["restore-sold", "restore", "Восстановление", "Проданные", "Товар купили — бот выставляет его ещё раз."],
  ["restore-expired", "restore", "Восстановление", "Истёкшие", "Срок лота кончился — бот публикует его снова."],
  ["restore-free", "restore", "Восстановление", "Бесплатные", "Восстанавливать лоты без премиума."],
  ["restore-premium", "restore", "Восстановление", "Премиум", "Восстанавливать лоты с платным статусом."],
  ["restore-publish", "restore", "Восстановление", "Как публиковать", "Как был, премиум или бесплатный."],
  ["restore-recreate", "restore", "Восстановление", "Пересоздавать", "Бот создаёт новый лот и удаляет старый."],
  ["restore-which", "restore", "Восстановление", "Какие товары восстанавливать", "Все товары или только выбранные."],
  ["accounts-page", "accounts", "Аккаунты", "Аккаунты", "Очереди дешёвых и дорогих аккаунтов для автовыдачи."],
  ["stock-intro", "accounts", "Аккаунты", "Автовыдача", "Обычный и премиум лот берут аккаунты из одной очереди. После продажи встаёт следующий свободный."],
  ["stock-marks", "accounts", "Аккаунты", "Отметка аккаунта", "На лоте — сейчас на товаре. Продан откреплён. В очереди ждёт своей очереди."],
  ["stock-reserve", "accounts", "Аккаунты", "Следующий аккаунт из дорогих", "Один раз после проблемы по лоту. Потом очередь снова берёт дешёвые."],
  ["stock-lot", "accounts", "Аккаунты", "Лот", "Какие лоты обслуживает общая очередь. После продажи аккаунт помечается этим лотом."],
  ["stock-pool-primary", "accounts", "Аккаунты", "Дешёвые", "Общая очередь. Следующий свободный встаёт на проданный лот."],
  ["stock-pool-reserve", "accounts", "Аккаунты", "Дорогие", "Резерв для ошибок и ручной выдачи доп. аккаунта."],
  ["errors-page", "errors", "Ошибки", "Ошибки", "Покупатели, которые написали !ошибка или ошибка."],
  ["errors-intro", "errors", "Ошибки", "Жалобы покупателей", "Список активных ошибок с ником клиента."],
  ["errors-list", "errors", "Ошибки", "Список ошибок", "Кнопка с ником открывает решение: выдать доп аккаунт."],
  ["deals-page", "deals", "Сделки", "Сделки", "Бот может сам подтверждать выполнение заказа."],
  ["deals-enabled", "deals", "Сделки", "Подтверждать сделки", "После выдачи бот отмечает заказ выполненным."],
  ["deals-which", "deals", "Сделки", "Какие сделки подтверждать", "Все товары или только выбранные."],
  ["messages-page", "messages", "Сообщения", "Сообщения", "Тексты, которые бот пишет покупателю."],
  ["notes-page", "notifications", "Уведомления", "Уведомления", "Сообщения приходят в Telegram."],
  ["notes-enabled", "notifications", "Уведомления", "Писать в Telegram", "Общий выключатель всех уведомлений."],
  ["general-page", "general", "Общее", "Общее", "Подпись, чат, вывод и служебные параметры."],
  ["general-read", "general", "Общее", "Отмечать чат прочитанным", "После ответа бота чат не висит непрочитанным."],
  ["general-watermark", "general", "Общее", "Подпись внизу сообщения", "Добавляется к ответам бота."],
  ["general-watermark-text", "general", "Общее", "Текст подписи", "Строка внизу сообщений бота.", "general-watermark"],
  ["general-withdraw", "general", "Общее", "Автовывод", "Выводить средства автоматически. Меняется только ритм."],
  ["general-withdraw-interval", "general", "Общее", "Как часто выводить", "Пауза между запросами вывода.", "general-withdraw"],
  ["general-timeout", "general", "Общее", "Ожидание ответа Playerok", "Сколько ждать сайт, прежде чем считать запрос неудачным."],
  ["general-logs", "general", "Общее", "Размер журнала", "Когда файл лога достигнет этого размера, он начнётся заново."],
  ["general-update", "general", "Общее", "Ставить обновления бота", "Официальное обновление может заменить ваши доработки."],
  ["general-update-notify", "general", "Общее", "Сообщать об обновлении", "Уведомление в Telegram, когда вышла новая версия."],
];

for (const [id, hint] of MESSAGES) {
  FIND.push([`msg-${id}`, "messages", "Сообщения", hint, "Текст сообщения покупателю в чат.", "messages-page"]);
}
for (const [id, title, hint] of EVENTS) {
  FIND.push([`event-${id}`, "notifications", "Уведомления", title, hint, "notes-page"]);
}

function findEntry(row) {
  return {
    id: row[0],
    page: row[1],
    section: row[2],
    title: row[3],
    hint: row[4],
    fallback: row[5] || "",
  };
}

function textHasToken(text, token) {
  if (token.length >= 3) return text.includes(token);
  return text.split(/[^0-9a-zа-яё]+/i).some((word) => word === token);
}

function findScore(entry, tokens) {
  const title = entry.title.toLowerCase();
  const hint = entry.hint.toLowerCase();
  const section = entry.section.toLowerCase();
  const hay = `${title} ${hint} ${section}`;
  if (!tokens.every((token) => textHasToken(hay, token))) return 0;
  if (title === tokens.join(" ")) return 5;
  if (tokens.every((token) => textHasToken(title, token))) return 4;
  if (tokens.every((token) => textHasToken(`${title} ${hint}`, token))) return 2;
  return 1;
}

function hideSettingsResults() {
  const box = document.getElementById("settings-results");
  const input = document.getElementById("settings-search");
  if (box) {
    box.hidden = true;
    box.innerHTML = "";
  }
  if (input) input.setAttribute("aria-expanded", "false");
}

function closeSettingsSearch() {
  settingsQuery = "";
  const input = document.getElementById("settings-search");
  if (input) input.value = "";
  hideSettingsResults();
}

function settingsHits() {
  const tokens = settingsQuery.trim().toLowerCase().split(/\s+/).filter(Boolean);
  if (!tokens.length) return [];
  return FIND
    .map((row, index) => ({ entry: findEntry(row), index }))
    .map((row) => ({ ...row, score: findScore(row.entry, tokens) }))
    .filter((row) => row.score > 0)
    .sort((a, b) => b.score - a.score || a.index - b.index)
    .slice(0, 20);
}

function renderSettingsResults() {
  const box = document.getElementById("settings-results");
  const input = document.getElementById("settings-search");
  if (!box) return;
  const hits = settingsHits();
  if (!settingsQuery.trim()) {
    hideSettingsResults();
    return;
  }
  box.hidden = false;
  if (input) input.setAttribute("aria-expanded", "true");
  if (!hits.length) {
    box.innerHTML = `<div class="settings-empty">Ничего не найдено</div>`;
    return;
  }
  box.innerHTML = hits.map(({ entry }) => `
    <button type="button" class="settings-hit" data-open-find="${esc(entry.id)}">
      <b>${esc(entry.title)}</b>
      <span>${esc(entry.hint)}</span>
      <small>${esc(entry.section)}</small>
    </button>`).join("");
}

function spotlight(node) {
  const target = node.closest(".row, .slider, .card, .seg, .hero") || node;
  target.scrollIntoView({ behavior: "smooth", block: "center" });
  target.classList.remove("found");
  void target.offsetWidth;
  target.classList.add("found");
  clearTimeout(findTimer);
  findTimer = setTimeout(() => target.classList.remove("found"), 1700);
}

function openFind(id) {
  const row = FIND.find((item) => item[0] === id);
  if (!row) return;
  const entry = findEntry(row);
  page = entry.page;
  query = "";
  editing = null;
  closeSettingsSearch();
  render();
  const node = main.querySelector(`[data-find="${entry.id}"]`)
    || (entry.fallback ? main.querySelector(`[data-find="${entry.fallback}"]`) : null);
  if (node) spotlight(node);
}

const settingsInput = document.getElementById("settings-search");
const settingsBox = document.getElementById("settings-results");

if (settingsInput) {
  settingsInput.addEventListener("input", () => {
    settingsQuery = settingsInput.value;
    renderSettingsResults();
  });
  settingsInput.addEventListener("focus", () => {
    if (settingsQuery.trim()) renderSettingsResults();
  });
  settingsInput.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      closeSettingsSearch();
      return;
    }
    if (event.key === "Enter") {
      const first = settingsHits()[0];
      if (!first) return;
      event.preventDefault();
      openFind(first.entry.id);
    }
  });
}

if (settingsBox) {
  settingsBox.addEventListener("click", (event) => {
    const button = event.target.closest("[data-open-find]");
    if (!button) return;
    openFind(button.dataset.openFind);
  });
}

document.addEventListener("click", (event) => {
  if (!event.target.closest(".finder")) hideSettingsResults();
});

window.addEventListener("pointerup", () => { dragging = false; });

async function reload(force) {
  const response = await fetch("/api/state");
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "не открылось");
  const signature = JSON.stringify({
    settings: data.settings && {
      ...data.settings,
      bump: { ...data.settings.bump, last_check: "", last_bump: "" },
      snipe: data.settings.snipe && { ...data.settings.snipe, last_check: "" },
    },
    messages: data.messages,
    lists: data.lists,
    stock: data.stock,
    journal: data.journal,
    errors: data.errors,
    snipe: data.snipe,
    snipe_journal: data.snipe_journal,
    greetings: data.greetings,
    alive: data.alive,
    account: data.account,
  });
  const changed = signature !== reload.last;
  reload.last = signature;
  state = data;
  paintStatus();
  if (force || (changed && !dragging && !main.contains(document.activeElement))) render();
}

reload(true).catch((error) => {
  main.innerHTML = `<h1>Пульт не открылся</h1><p class="lead">${esc(error.message)}</p>`;
});

setInterval(() => {
  reload(false).catch(() => {});
}, 8000);
