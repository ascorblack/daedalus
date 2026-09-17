// Two languages, for the screens a person meets before they have an account: signing in, and adding
// the first model. Everything past those is English for now, and the dictionary says so by only
// holding what those screens use — a key nobody translated is a visible gap, not a silent English
// string in the middle of a Russian page.
//
// The language is the reader's, not the installation's: ?lang= wins (the launcher opens the app in
// the language its own window is in), then what the reader chose here before, then the browser's.

import { useEffect, useState } from "react";

export type Lang = "en" | "ru";
export const LANGS: Lang[] = ["en", "ru"];
const STORE = "daedalus.lang";

/** Every string these screens show, in both languages. `{name}`-style holes are filled by `t`. */
export const DICT: Record<string, Record<Lang, string>> = {
  "lang.name.en": { en: "English", ru: "English" },
  "lang.name.ru": { en: "Русский", ru: "Русский" },
  "lang.pick": { en: "Language", ru: "Язык" },

  "nav.agents": { en: "Agents", ru: "Агенты" },
  "nav.voice": { en: "Voice", ru: "Голос" },
  "nav.inbox": { en: "Inbox", ru: "Входящие" },
  "nav.board": { en: "Board", ru: "Доска" },
  "nav.changes": { en: "Changes", ru: "Изменения" },
  "nav.schedules": { en: "Schedules", ru: "Расписания" },
  "nav.services": { en: "Services", ru: "Сервисы" },
  "nav.memory": { en: "Memory", ru: "Память" },
  "nav.usage": { en: "Usage", ru: "Расход" },
  "nav.health": { en: "Health", ru: "Состояние" },
  "nav.settings": { en: "Settings", ru: "Настройки" },
  "nav.more": { en: "More", ru: "Ещё" },

  // The rail's own chrome. A destination translated under an English heading is the half-and-half
  // the rest of this file exists to avoid, so the group names, the two buttons and the project
  // chip are here even though what they lead to is still English.
  "nav.group.work": { en: "Work", ru: "Работа" },
  "nav.group.autonomy": { en: "Autonomy", ru: "Самостоятельность" },
  "nav.group.knowledge": { en: "Knowledge", ru: "Знания" },
  "nav.group.observe": { en: "Observe", ru: "Наблюдение" },
  "shell.projects": { en: "Projects", ru: "Проекты" },
  "shell.projects.all": { en: "All projects", ru: "Все проекты" },
  "shell.projects.add": { en: "Add a project", ru: "Добавить проект" },
  "shell.search": { en: "Search…", ru: "Поиск…" },
  "shell.search.title": { en: "Search and go (Ctrl/⌘ K)", ru: "Поиск и переход (Ctrl/⌘ K)" },
  "shell.search.label": { en: "Search and go", ru: "Поиск и переход" },
  "shell.search.placeholder": { en: "Go to, open, create…", ru: "Перейти, открыть, создать…" },
  "shell.search.nomatch": { en: "Nothing matches.", ru: "Ничего не нашлось." },
  "shell.search.goto": { en: "Go to {name}", ru: "Перейти: {name}" },
  "shell.search.newagent": { en: "New agent", ru: "Новый агент" },
  "shell.search.workin": { en: "Work in {name}", ru: "Работать в «{name}»" },
  "shell.collapse": { en: "Collapse", ru: "Свернуть" },
  "shell.collapse.title": { en: "Collapse the rail", ru: "Свернуть панель" },
  "shell.expand.title": { en: "Expand the rail", ru: "Развернуть панель" },
  "shell.back": { en: "Back", ru: "Назад" },
  "shell.waiting": { en: "{n} waiting", ru: "{n} ждут вас" },

  "common.loading": { en: "Loading…", ru: "Загрузка…" },
  "common.cancel": { en: "Cancel", ru: "Отмена" },
  "common.retry": { en: "Try again", ru: "Ещё раз" },
  "common.continue": { en: "Continue", ru: "Дальше" },

  "login.title": { en: "Daedalus", ru: "Daedalus" },
  "login.sub": { en: "Your agent, in this browser. Sign in as its owner; the session stays for a month.", ru: "Ваш агент — в этом браузере. Войдите как владелец: сессия живёт месяц." },
  "login.passkey": { en: "Sign in with a passkey", ru: "Войти по ключу доступа" },
  "login.or": { en: "or", ru: "или" },
  "login.pairing.label": { en: "Pairing link or code", ru: "Ссылка привязки или код" },
  "login.pairing.hint": {
    en: "The server prints one at every start; the desktop app opens it by itself. It works once, for 30 minutes.",
    ru: "Сервер печатает её при каждом запуске, а приложение открывает само. Действует один раз, 30 минут.",
  },
  "login.pairing.spent": {
    en: "That pairing link was already used or has expired. Ask for a fresh one, or sign in another way.",
    ru: "Эта ссылка уже использована или истекла. Запросите новую или войдите иначе.",
  },
  "login.nopasskey": {
    en: "No passkey yet: sign in, then add one in Settings → Security, and next time Face ID, Touch ID or a security key is enough.",
    ru: "Ключа доступа ещё нет: войдите и добавьте его в «Настройки → Безопасность» — дальше хватит Face ID, Touch ID или ключа.",
  },
  "login.pairing.placeholder": { en: "https://…/api/auth/pair?code=… or the code", ru: "https://…/api/auth/pair?code=… или код" },
  "login.busy": { en: "Signing in…", ru: "Входим…" },
  "login.install": {
    en: "Install the site as an app from your browser's menu to open it like any other app.",
    ru: "Установите сайт как приложение из меню браузера — он будет открываться как обычное приложение.",
  },

  // Settings -> Voice -> the synthesis picker. The first screen past sign-in that is translated, and
  // it is translated because the whole point of it is picking a voice to be spoken to in: an operator
  // choosing a Russian voice is reading the page in Russian.
  "tts.title": { en: "Voice (speech synthesis)", ru: "Голос (синтез речи)" },
  "tts.intro": {
    en: "A voice that runs here, on this machine's processor, with no endpoint and no key. Download one and every answer on the voice page is read in it — ahead of a speech endpoint and far ahead of the browser's own synthesiser. Listen to one before you choose it; nothing is downloaded until you ask.",
    ru: "Голос, который работает здесь, на процессоре этой машины, без адреса и без ключа. Скачайте один — и все ответы на голосовой странице будут читаться им, раньше речевого эндпоинта и намного раньше встроенного синтезатора браузера. Послушайте, прежде чем выбрать: ничего не скачивается, пока вы не попросите.",
  },
  "tts.inuse": { en: "In use", ru: "Используется" },
  "tts.inuse.none": { en: "none — an endpoint or the browser speaks", ru: "ничего — говорит эндпоинт или браузер" },
  "tts.ondisk": { en: "On disk", ru: "На диске" },
  "tts.ondisk.none": { en: "nothing yet", ru: "пока ничего" },
  "tts.ondisk.some": { en: "{n} voices · {size}", ru: "{n} голосов · {size}" },
  "tts.state.loading": { en: "loading", ru: "загружается" },
  "tts.state.error": { en: "it would not load", ru: "не загрузился" },
  "tts.search": { en: "Search voices", ru: "Поиск голосов" },
  "tts.language.any": { en: "Any language", ru: "Любой язык" },
  "tts.fast.only": { en: "Keeps up with speech", ru: "Успевает за речью" },
  "tts.recommended": { en: "Recommended", ru: "Рекомендуется" },
  "tts.recommended.for": { en: "Recommended for {lang}", ru: "Рекомендуется для: {lang}" },
  "tts.card.using": { en: "In use", ru: "Выбран" },
  "tts.gender.female": { en: "Female", ru: "Женский" },
  "tts.gender.male": { en: "Male", ru: "Мужской" },
  "tts.gender.mixed": { en: "Several voices", ru: "Несколько голосов" },
  "tts.slow": { en: "Slower than speech", ru: "Медленнее речи" },
  "tts.quality": { en: "Quality", ru: "Качество" },
  "tts.speed": { en: "Speed", ru: "Скорость" },
  "tts.facts.size": { en: "{dl} download · {disk} on disk · ~{mem} MB in memory", ru: "{dl} скачать · {disk} на диске · ~{mem} МБ в памяти" },
  "tts.facts.rate": { en: "{khz} kHz · {licence}", ru: "{khz} кГц · {licence}" },
  "tts.facts.speakers": { en: "{n} voices inside this model", ru: "{n} голосов внутри модели" },
  "tts.play": { en: "Play sample", ru: "Послушать" },
  "tts.playing": { en: "Playing…", ru: "Звучит…" },
  "tts.download": { en: "Download {size}", ru: "Скачать {size}" },
  "tts.use": { en: "Use this one", ru: "Выбрать этот" },
  "tts.stop": { en: "Stop using it", ru: "Перестать использовать" },
  "tts.delete": { en: "Delete", ru: "Удалить" },
  "tts.cancel": { en: "Stop", ru: "Остановить" },
  "tts.queued": { en: "Waiting — voices are fetched one at a time.", ru: "Ожидание: голоса скачиваются по одному." },
  "tts.failed": { en: "the download failed", ru: "скачать не удалось" },
  "tts.nomatch": { en: "No voice matches that. Clear the filters to see all {n}.", ru: "Ни один голос не подошёл. Снимите фильтры, чтобы увидеть все {n}." },
  "tts.speaker": { en: "Voice inside this model", ru: "Голос внутри модели" },
  "tts.speaker.first": { en: "the first one", ru: "первый" },
  "tts.speed.label": { en: "Speaking speed ({speed}x)", ru: "Скорость речи ({speed}x)" },
  "tts.threads": { en: "Synthesis threads", ru: "Потоки синтеза" },
  "tts.engine.installing": { en: "installing the speech engine…", ru: "устанавливаем речевой движок…" },
  "tts.toast.downloading": { en: "downloading…", ru: "скачиваем…" },
  "tts.toast.using": { en: "answers are read in this voice now", ru: "теперь ответы читаются этим голосом" },
  "tts.toast.stopped": { en: "back to the endpoint and the browser", ru: "снова эндпоинт и браузер" },
  "tts.toast.removed": { en: "removed", ru: "удалён" },

  // The languages the synthesis catalog speaks. Kept here rather than in a map beside the picker so
  // that a Russian reader sees "Немецкий" and not "German" in the filter they are choosing from.
  "tts.lang.de": { en: "German", ru: "Немецкий" },
  "tts.lang.en": { en: "English", ru: "Английский" },
  "tts.lang.es": { en: "Spanish", ru: "Испанский" },
  "tts.lang.fr": { en: "French", ru: "Французский" },
  "tts.lang.it": { en: "Italian", ru: "Итальянский" },
  "tts.lang.pl": { en: "Polish", ru: "Польский" },
  "tts.lang.pt": { en: "Portuguese", ru: "Португальский" },
  "tts.lang.ru": { en: "Russian", ru: "Русский" },
  "tts.lang.uk": { en: "Ukrainian", ru: "Украинский" },
  "tts.lang.zh": { en: "Chinese", ru: "Китайский" },

  "add.title": { en: "Add a model", ru: "Добавить модель" },
  "add.sub": { en: "Your agent has no model yet — one is all it takes to start.", ru: "У агента ещё нет модели. Одной достаточно, чтобы начать." },
  "add.step1": { en: "Where it runs", ru: "Где она работает" },
  "add.step1.sub": { en: "An endpoint this installation can reach.", ru: "Адрес, до которого эта установка достаёт." },
  "add.step2": { en: "Which model", ru: "Какая модель" },
  "add.step2.sub": { en: "Listed by the endpoint itself.", ru: "Список приходит от самого адреса." },
  "add.step3": { en: "How it runs", ru: "Как она работает" },
  "add.step3.sub": { en: "All of it changeable later in Settings → Models.", ru: "Всё можно поменять позже в «Настройки → Модели»." },
  "add.step2.wait": { en: "Pick an endpoint above.", ru: "Выберите адрес выше." },

  "add.key.ready": { en: "key ready", ru: "ключ есть" },
  "add.key.none": { en: "no key", ru: "нет ключа" },
  "add.key.signedin": { en: "signed in", ru: "выполнен вход" },
  "add.key.unknown": { en: "key unknown", ru: "ключ неизвестен" },
  "add.key.hint.settings": { en: "Add its key in Settings → Models.", ru: "Добавьте ключ в «Настройки → Модели»." },
  "add.key.hint.proxy": { en: "Its key is missing from the key proxy.", ru: "В прокси ключей нет ключа для него." },
  "add.key.hint.cli": { en: "Sign in with its command-line tool on this machine.", ru: "Войдите через его консольную утилиту на этой машине." },
  "add.key.held": { en: "key held by the key proxy", ru: "ключ хранит прокси ключей" },
  "add.key.own": { en: "keyed here", ru: "ключ хранится здесь" },
  "add.key.free": { en: "needs no key", ru: "ключ не нужен" },
  "add.noaddress": { en: "no address configured", ru: "адрес не задан" },
  "add.noaddress.hint": { en: "Give it an address in Settings → Models.", ru: "Укажите адрес в «Настройки → Модели»." },

  "add.custom": { en: "OpenAI-compatible endpoint", ru: "Совместимый с OpenAI адрес" },
  "add.custom.new": { en: "new", ru: "новый" },
  "add.custom.sub": { en: "Anything serving /v1/chat/completions: a local vLLM, a machine on the network, another vendor.", ru: "Всё, что отвечает на /v1/chat/completions: локальный vLLM, машина в сети, другой поставщик." },
  "add.custom.name": { en: "Name it", ru: "Название" },
  "add.custom.url": { en: "Address (usually ending in /v1)", ru: "Адрес (обычно оканчивается на /v1)" },
  "add.custom.key": { en: "Key (blank if it needs none)", ru: "Ключ (пусто, если не нужен)" },
  "add.custom.save": { en: "Add this endpoint", ru: "Добавить адрес" },
  "add.custom.saving": { en: "Adding…", ru: "Добавляем…" },

  "add.filter": { en: "Filter {n} models", ru: "Поиск среди {n} моделей" },
  "add.filter.plain": { en: "Filter", ru: "Поиск" },
  "add.typed": { en: "Model id", ru: "Идентификатор модели" },
  "add.typed.hint": { en: "…or type it", ru: "…или впишите" },
  "add.asking": { en: "Asking the endpoint what it serves…", ru: "Спрашиваем адрес, что он отдаёт…" },
  "add.nolist": { en: "The endpoint did not list its models", ru: "Адрес не отдал список моделей" },
  "add.nolist.sub": { en: "Type the model id above — the list is a convenience, not a requirement.", ru: "Впишите идентификатор модели выше: список — удобство, а не обязательность." },
  "add.nomatch": { en: "Nothing matches that filter.", ru: "Под фильтр ничего не подошло." },

  "add.label": { en: "Label", ru: "Название" },
  "add.label.hint": { en: "what the app calls it", ru: "как приложение будет её называть" },
  "add.window": { en: "Context window", ru: "Окно контекста" },
  "add.output": { en: "Max output", ru: "Максимум ответа" },
  "add.thinking.on": { en: "thinking on", ru: "рассуждение вкл" },
  "add.thinking.off": { en: "thinking off", ru: "рассуждение выкл" },
  "add.effort": { en: "reasoning effort", ru: "глубина рассуждения" },
  "add.effort.low": { en: "low", ru: "низкая" },
  "add.effort.medium": { en: "medium", ru: "средняя" },
  "add.effort.high": { en: "high", ru: "высокая" },
  "add.images.on": { en: "images on", ru: "картинки вкл" },
  "add.images.off": { en: "images off", ru: "картинки выкл" },
  "add.images.title": { en: "the model accepts pictures", ru: "модель принимает картинки" },

  "add.foot.empty": { en: "Pick an endpoint and a model", ru: "Выберите адрес и модель" },
  "add.save": { en: "Add this model", ru: "Добавить модель" },
  "add.save.first": { en: "Add it and start", ru: "Добавить и начать" },
  "add.saving": { en: "Saving…", ru: "Сохраняем…" },

  "add.pill.context": { en: "{n} context", ru: "контекст {n}" },
  "add.pill.images": { en: "images", ru: "картинки" },
  "add.pill.reasoning": { en: "reasoning", ru: "рассуждение" },
  "add.pill.free": { en: "free", ru: "бесплатно" },
  "add.pill.price": { en: "{in} in · {out} out / 1M", ru: "{in} вход · {out} выход / 1M" },
};

function fromUrl(): Lang | null {
  try {
    const raw = new URLSearchParams(window.location.search).get("lang");
    return raw === "ru" || raw === "en" ? raw : null;
  } catch {
    return null;
  }
}

function stored(): Lang | null {
  try {
    const raw = localStorage.getItem(STORE);
    return raw === "ru" || raw === "en" ? raw : null;
  } catch {
    return null; // private mode
  }
}

function fromBrowser(): Lang {
  const tags = [navigator.language, ...(navigator.languages ?? [])];
  return tags.some((tag) => (tag ?? "").toLowerCase().startsWith("ru")) ? "ru" : "en";
}

// A language named in the URL is a choice too: the launcher opens the app in the language its own
// window is in, and the reader should not have to make it twice. It is kept and then taken out of
// the address, so that it settles the question once instead of overriding the reader on every
// visit — and so that a link copied out of the bar carries no answer with it.
let current: Lang = fromUrl() ?? stored() ?? fromBrowser();
if (fromUrl()) {
  remember(current);
  stripUrl();
}

/** Take `lang` out of the address without reloading or adding to the history. */
function stripUrl(): void {
  try {
    const url = new URL(window.location.href);
    url.searchParams.delete("lang");
    window.history.replaceState(window.history.state, "", url.pathname + url.search + url.hash);
  } catch {
    /* no history to speak of */
  }
}

const listeners = new Set<() => void>();

function remember(next: Lang): void {
  try {
    localStorage.setItem(STORE, next);
  } catch {
    /* private mode */
  }
}

export function lang(): Lang {
  return current;
}

export function setLang(next: Lang): void {
  if (next === current) return;
  current = next;
  remember(next);
  markDocument();
  for (const fire of listeners) fire();
}

/** Tell the page which language it is in, so the browser hyphenates and reads it correctly. */
function markDocument(): void {
  if (typeof document !== "undefined") document.documentElement.lang = current;
}

/** One string, in the current language; `{name}` holes filled from `vars`. An unknown key shows itself. */
export function t(key: string, vars?: Record<string, string | number>): string {
  const entry = DICT[key];
  if (!entry) return key;
  const text = entry[current] ?? entry.en;
  if (!vars) return text;
  return text.replace(/\{(\w+)\}/g, (whole, name: string) => (name in vars ? String(vars[name]) : whole));
}

/** The current language, and re-render when it changes. Returns the setter too, for a picker. */
export function useLang(): [Lang, (next: Lang) => void] {
  const [value, setValue] = useState(current);
  useEffect(() => {
    const fire = () => setValue(current);
    listeners.add(fire);
    fire();
    return () => {
      listeners.delete(fire);
    };
  }, []);
  return [value, setLang];
}

markDocument();
