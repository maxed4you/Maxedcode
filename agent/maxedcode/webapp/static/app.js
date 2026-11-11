/* maxedcode app: the page. Plain JS, no build, no libraries; the server is maxedcode/webapp/server.py.
 * The layout and behaviour follow the DeepSeek Harness window this app replaced.
 *
 * A turn is POST /api/turn, answered with maxedcode's stream-json lines (NDJSON) as they come. The server owns
 * the turn: leaving the chat or reloading does not stop it; GET /api/watch?session=&from=N replays its lines
 * from N and goes on live (the page comes back), only POST /api/stop ends it early:
 *   session {id, cwd}                  the chat's session (added by the server)
 *   delta {reasoning?, text?, reset?}  live reasoning / answer text of the current request (reset: retried)
 *   assistant {message, usage}         one model step; its tool_calls become steps of the work list
 *   tool_result {id, tool, content}    the result of one step
 *   ultra {text}                        progress of an ultra (staged) run
 *   result {result, usage, timing}     the turn's answer
 *   exit {code, stderr}                the process ended (added by the server)
 */
(function () {
  'use strict'

  // ── icons (stroke, 24px grid) ──
  var ICONS = {
    plusCircle: '<circle cx="12" cy="12" r="9"/><path d="M12 8v8M8 12h8"/>',
    plus: '<path d="M12 5v14M5 12h14"/>',
    search: '<circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/>',
    sliders: '<path d="M4 7h10M18 7h2M4 17h4M12 17h8"/><circle cx="16" cy="7" r="2"/><circle cx="10" cy="17" r="2"/>',
    folderPlus: '<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/><path d="M12 10.5v5M9.5 13h5"/>',
    folder: '<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>',
    folderOpen: '<path d="M3 17V7a2 2 0 0 1 2-2h4l2 2h7a2 2 0 0 1 2 2v1"/><path d="M3 17l2.5-6.2A2 2 0 0 1 7.4 9.5H21l-2.6 7.3a2 2 0 0 1-1.9 1.2H5a2 2 0 0 1-2-1z"/>',
    gear: '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/>',
    panelLeft: '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M9 4v16"/>',
    panelRight: '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M15 4v16"/>',
    chevron: '<path d="m7 10 5 5 5-5"/>',
    chevronRight: '<path d="m10 7 5 5-5 5"/>',
    arrowUp: '<path d="M12 19V5M6 11l6-6 6 6"/>',
    dots: '<circle cx="5" cy="12" r="1"/><circle cx="12" cy="12" r="1"/><circle cx="19" cy="12" r="1"/>',
    copy: '<rect x="8" y="8" width="12" height="12" rx="2"/><path d="M16 8V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h2"/>',
    branch: '<circle cx="6" cy="6" r="2"/><circle cx="18" cy="6" r="2"/><circle cx="12" cy="18" r="2"/><path d="M6 8v2a4 4 0 0 0 4 4h2M18 8v2a4 4 0 0 1-4 4h-2v2"/>',
    database: '<ellipse cx="12" cy="6" rx="7" ry="3"/><path d="M5 6v12c0 1.7 3.1 3 7 3s7-1.3 7-3V6M5 12c0 1.7 3.1 3 7 3s7-1.3 7-3"/>',
    gauge: '<path d="M12 14l4-4"/><path d="M3.3 17a9 9 0 1 1 17.4 0"/>',
    ring: '<circle cx="12" cy="12" r="8" stroke-dasharray="40 60"/>',
    x: '<path d="M6 6l12 12M18 6 6 18"/>',
    chevronLeft: '<path d="m15 18-6-6 6-6"/>',
    refresh: '<path d="M20 11a8 8 0 1 0-2.3 5.7"/><path d="M20 4v7h-7"/>',
    external: '<path d="M7 17 17 7"/><path d="M8 7h9v9"/>',
    shield: '<path d="M12 3 5 6v5c0 4.5 3 8.3 7 10 4-1.7 7-5.5 7-10V6z"/>',
    link: '<path d="M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1"/><path d="M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1"/>',
    check: '<path d="m5 12 5 5 9-10"/>',
    terminal: '<path d="m5 8 4 4-4 4M12 16h7"/>',
    compass: '<circle cx="12" cy="12" r="9"/><path d="m15.5 8.5-2 5-5 2 2-5z"/>',
    file: '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5"/>',
    pencil: '<path d="M4 20h4L19 9l-4-4L4 16z"/><path d="m14 6 4 4"/>',
    trash: '<path d="M4 7h16M10 11v6M14 11v6M6 7l1 12a2 2 0 0 0 2 2h6a2 2 0 0 0 2-2l1-12M9 7V4h6v3"/>',
    globe: '<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3a14 14 0 0 1 0 18M12 3a14 14 0 0 0 0 18"/>',
    list: '<path d="M9 6h11M9 12h11M9 18h11M4 6h.01M4 12h.01M4 18h.01"/>',
    sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>',
    moon: '<path d="M20 14.5A8 8 0 1 1 9.5 4a6.5 6.5 0 0 0 10.5 10.5z"/>',
    monitor: '<rect x="3" y="4" width="18" height="12" rx="2"/><path d="M8 20h8M12 16v4"/>',
    brain: '<path d="M12 5a3 3 0 0 0-5.8 1A3 3 0 0 0 4 11a3 3 0 0 0 2 5 3 3 0 0 0 6 1zM12 5a3 3 0 0 1 5.8 1A3 3 0 0 1 20 11a3 3 0 0 1-2 5 3 3 0 0 1-6 1z"/>',
    download: '<path d="M12 4v11M7 10l5 5 5-5M5 20h14"/>',
    arrowDown: '<path d="M12 5v14M6 13l6 6 6-6"/>',
    code: '<path d="m9 8-4 4 4 4M15 8l4 4-4 4"/>',
    wrap: '<path d="M4 6h16M4 12h13a3 3 0 0 1 0 6h-4M4 18h5"/><path d="m14 16-2 2 2 2"/>',
    info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v5M12 8h.01"/>',
    alert: '<path d="M12 4 2.5 20h19z"/><path d="M12 10v4M12 17h.01"/>',
    bulb: '<path d="M9 18h6M10 21h4M12 3a6 6 0 0 0-3.5 10.9c.6.5 1 1.2 1 2.1h5c0-.9.4-1.6 1-2.1A6 6 0 0 0 12 3z"/>',
    archive: '<rect x="3" y="4" width="18" height="4" rx="1"/><path d="M5 8v11a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1V8M10 12h4"/>',
    quoteMark: '<path d="M7 7h4v4c0 3-1.5 5-4 6M14 7h4v4c0 3-1.5 5-4 6"/>',
    eye: '<path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/>',
    filePlus: '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5M12 11v6M9 14h6"/>',
    square: '<rect x="7" y="7" width="10" height="10" rx="1.5" fill="currentColor"/>',
    play: '<path d="M8 5.5v13l10.5-6.5z"/>',
  }
  var FOLDER_COLOR = '<svg class="i" viewBox="0 0 24 24" style="stroke:none"><path d="M2 6a2 2 0 0 1 2-2h5l2 2h9a2 2 0 0 1 2 2v1H2z" fill="#f2b83c"/><path d="M2 9h20v9a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2z" fill="#ffd25e"/><path d="M2 12h20v1H2z" fill="#3b82f6" opacity=".9"/></svg>'
  function icon(name, cls) {
    if (name === 'folderColor') return FOLDER_COLOR
    return '<svg class="i' + (cls ? ' ' + cls : '') + '" viewBox="0 0 24 24">' + (ICONS[name] || '') + '</svg>'
  }

  // ── i18n ──
  var TEXT = {
    cloudTab: ['Облако', 'Cloud'],
    secChat: ['Чат', 'Chat'], secModels: ['Модели', 'Models'], secTools: ['Инструменты', 'Tools'], secMcp: ['MCP', 'MCP'],
    secHooks: ['Хуки', 'Hooks'], secPerms: ['Разрешения', 'Permissions'], secMemory: ['Память', 'Memory'],
    modelsIntro: ['Где работает модель агента и как она отвечает. То же, что /local и /server в терминале.', 'Where the agent\'s model runs and how it answers: the same as /local and /server in the terminal.'],
    localServer: ['Локальный сервер', 'Local server'], online: ['отвечает', 'answering'],
    memoryIntro: ['Общая память видна каждой сессии терминала и окна; файлы памяти — у папки чата.', 'The shared memory is seen by every terminal and window session; memory files belong to the chat folder.'],
    toolsIntro: ['Навыки, свои команды, подагенты и встроенные инструменты — те же файлы, что у CLI.', 'Skills, custom commands, subagents and built-in tools: the same files as the CLI.'],
    rule_allow: ['Разрешено без вопроса', 'Allowed without asking'], rule_deny: ['Запрещено', 'Denied'], rule_ask: ['Спрашивать', 'Ask first'],
    cloudDefaultTag: ['по умолчанию', 'default'], keyOk: ['ключ сохранён', 'key saved'], cloudModelLbl: ['Модель', 'Model'],
    addProvider: ['Добавить ключ провайдера', 'Add a provider key'], cloudProvider: ['Провайдер', 'Provider'], cloudKeyLbl: ['API-ключ', 'API key'],
    topPSub: ['0–1; доля вероятных слов, из которых выбирает модель (/sampling top_p)', '0-1; the share of likely words the model picks from (/sampling top_p)'],
    topKSub: ['Из скольких лучших слов выбирать; 0 — без ограничения (/sampling topk)', 'How many top words to pick from; 0 = no limit (/sampling topk)'],
    minPSub: ['0–1; отсекает маловероятные слова (/sampling min_p)', '0-1; cuts unlikely words (/sampling min_p)'],
    cmdUndo: ['Откатить изменения файлов последнего хода', 'Undo the file changes of the last turn'],
    cmdRedo: ['Вернуть то, что откатил /undo', 'Put back what /undo reverted'],
    cmdCompact: ['Сжать историю в сводку', 'Compress the history into a summary'],
    cmdContext: ['Что занимает контекст', 'What fills the context'], cmdCache: ['Кэш префикса последнего запроса', 'Prefix cache of the last request'],
    cmdRun: ['Выйти из Plan и сделать запланированное', 'Leave Plan and do what was planned'],
    cmdInit: ['Написать CLAUDE.md для проекта', 'Write CLAUDE.md for the project'], cmdHelp: ['Все команды', 'All commands'],
    forkFiles: ['Вернуть и файлы к этому месту? Изменения файлов из следующих ходов этого чата будут отменены (как /rewind в терминале).\nОК — вернуть файлы, Отмена — только ответвить чат.',
      'Put the files back to this point too? The file changes of this chat\'s later turns are undone (as /rewind in the terminal).\nOK = restore the files, Cancel = only fork the chat.'],
    forkRestored: ['Файлов возвращено', 'Files restored'], noteSaved: ['Заметка сохранена', 'Note saved'],
    cmdNotNeeded: ['В окне не нужно: поле ввода растягивается само, окно закрывается как обычно', 'Not needed in the window: the input grows by itself, the window closes as usual'],
    helpHead: ['Команды', 'Commands'], helpMore: ['Ваши команды и навыки — в меню «/». Настройки: Ctrl+,', 'Your commands and skills are in the "/" menu. Settings: Ctrl+,'],
    helpModel: ['Настройки → Модели', 'Settings → Models'], helpServer: ['Настройки → Модели → Найти серверы', 'Settings → Models → Find servers'],
    helpCloud: ['Настройки → Облако', 'Settings → Cloud'], helpEffort: ['ползунок у поля ввода', 'the slider next to the input'],
    helpSampling: ['Настройки → Модели → Ответы', 'Settings → Models → Answers'], helpMemory: ['Настройки → Память', 'Settings → Memory'],
    helpPerms: ['Настройки → Разрешения', 'Settings → Permissions'], helpMcp: ['Настройки → MCP', 'Settings → MCP'],
    helpSkill: ['меню «/» и Настройки → Инструменты', 'the "/" menu and Settings → Tools'], helpHistory: ['список чатов слева, поиск', 'the chat list on the left, search'],
    helpRewind: ['«Ответвить отсюда» у ответа (по желанию — и с возвратом файлов)', '"Fork from here" on a reply (files restored too, if you want)'], helpCopy: ['кнопка копирования у ответа, меню чата → экспорт', 'the copy button on a reply, chat menu → export'],
    cancel: ['Отмена', 'Cancel'], showAll: ['Показать все', 'Show all'], showLess: ['Свернуть', 'Show less'],
    cloudIntro: ['Облачные модели пишут код по описанию — код проекта, пути и история никуда не уходят, каждый запрос вы подтверждаете (можно отключить ниже). Те же ключи, что /cloud в терминале. Модель для задачи выбирает агент.', 'Cloud models write code from a description: project code, paths and history never leave this PC, and you approve every request (can be turned off below). The same keys as /cloud in the terminal. The agent picks the model for each task.'],
    cloudAccounts: ['Ключи (★ — по умолчанию)', 'Keys (★ = default)'], cloudNoKey: ['нет ключа', 'no key'],
    cloudMakeDefault: ['По умолчанию', 'Make default'], cloudTest: ['Проверить', 'Test'], cloudRefresh: ['Модели', 'Models'],
    cloudRemoveAsk: ['Удалить ключ', 'Remove the key'], cloudAuto: ['провайдер: по ключу', 'provider: from the key'],
    cloudKeyPh: ['Вставьте API-ключ', 'Paste an API key'], cloudModelPh: ['модель (необязательно)', 'model (optional)'],
    cloudKeySafe: ['Ключ шифруется Windows (DPAPI) и не показывается снова. Несколько ключей одного провайдера — запасные: при лимите запрос уходит на следующий.', 'The key is encrypted by Windows (DPAPI) and never shown again. Several keys of one provider are spares: a rate-limited request moves to the next one.'],
    cloudOptions: ['Параметры', 'Options'], cloudSub: ['Подагенты', 'Subagents'], cloudSubSub: ['Подагенты тоже могут просить облако', 'Subagents may ask the cloud too'],
    cloudAutoSend: ['Отправлять без подтверждения', 'Send without asking'], cloudAutoSendSub: ['Задача уходит в облако сразу, без карточки в чате и окна в терминале (/cloud autosend). Защита от путей, ключей и кода проекта остаётся.', 'Tasks go to the cloud right away, with no card in the chat or dialog in the terminal (/cloud autosend). The guard against paths, keys and project code stays on.'],
    cloudTokens: ['Длина ответа', 'Max output tokens'], cloudTokensSub: ['256–65536; думающие модели тратят часть на размышление', '256-65536; thinking models spend part on reasoning'],
    cloudNotes: ['На что годится каждая модель', 'What each model is good for'],
    cloudNotesSub: ['Агент сам заполняет пустые заметки после поиска в интернете и по ним выбирает модель. Можно поправить.', 'The agent fills empty notes after a web search and picks models by them. You can edit them.'],
    cloudNotePh: ['пусто — агент поищет сам', 'empty: the agent looks it up'],
    setupTitle: ['Где работает модель?', 'Where does the model run?'],
    setupIntro: ['Сервер ещё не выбран. Вот что нашлось на этом компьютере; сменить потом: Настройки → maxedcode.', 'No model server is chosen yet. This is what runs or is installed on this PC; change it later in Settings → maxedcode.'],
    setupScan: ['Ищу серверы…', 'Looking for servers…'], setupNone: ['На этом компьютере серверов не нашлось. Установите LM Studio, Ollama или Strata, либо введите адрес.', 'No model server found on this PC. Install LM Studio, Ollama or Strata, or enter an address.'],
    setupUse: ['Выбрать', 'Use'], setupStart: ['Запустить', 'Start'], setupStarting: ['Запускаю…', 'Starting…'],
    setupRunning: ['запущен', 'running'], setupInstalled: ['установлен, не запущен', 'installed, not running'],
    setupModels: ['моделей', 'models'], setupAgain: ['Искать снова', 'Look again'],
    setupUrl: ['Другой адрес (IP или URL)', 'Another address (IP or URL)'], setupFind: ['Найти серверы', 'Find servers'],
    setupNoStart: ['Не ответил. Запустите его вручную: ', 'Did not answer. Start it yourself: '],
    setupRuns: ['запуск', 'starts with'], setupFrom: ['программа', 'program'],
    setupStartHint: ['Запустить сейчас (у сервера откроется своё окно)', 'Start it now (the server opens its own window)'],
    setupUseHint: ['Только запомнить адрес, сервер можно запустить позже', 'Only keep the address; start the server later'],
    setupRecent: ['Недавние', 'Recent'], setupFound: ['На этом компьютере', 'On this PC'],
    recentServers: ['Недавние серверы', 'Recent servers'], recentSub: ['Последние, с которыми работали; ключ облака нужно ввести снова', 'The last ones used; a cloud API needs its key again'],
    recentUse: ['Вернуть', 'Switch back'],
    updVersion: ['Версия', 'Version'], updCheck: ['Проверить сейчас', 'Check now'], updChecking: ['Проверяю…', 'Checking…'],
    updUpToDate: ['последняя', 'up to date'], updReady: ['скачана, установится при следующем запуске', 'downloaded, installs at the next start'],
    updNoInstall: ['эта копия не установка (git), не обновляется', 'this copy is not an install (git): not updated'],
    updAuto: ['Обновлять автоматически', 'Update automatically'],
    updAutoSub: ['Раз в день смотрит новый релиз на GitHub, скачивает его в фоне и ставит при следующем запуске. Сервер, чаты и настройки остаются.', 'Once a day looks for a new release on GitHub, downloads it in the background and installs it at the next start. The server, chats and settings stay.'],
    updatedTo: ['maxedcode обновлён до ', 'maxedcode was updated to '],
    updDownloaded: [' скачана: установится, когда откроете maxedcode снова', ' is downloaded: it installs when you open maxedcode again'],
    newChat: ['Новая сессия', 'New session'], newChatKey: ['Новая сессия (Ctrl + Alt + N)', 'New session (Ctrl + Alt + N)'],
    workspaces: ['Проекты', 'Projects'], defaultWs: ['Общая папка', 'Shared folder'], addFolder: ['Добавить проект', 'Add a project'],
    sharedFolder: ['Папка чатов без проекта', 'Folder of chats without a project'],
    faster: ['Быстрее', 'Faster'], smarter: ['Умнее', 'Smarter'],
    effortInfo: ['Сколько модель думает перед ответом. Выкл и Низкое — быстрые ответы; Высокое — дольше думает; Ultra — сначала план, потом каждая часть отдельно, проверка и исправления (для больших задач). Своё на каждый чат.',
      'How much the model thinks before it answers. Off and Low: quick answers; High: thinks longer; Ultra: a plan first, then each part on its own, a review and fixes (for big tasks). Set per chat.'],
    sharedFolderSub: ['Каждый чат без проекта получает здесь свою папку — она появляется, только когда агенту нужно что-то сохранить. Проект — любая ваша папка: «+» у списка проектов слева или выбор над полем ввода.',
      'Each chat without a project gets its own folder here, made only when the agent needs to save something. A project is any folder of yours: "+" by the project list on the left, or the picker above the input.'],
    change: ['Изменить', 'Change'], openFolder: ['Открыть в Проводнике', 'Open in File Explorer'],
    chatWorksIn: ['Чат работает в папке', 'This chat works in'], chooseFolder: ['Выбрать другую папку…', 'Choose another folder…'],
    search: ['Поиск сеансов', 'Search sessions'], sort: ['Сортировка', 'Sort'], sortRecent: ['По активности', 'By activity'], sortName: ['По имени', 'By name'],
    toggleSide: ['Свернуть боковую панель', 'Collapse the sidebar'], togglePanel: ['Правая боковая панель (Ctrl + Shift + B)', 'Right sidebar (Ctrl + Shift + B)'],
    settings: ['Настройки', 'Settings'], general: ['Общие', 'General'],
    viewChat: ['Чат', 'Chat'], viewTrace: ['Траектория', 'Trajectory'],
    explorer: ['Проводник файлов', 'File Explorer'], openIn: ['Открыть в', 'Open in'], moreActions: ['Другие действия', 'More actions'],
    apps: { explorer: ['Проводник файлов (по умолчанию)', 'File Explorer (default)'], vscode: ['VS Code', 'VS Code'], cursor: ['Cursor', 'Cursor'],
      sublime: ['Sublime Text', 'Sublime Text'], github: ['GitHub Desktop', 'GitHub Desktop'], gitbash: ['Git Bash', 'Git Bash'], terminal: ['Терминал Windows', 'Windows Terminal'] },
    rename: ['Переименовать', 'Rename'], exportChat: ['Экспорт в Markdown', 'Export to Markdown'], deleteChat: ['Удалить', 'Delete'],
    deleteAsk: ['Удалить этот чат? Сессия maxedcode тоже удалится.', 'Delete this chat? Its maxedcode session goes too.'],
    hideFolder: ['Убрать из списка', 'Remove from the list'], moveSameProject: ['Чат переносится только внутри своего проекта', 'A chat moves only within its own project'],
    noProject: ['Чаты без проекта', 'Chats without a project'], chatOwnFolder: ['Папка чата', "Chat's folder"],
    newGroup: ['Новая папка', 'New folder'], newGroupIn: ['Новая папка для чатов', 'New folder for chats'], renameProject: ['Переименовать', 'Rename'],
    groupDelete: ['Удалить папку (чаты останутся)', 'Delete the folder (chats stay)'], moveTo: ['Переместить в папку', 'Move to folder'],
    noGroup: ['Без папки', 'No folder'], newProject: ['Новый проект', 'New project'], newProjectSub: ['Пустая папка в .maxedcode\\projects', 'An empty folder in .maxedcode\\projects'],
    openProject: ['Открыть папку…', 'Open a folder…'], openProjectSub: ['Существующая папка на диске', 'An existing folder on the disk'],
    projectName: ['Название проекта', 'Project name'], folderActions: ['Действия', 'Actions'], resetName: ['Вернуть имя папки', "Use the folder's name"],
    placeholderNew: ['Опишите, что вы хотите построить: /команды, @файлы или сеансы.', 'Describe what you want to build: /commands, @files or sessions.'],
    placeholder: ['Отправьте сообщение или запустите задачу, /команды, @файлы или сеансы', 'Send a message or start a task, /commands, @files or sessions'],
    attach: ['Добавить', 'Add'], attachFile: ['Файл…', 'File…'], attachFolder: ['Рабочая папка…', 'Workspace folder…'],
    model: ['Модель', 'Model'], effort: ['Усилие', 'Effort'],
    planSub: ['Только читает и ищет, ничего не меняет', 'Reads and searches only, changes nothing'],
    autoSub: ['Выполняет инструменты без вопросов', 'Runs tools without asking'],
    manualSub: ['Спрашивает перед правками файлов и командами', 'Asks before file edits and commands'],
    editsSub: ['Правит файлы сам, перед командами спрашивает', 'Edits files on its own, asks before commands'],
    apHead: ['Разрешить?', 'Allow?'], apYes: ['Да', 'Yes'], apNo: ['Нет', 'No'], apAlways: ['Да, всегда (эту команду)', 'Yes, always (this exact call)'],
    apAllowed: ['Разрешено', 'Allowed'], apDenied: ['Отклонено', 'Denied'], apAlwaysDone: ['Разрешено навсегда', 'Always allowed'],
    apWaiting: ['Ждёт вашего ответа', 'Waiting for your answer'],
    apEdit: ['Изменить файл', 'Change a file'], apCommand: ['Выполнить команду', 'Run a command'], apPython: ['Запустить Python', 'Run Python'],
    apMcp: ['Вызвать MCP-инструмент', 'Call an MCP tool'], apTool: ['Вызвать инструмент', 'Call the tool'],
    apBrowser: ['Поработать в твоём браузере', 'Work in your browser'],
    apCloud: ['Отправить облачной модели этот текст (больше ничего не уходит)', 'Send this text to a cloud model (nothing else leaves this PC)'],
    apCloudAlways: ['Да, больше не спрашивать', 'Yes, stop asking'], apCloudAlwaysDone: ['Отправлено; дальше без подтверждения (Настройки → Облако)', 'Sent; no more asking (Settings -> Cloud)'],
    openFile: ['Открыть файл', 'Open the file'], filesN: ['файл(ов)', 'file(s)'], sendNotes: ['Отправить замечания', 'Send the comments'],
    notesIntro: ['Замечания к изменениям — исправь:', 'Comments on the changes, please fix:'], noChanges: ['В этом чате файлы не менялись', 'No files changed in this chat'],
    addNote: ['Замечание к строке', 'Comment on this line'], notePlaceholder: ['Что не так? Ctrl+Enter — добавить', "What's wrong? Ctrl+Enter to add"], notifyDone: ['Готово', 'Done'],
    working: ['Работает', 'Working'], doneIn: ['Завершено за ', 'Done in '], done: ['Завершено', 'Done'], stopped: ['Остановлено', 'Stopped'],
    sec: ['с', 's'], stepWord: ['шаг', 'step'], min: ['мин', 'min'], hourShort: ['ч', 'h'], dayShort: ['д', 'd'], justNow: ['сейчас', 'now'],
    reasoning: ['Размышления', 'Reasoning'],
    fsUp: ['На уровень выше', 'Up one level'], fsFilter: ['Фильтр по имени', 'Filter by name'], fsNewFile: ['Новый файл', 'New file'],
    fsNewFolder: ['Новая папка', 'New folder'], fsHidden: ['Скрытые файлы', 'Hidden files'], fsNoMatch: ['Ничего не найдено', 'Nothing matches'],
    fsEmpty: ['Папка пуста', 'The folder is empty'], fsItems: ['элементов', 'items'], fsOpenHere: ['Открыть папку здесь', 'Open the folder here'],
    fsPreview: ['Просмотр', 'Preview'], fsOpenWith: ['Открыть в Windows', 'Open with Windows'], fsReveal: ['Показать в Проводнике', 'Show in Explorer'],
    fsMention: ['Упомянуть в сообщении', 'Mention in the message'], fsDelete: ['Удалить в корзину', 'Delete to the Recycle Bin'],
    fsDeleteAsk: ['Переместить в корзину?', 'Move to the Recycle Bin?'], dropFolderApp: ['Путь папки при перетаскивании виден только в окне приложения', "A dropped folder's path is known only in the app window"], opFailed: ['Не получилось', 'It did not work'], copyAnyPath: ['Копировать путь', 'Copy the path'], helperSteps: ['Ход работы', 'Its work'], openInPanel: ['Открыть в боковой панели', 'Open in the side panel'],
    subagentWord: ['Подагент', 'Subagent'], task: ['Задача', 'Task'], thinking: ['Думает', 'Thinking'], clickToExpand: ['Нажмите, чтобы развернуть', 'Click to expand'], args: ['Аргументы', 'Arguments'], result: ['Результат', 'Result'],
    copy: ['Копировать', 'Copy'], copied: ['Скопировано', 'Copied'], fork: ['Ответвить отсюда', 'Fork from here'],
    usage: ['Использование', 'Usage'], tok: ['ток', 'tok'], tokPerSec: ['ток/с', 'tok/s'], genSpeed: ['Генерация', 'Generation'], readSpeed: ['Чтение промпта', 'Prompt reading'],
    ctxUsed: ['контекст', 'context'], ctxFree: ['Свободно', 'Free'],
    turns: ['Ходов', 'Turns'], steps: ['шагов', 'steps'], cacheHit: ['Попадание в кэш', 'Cache hit'],
    failed: ['Ход завершился с ошибкой', 'The turn failed'],
    btw: ['Побочный вопрос (не сохраняется)', 'Side question (not saved)'], btwNeedsChat: ['/btw работает в начатом чате.', '/btw needs a started chat.'],
    busy: ['Идёт ход: дождитесь конца или остановите.', 'A turn is running: wait or stop it.'],
    queued: ['В очереди', 'Queued'],
    cmdBtw: ['Побочный вопрос с контекстом чата (не попадает в историю)', 'Side question with the chat context (not added to history)'],
    cmdPlan: ['Режим Plan: только читать и планировать', 'Plan mode: read and plan only'], cmdAuto: ['Режим Auto: делать', 'Auto mode: do the work'], cmdManual: ['Режим Manual: спрашивать перед действиями', 'Manual mode: ask before actions'],
    cmdExport: ['Скачать этот чат в Markdown', 'Download this chat as Markdown'], cmdNew: ['Новая сессия', 'New session'],
    commands: ['Команды', 'Commands'], filesHead: ['Файлы и папки', 'Files and folders'], sessionsHead: ['Сессии', 'Sessions'],
    start: ['Старт', 'Start'], files: ['Файлы', 'Files'], terminal: ['Терминал', 'Terminal'],
    wsFiles: ['Файлы рабочей области', 'Workspace files'], wsFilesSub: ['Просмотр файлов в рабочей области этого сеанса', "Browse the files of this session's workspace"],
    browser: ['Браузер', 'Browser'], browserSub: ['Просмотр веб-страниц', 'Browse web pages'],
    bAddress: ['Введите адрес HTTP(S)', 'Enter an HTTP(S) address'], bBack: ['Назад', 'Back'], bForward: ['Вперёд', 'Forward'],
    bReload: ['Перезагрузить', 'Reload'], bGo: ['Перейти', 'Go'], bExternal: ['Открыть в системном браузере', 'Open in system browser'],
    bSandboxOff: ['Отключить ограничения песочницы', 'Disable sandbox restrictions'],
    bSandboxOn: ['Восстановить ограничения песочницы', 'Restore sandbox restrictions'],
    bSandboxWarn: ['Ограничения песочницы отключены: страница может уводить окно, скачивать файлы и открывать диалоги.',
      'Sandbox restrictions are disabled; the page can navigate the app, download files and open dialogs.'],
    bStart: ['Введите адрес HTTP(S), чтобы начать просмотр', 'Enter an HTTP(S) address to start browsing'],
    bLoadFail: ['Страница не открылась', 'The page did not open'],
    termExit: ['процесс завершён, код', 'process exited, code'], termGone: ['терминал закрыт', 'the terminal is closed'],
    termFail: ['Терминал не запустился', 'The terminal did not start'],
    bLoading: ['Открытие…', 'Opening…'], bPrevious: ['Ранее открыто', 'Previously opened'], bRestore: ['Восстановить страницу', 'Restore page'],
    bErrEmpty: ['Введите адрес.', 'Enter an address.'], bErrInvalid: ['Этот адрес недействителен или слишком длинный.', 'That address is invalid or too long.'],
    bErrProtocol: ['Поддерживаются только адреса HTTP и HTTPS; локальные файлы открывайте через «Файлы».', 'Only HTTP and HTTPS addresses are supported; open local files from Files.'],
    bErrCredentials: ['Адрес не может содержать имя пользователя или пароль.', 'Addresses cannot contain a username or password.'],
    bErrSelf: ['Встроенный браузер не открывает само окно maxedcode.', 'The embedded browser cannot open the maxedcode window itself.'],
    newTerm: ['Новый терминал', 'New terminal'], newTermSub: ['Запуск команд в рабочей области сеанса', "Run commands in the session's workspace"],
    binary: ['Двоичный файл', 'Binary file'], cut: ['… файл обрезан', '… the file is cut'],
    permission: ['Разрешение', 'Permission'], permissionSub: ['Режим по умолчанию для новых сеансов', 'The default mode for new sessions'],
    language: ['Язык', 'Language'], theme: ['Внешний вид', 'Appearance'], light: ['Светлая', 'Light'], dark: ['Тёмная', 'Dark'], system: ['Система', 'System'],
    fontSize: ['Размер шрифта', 'Font size'], fontSizeSub: ['Влияет только на содержание разговора', 'Affects the conversation only'], px: ['пикселей', 'pixels'],
    details: ['Детали работы', 'Work details'], detailsSub: ['Насколько подробно показывать работу', 'How much of the work to show'],
    detailsFull: ['Подробный', 'Detailed'], detailsTerse: ['Краткий', 'Brief'],
    fold: ['Когда сворачивать детали работы', 'When to collapse the work details'], foldSub: ['Когда автоматически сворачивать сведения о работе', 'When the work details collapse by themselves'],
    foldDone: ['По завершении', 'When done'], foldNever: ['Никогда', 'Never'],
    replies: ['Ответы после сворачивания', 'Replies after collapsing'], repliesSub: ['Какие ответы помощника остаются видимыми, когда сведения о работе свёрнуты', 'Which assistant replies stay visible when the work details are collapsed'],
    repliesAll: ['Все ответы', 'All replies'], repliesFinal: ['Только итоговый', 'Only the final one'],
    busySend: ['Отправка во время работы', 'Sending while busy'], busySendSub: ['Что делает Enter, пока агент работает', 'What Enter does while the agent works'],
    busyQueue: ['В очередь', 'Queue'], busyInterrupt: ['Остановить и отправить', 'Stop and send'],
    traceToggle: ['Показать траекторию', 'Show the trajectory'], traceToggleSub: ['Вкладка со всеми шагами хода: размышления, вызовы, результаты', 'A tab with every step of a turn: reasoning, calls, results'],
    shortcuts: ['Сочетания клавиш', 'Keyboard shortcuts'],
    shortcutsSub: ['Ctrl+Alt+N — новая сессия · Ctrl+Alt+K — поиск чатов · Ctrl+Alt+G — переименовать · Ctrl+Shift+A — в архив · Ctrl+Alt+B — левая панель · Ctrl+Shift+B — правая панель · Ctrl+Alt+P — файлы · Ctrl+` — терминал · Ctrl+Alt+T — браузер · Ctrl+Alt+, или Ctrl+/ — настройки · Ctrl+= / Ctrl+- — шрифт · Esc Esc — остановить',
      'Ctrl+Alt+N new session · Ctrl+Alt+K search chats · Ctrl+Alt+G rename · Ctrl+Shift+A archive · Ctrl+Alt+B left sidebar · Ctrl+Shift+B right panel · Ctrl+Alt+P files · Ctrl+` terminal · Ctrl+Alt+T browser · Ctrl+Alt+, or Ctrl+/ settings · Ctrl+= / Ctrl+- font · Esc Esc stop'],
    mxTitle: ['Настройки maxedcode', 'maxedcode settings'],
    brHead: ['Браузер', 'Browser'],
    brIntro: ['Агент может работать в твоём Chrome или Edge, там, где ты уже вошёл на сайты. Задачу в браузере делает отдельный помощник в своём контексте, в чат приходит только итог, поэтому кэш чата не сбрасывается.',
      'The agent can work in your own Chrome or Edge, where you are already signed in. A separate helper does the browser task in its own context and only the result comes back to the chat, so the chat cache stays intact.'],
    brToggle: ['Агент пользуется моим браузером', 'The agent may use my browser'],
    brToggleSub: ['В Manual и Accept edits перед каждой задачей спросит. В терминале включится после перезапуска maxedcode.', 'Manual and Accept edits ask before each task. The terminal picks it up after a maxedcode restart.'],
    brConnected: ['Подключено', 'Connected'], brWaiting: ['Расширение ещё не подключено', 'The extension is not connected yet'],
    brGuide: ['Открыть пошаговый гайд', 'Open the step-by-step guide'],
    brGuideSub: ['со скриншотами, в браузере', 'with screenshots, in your browser'],
    brStep1: ['В браузере вставь в адресную строку', 'In the browser, paste'],
    brStep1b: ['и нажми Enter (или меню ⋮ → Расширения → Управление расширениями). Не F12: это страница «Расширения».',
      'into the address bar and press Enter (or menu ⋮ → Extensions → Manage Extensions). Not F12: it is the Extensions page.'],
    brStep2: ['Справа вверху включи переключатель «Режим разработчика».', 'Turn on the "Developer mode" switch at the top right.'],
    brStep3: ['Проще всего: нажми «Показать папку» и перетащи папку extension мышью на страницу «Расширения» (режим разработчика включён).',
      'Easiest: click "Show the folder" and drag the extension folder onto the Extensions page (Developer mode on).'],
    brReveal: ['Показать папку', 'Show the folder'],
    brStep3b: ['Или «Загрузить распакованное»: вставь путь в поле «Папка» внизу окна выбора папки и нажми «Выбор папки». Если вставляешь в адресную строку вверху, сначала нажми Enter и дождись, пока в списке появится manifest.json, иначе выберется прежняя папка (ошибка «Файл манифеста отсутствует»):',
      'Or "Load unpacked": paste the path into the "Folder:" box at the bottom of the folder window and click "Select Folder". Pasting into the address bar at the top needs Enter first, until the list shows manifest.json; otherwise the previous folder is chosen ("Manifest file is missing or unreadable"):'],
    brStep4: ['Появится карточка maxedcode, браузер подключится сам. Проверить:', 'The maxedcode card appears and the browser connects by itself. Check:'],
    brCheck: ['Проверить', 'Check'], brCopy: ['Копировать путь', 'Copy path'],
    mxIntro: ['Общие с терминалом: те же файлы, что у CLI. Окно берёт изменения со следующего хода, запущенный терминал — после перезапуска. Пустое поле — как по умолчанию (показано серым).',
      'Shared with the terminal: the same files the CLI uses. The window applies changes from the next turn, a running terminal after a restart. An empty field means the default (shown in grey).'],
    server: ['Модель и сервер', 'Model and server'], answers: ['Ответы', 'Answers'], memoryGroup: ['Память (общая для CLI и окна)', 'Memory (shared by the CLI and the window)'],
    base_url: ['Адрес сервера', 'Server URL'], base_urlSub: ['В любом виде: 192.168.0.10, 192.168.0.10:1234, http://host:8080/v1 — порт и /v1 найдутся сами', 'Any form: 192.168.0.10, 192.168.0.10:1234, http://host:8080/v1 - the port and /v1 are found'],
    provider: ['Где работает модель', 'Where the model runs'], providerSub: ['Свой сервер (этот ПК или сеть) или облачный API', 'Your own server (this PC or the network) or a cloud API'],
    provOwn: ['Свой сервер', 'Own server'], apiKey: ['API-ключ', 'API key'],
    apiKeySub: ['Для облака или сервера с ключом; хранится в config.json программы', 'For a cloud or a server with a key; kept in the program\'s config.json'],
    keySaved: ['сохранён', 'saved'], keyNone: ['нет — локальному серверу не нужен', 'none - a local server needs none'],
    modelSub: ['Авто — модель, которую отдаёт сервер (как в терминале)', 'Auto: the model the server serves (as in the terminal)'],
    modelCloud: ['Выберите модель: облачный API сам её не выбирает', 'Pick a model: a cloud API does not choose one for you'],
    ctx: ['Контекст', 'Context'], ctxSub: ['Берётся с сервера сам', 'Read from the server'],
    temperature: ['Температура', 'Temperature'], temperatureSub: ['0–2; пусто — как настроено для модели', '0–2; empty = as tuned for the model'],
    effortDefault: ['Effort по умолчанию', 'Default effort'], effortSub: ['Для новых чатов и CLI (/effort); в чате — у поля ввода', 'For new chats and the CLI (/effort); per chat: next to the input'],
    memory: ['О тебе и как отвечать', 'About you and how to answer'],
    memorySub: ['Видит каждая сессия, в терминале и в окне. Например: «Я не программист — объясняй простыми словами».', 'Every session sees it, in the terminal and in the window. E.g. "I am not a programmer: explain in plain words."'],
    auto: ['авто', 'auto'], offline: ['сервер не отвечает', 'the server does not answer'], tokens: ['токенов', 'tokens'],
    byDefault: ['по умолчанию', 'default'], save: ['Сохранить', 'Save'], saving: ['Сохраняю…', 'Saving…'], saved: ['Сохранено', 'Saved'],
    loading: ['Загрузка…', 'Loading…'], filesLbl: ['Файлы', 'Files'], codeWord: ['Код', 'Code'],
    toBottom: ['К концу', 'To the end'], escAgain: ['Нажмите Esc ещё раз, чтобы остановить', 'Press Esc again to stop'],
    connLost: ['Нет связи с сервером maxedcode. Если окно перезапускали — откройте его заново.', 'No connection to the maxedcode server. If the window was restarted, open it again.'],
    showAll: ['Показать всё', 'Show all'], linesWord: ['строк', 'lines'], moreLines: ['строк ещё', 'more lines'],
    changes: ['Изменения', 'Changes'], changedFiles: ['Изменённые файлы', 'Changed files'],
    notice_cut: ['Ответ упёрся в лимит длины — агент продолжает его', 'The reply hit the length limit; the agent continues it'],
    notice_empty: ['Модель вернула пустой ответ — спрашиваю ещё раз', 'The model returned an empty reply; asking again'],
    notice_loop: ['Агент повторялся — повторы убраны из контекста', 'The agent repeated itself; the repeats were removed from the context'],
    quote: ['Спросить', 'Ask'], selText: ['Выделенный текст', 'Selected text'], editQueued: ['Нажмите, чтобы изменить', 'Click to edit'],
    archive: ['В архив', 'Archive'], unarchive: ['Вернуть из архива', 'Restore from the archive'], archived: ['В архиве', 'Archived'],
    showArchived: ['Показывать архив', 'Show the archive'], inText: ['В тексте чатов', 'In chat text'],
    copyPath: ['Копировать путь папки', 'Copy the folder path'], notFound: ['Не найдено', 'Not found'],
    source: ['Исходник', 'Source'], rendered: ['Просмотр', 'Preview'],
    traceSearch: ['Поиск по траектории', 'Search the trajectory'], collapseAll: ['Свернуть всё', 'Collapse all'], expandAll: ['Развернуть всё', 'Expand all'],
    turnWord: ['Ход', 'Turn'], found: ['найдено', 'found'], modelTime: ['ответ модели', 'model reply'],
    cliTab: ['Команды CLI', 'CLI commands'],
    cliIntro: ['То же, что slash-команды терминала, на тех же файлах: поменяли здесь — поменялось и в CLI. Окно берёт изменения со следующего хода. /new, /btw, /plan, /auto, /effort, /model, история, копирование и экспорт — уже кнопки в окне.',
      'The same as the terminal\'s slash commands, on the same files: a change here is a change in the CLI too. The window applies it from the next turn. /new, /btw, /plan, /auto, /effort, /model, history, copy and export are buttons in the window already.'],
    cliFolder: ['Папка чата (правила проекта, память, навыки)', "The chat's folder (project rules, memory, skills)"],
    rulesHead: ['Разрешения — /permissions, /allow, /deny', 'Permissions: /permissions, /allow, /deny'],
    rulesSub: ['В окне агент работает без вопросов: команда, которой правило велит спросить (ask), опасная команда и инструмент MCP не выполнятся, пока для них нет правила allow. deny запрещает всегда.',
      'In the window the agent works without asking: a command a rule says to ask about (ask), a dangerous command and an MCP tool do not run until an allow rule covers them. deny always forbids.'],
    rulePh: ['например powershell(git *), py_run или mcp__сервер__инструмент', 'e.g. powershell(git *), py_run or mcp__server__tool'],
    fromSettings: ['из settings.json — меняется там', 'from settings.json: edit it there'],
    add: ['Добавить', 'Add'], remove: ['Убрать', 'Remove'], none: ['Нет', 'None'],
    mcpHead: ['MCP-серверы — /mcp', 'MCP servers: /mcp'],
    mcpSub: ['Внешние инструменты. Сервер меняет список инструментов: первый ход после этого перечитает промт целиком.',
      'External tools. A server changes the tool list: the first turn after it re-reads the whole prompt.'],
    mcpName: ['имя', 'name'], mcpCmd: ['команда с аргументами или https://адрес', 'a command with arguments or an https:// address'],
    memHead: ['Память — /memory', 'Memory: /memory'], memSub: ['Файлы, которые каждая сессия в этой папке получает в начале. Нажмите, чтобы открыть.', 'The files every session in this folder gets at the start. Click to open.'],
    notePh: ['Заметка для этого проекта (её увидит каждая сессия в этой папке)', 'A note for this project (every session in this folder sees it)'], saveNote: ['Сохранить заметку', 'Save the note'],
    skillsHead: ['Навыки — /skill', 'Skills: /skill'], skillsSub: ['В чате: /имя или /skill имя — агент получит текст навыка.', 'In the chat: /name or /skill name: the agent gets the skill text.'],
    commandsHead: ['Свои команды — /commands', 'Custom commands: /commands'], commandsSub: ['Файлы .md в .maxedcode/commands (проекта или ~/.maxedcode). В чате: /имя аргументы.', '.md files in .maxedcode/commands (the project\'s or ~/.maxedcode). In the chat: /name arguments.'],
    agentsHead: ['Подагенты — /agents', 'Subagents: /agents'], agentsSub: ['Описания в .maxedcode/agents: агент сам выбирает их через инструмент subagent.', 'Definitions in .maxedcode/agents: the agent picks them through the subagent tool.'],
    hooksHead: ['Хуки — /hooks', 'Hooks: /hooks'], hooksSub: ['Команды из settings.json, которые запускаются на события агента (только просмотр).', 'Commands from settings.json run on agent events (view only).'],
    toolsHead: ['Инструменты — /tools', 'Tools: /tools'], toolsSub: ['Что агент умеет вызывать (входит в начало промта).', 'What the agent can call (part of the prompt prefix).'],
    cmdSkill: ['Навык', 'Skill'], cmdCustom: ['Своя команда', 'Custom command'],
    wrapLines: ['Перенос строк', 'Wrap lines'], toTerminal: ['В терминал', 'To the terminal'],
    calloutNOTE: ['Заметка', 'Note'], calloutTIP: ['Совет', 'Tip'], calloutIMPORTANT: ['Важно', 'Important'],
    calloutWARNING: ['Внимание', 'Warning'], calloutCAUTION: ['Осторожно', 'Caution'],
    askHead: ['Агент спрашивает — выберите ответ', 'The agent asks: pick an answer'], askPicked: ['Выбрано', 'Picked'], askOwn: ['или напишите свой ответ ниже', 'or write your own answer below'],
    nrMcp: ['Окно не может спросить разрешения на инструмент MCP. Разрешите его — и повторите запрос.', 'The window cannot ask before an MCP tool runs. Allow it, then ask again.'],
    nrAllow: ['Разрешить этот инструмент', 'Allow this tool'], nrAllowed: ['Правило добавлено — повторите запрос', 'Rule added: ask again'],
    nrAsk: ['Для этого правило велит спрашивать (ask), а окно спросить не может. Уберите правило, если оно не нужно.', 'A rule says to ask before this (ask), and the window cannot ask. Remove the rule if it is not needed.'],
    nrRules: ['Открыть правила', 'Open the rules'],
    nrDanger: ['Опасная команда не выполнена. Проверьте её и выполните сами, если уверены.', 'A dangerous command was not run. Check it and run it yourself if you are sure.'],
    nrTerminal: ['Открыть в терминале', 'Open in the terminal'],
  }
  // macOS / Linux (a browser tab or the pywebview window, see webapp/native.py): their own names for the file
  // manager, the terminal and the trash. The Windows app window (navigator.platform "Win32") keeps the texts above.
  var OS = /^Mac/i.test(navigator.platform || '') ? 'mac' : /^Win/i.test(navigator.platform || '') ? 'win' : 'other'
  if (OS !== 'win') {
    var FM = OS === 'mac' ? ['Finder', 'Finder'] : ['файловом менеджере', 'the file manager']
    TEXT.explorer = OS === 'mac' ? ['Finder', 'Finder'] : ['Файловый менеджер', 'File manager']
    TEXT.apps.explorer = [TEXT.explorer[0] + ' (по умолчанию)', TEXT.explorer[1] + ' (default)']
    TEXT.apps.terminal = ['Терминал', 'Terminal']
    TEXT.openFolder = ['Открыть в ' + FM[0], 'Open in ' + FM[1]]
    TEXT.fsOpenWith = ['Открыть программой по умолчанию', 'Open with the default app']
    TEXT.fsReveal = ['Показать в ' + FM[0], 'Show in ' + FM[1]]
    TEXT.fsDelete = ['Удалить в корзину', 'Move to the Trash']
    TEXT.fsDeleteAsk = ['Переместить в корзину?', 'Move to the Trash?']
    TEXT.cloudKeySafe = ['Ключ хранится в связке ключей системы (или в файле, доступном только вам) и не показывается снова. Несколько ключей одного провайдера — запасные: при лимите запрос уходит на следующий.',
      'The key is kept in the system keyring (or in a file only you can read) and never shown again. Several keys of one provider are spares: a rate-limited request moves to the next one.']
  }
  // The pywebview window (macOS / Linux) gives the page what a desktop window can: dialogs, real paths of dropped
  // files, outside links in the system browser, notifications. undefined in the Windows exe and in a browser.
  function pyApi() { return !(window.chrome && window.chrome.webview) && window.pywebview && window.pywebview.api || null }
  if (!(window.chrome && window.chrome.webview)) {
    var openPage = window.open
    window.open = function (url) {
      var a = pyApi(), u
      try { u = new URL(String(url || ''), location.href) } catch (e) { u = null }
      if (a && a.open_url && u && u.origin !== location.origin && /^(https?|mailto):$/.test(u.protocol)) { a.open_url(u.href); return null }
      return openPage.apply(window, arguments)
    }
  }
  var TOOL_LABELS = {
    grep: ['Искомый код', 'Searched code', 'search'], glob: ['Поиск файлов', 'Found files', 'search'],
    read_file: ['Прочитан файл', 'Read a file', 'file'], edit_file: ['Изменён файл', 'Edited a file', 'pencil'],
    patch: ['Изменён файл', 'Edited a file', 'pencil'], apply_diff: ['Изменён файл', 'Edited a file', 'pencil'],
    write_file: ['Записан файл', 'Wrote a file', 'pencil'], powershell: ['Команда', 'Command', 'terminal'],
    bash: ['Команда', 'Command', 'terminal'],
    py_run: ['Запуск Python', 'Ran Python', 'terminal'], code_mode: ['Скрипт', 'Script', 'terminal'],
    web_search: ['Поиск в интернете', 'Web search', 'globe'], fetch_url: ['Открыта страница', 'Fetched a page', 'globe'],
    browser: ['Браузер', 'Browser', 'globe'],
    todo_write: ['План задач', 'Task list', 'list'], subagent: ['Подагент', 'Subagent', 'branch'],
    remember: ['Заметка в память', 'Memory note', 'brain'], skill: ['Навык', 'Skill', 'brain'],
    ask_user: ['Вопрос', 'Question', 'list'], py_source: ['Исходник', 'Source', 'file'], tool_search: ['Поиск инструмента', 'Tool search', 'search'],
    bg_output: ['Фоновая задача', 'Background job', 'terminal'], bg_kill: ['Остановлена задача', 'Stopped a job', 'terminal'],
    cloud_code: ['Облачный черновик', 'Cloud draft', 'globe'], exit_plan_mode: ['План готов', 'Plan ready', 'list'], finish: ['Готово', 'Done', 'check'],
  }
  var EFFORTS = ['off', 'low', 'medium', 'high', 'ultra']

  function pref(key, fallback) {
    try { var v = localStorage.getItem('mx.' + key); return v === null ? fallback : JSON.parse(v) } catch (e) { return fallback }
  }
  function setPref(key, value) { try { localStorage.setItem('mx.' + key, JSON.stringify(value)) } catch (e) {} }
  // Until picked in Settings -> General: the language of Windows (WebView2 reports it), English for any other
  var lang = pref('lang', /^ru\b/i.test(navigator.language || '') ? 'ru' : 'en')
  // Once: while Plan/Auto was one switch for the whole window, picking Plan in a chat also saved it as the
  // default, and every chat went on in Plan. Back to Auto, as the old window ran.
  if (!pref('seatsPerChat', false)) {
    setPref('mode', 'plan')
    setPref('effort', '')
    setPref('seats', {})
    setPref('seatsPerChat', true)
  }
  // Once: four modes as in the terminal (Manual / Accept edits / Auto / Plan), and Plan is the default for new chats.
  if (!pref('modes4', false)) { setPref('mode', 'plan'); setPref('modes4', true) }
  function t(key) { var r = TEXT[key]; return r ? r[lang === 'ru' ? 0 : 1] : key }
  function tl(row) { return row[lang === 'ru' ? 0 : 1] }

  // ── state ──
  var state = {
    sessions: [], chat: null,
    folder: pref('folder', ''), folders: pref('folders', []),
    hidden: pref('hiddenFolders', []), sort: pref('sort', 'recent'), ctx: 0, home: '', settings: null, apps: ['explorer'],
    view: 'chat', last: null, unseen: pref('unseen', {}),
  }
  var $ = function (id) { return document.getElementById(id) }
  function el(tag, cls, text) {
    var e = document.createElement(tag)
    if (cls) e.className = cls
    if (text !== undefined && text !== null) e.textContent = text
    return e
  }
  function iconBtn(name, title, onclick) {
    var b = el('button', 'icon-btn')
    b.innerHTML = icon(name)
    if (title) b.title = title
    if (onclick) b.onclick = onclick
    return b
  }
  function api(path, body) {
    var init = body === undefined ? {} : { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body) }
    return fetch(path, init).then(function (r) { return r.json() })
  }
  // The browser extension speaks the window's language: the server keeps it in a file the browser bridge reads
  function shareLang() { api('/api/lang', { lang: lang }).catch(function () {}) }
  shareLang()
  loadUpdate(true)
  function base(p) {
    if (state.workspace && same(p, state.workspace)) return t('noProject')
    if (ownFolder(p)) return t('chatOwnFolder')
    return String(p || '').replace(/[\\/]+$/, '').split(/[\\/]/).pop() || p
  }
  function same(a, b) { return String(a || '').toLowerCase() === String(b || '').toLowerCase() }
  function parentOf(p) { return String(p || '').replace(/[\\/]+$/, '').replace(/[\\/][^\\/]*$/, '') }
  // A chat without a project works in its own folder inside the chats folder (state.workspace): in the list all
  // of them are one group, the chats folder.
  function ownFolder(p) { return !!(state.workspace && p && same(parentOf(p), state.workspace)) }
  function groupOf(cwd) { return ownFolder(cwd) ? state.workspace : cwd }
  function clock(d) { d = d || new Date(); return ('0' + d.getHours()).slice(-2) + ':' + ('0' + d.getMinutes()).slice(-2) }
  function kfmt(n) { return n >= 1000 ? (n / 1000).toFixed(1).replace(/\.0$/, '') + 'K' : String(n) }
  // a step's time: tenths under 10 s ("2.4 s"), then as secs()
  function dur(ms) { return ms < 10000 ? (Math.max(0, ms) / 1000).toFixed(1) + ' ' + t('sec') : secs(ms) }
  function secs(ms) { var s = Math.max(0, Math.round(ms / 1000)); return s < 60 ? s + t('sec') : Math.floor(s / 60) + t('min') + ' ' + (s % 60) + t('sec') }
  // A new message or step slides in once; the mark goes when it has, so showing the chat again does not replay it
  function fresh(node) {
    node.classList.add('fresh')
    node.addEventListener('animationend', function done(e) { if (e.target === node) { node.classList.remove('fresh'); node.removeEventListener('animationend', done) } })
    return node
  }
  // The chat follows the answer until the reader scrolls up, whatever the distance (a nearness test let a small
  // scroll up be pulled back, and a big piece of new text stopped the following); back at the end it follows again.
  var stick = true, lastTop = 0
  function fromEnd(s) { return s.scrollHeight - s.scrollTop - s.clientHeight }
  function scrollDown(force) {
    var s = $('scroll')
    if (force) stick = true
    if (stick) { s.scrollTop = s.scrollHeight; lastTop = s.scrollTop }
    updateToBottom()
  }
  function onChatScroll() {
    var s = $('scroll'), gap = fromEnd(s)
    if (gap < 30) stick = true
    else if (s.scrollTop < lastTop - 2) stick = false   // moved up and not at the end (not text shrinking at the end)
    lastTop = s.scrollTop
    updateToBottom()
  }
  // The reader scrolled up: the chat stops following the answer and a button brings them back to the end.
  function updateToBottom() {
    var s = $('scroll')
    $('to-bottom').hidden = state.view !== 'chat' || stick || fromEnd(s) < 30
  }
  // A box with its own scroll (reasoning) keeps showing its newest lines unless the reader scrolled it up
  function keepEnd(box, change) {
    var end = box.scrollHeight - box.scrollTop - box.clientHeight < 24
    change()
    if (end) box.scrollTop = box.scrollHeight
  }
  var toastEl = null, toastTimer = null
  function toast(text, ms) {
    if (!toastEl) { toastEl = el('div', 'toast'); document.body.appendChild(toastEl) }
    toastEl.textContent = text
    toastEl.hidden = false
    clearTimeout(toastTimer)
    toastTimer = setTimeout(function () { toastEl.hidden = true }, ms || 1800)
  }
  // ── auto-update (maxedcode/update.py): what this start installed, or a newer version waiting for the next one ──
  var updateInfo = null
  function loadUpdate(show) {
    return api('/api/update').then(function (u) {
      updateInfo = u
      if (show && u.notice) toast(t('updatedTo') + u.notice, 6000)
      else if (show && u.ready) toast('maxedcode ' + u.ready + t('updDownloaded'), 6000)
      if (!$('modal').hidden) renderGeneral()
    }, function () {})
  }
  function button(label, onclick) {
    var b = el('button', 'btn small outline', label)     // secondary: the accent fill is for the one main action
    b.type = 'button'
    b.onclick = onclick
    return b
  }
  function copy(text, button) {
    navigator.clipboard.writeText(text).then(function () {
      if (!button) return
      var old = button.innerHTML
      button.innerHTML = icon('check')
      setTimeout(function () { button.innerHTML = old }, 1200)
    })
  }

  // ── syntax highlighting: one pass of a combined regex per language family; keywords, strings, comments,
  // numbers, calls and shell variables. Good enough to read code, not a parser. ──
  var LANG_ALIAS = { python: 'py', py3: 'py', javascript: 'js', typescript: 'js', ts: 'js', jsx: 'js', tsx: 'js', mjs: 'js',
    powershell: 'ps', pwsh: 'ps', ps1: 'ps', bash: 'sh', shell: 'sh', zsh: 'sh', console: 'sh', bat: 'sh', cmd: 'sh',
    jsonc: 'json', cpp: 'c', 'c++': 'c', cs: 'c', csharp: 'c', java: 'c', go: 'c', rust: 'c', rs: 'c', kotlin: 'c', swift: 'c',
    xml: 'html', svg: 'html', vue: 'html', scss: 'css', yml: 'yaml', toml: 'yaml', ini: 'yaml' }
  var LANG_NAME = { py: 'Python', python: 'Python', js: 'JavaScript', javascript: 'JavaScript', ts: 'TypeScript', typescript: 'TypeScript',
    ps: 'PowerShell', ps1: 'PowerShell', powershell: 'PowerShell', pwsh: 'PowerShell', sh: 'Bash', bash: 'Bash', shell: 'Shell',
    cmd: 'CMD', bat: 'Batch', json: 'JSON', html: 'HTML', xml: 'XML', css: 'CSS', sql: 'SQL', yaml: 'YAML', yml: 'YAML', toml: 'TOML',
    c: 'C', cpp: 'C++', cs: 'C#', csharp: 'C#', java: 'Java', go: 'Go', rust: 'Rust', md: 'Markdown', markdown: 'Markdown', diff: 'Diff' }
  function words(list) { var o = {}; list.split(' ').forEach(function (w) { o[w] = 1 }); return o }
  var STR_DQ = /"(?:\\.|[^"\\\n])*"/.source, STR_SQ = /'(?:\\.|[^'\\\n])*'/.source
  var HL = {
    py: { kw: words('and as assert async await break class continue def del elif else except False finally for from global if import in is lambda None nonlocal not or pass raise return True try while with yield match case self print'),
      comment: /#.*/.source, str: /"""[\s\S]*?"""|'''[\s\S]*?'''/.source + '|[rbfuRBFU]{0,2}' + STR_DQ + '|[rbfuRBFU]{0,2}' + STR_SQ },
    js: { kw: words('async await break case catch class const continue debugger default delete do else export extends false finally for from function if import in instanceof let new null of return static super switch this throw true try typeof undefined var void while yield interface type enum implements public private protected readonly as'),
      comment: /\/\/.*|\/\*[\s\S]*?\*\//.source, str: /`(?:\\.|[^`\\])*`/.source + '|' + STR_DQ + '|' + STR_SQ },
    ps: { kw: words('begin break catch class continue do else elseif end exit filter finally for foreach function if in param process return switch throw trap try until while -eq -ne -gt -ge -lt -le -like -notlike -match -notmatch -and -or -not -in -notin -contains'),
      ci: true, comment: /<#[\s\S]*?#>|#.*/.source, str: /@"[\s\S]*?"@|@'[\s\S]*?'@|"(?:`.|[^"`\n])*"|'(?:''|[^'\n])*'/.source,
      vars: /\$\{[^}]*\}|\$[\w:]+/.source, words: /-?[A-Za-z_][\w-]*/.source },
    sh: { kw: words('if then else elif fi for while do done case esac in function return local export echo exit set unset cd source sudo'),
      comment: /#.*/.source, str: /"(?:\\.|[^"\\])*"|'[^']*'/.source, vars: /\$\{[^}]*\}|\$\w+|%\w+%/.source },
    json: { kw: words('true false null'), str: STR_DQ },
    c: { kw: words('auto bool break case catch char class const continue default delete do double else enum extern false final float for fn func go if impl import include int let long loop match mod mut namespace new nil null package private protected public return self short signed sizeof static struct super switch template this throw true try typedef typename union unsigned use using var virtual void volatile while string object interface override async await yield'),
      comment: /\/\/.*|\/\*[\s\S]*?\*\/|#\w+/.source, str: STR_DQ + '|' + STR_SQ + '|' + /`[^`]*`/.source },
    sql: { kw: words('select from where and or not insert into values update set delete create table index view drop alter join left right inner outer on group by order having limit offset as distinct union all null is in like between case when then else end primary key foreign references default exists'),
      ci: true, comment: /--.*/.source, str: /'(?:''|[^'])*'/.source },
    css: { kw: words('important'), comment: /\/\*[\s\S]*?\*\//.source, str: /"[^"]*"|'[^']*'/.source, vars: /#[\da-fA-F]{3,8}\b|--[\w-]+/.source },
    html: { kw: {}, comment: /<!--[\s\S]*?-->/.source, str: /"[^"]*"|'[^']*'/.source, vars: /<\/?[\w:-]+|\/?>/.source },
    yaml: { kw: words('true false null yes no on off'), comment: /#.*/.source, str: /"(?:\\.|[^"\\])*"|'[^']*'/.source, vars: /^[ \t-]*[\w.-]+(?=\s*[:=])/.source },
  }
  var NEVER = /\b\B/.source, NUM = /\b0x[\da-fA-F]+\b|\b\d[\d_]*(?:\.\d+)?(?:[eE][+-]?\d+)?\b/.source, WORD = /[A-Za-z_$][\w$]*/.source
  var HL_RE = {}
  function highlight(code, lang) {
    var key = LANG_ALIAS[lang] || lang, def = HL[key]
    if (!def) return esc(code)
    var re = HL_RE[key]
    if (!re) {
      re = HL_RE[key] = new RegExp(['(' + (def.comment || NEVER) + ')', '(' + def.str + ')', '(' + NUM + ')',
        '(' + (def.vars || NEVER) + ')', '(' + (def.words || WORD) + ')'].join('|'), 'gm')
    }
    var out = '', last = 0, m
    re.lastIndex = 0
    while ((m = re.exec(code)) !== null) {
      if (!m[0]) { re.lastIndex++; continue }
      out += esc(code.slice(last, m.index))
      last = re.lastIndex
      var cls = m[1] ? 'c' : m[2] ? 's' : m[3] ? 'n' : m[4] ? 'v' : null
      if (!cls) {
        var probe = def.ci ? m[5].toLowerCase() : m[5]
        if (def.kw[probe]) cls = 'k'
        else if (key === 'ps' && /^[A-Z][a-z]+-[A-Z]\w*$/.test(m[5])) cls = 'f'      // a cmdlet: Verb-Noun
        else if (/^\s*\(/.test(code.slice(last, last + 40))) cls = 'f'
      }
      out += cls ? '<span class="t' + cls + '">' + esc(m[0]) + '</span>' : esc(m[0])
    }
    return out + esc(code.slice(last))
  }
  function codeBlock(code, lang) {
    var key = String(lang || '').toLowerCase(), name = LANG_NAME[key] || (key ? key.charAt(0).toUpperCase() + key.slice(1) : t('codeWord'))
    var shell = /^(ps|ps1|powershell|pwsh|sh|bash|shell|cmd|bat|console)$/.test(key) && code.trim() && code.trim().indexOf('\n') < 0
    return '<div class="codeblock"><div class="cb-head"><span class="cb-lang">' + icon('code', 's14') + esc(name) + '</span><span class="grow"></span>' +
      (shell ? '<button class="cb-btn" data-term title="' + t('toTerminal') + '">' + icon('terminal', 's14') + '<span>' + t('toTerminal') + '</span></button>' : '') +
      '<button class="cb-btn" data-wrap title="' + t('wrapLines') + '">' + icon('wrap', 's14') + '</button>' +
      '<button class="cb-btn" data-copy title="' + t('copy') + '">' + icon('copy', 's14') + '</button></div>' +
      '<pre><code>' + highlight(code, key) + '</code></pre></div>'
  }
  var CALLOUT_ICON = { NOTE: 'info', TIP: 'bulb', IMPORTANT: 'info', WARNING: 'alert', CAUTION: 'alert' }

  // ── markdown (own: headings, lists, quotes, tables, code, inline marks, links) ──
  function esc(s) { return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;') }
  var PATH_RE = /^[A-Za-z]:[\\/][^<>"|?*\n]*$/
  function inline(s) {
    var codes = [], paths = []
    s = esc(s).replace(/`([^`]+)`/g, function (_, c) { codes.push(c); return '\u0000' + (codes.length - 1) + '\u0000' })
    // bare paths are kept out of the bold/italic rules (__init__.py) and come back as links
    s = s.replace(/(^|[\s(«])([A-Za-z]:\\[^\s<>"'|?*()«»]*[^\s<>"'|?*()«».,;:!])/g, function (_, pre, path) {
      paths.push(path)
      return pre + '\u0001' + (paths.length - 1) + '\u0001'
    })
    s = s.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noreferrer">$1</a>')
      .replace(/(^|[\s(])(https?:\/\/[^\s<)]+)/g, '$1<a href="$2" target="_blank" rel="noreferrer">$2</a>')
      .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>').replace(/__([^_]+)__/g, '<strong>$1</strong>')
      .replace(/(^|[^*\w])\*([^*\s][^*]*)\*/g, '$1<em>$2</em>').replace(/~~([^~]+)~~/g, '<del>$1</del>')
    s = s.replace(/\u0001(\d+)\u0001/g, function (_, i) { return '<a class="path" data-path="' + paths[+i] + '">' + paths[+i] + '</a>' })
    return s.replace(/\u0000(\d+)\u0000/g, function (_, i) {
      var c = codes[+i]
      return PATH_RE.test(c) ? '<code class="path" data-path="' + c + '">' + c + '</code>' : '<code>' + c + '</code>'
    })
  }
  function md(src) {
    var lines = String(src || '').replace(/\r\n/g, '\n').split('\n'), out = [], i = 0
    while (i < lines.length) {
      var line = lines[i]
      var fence = line.match(/^\s*(```|~~~)\s*([\w+#.-]*)/)
      if (fence) {
        var code = []
        i++
        while (i < lines.length && !lines[i].trim().startsWith(fence[1])) code.push(lines[i++])
        i++
        out.push(codeBlock(code.join('\n'), fence[2]))
        continue
      }
      var h = line.match(/^(#{1,6})\s+(.*)$/)
      if (h) { var n = Math.min(h[1].length, 4); out.push('<h' + n + '>' + inline(h[2]) + '</h' + n + '>'); i++; continue }
      if (/^\s*([-*_])(\s*\1){2,}\s*$/.test(line)) { out.push('<hr>'); i++; continue }
      if (/^\s*>/.test(line)) {
        var q = []
        while (i < lines.length && /^\s*>/.test(lines[i])) q.push(lines[i++].replace(/^\s*>\s?/, ''))
        var alert = q.length && q[0].match(/^\[!(NOTE|TIP|IMPORTANT|WARNING|CAUTION)\]\s*(.*)$/i)
        if (alert) {
          var kind = alert[1].toUpperCase()
          out.push('<div class="callout co-' + kind.toLowerCase() + '"><div class="callout-t">' + icon(CALLOUT_ICON[kind], 's14') +
            (alert[2] ? inline(alert[2]) : t('callout' + kind)) + '</div>' + md(q.slice(1).join('\n')) + '</div>')
        } else out.push('<blockquote>' + md(q.join('\n')) + '</blockquote>')
        continue
      }
      if (/^\s*\|.*\|\s*$/.test(line) && i + 1 < lines.length && /^\s*\|?\s*:?-{2,}/.test(lines[i + 1])) {
        var split = function (l) { return l.trim().replace(/^\||\|$/g, '').split('|') }
        var cells = function (l) { return split(l).map(function (c) { return inline(c.trim()) }) }
        var align = split(lines[i + 1]).map(function (c) {
          c = c.trim()
          return /^:-+:$/.test(c) ? 'center' : /-+:$/.test(c) ? 'right' : ''
        })
        var at = function (k) { return align[k] ? ' style="text-align:' + align[k] + '"' : '' }
        var head = cells(line), rows = []
        i += 2
        while (i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i])) rows.push(cells(lines[i++]))
        out.push('<div class="table-wrap"><table><thead><tr>' + head.map(function (c, k) { return '<th' + at(k) + '>' + c + '</th>' }).join('') + '</tr></thead><tbody>' +
          rows.map(function (r) { return '<tr>' + r.map(function (c, k) { return '<td' + at(k) + '>' + c + '</td>' }).join('') + '</tr>' }).join('') + '</tbody></table></div>')
        continue
      }
      if (/^(\s*)([-*+]|\d+[.)])\s+/.test(line)) {
        var ordered = /^\s*\d/.test(line), items = []
        while (i < lines.length) {
          var m = lines[i].match(/^(\s*)([-*+]|\d+[.)])\s+(.*)$/)
          if (m) { items.push([m[1].length, m[3]]); i++; continue }
          if (lines[i].trim() && /^\s{2,}/.test(lines[i]) && items.length) { items[items.length - 1][1] += '\n' + lines[i].trim(); i++; continue }
          break
        }
        var first = ordered && line.match(/^\s*(\d+)/)
        out.push(first && +first[1] > 1 ? list(items, ordered).replace(/^<ol>/, '<ol start="' + first[1] + '">') : list(items, ordered))
        continue
      }
      if (!line.trim()) { i++; continue }
      var buf = []
      while (i < lines.length && lines[i].trim() && !/^(\s*(```|~~~)|#{1,6}\s|\s*>|\s*([-*+]|\d+[.)])\s)/.test(lines[i])) buf.push(lines[i++])
      if (!buf.length) buf.push(lines[i++])
      out.push('<p>' + buf.map(inline).join('<br>') + '</p>')
    }
    return out.join('')
  }
  function list(items, ordered) {
    var depth = items.length ? items[0][0] : 0, tag = ordered ? 'ol' : 'ul', html = '<' + tag + '>'
    for (var k = 0; k < items.length; k++) {
      var start = k, sub = []
      while (k + 1 < items.length && items[k + 1][0] > depth) sub.push(items[++k])
      var body = items[start][1], task = body.match(/^\[( |x|X)\]\s+([\s\S]*)$/)
      if (task) body = task[2]
      html += '<li' + (task ? ' class="task' + (task[1] !== ' ' ? ' done' : '') + '"' : '') + '>' +
        (task ? '<span class="box">' + (task[1] !== ' ' ? icon('check', 's12') : '') + '</span>' : '') +
        inline(body).replace(/\n/g, '<br>') + (sub.length ? list(sub, ordered) : '') + '</li>'
    }
    return html + '</' + tag + '>'
  }
  document.addEventListener('click', function (e) {
    var b = e.target.closest && e.target.closest('[data-copy]')
    if (b) copy((b.closest('.codeblock') || b.parentNode).querySelector('pre').textContent, b)
    var w = e.target.closest && e.target.closest('[data-wrap]')
    if (w) { var cb = w.closest('.codeblock'); cb.classList.toggle('wrap'); w.classList.toggle('on', cb.classList.contains('wrap')) }
    var tm = e.target.closest && e.target.closest('[data-term]')
    if (tm) openTerminal(tm.closest('.codeblock').querySelector('pre').textContent.trim())
    var p = e.target.closest && e.target.closest('[data-path]')
    if (p) { e.preventDefault(); openPath(p.dataset.path) }
  })

  // ── tool steps ──
  var ARG_KEYS = ['command', 'pattern', 'query', 'url', 'path', 'file_path', 'prompt', 'task', 'name', 'note', 'id', 'code']
  function parseArgs(args) { try { return typeof args === 'string' ? JSON.parse(args || '{}') : args || {} } catch (e) { return { _raw: String(args) } } }
  var EDIT_TOOLS = { edit_file: 1, write_file: 1, patch: 1, apply_diff: 1 }
  // A failed step, told apart the way maxedcode does it (config._STEP_ERROR), plus PowerShell's "exited with code N"
  var STEP_ERROR = /^\s*(Error|Tool error|Not executed|Interrupted|The user rejected|Ошибка|blocked|refused)|exit(?:ed with)? code [1-9]|Traceback \(most recent/i
  var ANSI_RE = /\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[()][A-Z0-9]/g
  var RESULT_LINES = 60          // a longer result shows its head and a "show all" button
  function argSummary(name, args) {
    var a = parseArgs(args)
    if (name === 'todo_write' && Array.isArray(a.todos)) {
      var done = a.todos.filter(function (x) { return x && x.status === 'done' }).length
      var cur = a.todos.filter(function (x) { return x && x.status === 'in_progress' })[0]
      return done + '/' + a.todos.length + (cur ? ' · ' + String(cur.content || '') : '')
    }
    for (var k = 0; k < ARG_KEYS.length; k++) {
      var v = a[ARG_KEYS[k]]
      if (typeof v === 'string' && v.trim()) return v.replace(/\s+/g, ' ').slice(0, 200)
    }
    if (a._raw) return a._raw.slice(0, 200)
    var s = JSON.stringify(a)
    return s === '{}' ? '' : s.slice(0, 200)
  }
  // A line diff of one edit (the call's arguments; maxedcode applied exactly this): common head and tail cut,
  // the middle by LCS when it is small enough.
  // a file's lines: the newline that ends the last line does not start one more
  function textLines(text) {
    var s = String(text || '').replace(/\r\n/g, '\n')
    return s ? s.replace(/\n$/, '').split('\n') : []
  }
  function diffLines(oldText, newText) {
    var a = textLines(oldText), b = textLines(newText)
    var p = 0, s = 0, out = []
    while (p < a.length && p < b.length && a[p] === b[p]) p++
    while (s < a.length - p && s < b.length - p && a[a.length - 1 - s] === b[b.length - 1 - s]) s++
    var A = a.slice(p, a.length - s), B = b.slice(p, b.length - s), n = A.length, m = B.length, i, j
    a.slice(Math.max(0, p - 2), p).forEach(function (l) { out.push([' ', l]) })
    if (n * m <= 160000) {
      var L = []
      for (i = 0; i <= n; i++) L.push(new Uint16Array(m + 1))
      for (i = n - 1; i >= 0; i--) for (j = m - 1; j >= 0; j--) L[i][j] = A[i] === B[j] ? L[i + 1][j + 1] + 1 : Math.max(L[i + 1][j], L[i][j + 1])
      i = 0
      j = 0
      while (i < n && j < m) {
        if (A[i] === B[j]) { out.push([' ', A[i]]); i++; j++ }
        else if (L[i + 1][j] >= L[i][j + 1]) out.push(['-', A[i++]])
        else out.push(['+', B[j++]])
      }
      while (i < n) out.push(['-', A[i++]])
      while (j < m) out.push(['+', B[j++]])
    } else {
      A.forEach(function (l) { out.push(['-', l]) })
      B.forEach(function (l) { out.push(['+', l]) })
    }
    b.slice(b.length - s, b.length - s + 2).forEach(function (l) { out.push([' ', l]) })
    return out
  }
  function editLines(name, a) {
    if (name === 'edit_file') return diffLines(a.old_text, a.new_text)
    if (name === 'write_file') return textLines(a.content).map(function (l) { return ['+', l] })
    if (name === 'patch') {
      var out = []
      ;(Array.isArray(a.blocks) ? a.blocks : []).forEach(function (blk, k) {
        if (k) out.push(['@', '…'])
        out = out.concat(diffLines(blk && blk.old_text, blk && blk.new_text))
      })
      return out
    }
    if (name === 'apply_diff') {
      return String(a.diff || '').replace(/\r\n/g, '\n').split('\n').filter(function (l) { return !/^(---|\+\+\+) /.test(l) }).map(function (l) {
        return /^@@/.test(l) ? ['@', l] : /^[+-]/.test(l) ? [l[0], l.slice(1)] : [' ', l.replace(/^ /, '')]
      })
    }
    return []
  }
  function diffEl(lines) {
    var pre = el('pre', 'mono diff')
    lines.slice(0, 400).forEach(function (l) {
      pre.appendChild(el('span', l[0] === '+' ? 'diff-add' : l[0] === '-' ? 'diff-del' : l[0] === '@' ? 'diff-hunk' : '',
        (l[0] === '@' ? '' : l[0] + ' ') + l[1] + '\n'))
    })
    if (lines.length > 400) pre.appendChild(el('span', 'diff-hunk', '… ' + (lines.length - 400) + ' ' + t('moreLines') + '\n'))
    return pre
  }
  function todoEl(todos) {
    var ul = el('ul', 'todo')
    todos.forEach(function (x) {
      var st = x && x.status
      var li = el('li', st === 'done' ? 'done' : st === 'in_progress' ? 'now' : '')
      li.appendChild(el('span', 'mark', st === 'done' ? '✓' : st === 'in_progress' ? '▶' : '○'))
      li.appendChild(el('span', '', String((x && x.content) || '')))
      ul.appendChild(li)
    })
    return ul
  }
  function prettyArgs(args) {
    var a = parseArgs(args)
    if (a && typeof a.command === 'string' && Object.keys(a).length <= 3) return a.command
    if (a && typeof a.code === 'string') return a.code
    return a._raw || JSON.stringify(a, null, 2)
  }
  function colorDiff(pre, text) {
    if (!/^@@/m.test(text)) { pre.textContent = text; return }
    text.split('\n').forEach(function (line) {
      pre.appendChild(el('span', /^@@/.test(line) ? 'diff-hunk' : /^\+(?!\+\+)/.test(line) ? 'diff-add' : /^-(?!--)/.test(line) ? 'diff-del' : '', line + '\n'))
    })
  }
  function toolLabel(name) {
    var row = TOOL_LABELS[name]
    if (row) return { label: tl(row), icon: row[2] }
    if (/^mcp__/.test(name)) return { label: name.replace(/^mcp__/, '').replace(/__/g, ' · '), icon: 'globe' }
    return { label: name, icon: 'terminal' }
  }
  function stepEl(call) {
    var info = toolLabel(call.name), a = parseArgs(call.args)
    var d = el('div', 'step')
    d._name = call.name
    var head = el('button', 'step-head')
    head.innerHTML = '<span class="ico">' + icon(info.icon) + '</span>'
    head.appendChild(el('span', 'tname', info.label))
    var lines = EDIT_TOOLS[call.name] && typeof a.path === 'string' ? editLines(call.name, a) : null
    var body = el('div', 'step-body')
    if (lines) {
      var add = 0, del = 0
      lines.forEach(function (l) { if (l[0] === '+') add++; else if (l[0] === '-') del++ })
      head.appendChild(el('span', 'targ', base(a.path)))
      var stats = el('span', 'stats')
      stats.appendChild(el('span', 'add', '+' + add))
      stats.appendChild(el('span', 'del', '−' + del))
      head.appendChild(stats)
      body.appendChild(el('div', 'label', t('changes') + ' · ' + a.path))
      body.appendChild(diffEl(lines))
      d._edit = { path: a.path, add: add, del: del }
    } else {
      head.appendChild(el('span', 'targ', argSummary(call.name, call.args)))
      if (call.name === 'todo_write' && Array.isArray(a.todos)) body.appendChild(todoEl(a.todos))
      else {
        body.appendChild(el('div', 'label', t('args') + ' · ' + call.name))
        body.appendChild(el('pre', 'mono', prettyArgs(call.args)))
      }
    }
    var clock = el('span', 'dur')
    head.appendChild(clock)
    var st = el('span', 'st')
    st.appendChild(el('span', 'spin'))
    head.appendChild(st)
    head.onclick = function () { d.classList.toggle('open') }
    d.appendChild(head)
    d.appendChild(body)
    d._st = st
    d._clock = clock
    d._body = body
    d._call = { name: call.name, args: a }
    return d
  }
  // A step maxedcode did not run because nobody could confirm it (the window has no approval prompt): what to do.
  function notRunCard(d, text) {
    var call = d._call || {}, box = el('div', 'nr-card'), msg
    if (/MCP tools may have side effects/.test(text)) {
      msg = t('nrMcp')
      var allow = el('button', 'btn small', t('nrAllow'))
      allow.onclick = function () {
        api('/api/cli', { cwd: chatFolder(), do: 'rule-add', kind: 'allow', rule: call.name }).then(function (r) {
          if (r.error) return toast(r.error)
          cliData = r
          allow.remove()
          toast(t('nrAllowed'))
        })
      }
      box.appendChild(allow)
    } else if (/permission rule requires approval/.test(text)) {
      msg = t('nrAsk')
      var rules = el('button', 'pick small', t('nrRules'))
      rules.onclick = function () { openSettings(); document.querySelector('.tabs button[data-tab="cli"]').click() }
      box.appendChild(rules)
    } else if ((call.name === 'powershell' || call.name === 'bash') && call.args && call.args.command) {
      msg = t('nrDanger')
      var term = el('button', 'pick small', t('nrTerminal'))
      term.onclick = function () { openTerminal(call.args.command) }
      box.appendChild(term)
    } else return
    box.insertBefore(el('div', 'muted small', msg), box.firstChild)
    d._body.appendChild(box)
    d.classList.add('open')
  }
  // A helper (subagent, browser helper) at work: its steps as a timeline under its card, each with what it did and
  // its reasoning (folded to a few lines, a click shows all). The same timeline can open in the side panel, where
  // it has room; both stay live. d._log.data is the model, every view is drawn from it.
  function helperLog(d) {
    if (d._log) return d._log
    var lg = d._log = { data: [], views: [], follow: true }
    var box = el('div', 'helper-log'), head = el('div', 'hl-head')
    var title = el('button', 'hl-title')
    title.innerHTML = '<span class="chev">' + icon('chevron', 's14') + '</span>' + icon('brain', 's14')
    title.appendChild(el('span', '', t('helperSteps')))
    lg.count = title.appendChild(el('span', 'hl-count'))
    title.onclick = function (e) { e.stopPropagation(); box.classList.toggle('open') }
    head.appendChild(title)
    head.appendChild(iconBtn('panelRight', t('openInPanel'), function (e) { e.stopPropagation(); helperPanel(d) }))
    box.appendChild(head)
    var list = el('div', 'hl-list')
    box.appendChild(list)
    if (d.closest && !d._done && pref('helperOpen', true)) box.classList.add('open')
    d.insertBefore(box, d._body)
    lg.views.push(list)
    return lg
  }
  function helperEntry(lg, step) {
    var n = +step || (lg.data.length ? lg.data[lg.data.length - 1].step : 1)
    var e = lg.data.filter(function (x) { return x.step === n })[0]
    if (!e) { e = { step: n, acts: [], think: '', open: false }; lg.data.push(e); lg.data.sort(function (a, b) { return a.step - b.step }) }
    return e
  }
  function drawHelper(d) {
    var lg = d._log
    lg.count.textContent = lg.data.length ? t('stepWord') + ' ' + lg.data[lg.data.length - 1].step : ''
    lg.views.forEach(function (list) {
      var stick = list.scrollHeight - list.scrollTop - list.clientHeight < 24
      list.innerHTML = ''
      list._seen = list._seen || {}
      lg.data.forEach(function (e, k) {
        var last = k === lg.data.length - 1 && !d._done
        var item = el('div', 'hl-step' + (last ? ' live' : '') + (list._seen[e.step] ? '' : ' fresh'))
        list._seen[e.step] = 1
        var top = el('div', 'hl-top')
        top.appendChild(el('span', 'hl-n', String(e.step)))
        var acts = el('div', 'hl-acts')
        ;(e.acts.length ? e.acts : [last ? t('thinking') + '…' : '']).forEach(function (a) { if (a) acts.appendChild(el('div', 'hl-act', a)) })
        top.appendChild(acts)
        item.appendChild(top)
        if (e.think) {
          var th = el('div', 'hl-think' + (e.open || last ? ' full' : ''))
          th.textContent = e.think
          th.title = e.open ? '' : t('clickToExpand')
          th.onclick = function (ev) { ev.stopPropagation(); e.open = !e.open; drawHelper(d) }
          item.appendChild(th)
        }
        list.appendChild(item)
      })
      if (stick) list.scrollTop = list.scrollHeight
    })
  }
  function helperThink(d, step, text) {
    var lg = helperLog(d)
    helperEntry(lg, step).think = String(text || '').trim()
    drawHelper(d)
  }
  // "step 3 · browser click → thinking": the action of that step (the number is shown as the step's badge)
  function helperAct(d, text) {
    var m = String(text || '').match(/^step (\d+)\s*[·:-]?\s*([\s\S]*)$/), lg = helperLog(d)
    var e = helperEntry(lg, m ? m[1] : 0), a = (m ? m[2] : String(text || '')).replace(/\s*→\s*thinking$/, '').trim()
    if (a && a !== 'thinking' && e.acts.indexOf(a) < 0) e.acts.push(a)
    drawHelper(d)
  }
  function helperPanel(d) {
    var lg = helperLog(d), call = d._call || {}
    addTab({ id: 'helper:' + (d._id || (d._id = 'h' + Date.now().toString(36))), icon: 'brain',
      label: (call.name === 'browser' ? t('browser') : t('subagentWord')) + ' · ' + clipText(argSummary(call.name, call.args) || '', 28),
      make: function () {
        var box = el('div', 'helper-panel')
        var task = (call.args && (call.args.task || call.args.prompt || call.args.goal)) || ''
        if (task) { box.appendChild(el('div', 'label', t('task'))); box.appendChild(el('div', 'hp-task', String(task))) }
        var list = el('div', 'hl-list')
        box.appendChild(list)
        lg.views.push(list)
        box.dispose = function () { lg.views = lg.views.filter(function (v) { return v !== list }) }
        setTimeout(function () { drawHelper(d) })
        return box
      } })
  }
  function finishStep(d, content, ms) {
    if (!d || d._done) return
    d._done = true
    if (!ms && d._t0) ms = Date.now() - d._t0
    d._clock.textContent = ms >= 50 ? dur(ms) : ''
    var text = String(content || '').replace(ANSI_RE, '')
    var bad = STEP_ERROR.test(text)
    d._bad = bad
    d.classList.toggle('failed', bad)
    d._st.className = 'st ' + (bad ? 'err' : 'ok')
    d._st.innerHTML = icon(bad ? 'x' : 'check', 's14')
    var code = text.match(/exit(?:ed with)? code (-?\d+)/i)
    if (code && code[1] !== '0') d._st.parentNode.insertBefore(el('span', 'exit', 'exit ' + code[1]), d._st)
    d._body.appendChild(el('div', 'label', t('result')))
    var lines = text.slice(0, 30000).split('\n')
    var pre = el('pre', 'mono')
    colorDiff(pre, lines.slice(0, RESULT_LINES).join('\n'))
    d._body.appendChild(pre)
    if (lines.length > RESULT_LINES) {
      var all = el('button', 'more-btn', t('showAll') + ' (' + lines.length + ' ' + t('linesWord') + ')')
      all.onclick = function () { pre.innerHTML = ''; colorDiff(pre, lines.join('\n')); all.remove() }
      d._body.appendChild(all)
    }
    if (/^\s*Error: NOT run/.test(text)) notRunCard(d, text)
    if (d._log) drawHelper(d)
  }

  // ── an assistant answer: the work (reasoning + steps, collapsible) and the answer text ──
  function Answer(parent, live) {
    this.el = el('div', 'answer' + (live ? ' running open' : '') + (pref('details', 'full') === 'terse' ? ' terse' : '') +
      (pref('replies', 'all') === 'all' ? ' all-replies' : ''))
    this.head = el('button', 'work-head')
    this.line = el('div', 'work-line')
    this.work = el('div', 'work')
    this.textEl = el('div', 'text')
    this.el.appendChild(this.head)
    this.el.appendChild(this.line)
    this.el.appendChild(this.work)
    this.el.appendChild(this.textEl)
    parent.appendChild(this.el)
    var self = this
    this.head.onclick = function () { self.el.classList.toggle('open') }
    this.steps = {}
    this.edits = []
    this.think = null
    this.text = ''
    this.mark = 0
    this.workCount = 0
    this.t0 = Date.now()
    this.live = live
    this.setHead()
    if (live) this.timer = setInterval(function () { self.setHead(); self.tickSteps() }, 1000)
  }
  // running steps show how long they have been at it (a browser helper may work for minutes)
  Answer.prototype.tickSteps = function () {
    var steps = this.steps
    Object.keys(steps).forEach(function (k) {
      var d = steps[k]
      if (d && !d._done && d._t0 && Date.now() - d._t0 >= 1000) d._clock.textContent = secs(Date.now() - d._t0)
    })
  }
  Answer.prototype.setHead = function (final) {
    var label
    if (this.live && !final) label = '<span class="spin"></span><span>' + t('working') + ' ' + secs(Date.now() - this.t0) + '</span>'
    else {
      if (this.live) this.ms = Date.now() - this.t0
      if (final === 'stopped') label = '<span>' + t('stopped') + (this.ms ? ' · ' + secs(this.ms) : '') + '</span>'
      else label = '<span>' + (this.ms ? t('doneIn') + secs(this.ms) : t('done')) + '</span>'
    }
    this.head.innerHTML = label + '<span class="chev">' + icon('chevron', 's14') + '</span>'
  }
  Answer.prototype.addThink = function (text) {
    if (!text) return
    if (!this.think) {
      var item = el('div', 'think-item')
      var head = el('button', 'step-head')
      head.innerHTML = '<span class="ico">' + icon('brain') + '</span>'
      head.appendChild(el('span', 'tname', t('reasoning')))
      var body = el('div', 'think')
      head.onclick = function () { body.hidden = !body.hidden }
      item.appendChild(head)
      item.appendChild(body)
      this.work.appendChild(item)
      this.think = { el: body, text: '' }
      this.workCount++
    }
    var th = this.think
    th.text += text
    keepEnd(th.el, function () { th.el.textContent = th.text })
    scrollDown()
  }
  Answer.prototype.addText = function (text) {
    if (!text) return
    this.text += text
    var self = this
    if (this._queued) return
    this._queued = true
    requestAnimationFrame(function () { self._queued = false; self.textEl.innerHTML = md(self.text); scrollDown() })
  }
  Answer.prototype.boundary = function () {
    if (this.think) this.think = null
    this.mark = this.work.children.length
    this.textMark = this.text.length
  }
  Answer.prototype.reset = function () {
    while (this.work.children.length > this.mark) this.work.removeChild(this.work.lastChild)
    this.think = null
    this.text = this.text.slice(0, this.textMark || 0)
    this.textEl.innerHTML = md(this.text)
  }
  Answer.prototype.calls = function (calls) {
    var self = this
    // text written before tool calls is part of the work (it was a step, not the answer)
    var said = this.text.slice(this.textMark || 0).trim()
    if (said) {
      this.text = this.text.slice(0, this.textMark || 0)
      this.textEl.innerHTML = md(this.text)
      var note = el('div', 'text reply')
      note.innerHTML = md(said)
      this.work.appendChild(note)
    }
    this.think = null
    calls.forEach(function (c) {
      var d = stepEl({ name: (c.function || {}).name || c.name, args: (c.function || {}).arguments || c.args })
      if (self.live) { d._t0 = Date.now(); fresh(d) }
      self.steps[c.id] = d
      if (d._edit) self.edits.push(d)
      self.work.appendChild(d)
      self.workCount++
    })
    this.boundary()
    scrollDown()
  }
  Answer.prototype.toolResult = function (id, content, ms) { finishStep(this.steps[id], content, ms) }
  // The running browser helper or subagent card (headless `child` events belong to it)
  Answer.prototype.helperStep = function () {
    var steps = this.steps || {}, ids = Object.keys(steps)
    for (var i = ids.length - 1; i >= 0; i--) {
      var s = steps[ids[i]]
      if (s && !s._done && (s._name === 'browser' || s._name === 'subagent')) return s
    }
    return null
  }
  Answer.prototype.child = function (ev) {
    if (ev.think) { var h = this.helperStep(); if (h) helperThink(h, ev.step, ev.think); return }
    this.childLine(ev.text)
  }
  // A browser helper or subagent at work: its latest step under its card
  Answer.prototype.childLine = function (text) {
    var d = this.helperStep()
    if (!d) return
    if (!d._child) { d._child = el('div', 'step-child'); d.insertBefore(d._child, d.children[1] || null) }
    var line = String(text || '')
    if (lang === 'ru') line = line.replace(/^step (\d+)/, 'шаг $1').replace(/→ thinking$/, '→ думает').replace(/^step (\d+) · thinking$/, 'шаг $1 · думает')
    d._child.textContent = '↳ ' + line
    helperAct(d, text)
  }
  // Manual / Accept edits: the turn waits for the answer (headless polls for the server's file)
  Answer.prototype.approvalCard = function (ev, chat) {
    var a = ev.input || {}, box = el('div', 'ask-card approval')
    this.approvals = this.approvals || {}
    this.approvals[ev.id] = box
    var act = EDIT_TOOLS[ev.tool] ? 'apEdit' : (ev.tool === 'powershell' || ev.tool === 'bash') ? 'apCommand' : ev.tool === 'py_run' ? 'apPython'
      : ev.tool === 'browser' ? 'apBrowser' : ev.tool === 'cloud_code' ? 'apCloud' : /^mcp__/.test(ev.tool) ? 'apMcp' : 'apTool'
    box.appendChild(el('div', 'label', t('apHead') + ' ' + t(act) + (act === 'apTool' ? ' ' + ev.tool : '')))
    var lines = EDIT_TOOLS[ev.tool] ? editLines(ev.tool, a) : null
    if (lines && lines.length) box.appendChild(diffEl(lines))
    else box.appendChild(el('pre', 'mono ap-body', String(a.command || a.code || a.path || a.url || a.task || JSON.stringify(a, null, 1)).slice(0, 4000)))
    if (EDIT_TOOLS[ev.tool] && a.path) box.insertBefore(el('div', 'muted small', a.path), box.children[1])
    var row = el('div', 'ask-opts')
    box.cloud = ev.tool === 'cloud_code'      // "always" there turns asking off for all cloud tasks, not one call
    ;[['allow', 'apYes', 'btn small'], ['always', box.cloud ? 'apCloudAlways' : 'apAlways', 'btn small outline'], ['deny', 'apNo', 'btn small danger']].forEach(function (x) {
      var b = el('button', x[2], t(x[1]))
      b.onclick = function () {
        Array.prototype.forEach.call(row.children, function (c) { c.disabled = true })
        api('/api/approve', { id: ev.id, decision: x[0] })
      }
      row.appendChild(b)
    })
    box.appendChild(row)
    box.appendChild(el('div', 'muted small ap-state', t('apWaiting')))
    this.el.appendChild(box)
    if (visible(chat)) scrollDown()
  }
  Answer.prototype.approvalDone = function (id, decision) {
    var box = this.approvals && this.approvals[id]
    if (!box) return
    box.classList.add('done', decision === 'deny' ? 'denied' : 'allowed')
    var row = box.querySelector('.ask-opts')
    if (row) row.remove()
    box.querySelector('.ap-state').textContent = t(decision === 'deny' ? 'apDenied' : decision !== 'always' ? 'apAllowed'
      : box.cloud ? 'apCloudAlwaysDone' : 'apAlwaysDone')
  }
  // A run-level note of maxedcode (headless `notice`): the reply hit the length limit and goes on, and the like.
  Answer.prototype.note = function (kind, text) {
    this.work.appendChild(el('div', 'notice', TEXT['notice_' + kind] ? t('notice_' + kind) : text))
    this.workCount++
    this.boundary()
    scrollDown()
  }
  // ask_user ends maxedcode's run (headless): the question is the answer. Its options become buttons that send
  // the chosen one as the next message; a typed answer works too.
  Answer.prototype.askCard = function (a, chat) {
    var opts = (Array.isArray(a && a.options) ? a.options : []).map(String).filter(Boolean)
    if (!opts.length) return
    // the answer text ends with the same options as a list (headless writes them): the buttons replace it
    var list = '\n' + opts.map(function (o) { return '- ' + o }).join('\n')
    if (this.text.trim().slice(-list.trim().length) === list.trim()) {
      this.text = this.text.trim().slice(0, -list.trim().length).trim()
      this.textEl.innerHTML = md(this.text)
    }
    var box = el('div', 'ask-card')
    box.appendChild(el('div', 'label', t('askHead')))
    var row = el('div', 'ask-opts')
    opts.forEach(function (o) {
      var b = el('button', 'ask-opt')
      b.appendChild(el('span', 'radio'))
      b.appendChild(el('span', '', o))
      b.onclick = function () {
        b.classList.add('on')
        Array.prototype.forEach.call(row.children, function (x) { x.disabled = true })
        box.appendChild(el('div', 'muted small', t('askPicked') + ': ' + o))
        reply(chat, o)
      }
      row.appendChild(b)
    })
    box.appendChild(row)
    box.appendChild(el('div', 'muted small', t('askOwn')))
    this.el.appendChild(box)
  }
  // The files this turn's edit steps changed, one row each (+added −removed lines), a click opens the file.
  Answer.prototype.changedFiles = function () {
    var byPath = {}, order = []
    this.edits.forEach(function (d) {
      if (!d._done || d._bad) return
      var k = d._edit.path.toLowerCase()
      if (!byPath[k]) { byPath[k] = { path: d._edit.path, add: 0, del: 0 }; order.push(k) }
      byPath[k].add += d._edit.add
      byPath[k].del += d._edit.del
    })
    if (!order.length) return
    var box = el('div', 'changed')
    box.appendChild(el('div', 'label', t('changedFiles')))
    order.forEach(function (k) {
      var f = byPath[k], r = el('button', 'changed-row')
      r.innerHTML = icon('file', 's14')
      r.appendChild(el('span', 'n', base(f.path)))
      r.appendChild(el('span', 'p', f.path))
      r.appendChild(el('span', 'add', '+' + f.add))
      r.appendChild(el('span', 'del', '−' + f.del))
      r.onclick = function () { openPreview(f.path) }
      box.appendChild(r)
    })
    this.el.appendChild(box)
  }
  Answer.prototype.end = function (opts) {
    opts = opts || {}
    clearInterval(this.timer)
    var self = this
    Object.keys(this.steps).forEach(function (k) { var d = self.steps[k]; if (!d._done) { d._st.className = 'st'; d._st.textContent = '·' } })
    this.el.classList.remove('running')
    this.setHead(opts.stopped ? 'stopped' : true)
    if (!this.workCount && !opts.stopped) { this.head.hidden = true; this.line.hidden = true }
    if (pref('fold', 'done') === 'done') this.el.classList.remove('open')
    this.textEl.innerHTML = md(this.text)
    this.changedFiles()
    if (this.text.trim() || opts.usage) {
      var row = el('div', 'actions')
      row.appendChild(iconBtn('copy', t('copy'), function (e) { copy(self.text.trim(), e.currentTarget) }))
      if (opts.fork) row.appendChild(iconBtn('branch', t('fork'), opts.fork))
      var u = opts.usage
      if (u && u.prompt_tokens) {
        var use = el('span', 'use')
        use.innerHTML = icon('database', 's14')
        use.appendChild(document.createTextNode(t('usage') + ' ' + kfmt(u.prompt_tokens + (u.completion_tokens || 0)) + ' ' + t('tok')))
        row.appendChild(use)
      }
      if (opts.time) row.appendChild(el('span', '', opts.time))
      this.el.appendChild(row)
    }
  }
  // Selected text the user asks about leads the message as "> " lines (one block per selection); it is drawn as
  // a card above the bubble, the way it looked in the input.
  function splitQuotes(text) {
    var lines = text.split('\n'), quotes = [], cur = null, i = 0
    for (; i < lines.length; i++) {
      var l = lines[i]
      if (/^> ?/.test(l)) { if (!cur) { cur = []; quotes.push(cur) } cur.push(l.replace(/^> ?/, '')) }
      else if (!l.trim() && cur) cur = null
      else break
    }
    return { quotes: quotes.map(function (q) { return q.join('\n') }), rest: quotes.length ? lines.slice(i).join('\n') : text }
  }
  function quoteCard(text, onRemove) {
    var c = el('div', 'quote-card')
    c.appendChild(el('div', 'qtext', text))
    var lab = el('div', 'qlabel')
    lab.innerHTML = icon('quoteMark', 's14')
    lab.appendChild(el('span', '', t('selText')))
    c.appendChild(lab)
    if (onRemove) { var x = iconBtn('x', '', onRemove); x.className += ' qx'; c.appendChild(x) }
    return c
  }
  function userEl(parent, text, time) {
    var box = el('div', 'user'), parts = splitQuotes(String(text))
    parts.quotes.forEach(function (q) { box.appendChild(quoteCard(q)) })
    // an attachment note shows as its file name (the model gets the whole note)
    if (parts.rest.trim()) box.appendChild(el('div', 'bubble', parts.rest.replace(/\[File "([^"]+)" \([^)]*\): verbatim[^\]]*\]\n?/g, '📎 $1\n').trim()))
    var meta = el('div', 'meta')
    if (time) meta.appendChild(el('span', '', time))
    meta.appendChild(iconBtn('copy', t('copy'), function (e) { copy(text, e.currentTarget) }))
    box.appendChild(meta)
    parent.appendChild(box)
    return box
  }

  // ── sidebar ──
  function ago(ts) {
    var s = Date.now() / 1000 - ts
    if (s < 60) return t('justNow')
    if (s < 3600) return Math.floor(s / 60) + t('min')
    if (s < 86400) return Math.floor(s / 3600) + t('hourShort')
    return Math.floor(s / 86400) + t('dayShort')
  }
  function knownFolders() {
    var seen = {}, out = []
    function add(f) {
      if (!f) return
      var k = f.toLowerCase()
      if (seen[k] || state.hidden.indexOf(k) >= 0 && !same(f, state.folder)) return
      seen[k] = 1
      out.push(f)
    }
    if (state.workspace) add(state.workspace)          // chats without a project: always there to start one
    if (state.folder) add(groupOf(state.folder))
    state.folders.forEach(add)
    state.sessions.forEach(function (s) { if (!s.archived || ui.showArchived) add(groupOf(s.cwd)) })
    if (state.sort === 'name') out.sort(function (a, b) { return base(a).localeCompare(base(b)) })
    return out
  }
  var FOLDERS_SHOWN = 12, CHATS_SHOWN = 10
  var ui = { allFolders: false, open: {}, more: {}, filter: '', hits: [], showArchived: false }
  // Two folders with the same name ("src") get their parent folder after the name.
  function folderLabels(folders) {
    var count = {}, out = {}
    folders.forEach(function (f) { var b = base(f); count[b] = (count[b] || 0) + 1 })
    folders.forEach(function (f) {
      var b = base(f), parent = String(f).replace(/[\\/]+$/, '').split(/[\\/]/).slice(-2, -1)[0]
      out[f.toLowerCase()] = count[b] > 1 && parent ? b + ' · ' + parent : b
    })
    return out
  }
  function renderSide() {
    var box = $('chats')
    box.innerHTML = ''
    var folders = knownFolders(), q = ui.filter.toLowerCase(), labels = folderLabels(folders), listed = {}
    var shown = ui.allFolders || q ? folders : folders.slice(0, FOLDERS_SHOWN)
    shown.forEach(function (f) {
      var key = f.toLowerCase()
      var chats = state.sessions.filter(function (s) {
        return same(groupOf(s.cwd), f) && (!s.archived || ui.showArchived) && (!q || s.title.toLowerCase().indexOf(q) >= 0)
      })
      if (q && !chats.length) return
      var current = same(f, groupOf((state.chat && state.chat.cwd) || state.folder))
      var open = q ? true : ui.open[key] !== undefined ? ui.open[key] : current
      var row = el('div', 'folder-row' + (open ? ' open' : ''))
      row.innerHTML = '<span class="fi">' + icon(open ? 'folderOpen' : 'folder') + '</span>'
      var nameEl = row.appendChild(el('span', 'name', folderName(f, labels)))
      row.appendChild(iconBtn('dots', t('folderActions'), function (e) { e.stopPropagation(); projectMenu(e.currentTarget, f) }))
      row.appendChild(iconBtn('plus', t('newChat'), function (e) { e.stopPropagation(); setFolder(f); newChat() }))
      row.title = f
      row.onclick = function () { ui.open[key] = !open; ui.opening = open ? null : key; renderSide() }
      dropTarget(row, f, '')
      box.appendChild(row)
      if (ui.renaming === key) editName(row, nameEl, folderName(f, labels), function (v) { setAlias(f, v) })
      // only a folder opened by this click slides open: a redraw (a new chat, the list refreshed) does not blink
      var wrap = box.appendChild(el('div', 'folder-body' + (open ? ' open' : '') + (ui.opening === key ? ' opening' : '')))
      if (!open) return
      if (state.sort === 'name') chats.sort(function (a, b) { return a.title.localeCompare(b.title) })
      // the user's own folders of this project first, each with its chats; then the chats in no folder
      groupsOf(f).forEach(function (g) {
        var inG = chats.filter(function (s) { return chatGroups[s.id] === g.id })
        if (q && !inG.length) return
        var gOpen = q ? true : !!ui.open['g:' + g.id]
        var gr = el('div', 'group-row' + (gOpen ? ' open' : ''))
        gr.innerHTML = '<span class="fi">' + icon(gOpen ? 'folderOpen' : 'folder', 's14') + '</span>'
        var gName = gr.appendChild(el('span', 'name', g.name))
        gr.appendChild(el('span', 'count', inG.length ? String(inG.length) : ''))
        gr.appendChild(iconBtn('dots', t('folderActions'), function (e) { e.stopPropagation(); groupMenu(e.currentTarget, f, g) }))
        gr.onclick = function () { ui.open['g:' + g.id] = !gOpen; ui.opening = gOpen ? null : 'g:' + g.id; renderSide() }
        dropTarget(gr, f, g.id)
        wrap.appendChild(gr)
        if (ui.renaming === 'g:' + g.id) editName(gr, gName, g.name, function (v) { g.name = v; saveGroups() })
        if (!gOpen) return
        inG.forEach(function (s) {
          listed[s.id] = 1
          var cr = wrap.appendChild(chatRow(s, true))
          if (ui.opening === 'g:' + g.id) cr.classList.add('opening')
        })
      })
      var loose = chats.filter(function (s) { return !groupById(f, chatGroups[s.id]) })
      var limit = ui.more[key] || q ? loose.length : CHATS_SHOWN
      loose.slice(0, limit).forEach(function (s) { listed[s.id] = 1; wrap.appendChild(chatRow(s)) })
      if (loose.length > limit) {
        var more = el('div', 'chat more', (lang === 'ru' ? 'Ещё ' : 'More ') + (loose.length - limit))
        more.onclick = function () { ui.more[key] = true; renderSide() }
        wrap.appendChild(more)
      }
    })
    // the query found in the chats' text (the CLI's search index), chats not already listed by their title
    var hits = q ? ui.hits.filter(function (h) { return !listed[h.id] }) : []
    if (hits.length) {
      box.appendChild(el('div', 'side-sub', t('inText')))
      hits.forEach(function (h) {
        var s = state.sessions.filter(function (x) { return x.id === h.id })[0]
        if (!s) return
        box.appendChild(chatRow(s))
        var sn = el('div', 'snippet')
        sn.innerHTML = esc(h.snippet).replace(/\u2039/g, '<mark>').replace(/\u203a/g, '</mark>')
        sn.onclick = function () { openChat(s) }
        box.appendChild(sn)
      })
    }
    if (!q && folders.length > shown.length) {
      var all = el('div', 'folder-row muted', (lang === 'ru' ? 'Все папки ' : 'All folders ') + '(' + (folders.length - shown.length) + ')')
      all.onclick = function () { ui.allFolders = true; renderSide() }
      box.appendChild(all)
    }
    ui.opening = null
  }
  // ── projects: a display name of their own, the user's folders for chats (kept by the page, per project) ──
  var aliases = pref('folderNames', {}), projGroups = pref('chatFolders', {}), chatGroups = pref('chatFolderOf', {})
  function folderName(f, labels) { return aliases[f.toLowerCase()] || (labels && labels[f.toLowerCase()]) || base(f) }
  function setAlias(f, v) {
    var k = f.toLowerCase()
    if (v && v !== base(f)) aliases[k] = v; else delete aliases[k]
    setPref('folderNames', aliases)
    renderFolder()
  }
  function groupsOf(f) { return projGroups[String(f).toLowerCase()] || [] }
  function groupById(f, id) { return id ? groupsOf(f).filter(function (g) { return g.id === id })[0] : null }
  function saveGroups() { setPref('chatFolders', projGroups); setPref('chatFolderOf', chatGroups) }
  function addGroup(f, chatId) {
    var k = f.toLowerCase(), g = { id: 'g' + Date.now().toString(36), name: t('newGroup') }
    ;(projGroups[k] = projGroups[k] || []).push(g)
    if (chatId) chatGroups[chatId] = g.id
    saveGroups()
    ui.open[k] = true
    ui.open['g:' + g.id] = true
    ui.renaming = 'g:' + g.id
    renderSide()
  }
  function moveChat(id, gid) {
    if (gid) chatGroups[id] = gid; else delete chatGroups[id]
    saveGroups()
    renderSide()
  }
  // A chat dragged onto its project's row or onto one of the project's folders (the chat's folder on the disk
  // does not change, so it stays in its own project).
  function dropTarget(row, f, gid) {
    function ok(e) { return e.dataTransfer && Array.prototype.indexOf.call(e.dataTransfer.types, 'text/x-chat') >= 0 }
    row.ondragover = function (e) { if (ok(e)) { e.preventDefault(); row.classList.add('drop') } }
    row.ondragleave = function () { row.classList.remove('drop') }
    row.ondrop = function (e) {
      row.classList.remove('drop')
      if (!ok(e)) return                                 // files from Windows: the window's own drop handler
      var id = e.dataTransfer.getData('text/x-chat'), s = state.sessions.filter(function (x) { return x.id === id })[0]
      e.preventDefault()
      if (!s) return
      if (!same(groupOf(s.cwd), f)) return toast(t('moveSameProject'))
      moveChat(id, gid)
    }
  }
  function projectMenu(anchor, f) {
    var key = f.toLowerCase(), scratch = same(f, state.workspace)
    showMenu(anchor, [
      { label: t('newChat'), icon: 'plusCircle', run: function () { setFolder(f); newChat() } },
      { label: t('newGroupIn'), icon: 'folderPlus', run: function () { addGroup(f) } },
      { label: t('renameProject'), icon: 'pencil', run: function () { ui.renaming = key; renderSide() } },
      aliases[key] ? { label: t('resetName'), icon: 'refresh', run: function () { setAlias(f, ''); renderSide() } } : null,
      '-',
      { label: t('openFolder'), icon: 'folderColor', run: function () { api('/api/open', { path: f, app: 'explorer' }) } },
      { label: t('copyPath'), icon: 'copy', run: function () { copy(f); toast(t('copied')) } },
      scratch ? null : '-',
      scratch ? null : { label: t('hideFolder'), icon: 'x', run: function () {
        state.hidden.push(key)
        setPref('hiddenFolders', state.hidden)
        state.folders = state.folders.filter(function (x) { return !same(x, f) })
        setPref('folders', state.folders)
        renderSide()
      } },
    ].filter(Boolean), { align: 'right' })
  }
  function groupMenu(anchor, f, g) {
    showMenu(anchor, [
      { label: t('rename'), icon: 'pencil', run: function () { ui.renaming = 'g:' + g.id; renderSide() } },
      '-',
      { label: t('groupDelete'), icon: 'trash', danger: true, run: function () {
        var k = f.toLowerCase()
        projGroups[k] = groupsOf(f).filter(function (x) { return x !== g })
        if (!projGroups[k].length) delete projGroups[k]
        Object.keys(chatGroups).forEach(function (id) { if (chatGroups[id] === g.id) delete chatGroups[id] })
        saveGroups()
        renderSide()
      } },
    ], { align: 'right' })
  }
  function moveMenu(anchor, s) {
    var f = groupOf(s.cwd), cur = chatGroups[s.id] || ''
    var items = [{ head: t('moveTo') }, { label: t('noGroup'), icon: 'list', check: !groupById(f, cur), run: function () { moveChat(s.id, '') } }]
    groupsOf(f).forEach(function (g) { items.push({ label: g.name, icon: 'folder', check: cur === g.id, run: function () { moveChat(s.id, g.id) } }) })
    items.push('-', { label: t('newGroup'), icon: 'folderPlus', run: function () { addGroup(f, s.id) } })
    showMenu(anchor, items, { align: 'right' })
  }
  // An inline name editor in a sidebar row (project, folder): Enter or leaving it saves, Esc cancels.
  function editName(row, nameEl, value, save) {
    var input = el('input', 'rename')
    input.value = value
    row.replaceChild(input, nameEl)
    setTimeout(function () { input.focus(); input.select() })
    var done = false
    function finish(ok) {
      if (done) return
      done = true
      ui.renaming = null
      var v = input.value.trim()
      if (ok && v && v !== value) save(v)
      renderSide()
    }
    input.onkeydown = function (e) { if (e.key === 'Enter') finish(true); if (e.key === 'Escape') finish(false); e.stopPropagation() }
    input.onblur = function () { finish(true) }
    input.onclick = function (e) { e.stopPropagation() }
  }
  // A new project: an empty folder in ~\.maxedcode\projects named by the user, then a new chat in it.
  function newProject() {
    var row = el('div', 'folder-row open')
    row.innerHTML = '<span class="fi">' + icon('folderPlus') + '</span>'
    var input = row.appendChild(el('input', 'rename'))
    input.placeholder = t('projectName')
    $('chats').insertBefore(row, $('chats').firstChild)
    input.focus()
    var done = false
    function finish(ok) {
      if (done) return
      done = true
      var v = input.value.trim()
      if (!ok || !v) return renderSide()
      api('/api/new-project', { name: v }).then(function (x) {
        if (!x || x.error || !x.path) { toast(t('opFailed') + (x && x.error ? ': ' + x.error : '')); return renderSide() }
        setFolder(x.path)
        newChat()
        renderSide()
      })
    }
    input.onkeydown = function (e) { if (e.key === 'Enter') finish(true); if (e.key === 'Escape') finish(false); e.stopPropagation() }
    input.onblur = function () { finish(true) }
  }
  function addProjectMenu(anchor) {
    showMenu(anchor, [
      { label: t('newProject'), sub: t('newProjectSub'), icon: 'folderPlus', run: newProject },
      { label: t('openProject'), sub: t('openProjectSub'), icon: 'folderOpen', run: pickFolder },
    ], { align: 'right' })
  }
  function chatRow(s, nested) {
    var c = el('div', 'chat' + (nested ? ' nested' : '') + (state.chat && state.chat.id === s.id ? ' active' : '') + (s.archived ? ' archived' : ''))
    c.draggable = true
    c.ondragstart = function (e) { e.dataTransfer.setData('text/x-chat', s.id); e.dataTransfer.effectAllowed = 'move'; c.classList.add('dragging') }
    c.ondragend = function () { c.classList.remove('dragging') }
    if ((live[s.id] && live[s.id].run) || s.running) c.appendChild(el('span', 'spin'))
    else if (state.unseen[s.id]) c.appendChild(el('span', 'dot done'))     // finished while another chat was open
    var name = el('span', 'name', s.title)
    c.appendChild(name)
    c.appendChild(el('span', 'when', ago(s.ts)))
    c.appendChild(iconBtn('folder', t('moveTo'), function (e) { e.stopPropagation(); moveMenu(e.currentTarget, s) }))
    c.appendChild(iconBtn('pencil', t('rename'), function (e) { e.stopPropagation(); renameChat(s, c, name) }))
    c.appendChild(iconBtn('archive', s.archived ? t('unarchive') : t('archive'), function (e) { e.stopPropagation(); archiveChat(s, !s.archived) }))
    c.appendChild(iconBtn('trash', t('deleteChat'), function (e) { e.stopPropagation(); deleteChat(s) }))
    c.onclick = function () { openChat(s) }
    c.title = s.title
    return c
  }
  function renameChat(s, row, nameEl) {
    var input = el('input', 'rename')
    input.value = s.title
    row.replaceChild(input, nameEl)
    input.focus()
    input.select()
    var done = false
    function finish(save) {
      if (done) return
      done = true
      var v = input.value.trim()
      if (save && v && v !== s.title) {
        api('/api/rename', { session: s.id, cwd: s.cwd, title: v }).then(function () {
          if (state.chat && state.chat.id === s.id) { state.chat.title = v; $('chat-title').textContent = v }
          loadSessions()
        })
      } else renderSide()
    }
    input.onkeydown = function (e) { if (e.key === 'Enter') finish(true); if (e.key === 'Escape') finish(false); e.stopPropagation() }
    input.onblur = function () { finish(true) }
    input.onclick = function (e) { e.stopPropagation() }
  }
  function deleteChat(s) {
    if (!confirm(t('deleteAsk'))) return
    api('/api/delete', { session: s.id, cwd: s.cwd }).then(function () {
      if (state.chat && state.chat.id === s.id) newChat()
      loadSessions()
    })
  }
  function archiveChat(s, on) {
    api('/api/archive', { session: s.id, on: on }).then(function () {
      toast(on ? t('archived') : t('unarchive'))
      loadSessions()
    })
  }
  // A finished turn or an approval request while the window is not in front: a Windows notification (the app
  // window shows it and flashes its taskbar button), in a plain browser the page's own notification.
  function notify(chat, text) {
    if (document.hasFocus() && visible(chat)) return
    var title = (chat.title || 'maxedcode').slice(0, 80)
    if (window.chrome && chrome.webview) chrome.webview.postMessage({ notify: true, title: title, text: text })
    else if (pyApi() && pyApi().notify) pyApi().notify(title, text)
    else if (window.Notification && Notification.permission === 'granted') new Notification(title, { body: text })
    else if (window.Notification && Notification.permission === 'default') Notification.requestPermission()
  }
  function markUnseen(id) {
    if (!id || (state.chat && state.chat.id === id)) return
    state.unseen[id] = 1
    setPref('unseen', state.unseen)
    renderSide()
  }
  function loadSessions() {
    return api('/api/sessions').then(function (list) { state.sessions = Array.isArray(list) ? list : []; renderSide() })
  }

  // ── chat view ──
  function setFolder(f) {
    state.folder = f || state.home
    setPref('folder', state.folder)
    state.hidden = state.hidden.filter(function (k) { return k !== state.folder.toLowerCase() })
    setPref('hiddenFolders', state.hidden)
    if (!state.folders.some(function (x) { return same(x, state.folder) })) {
      state.folders.unshift(state.folder)
      state.folders = state.folders.slice(0, 20)
      setPref('folders', state.folders)
    }
    renderFolder()
  }
  function chatFolder() { return (state.chat && state.chat.id ? state.chat.cwd : state.folder) || state.workspace || state.home }
  function renderFolder() {
    $('hero-folder-name').textContent = folderName(groupOf(chatFolder()))
    $('hero-folder').title = chatFolder()
    $('open-default').title = t('explorer') + ' — ' + chatFolder()
    $('chat-folder-name').textContent = ownFolder(chatFolder()) ? t('chatOwnFolder') : folderName(chatFolder())
    $('chat-folder').title = t('chatWorksIn') + ': ' + chatFolder()
  }
  // Where the open chat works: the full path, open it, copy it (a started chat keeps its folder).
  function chatFolderMenu() {
    var f = chatFolder()
    showMenu($('chat-folder'), [{ head: t('chatWorksIn') }, { label: f, icon: 'folder', title: f, run: function () { api('/api/open', { path: f, app: 'explorer' }) } }, '-',
      { label: t('openFolder'), icon: 'folderColor', run: function () { api('/api/open', { path: f, app: 'explorer' }) } },
      { label: t('copyPath'), icon: 'copy', run: function () { copy(f) } }])
  }
  // The language changed: what was drawn before in the old one is drawn again (the open chat unless a turn runs in
  // it, the folder labels, the input, the side panel's tabs; a file panel is rebuilt, a terminal or page is kept).
  function relabel() {
    renderFolder()
    setHome($('app').classList.contains('home'))
    renderAttached()
    renderQueue()
    var c = state.chat
    if (c && c.id && !c.run && c.items.length) { c.view.innerHTML = ''; drawTranscript(c); scrollDown(true) }
    panel.tabs.forEach(function (tab) {
      if (tab.keep && tab.keep.kind === 'files') {
        tab.label = t('files') + ' · ' + base(tab.keep.arg)
        if (tab.node && tab.id !== panel.active) { tab.node.remove(); tab.node = null }
      }
    })
    if (!$('panel').hidden) renderPanel()
  }
  function setHome(on) {
    $('app').classList.toggle('home', on)
    $('input').placeholder = on ? t('placeholderNew') : t('placeholder')
  }
  // Each chat keeps its own view (a div shown in #log), items, run and queue: a running turn keeps streaming
  // into its chat while another chat is open or a new one is started, as in the old window.
  var live = {}          // session id (or 'new:N' until the server names it) -> chat with a running turn
  var newN = 0
  function makeChat(id, cwd, title) {
    return { id: id, cwd: cwd, title: title || '', view: el('div', 'chatlog'), items: [], run: null, queue: [], last: null,
             mode: pref('mode', 'plan'), effort: '' }
  }
  // A typed but unsent message stays with its chat (switching chats or reloading keeps it).
  var drafts = pref('drafts', {})
  function draftKey(c) { return (c && c.id) || 'new' }
  function keepDraft() {
    var c = state.chat
    if (!c) return
    var v = $('input').value
    c.draft = v
    if (v) drafts[draftKey(c)] = v
    else delete drafts[draftKey(c)]
    setPref('drafts', drafts)
  }
  function show(chat) {
    var switched = state.chat !== chat
    if (switched && state.chat) keepDraft()
    state.chat = chat
    if (switched) {
      $('input').value = chat.draft !== undefined ? chat.draft : drafts[draftKey(chat)] || ''
      autosize()
    }
    if (chat.id && state.unseen[chat.id]) { delete state.unseen[chat.id]; setPref('unseen', state.unseen) }
    try { sessionStorage.setItem('mx.chat', chat.id || '') } catch (e) {}
    var log = $('log')
    if (log.firstChild !== chat.view || log.children.length > 1) {     // the same chat again: not re-inserted (no fade)
      while (log.firstChild) log.removeChild(log.firstChild)
      log.appendChild(chat.view)
    }
    $('chat-title').textContent = chat.title || ''
    renderChanges()
    setHome(!chat.id && !chat.run && !chat.view.children.length)
    state.last = chat.last
    renderFolder()
    renderSide()
    renderStatus()
    renderQueue()
    setBusy(!!chat.run)
    renderSeats()
    if (state.view === 'trace') drawTrace()
  }
  function newChat() {
    // already in an untouched new chat of this folder: nothing to redraw
    var cur = state.chat
    if (cur && !cur.id && !cur.run && !cur.items.length && same(cur.cwd, state.folder) && state.view === 'chat') {
      $('input').focus()
      return
    }
    var c = makeChat(null, state.folder, '')
    c.key = 'new:' + (++newN)
    setView('chat')
    show(c)
    $('input').focus()
  }
  function openChat(s) {
    var c = live[s.id]
    if (c) { show(c); scrollDown(true); return }
    c = makeChat(s.id, s.cwd, s.title)
    show(c)
    api('/api/session?id=' + encodeURIComponent(s.id) + '&cwd=' + encodeURIComponent(s.cwd || '')).then(function (data) {
      c.items = data.items || []
      c.times = data.times || null
      var seat = pref('seats', {})[s.id]
      c.mode = (seat && seat.mode) || c.mode
      c.effort = (seat && seat.effort) || ''
      drawTranscript(c)
      if (state.chat === c) renderChanges()
      c.last = statsFrom(c.items)
      if (data.running && !c.run) resume(c, data.prompt, data.started)
      if (state.chat !== c) return
      state.last = c.last
      renderStatus()
      renderSeats()
      if (state.view === 'trace') drawTrace()
      scrollDown(true)
    })
  }
  function drawTranscript(chat) {
    var log = chat.view, ans = null, turn = 0, last = null, asked = null, step = 0
    var times = chat.times || {}, turns = times.turns || {}, tools = times.tools || {}, thinks = times.think || {}
    function close() { if (ans) { var n = turn; ans.ms = (turns[n] || {}).ms || 0; ans.end({ fork: function () { fork(n) } }); last = ans; ans = null } }
    chat.items.forEach(function (m) {
      if (m.role === 'user') {
        close()
        asked = null          // a later message answered the question
        turn++
        step = 0
        userEl(log, m.text)
      } else if (m.role === 'assistant') {
        if (!ans) ans = new Answer(log, false)
        if (!m.ms) m.ms = ((turns[turn] || {}).model || [])[step] || 0      // the trace shows it
        step++
        if (m.reasoning) ans.addThink(m.reasoning)
        if (m.text) ans.text += (ans.text && !/\n$/.test(ans.text) ? '\n\n' : '') + m.text
        ;(m.calls || []).forEach(function (c) { if (c.name === 'ask_user') asked = parseArgs(c.args) })
        if (m.calls && m.calls.length) {
          ans.textMark = ans.text.length - (m.text || '').length
          ans.calls(m.calls)
          m.calls.forEach(function (c) {
            ;((times.acts || {})[c.id] || []).forEach(function (x) { if (ans.steps[c.id]) helperAct(ans.steps[c.id], x) })
            ;(thinks[c.id] || []).forEach(function (x) { if (ans.steps[c.id]) helperThink(ans.steps[c.id], x[0], x[1]) })
          })
        } else ans.boundary()
      } else if (m.role === 'tool' && ans) {
        ans.toolResult(m.id, m.content, m.ms || tools[m.id])
      }
    })
    close()
    if (asked && last) last.askCard(asked, chat)      // the chat ended on the agent's question
  }
  function statsFrom(items) {
    var turns = 0, steps = 0
    items.forEach(function (m) { if (m.role === 'user') { turns++; steps = 0 } else if (m.role === 'assistant') steps++ })
    return { turns: turns, steps: steps }
  }
  function setView(v) {
    state.view = v
    Array.prototype.forEach.call($('views').children, function (b) { b.classList.toggle('on', b.dataset.view === v) })
    $('log').hidden = v !== 'chat'
    $('trace').hidden = v !== 'trace'
    if (v === 'trace') drawTrace()
  }
  var traceQ = ''
  function traceItem(m) {
    var item = el('div', 'trace-item')
    if (m.role === 'user') { item.appendChild(el('div', 'who', lang === 'ru' ? 'Пользователь' : 'User')); item.appendChild(el('div', 'bubble', m.text)) }
    else if (m.role === 'assistant') {
      item.appendChild(el('div', 'who', 'maxedcode'))
      if (m.ms) item.appendChild(el('div', 'timing', t('modelTime') + ' ' + (m.ms / 1000).toFixed(1) + ' ' + t('sec')))
      if (m.reasoning) { item.appendChild(el('div', 'label', t('reasoning'))); item.appendChild(el('pre', 'mono', m.reasoning)) }
      if (m.text) { var tx = el('div', 'text'); tx.innerHTML = md(m.text); item.appendChild(tx) }
      ;(m.calls || []).forEach(function (c) { item.appendChild(el('div', 'label', c.name)); item.appendChild(el('pre', 'mono', prettyArgs(c.args))) })
    } else if (m.role === 'tool') {
      item.appendChild(el('div', 'label', t('result')))
      var pre = el('pre', 'mono')
      colorDiff(pre, m.content || '')
      item.appendChild(pre)
    }
    return item
  }
  function drawTrace() {
    var box = $('trace')
    box.innerHTML = ''
    var bar = el('div', 'trace-bar'), q = el('input', 'field trace-q'), count = el('span', 'muted'), all = el('button', 'pick small', t('collapseAll'))
    q.placeholder = t('traceSearch')
    q.value = traceQ
    q.spellcheck = false
    var list = el('div'), open = true
    q.oninput = function () { traceQ = q.value; fill() }
    all.onclick = function () {
      open = !open
      all.textContent = t(open ? 'collapseAll' : 'expandAll')
      Array.prototype.forEach.call(list.querySelectorAll('.trace-turn'), function (g) { g.classList.toggle('closed', !open) })
    }
    bar.appendChild(q)
    bar.appendChild(count)
    bar.appendChild(all)
    box.appendChild(bar)
    box.appendChild(list)
    function fill() {
      list.innerHTML = ''
      var needle = traceQ.trim().toLowerCase(), n = 0, group = null, turn = 0
      ;((state.chat && state.chat.items) || []).forEach(function (m) {
        if (m.role === 'user' || !group) {
          turn++
          group = el('div', 'trace-turn')
          var gh = el('button', 'trace-turn-head', t('turnWord') + ' ' + turn)
          gh.onclick = function () { this.parentNode.classList.toggle('closed') }
          group.appendChild(gh)
          list.appendChild(group)
        }
        var item = traceItem(m)
        if (needle) {
          if (item.textContent.toLowerCase().indexOf(needle) < 0) return
          n++
          item.classList.add('hit')
        }
        group.appendChild(item)
      })
      if (needle) Array.prototype.forEach.call(list.querySelectorAll('.trace-turn'), function (g) { if (g.children.length < 2) g.remove() })
      count.textContent = needle ? n + ' ' + t('found') : ''
    }
    fill()
  }
  function fork(turn) {
    var chat = state.chat
    if (!chat || !chat.id || chat.run) return
    // the TUI's /rewind puts the files back too: asked only when a later turn of this chat edited files
    var seen = 0, later = false
    ;(chat.items || []).forEach(function (m) {
      if (m.role === 'user') seen++
      else if (seen > turn) (m.calls || []).forEach(function (c) { if (EDIT_TOOLS[c.name]) later = true })
    })
    var files = later && confirm(t('forkFiles'))
    api('/api/fork', { session: chat.id, cwd: chat.cwd, turns: turn, files: files }).then(function (r) {
      if (r.error) return alert(r.error)
      if (files) toast(t('forkRestored') + ': ' + (r.restored || []).length)
      loadSessions().then(function () {
        var s = state.sessions.filter(function (x) { return x.id === r.id })[0]
        if (s) openChat(s)
      })
    })
  }
  function exportChat() {
    var chat = state.chat || {}
    var lines = ['# ' + (chat.title || 'maxedcode'), '']
    ;(chat.items || []).forEach(function (m) {
      if (m.role === 'user') lines.push('## ' + (lang === 'ru' ? 'Вы' : 'You'), '', m.text, '')
      else if (m.role === 'assistant' && m.text) lines.push('## maxedcode', '', m.text, '')
    })
    var a = el('a')
    a.href = URL.createObjectURL(new Blob([lines.join('\n')], { type: 'text/markdown' }))
    a.download = (chat.title || 'chat').replace(/[\\/:*?"<>|]+/g, '_').slice(0, 60) + '.md'
    a.click()
  }

  // ── sending ──
  function setBusy(on) {
    var b = $('send')
    b.classList.toggle('stop', on)
    b.innerHTML = icon(on ? 'square' : 'arrowUp')
    updateSend()
  }
  function updateSend() { $('send').disabled = !(state.chat && state.chat.run) && !$('input').value.trim() && !attached.length && !quoted.length }
  function submit() {
    var input = $('input'), chat = state.chat
    if (!input.value.trim() && !attached.length && !quoted.length) { if (chat && chat.run) stop(); return }
    var text = (attachmentNotes() + input.value).trim()
    input.value = ''
    keepDraft()
    autosize()
    if (chat.run) {
      if (pref('busySend', 'queue') === 'interrupt') { chat.queue.unshift(text); stop() }
      else chat.queue.push(text)
      renderQueue()
      return
    }
    run(chat, text)
  }
  function renderQueue() {
    var box = $('queue'), chat = state.chat
    box.innerHTML = ''
    if (!chat) return
    chat.queue.forEach(function (q, i) {
      var row = el('div', 'queued')
      row.appendChild(el('span', '', t('queued') + ':'))
      var txt = el('span', 'txt', q)
      txt.title = t('editQueued')
      txt.onclick = function () {
        chat.queue.splice(i, 1)
        var input = $('input')
        input.value = q + (input.value.trim() ? '\n' + input.value : '')
        renderQueue()
        autosize()
        input.focus()
      }
      row.appendChild(txt)
      row.appendChild(iconBtn('x', '', function () { chat.queue.splice(i, 1); renderQueue() }))
      box.appendChild(row)
    })
  }
  function command(chat, text) {
    var m = text.match(/^\/([\w-]+)\s*([\s\S]*)$/)
    if (!m) return false
    var cmd = m[1].toLowerCase(), arg = m[2].trim()
    if (cmd === 'btw' && arg) { side(chat, arg); return true }
    if (cmd === 'plan' || cmd === 'auto' || cmd === 'manual' || cmd === 'act') {
      setMode(cmd === 'act' ? 'manual' : cmd)
      if (arg) run(chat, arg)
      return true
    }
    if (cmd === 'export') { exportChat(); return true }
    if (cmd === 'new') { newChat(); return true }
    if (cliCommand(chat, cmd, arg)) return true
    // the CLI's local commands: headless runs them for this chat's session, nothing goes to the model or the history
    if (LOCAL_CMDS.indexOf(cmd) >= 0) { run(chat, '/' + cmd + (arg ? ' ' + arg : ''), text); return true }
    if (cmd === 'help') { helpBox(chat); return true }
    if (cmd === 'run') {         // the CLI's /run: leave Plan and do what was planned
      if (curMode() === 'plan') setMode('manual')
      run(chat, arg || 'Continue: implement what you planned in the plan. No new plan.', text)
      return true
    }
    if (cmd === 'init') { run(chat, INIT_PROMPT, text); return true }
    // a skill (/name or /skill name) or a custom command from .maxedcode/commands: its text goes to the agent
    var name = cmd === 'skill' ? arg.split(/\s+/)[0] : m[1]
    var known = extraCommands().some(function (c) { return c.name === name })
    if (!known) return false
    var args = cmd === 'skill' ? arg.slice(name.length).trim() : arg
    api('/api/expand', { cwd: chat.cwd || chatFolder(), name: name, args: args }).then(function (r) {
      if (r.error) return toast(r.error)
      run(chat, r.text, text)
    })
    return true
  }
  // The terminal's commands that are a button or a setting here: done right away when they carry a value, else the
  // settings section that does the same opens. Nothing goes to the model (it would only read "/effort high" as text).
  var CLI_SECTIONS = { model: 'conn', models: 'conn', server: 'conn', settings: 'general', config: 'general', sampling: 'answers', cloud: 'cloud', mcp: 'mcp',
    hooks: 'hooks', permissions: 'perms', allow: 'perms', deny: 'perms', memory: 'memory', tools: 'cli', commands: 'cli',
    agents: 'cli', skill: 'cli' }
  function openSection(name) {
    openSettings()
    var b = document.querySelector('.tabs button[data-tab="' + name + '"]')
    if (b) b.onclick()
  }
  function cliCommand(chat, cmd, arg) {
    if (cmd === 'effort') {
      if (EFFORTS.indexOf(arg.toLowerCase()) < 0) { toast('/effort ' + EFFORTS.join('|') + ' — ' + t('helpEffort')); return true }
      setEffort(arg.toLowerCase())
      toast('effort: ' + arg.toLowerCase())
      return true
    }
    if (cmd === 'memory' && arg) {
      api('/api/cli', { cwd: chatFolder(), do: 'note', text: arg }).then(function (r) { toast(r.error || t('noteSaved')) })
      return true
    }
    if ((cmd === 'allow' || cmd === 'deny') && arg) {
      api('/api/cli', { cwd: chatFolder(), do: 'rule-add', kind: cmd, rule: arg }).then(function (r) {
        if (!r.error) cliData = r
        toast(r.error || t('rule_' + cmd) + ': ' + arg)
      })
      return true
    }
    if ((cmd === 'model' || cmd === 'models') && arg) {
      api('/api/settings', { model: arg }).then(function (d) { if (!d.error) gotSettings(d); toast(d.error || t('secModels') + ': ' + arg) })
      return true
    }
    if (cmd === 'save' && arg) {
      var s = currentSession()
      if (!s) { toast(t('btwNeedsChat')); return true }
      api('/api/rename', { session: s.id, cwd: s.cwd, title: arg }).then(function () { chat.title = arg; $('chat-title').textContent = arg; loadSessions() })
      return true
    }
    if (cmd === 'save') { renameCurrent(); return true }
    if (cmd === 'copy') {
      var items = (chat.items || []).filter(function (m) { return m.role === 'user' || (m.role === 'assistant' && m.text) })
      if (arg === 'last') items = items.filter(function (m) { return m.role === 'assistant' }).slice(-1)
      copy(items.map(function (m) { return (arg === 'last' ? '' : (m.role === 'user' ? '> ' : '')) + (m.text || '') }).join('\n\n'))
      toast(t('copied'))
      return true
    }
    if (cmd === 'search' || cmd === 'history' || cmd === 'sessions' || cmd === 'resume') {
      var box = $('search')
      if (box.hidden) openSearch()
      box.value = cmd === 'search' ? arg : ''
      box.oninput()
      box.focus()
      return true
    }
    if (cmd === 'rewind') { toast(t('helpRewind')); return true }
    if (cmd === 'editor' || cmd === 'quit') { toast(t('cmdNotNeeded')); return true }
    if (cmd === 'local') { openLauncher(); return true }
    if (CLI_SECTIONS[cmd] && !(cmd === 'skill' && arg)) { openSection(CLI_SECTIONS[cmd]); return true }
    return false
  }
  function visible(chat) { return state.chat === chat }
  // A card's button answers in its chat: sent now, or queued while a turn runs.
  function reply(chat, text) {
    if (chat.run) { chat.queue.push(text); if (visible(chat)) renderQueue(); return }
    run(chat, text)
  }
  function run(chat, text, shown) {
    if (!shown && command(chat, text)) return
    var body = { session: chat.id, cwd: chat.cwd || state.folder, prompt: text, mode: chat.mode, effort: chat.effort || null }
    follow(chat, shown || text, clock(), null, function () {
      return fetch('/api/turn', { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body) })
    })
  }
  // The page comes back to a turn the server is still running (the chat was left, or the page reloaded): the chat
  // as of the turn's start is drawn already; the turn's lines replay from the first one, then come live.
  function resume(chat, prompt, started) {
    follow(chat, prompt, '', started ? started * 1000 : null, function () {
      return fetch('/api/watch?session=' + encodeURIComponent(chat.id) + '&from=0')
    })
  }
  function follow(chat, text, time, started, start) {
    if (visible(chat)) setHome(false)
    var log = chat.view
    fresh(userEl(log, text, time))
    chat.items.push({ role: 'user', text: text })
    var ans = new Answer(log, true)
    fresh(ans.el)
    if (started) ans.t0 = started
    if (visible(chat)) scrollDown(true)
    if (!chat.title) { chat.title = text.slice(0, 60); if (visible(chat)) $('chat-title').textContent = chat.title }
    chat.run = { stopping: false }
    live[chat.id || chat.key] = chat
    if (visible(chat)) setBusy(true)
    renderSide()
    var streamed = false, usage = null, gotResult = false, steps = 0, timing = null, gone = false, asked = null
    var turnNo = statsFrom(chat.items).turns
    start()
      .then(function (r) {
        if (r.status === 404) { gone = true; return }       // it ended meanwhile: the saved chat has it all
        if (!r.ok) return r.json().then(function (e) { throw new Error(e.error || r.status) })
        var reader = r.body.getReader(), dec = new TextDecoder(), buf = ''
        function pump() {
          return reader.read().then(function (x) {
            if (x.done) return
            buf += dec.decode(x.value, { stream: true })
            var lines = buf.split('\n')
            buf = lines.pop()
            lines.forEach(function (line) {
              if (!line.trim()) return
              var ev
              try { ev = JSON.parse(line) } catch (e) { return }
              handle(ev)
            })
            return pump()
          })
        }
        return pump()
      })
      .catch(function (e) {
        ans.el.appendChild(el('div', 'error-box', t('failed') + ': ' + e.message))
      })
      .then(function () {
        var stopped = chat.run && chat.run.stopping
        chat.run = null
        delete live[chat.id || chat.key]
        if (gone) { ans.end({}); return reopen(chat) }
        ans.end({ usage: usage, time: clock(), stopped: stopped, fork: function () { fork(turnNo) } })
        chat.times = chat.times || { turns: {}, tools: {}, think: {} }
        chat.times.turns[turnNo] = { ms: ans.ms || 0 }
        if (gotResult && !stopped) notify(chat, String(ans.text || '').replace(/[#*`>|_\[\]]/g, '').trim().slice(0, 140) || t('notifyDone'))
        if (gotResult && !stopped && !chat.queue.length && asked) ans.askCard(asked, chat)
        chat.last = { turns: turnNo, steps: steps, usage: usage, timing: timing }
        if (visible(chat)) { setBusy(false); state.last = chat.last; renderStatus() }
        else markUnseen(chat.id)
        loadSessions()
        refreshTrees()
        if (chat.queue.length) { var next = chat.queue.shift(); if (visible(chat)) renderQueue(); run(chat, next) }
      })

    function handle(ev) {
      if (ev.type === 'session') {
        if (!chat.id) {
          delete live[chat.key]
          chat.id = ev.id
          chat.cwd = ev.cwd
          live[chat.id] = chat
          remember(chat)
          if (visible(chat)) {
            renderFolder()
            try { sessionStorage.setItem('mx.chat', chat.id) } catch (e) {}
          }
          loadSessions()
        }
        if (chat.run && chat.run.stopSoon) api('/api/stop', { session: chat.id })
      } else if (ev.type === 'delta') {
        if (ev.reset) { ans.reset(); streamed = false; return }
        if (ev.reasoning) ans.addThink(ev.reasoning)
        if (ev.text) { streamed = true; ans.addText(ev.text) }
      } else if (ev.type === 'ultra' && ev.text) {
        ans.addThink(ev.text + '\n')
      } else if (ev.type === 'notice') {
        ans.note(ev.kind, ev.text)
      } else if (ev.type === 'assistant') {
        var msg = ev.message || {}
        steps++
        usage = ev.usage || usage
        chat.last = { turns: turnNo, steps: steps, usage: usage }
        if (visible(chat)) { state.last = chat.last; renderStatus() }
        chat.items.push({ role: 'assistant', text: String(msg.content || ''), reasoning: msg.reasoning_content || '', ms: ev.model_ms || 0,
          calls: (msg.tool_calls || []).map(function (c) { return { id: c.id, name: (c.function || {}).name, args: (c.function || {}).arguments } }) })
        // The step's final text replaces what was streamed: a tool call the model wrote as text (<tool_call>…)
        // was shown live, and maxedcode has turned it into a real call by now.
        var said = String(msg.content || '').replace(/<think>[\s\S]*?<\/think>/g, '')
        if (streamed) { ans.text = ans.text.slice(0, ans.textMark || 0); ans.textEl.innerHTML = md(ans.text) }
        ans.addText(said)
        streamed = false
        ;(msg.tool_calls || []).forEach(function (c) {
          var fname = (c.function || {}).name
          if (fname === 'ask_user') asked = parseArgs((c.function || {}).arguments)
        })
        if (msg.tool_calls && msg.tool_calls.length) ans.calls(msg.tool_calls)
        else { if (ans.text && !/\n\n$/.test(ans.text)) ans.addText('\n\n'); ans.boundary() }
      } else if (ev.type === 'approval') {
        ans.approvalCard(ev, chat)
        notify(chat, t('apHead') + ' ' + (ev.input && (ev.input.command || ev.input.path) || ev.tool))
      } else if (ev.type === 'child') {
        ans.child(ev)
      } else if (ev.type === 'approval_result') {
        ans.approvalDone(ev.id, ev.decision)
      } else if (ev.type === 'tool_result') {
        ans.toolResult(ev.id, ev.content, ev.ms)
        chat.items.push({ role: 'tool', id: ev.id, content: String(ev.content || ''), ms: ev.ms || 0 })
        if (visible(chat)) renderChanges()
      } else if (ev.type === 'result') {
        gotResult = true
        usage = ev.usage || usage
        timing = ev.timing || null
        var answer = String(ev.result || '').trim()
        if (answer && ans.text.indexOf(answer) < 0) ans.addText((ans.text.trim() ? '\n\n' : '') + answer)
      } else if (ev.type === 'exit') {
        var stopping = chat.run && chat.run.stopping
        if (ev.code && !gotResult && !stopping) ans.el.appendChild(el('div', 'error-box', t('failed') + ' (' + ev.code + ')' + (ev.stderr ? '\n' + ev.stderr : '')))
      }
    }
  }
  // A turn the page came back to had ended in between: draw the saved chat again.
  function reopen(chat) {
    if (!visible(chat)) return
    loadSessions().then(function () {
      var s = state.sessions.filter(function (x) { return x.id === chat.id })[0]
      if (s) openChat(s)
    })
  }
  // Stop ends the turn on the server (the page only follows it). Queued messages stay and go next, as the old
  // window kept its inbox on cancel.
  function stop() {
    var chat = state.chat
    if (!chat || !chat.run) return
    chat.run.stopping = true
    if (chat.id) api('/api/stop', { session: chat.id })
    else chat.run.stopSoon = true          // the server has not named the chat yet: stop when it does
  }
  var LOCAL_CMDS = ['undo', 'redo', 'compact', 'context', 'cache']
  var INIT_PROMPT = 'Look at the project in the current folder: its folder tree, README, config and test files. Write (or improve, ' +
    'if it exists) its instruction file for an agent, CLAUDE.md or AGENTS.md: how to run and test this project, what lives in ' +
    'which folder, the conventions and what to avoid. Up to 120 lines of plain markdown, facts only, nothing you did not read.'
  // /help: every command the window understands and the button that does the same
  function helpBox(chat) {
    setHome(false)
    var box = el('div', 'side-q')
    box.appendChild(el('div', 'label', t('helpHead')))
    var list = el('div', 'help-list')
    COMMANDS.concat(HELP_EXTRA).forEach(function (c) {
      var r = el('div', 'help-row')
      r.appendChild(el('code', '', '/' + c[0]))
      r.appendChild(el('span', 'muted', t(c[1])))
      list.appendChild(r)
    })
    box.appendChild(list)
    box.appendChild(el('div', 'muted small', t('helpMore')))
    chat.view.appendChild(box)
    scrollDown(true)
  }
  function side(chat, question) {
    setHome(false)
    var box = el('div', 'side-q')
    box.appendChild(el('div', 'label', t('btw')))
    box.appendChild(el('div', 'muted', question))
    chat.view.appendChild(box)
    if (!chat.id) { box.appendChild(el('div', 'error-box', t('btwNeedsChat'))); return }
    var ans = new Answer(box, true)
    scrollDown(true)
    api('/api/btw', { session: chat.id, cwd: chat.cwd, question: question, effort: chat.effort || null }).then(function (r) {
      if (r.error) box.appendChild(el('div', 'error-box', r.error === 'busy' ? t('busy') : r.error))
      else ans.addText(r.text)
      ans.end({})
      if (visible(chat)) scrollDown()
    })
  }
  function renderStatus() {
    var s = $('status'), last = state.last
    s.innerHTML = ''
    if (!last) return
    var a = el('span')
    a.innerHTML = icon('gauge', 's14')
    var txt = t('turns') + ': ' + last.turns + ', ' + t('steps') + ': ' + last.steps
    // Speed of the whole turn: every step's generated tokens over the time spent generating them (reading the
    // prompt is its own speed, in the tooltip). Old servers' turns without the split are not guessed at.
    var tim = last.timing || {}
    if (tim.gen_tokens && tim.gen_ms) {
      txt += ' · ' + (tim.gen_tokens / (tim.gen_ms / 1000)).toFixed(1) + ' ' + t('tokPerSec')
      a.title = t('genSpeed') + ': ' + tim.gen_tokens + ' ' + t('tokens') + ' / ' + (tim.gen_ms / 1000).toFixed(1) + ' ' + t('sec') +
        (tim.read_tokens && tim.read_ms ? '\n' + t('readSpeed') + ': ' + Math.round(tim.read_tokens / (tim.read_ms / 1000)) + ' ' + t('tokPerSec') +
          ' (' + tim.read_tokens + ' ' + t('tokens') + ' / ' + (tim.read_ms / 1000).toFixed(1) + ' ' + t('sec') + ')' : '') +
        (tim.model_ms ? '\n' + t('modelTime') + ' ' + (tim.model_ms / 1000).toFixed(1) + ' ' + t('sec') : '')
    }
    a.appendChild(document.createTextNode(txt))
    s.appendChild(a)
    var u = last.usage
    if (u && u.prompt_tokens) {
      var b = el('span')
      b.innerHTML = icon('database', 's14')
      var hit = u.cached_tokens ? ' · ' + t('cacheHit') + ': ' + Math.round(100 * u.cached_tokens / u.prompt_tokens) + '%' : ''
      b.appendChild(document.createTextNode(kfmt(u.prompt_tokens + (u.completion_tokens || 0)) + ' ' + t('tok') + hit))
      s.appendChild(b)
      if (state.ctx) {
        // The ring fills as the context does: an empty ring is an empty context.
        var used = u.prompt_tokens + (u.completion_tokens || 0), pct = Math.min(100, Math.round(100 * used / state.ctx))
        var c = el('span', 'ctx-fill' + (pct >= 85 ? ' hot' : pct >= 60 ? ' warm' : ''))
        c.innerHTML = '<svg class="i s14 ctx-ring" viewBox="0 0 24 24"><circle cx="12" cy="12" r="8" class="track"/>' +
          '<circle cx="12" cy="12" r="8" class="arc" pathLength="100" stroke-dasharray="' + pct + ' 100" transform="rotate(-90 12 12)"/></svg>'
        c.appendChild(document.createTextNode(t('ctxUsed') + ' ' + pct + '%'))
        c.title = used + ' / ' + state.ctx + ' ' + t('tokens') + '\n' + t('ctxFree') + ': ' + Math.max(0, state.ctx - used)
        s.appendChild(c)
      }
    }
  }

  // ── composer seats ──
  function autosize() {
    var i = $('input')
    i.style.height = 'auto'
    i.style.height = Math.min(i.scrollHeight, 280) + 'px'
    updateSend()
  }
  // Plan/Auto and the effort belong to the chat, as in the old window; the settings pick the defaults for new chats.
  // the TUI's modes; Manual and Accept edits ask in the chat (approval cards), Plan only reads
  var MODE_NAMES = { manual: 'Manual', 'accept-edits': 'Accept edits', auto: 'Auto', plan: 'Plan' }
  function curMode() { return state.chat ? state.chat.mode : pref('mode', 'plan') }
  function curEffort() { return state.chat ? state.chat.effort : '' }
  function remember(chat) {
    if (!chat || !chat.id) return
    var seats = pref('seats', {})
    seats[chat.id] = { mode: chat.mode, effort: chat.effort }
    setPref('seats', seats)
  }
  function setMode(m) { if (state.chat) { state.chat.mode = m; remember(state.chat) } renderSeats() }
  function effective() { return (state.settings && state.settings.effective && state.settings.effective.effort) || 'medium' }
  // the model button names the model in use (short: no folder, no publisher), so nobody wonders what answers
  function shortModel(m) { return String(m || '').split(/[\\/]/).pop().replace(/\.gguf$/i, '').replace(/-0+1-of-\d+$/, '') }
  function renderSeats() {
    var eff = (state.settings && state.settings.effective) || {}, mn = $('model-name')
    if (mn) {
      var name = eff.model && eff.model !== 'local' ? shortModel(eff.model) : ''
      mn.textContent = name.length > 28 ? name.slice(0, 27) + '…' : name || 'maxedcode'
      mn.parentNode.title = (eff.model || '') + (eff.base_url ? '\n' + eff.base_url : '')
    }
    $('mode-name').textContent = MODE_NAMES[curMode()] || 'Plan'
    $('effort-name').textContent = tl(EFFORT_NAMES[curEffort() || effective()] || ['', ''])
  }

  // ── menus ──
  var menuState = { owner: null, items: [], details: [], detail: null, sel: -1, onPick: null }
  function showMenu(anchor, items, opts) {
    opts = opts || {}
    if (!opts.select && closedBy.el === anchor && Date.now() - closedBy.t < 700) { closedBy = { el: null, t: 0 }; return }
    hideMenus()
    var m = $('menu')
    m.innerHTML = ''
    m.className = 'menu' + (opts.cls ? ' ' + opts.cls : '')
    menuState = { owner: anchor, items: [], details: [], detail: null, sel: opts.select ? 0 : -1, keyboard: !!opts.select }
    items.forEach(function (it) {
      if (it === '-') return m.appendChild(el('div', 'sep'))
      if (it.head) return m.appendChild(el('div', 'head', it.head))
      if (it.node) return m.appendChild(it.node)
      var b = el('button', 'mi' + (it.danger ? ' danger' : ''))
      if (it.icon) b.innerHTML = it.icon.indexOf('<') === 0 ? it.icon : icon(it.icon)
      var txt = el('span', 'txt', it.label)
      if (it.sub) txt.appendChild(el('span', 'sub', it.sub))
      b.appendChild(txt)
      if (it.value) b.appendChild(el('span', 'val', it.value))
      if (it.check) b.insertAdjacentHTML('beforeend', icon('check', 's14'))
      if (it.submenu) b.insertAdjacentHTML('beforeend', icon('chevronRight', 's14'))
      if (it.title) b.title = it.title
      b.onmouseenter = function () {
        if (it.submenu) showSub(b, it.submenu()); else $('submenu').hidden = true
        if (menuState.detail) showDetail(menuState.items.indexOf(b))
      }
      b.onclick = function (e) {
        e.stopPropagation()
        if (it.submenu) return showSub(b, it.submenu())
        hideMenus()
        it.run && it.run()
      }
      menuState.items.push(b)
      menuState.details.push(it.detail || '')
      m.appendChild(b)
    })
    if (!m.children.length) return
    // the full description of the highlighted row, in one wrapping box under the list (rows stay one line)
    if (menuState.details.some(Boolean)) {
      var list = el('div', 'menu-list')
      while (m.firstChild) list.appendChild(m.firstChild)
      m.appendChild(list)
      menuState.detail = m.appendChild(el('div', 'menu-detail'))
      m.classList.add('with-detail')
    }
    if (anchor.classList) anchor.classList.add('on')
    place(m, anchor.getBoundingClientRect(), opts.align)
    markSel()
  }
  function showSub(row, items) {
    var m = $('submenu')
    m.innerHTML = ''
    items.forEach(function (it) {
      var b = el('button', 'mi')
      b.appendChild(el('span', 'txt', it.label))
      if (it.check) b.insertAdjacentHTML('beforeend', icon('check', 's14'))
      b.onclick = function (e) { e.stopPropagation(); hideMenus(); it.run() }
      m.appendChild(b)
    })
    m.hidden = false
    var r = row.getBoundingClientRect()
    m.style.top = Math.max(8, Math.min(r.top - 6, innerHeight - m.offsetHeight - 8)) + 'px'
    var left = r.right + 6
    if (left + m.offsetWidth > innerWidth - 8) left = r.left - m.offsetWidth - 6
    m.style.left = left + 'px'
  }
  function place(m, r, align) {
    m.hidden = false
    m.style.maxHeight = ''
    var top = r.top - m.offsetHeight - 6
    var below = !(top > 8 && align !== 'below')
    m.style.top = (below ? r.bottom + 6 : top) + 'px'
    // opened below: never past the bottom of the window, the list scrolls instead
    if (below) m.style.maxHeight = Math.max(120, Math.min(m.offsetHeight, innerHeight - r.bottom - 14)) + 'px'
    var left = align === 'right' ? r.right - m.offsetWidth : r.left
    m.style.left = Math.max(8, Math.min(left, innerWidth - m.offsetWidth - 8)) + 'px'
  }
  function markSel() {
    menuState.items.forEach(function (b, i) {
      b.classList.toggle('sel', i === menuState.sel)
      if (i === menuState.sel && b.scrollIntoView) b.scrollIntoView({ block: 'nearest' })
    })
    if (menuState.detail) showDetail(menuState.sel)
  }
  function showDetail(i) {
    var d = menuState.detail, text = (menuState.details || [])[i] || ''
    if (!d) return
    d.textContent = text
    d.hidden = !text
  }
  function hideMenus() {
    $('menu').hidden = true
    $('submenu').hidden = true
    if (menuState.owner && menuState.owner.classList) menuState.owner.classList.remove('on')
    menuState = { owner: null, items: [], details: [], detail: null, sel: -1 }
  }
  // A press on the button that opened the menu closes it; the click that follows must not open it again
  // (it did: mousedown hid the menu, then the button's click showed it once more, so it never closed).
  var closedBy = { el: null, t: 0 }
  document.addEventListener('mousedown', function (e) {
    if ($('menu').contains(e.target) || $('submenu').contains(e.target)) return
    var owner = !$('menu').hidden && !menuState.keyboard ? menuState.owner : null
    closedBy = owner && owner.contains && owner.contains(e.target) ? { el: owner, t: Date.now() } : { el: null, t: 0 }
    hideMenus()
  })
  function modeMenu() {
    showMenu($('mode-btn'), [
      { label: 'Manual', sub: t('manualSub'), check: curMode() === 'manual', run: function () { setMode('manual') } },
      { label: 'Accept edits', sub: t('editsSub'), check: curMode() === 'accept-edits', run: function () { setMode('accept-edits') } },
      { label: 'Auto', sub: t('autoSub'), check: curMode() === 'auto', run: function () { setMode('auto') } },
      { label: 'Plan', sub: t('planSub'), check: curMode() === 'plan', run: function () { setMode('plan') } },
    ])
  }
  // The model menu: the effort, then the local models of the program in use (a click loads one with its last
  // context), its stop, and Settings -> Models. Loaded once on the first open, then from the cache.
  // The model menu: the effort, the model in use, and Settings -> Models. Nothing here starts or stops a server
  // (a stray click must not load a big model): that is only in Settings -> Models and /local.
  function modelMenu() {
    var eff = (state.settings && state.settings.effective) || {}, server = (state.settings && state.settings.server) || {}
    var items = [{ node: effortPanel() }, '-']
    var where = [KIND_LABELS[server.kind] || '', server.online && server.ctx ? ctxLabel(server.ctx) : ''].filter(Boolean).join(' · ')
    items.push({ label: eff.model && eff.model !== 'local' ? shortModel(eff.model) : t('model'), sub: where || '',
      value: server.online ? '' : t('offline'), check: !!server.online, title: (eff.model || '') + '\n' + (eff.base_url || ''),
      run: function () { openSection('conn') } })
    items.push({ label: t('connTab') + '…', icon: 'link', run: function () { openSection('conn') } })
    items.push({ label: t('runWindow') + '…', icon: 'play', run: function () { openLauncher() } })
    showMenu($('model-btn'), items, { align: 'right' })
  }
  var EFFORT_NAMES = { off: ['Выкл', 'Off'], low: ['Низкое', 'Low'], medium: ['Среднее', 'Medium'], high: ['Высокое', 'High'], ultra: ['Ultra', 'Ultra'] }
  // Effort as a "Faster - Smarter" slider (the chat's own; "default" follows Settings -> maxedcode).
  function effortPanel() {
    var box = el('div', 'effort-pop')
    var head = el('div', 'ef-head')
    head.appendChild(el('span', '', t('effort')))
    var name = el('span', 'ef-name')
    head.appendChild(name)
    head.appendChild(el('span', 'grow'))
    var info = el('span', 'ef-info')
    info.innerHTML = icon('info', 's14')
    info.title = t('effortInfo')
    head.appendChild(info)
    box.appendChild(head)
    var ends = el('div', 'ef-ends')
    ends.appendChild(el('span', '', t('faster')))
    ends.appendChild(el('span', '', t('smarter')))
    box.appendChild(ends)
    var range = el('input', 'ef-range')
    range.type = 'range'
    range.min = 0
    range.max = EFFORTS.length - 1
    range.step = 1
    var reset = el('button', 'ef-reset')
    reset.type = 'button'
    function at() { return Math.max(0, EFFORTS.indexOf(curEffort() || effective())) }
    function show() {
      name.textContent = tl(EFFORT_NAMES[EFFORTS[+range.value]])
      reset.textContent = curEffort() ? t('byDefault') + ': ' + tl(EFFORT_NAMES[effective()] || [effective(), effective()]) : t('byDefault')
      reset.disabled = !curEffort()
    }
    range.value = at()
    range.oninput = show
    range.onchange = function () { setEffort(EFFORTS[+range.value]); show() }
    reset.onclick = function () { setEffort(''); range.value = at(); show() }
    box.appendChild(range)
    box.appendChild(reset)
    show()
    setTimeout(function () { range.focus() })
    return box
  }
  function setEffort(e) { if (state.chat) { state.chat.effort = e; remember(state.chat) } renderSeats() }
  function attachMenu() {
    var items = [{ label: t('attachFile'), icon: 'file', run: pickFiles }]
    if (!state.chat || !state.chat.id) items.push({ label: t('attachFolder'), icon: 'folderOpen', run: pickFolder })
    showMenu($('attach'), items)
  }
  function insertAtCursor(text) {
    var i = $('input'), s = i.selectionStart, e = i.selectionEnd
    i.value = i.value.slice(0, s) + text + i.value.slice(e)
    i.selectionStart = i.selectionEnd = s + text.length
    i.focus()
    autosize()
  }
  function mention(p) { return '@' + (/\s/.test(p) ? '"' + p + '"' : p) + ' ' }
  // The folder / file dialogs: the server shows them (Windows, a browser tab on macOS / Linux), the pywebview
  // window its own over the window
  function askFolder(start, title) {
    var a = pyApi()
    if (a && a.pick_folder) return Promise.resolve(a.pick_folder(start || '', title || '')).then(function (p) { return { path: p || null } }, function () { return { path: null } })
    return api('/api/pick-folder', { start: start, title: title })
  }
  function askFiles(start) {
    var a = pyApi()
    if (a && a.pick_files) return Promise.resolve(a.pick_files(start || '')).then(function (p) { return { paths: p || [] } }, function () { return { paths: [] } })
    return api('/api/pick-file', { start: start })
  }
  function pickFiles() {
    askFiles(chatFolder()).then(function (r) {
      ;(r.paths || []).forEach(function (p) { insertAtCursor(mention(p)) })
    })
  }
  function pickFolder() {
    askFolder(chatFolder(), t('addFolder')).then(function (r) { if (r.path) { setFolder(r.path); newChat() } })
  }
  function folderMenu(anchor) {
    var list = [state.workspace].concat(knownFolders().filter(function (f) { return !same(f, state.workspace) })).filter(Boolean)
    var items = list.slice(0, 12).map(function (f) {
      return { label: base(f), sub: f, title: f, icon: 'folder', check: same(f, chatFolder()), run: function () { setFolder(f); newChat() } }
    })
    items.push('-', { label: t('chooseFolder'), icon: 'folderPlus', run: pickFolder })
    showMenu(anchor, items, { align: 'below' })
  }
  function openMenu() {
    var f = chatFolder()
    showMenu($('open-menu'), state.apps.map(function (a) {
      return { label: tl(TEXT.apps[a] || [a, a]), icon: a === 'explorer' ? 'folderColor' : a === 'gitbash' || a === 'terminal' ? 'terminal' : 'file',
        run: function () { api('/api/open', { path: f, app: a }) } }
    }), { align: 'right' })
  }
  function currentSession() {
    var chat = state.chat
    if (!chat || !chat.id) return null
    return state.sessions.filter(function (x) { return x.id === chat.id })[0] || { id: chat.id, cwd: chat.cwd, title: chat.title }
  }
  function renameCurrent() {
    var chat = state.chat, s = currentSession()
    if (!s) return
    var v = prompt(t('rename'), chat.title)
    if (v && v.trim()) api('/api/rename', { session: s.id, cwd: s.cwd, title: v.trim() }).then(function () { chat.title = v.trim(); $('chat-title').textContent = chat.title; loadSessions() })
  }
  function moreMenu() {
    var s = currentSession()
    if (!s) return
    showMenu($('more-btn'), [
      { label: t('rename'), icon: 'pencil', run: renameCurrent },
      { label: t('exportChat'), icon: 'download', run: exportChat },
      { label: t('copyPath'), icon: 'copy', run: function () { copy(chatFolder()); toast(t('copied')) } },
      { label: s.archived ? t('unarchive') : t('archive'), icon: 'archive', run: function () { archiveChat(s, !s.archived) } },
      '-',
      { label: t('deleteChat'), icon: 'trash', danger: true, run: function () { deleteChat(s) } },
    ], { align: 'right' })
  }

  // ── "/" and "@" in the input ──
  // the third field: a button does the same (mode picker, New session, the chat's menu), so the command is offered
  // only once its first letters are typed, not in the list a bare "/" opens
  var COMMANDS = [['local', 'cmdLocal'], ['settings', 'cmdSettings'], ['btw', 'cmdBtw'], ['undo', 'cmdUndo'], ['compact', 'cmdCompact'], ['context', 'cmdContext'],
    ['run', 'cmdRun', 1], ['redo', 'cmdRedo', 1], ['cache', 'cmdCache', 1], ['init', 'cmdInit', 1], ['help', 'cmdHelp', 1],
    ['plan', 'cmdPlan', 1], ['auto', 'cmdAuto', 1], ['manual', 'cmdManual', 1], ['export', 'cmdExport', 1], ['new', 'cmdNew', 1]]
  // the CLI's commands that are buttons/settings in the window (shown by /help)
  var HELP_EXTRA = [['model', 'helpModel'], ['server', 'helpServer'], ['cloud', 'helpCloud'], ['effort', 'helpEffort'],
    ['sampling', 'helpSampling'], ['memory', 'helpMemory'], ['permissions', 'helpPerms'], ['mcp', 'helpMcp'],
    ['skill', 'helpSkill'], ['history', 'helpHistory'], ['rewind', 'helpRewind'], ['copy', 'helpCopy']]
  var cliData = null          // Settings -> CLI commands of the chat's folder (skills, custom commands for the "/" menu)
  function loadCli() {
    return api('/api/cli?cwd=' + encodeURIComponent(chatFolder())).then(function (d) { cliData = d && !d.error ? d : null; return d })
  }
  function extraCommands() {
    if (!cliData) return []
    return (cliData.commands || []).map(function (c) { return { name: c.name, sub: c.description || '', kind: t('cmdCustom') } })
      .concat((cliData.skills || []).map(function (c) { return { name: c.name, sub: c.description || '', kind: t('cmdSkill') } }))
  }
  // a skill description is written for the model ("Use when the user asks to review ..."): the list shows the
  // part a person reads ("Review ..."), up to the first sentence; the whole text goes to the detail box
  function shortDesc(text) {
    var s = String(text || '').replace(/^\s*use (?:this )?when (?:the user (?:asks|wants) (?:you )?(?:to |for )?|a task involves |)/i, '')
    s = s.split(/(?<=[.;])\s|\s[-–—]\s|\s\(/)[0].replace(/[.;]$/, '')
    return s ? s.charAt(0).toUpperCase() + s.slice(1) : ''
  }
  var findTimer = null
  function inputMenus() {
    var i = $('input'), before = i.value.slice(0, i.selectionStart)
    var slash = before.match(/^\/([\w-]*)$/)
    if (slash) {
      var word = slash[1].toLowerCase()
      var all = COMMANDS.filter(function (c) { return word || !c[2] }).map(function (c) { return { name: c[0], sub: t(c[1]) } })
        .concat(extraCommands())
      var items = [{ head: t('commands') }].concat(all.filter(function (c) { return c.name.toLowerCase().indexOf(word) === 0 }).slice(0, 30).map(function (c) {
        var full = String(c.sub || ''), short = c.kind ? shortDesc(full) : full
        return { label: '/' + c.name, sub: short, value: c.kind || '', detail: full && full !== short ? full : '',
          run: function () { i.value = '/' + c.name + ' ' + i.value.slice(i.selectionStart); i.focus(); autosize() } }
      }))
      return items.length > 1 ? showMenu(i.parentNode, items, { select: true, cls: 'compact' }) : hideMenus()
    }
    var at = before.match(/(^|\s)@([^\s@"]*)$/)
    if (at) {
      clearTimeout(findTimer)
      findTimer = setTimeout(function () {
        var q = at[2]
        api('/api/find?root=' + encodeURIComponent(chatFolder()) + '&q=' + encodeURIComponent(q)).then(function (files) {
          var start = i.selectionStart - q.length - 1
          function put(text) { i.value = i.value.slice(0, start) + text + i.value.slice(i.selectionStart); i.focus(); autosize() }
          var items = [{ head: t('filesHead') }].concat((files || []).slice(0, 12).map(function (f) {
            return { label: f.rel, icon: f.dir ? 'folder' : 'file', run: function () { put(mention(f.path)) } }
          }))
          if (items.length > 1) showMenu(i.parentNode, items, { select: true }); else hideMenus()
        })
      }, 120)
      return
    }
    if (menuState.keyboard) hideMenus()
  }

  // ── Changes: every file the chat's edits touched, as Claude's Changes card: "+N −M" at the top opens a panel tab
  // with each file's edits; a comment on any line goes to the agent with the others in one message ──
  function chatChanges(chat) {
    var results = {}, files = {}, order = []
    ;(chat && chat.items || []).forEach(function (m) { if (m.role === 'tool') results[m.id] = m.content })
    ;(chat && chat.items || []).forEach(function (m) {
      ;(m.calls || []).forEach(function (c) {
        if (!EDIT_TOOLS[c.name]) return
        var res = results[c.id]
        if (res === undefined || STEP_ERROR.test(String(res).slice(0, 300))) return
        var a = parseArgs(c.args) || {}
        var path = a.path || (String(a.diff || '').match(/^\+\+\+ (?:b\/)?(\S+)/m) || [])[1] || '?'
        var lines = editLines(c.name, a) || []
        if (!files[path]) { files[path] = { path: path, edits: [], add: 0, del: 0 }; order.push(path) }
        var f = files[path]
        f.edits.push(lines)
        lines.forEach(function (l) { if (l[0] === '+') f.add++; else if (l[0] === '-') f.del++ })
      })
    })
    return order.map(function (k) { return files[k] })
  }
  function renderChanges() {
    var chat = state.chat, list = chatChanges(chat), add = 0, del = 0
    list.forEach(function (f) { add += f.add; del += f.del })
    var b = $('changes-btn')
    b.hidden = !list.length
    b.innerHTML = '<span class="add">+' + add + '</span> <span class="del">−' + del + '</span>'
    b.title = t('changes') + ': ' + list.length + ' ' + t('filesN')
    var tab = panel.tabs.filter(function (x) { return x.id === 'changes' })[0]
    if (tab && tab.node) { var fresh = changesView(); tab.node.replaceWith(fresh); tab.node = fresh; if (panel.active !== 'changes') fresh.hidden = true }
  }
  function openChanges() {
    addTab({ id: 'changes', label: t('changes'), icon: 'pencil', make: changesView })
  }
  function changesView() {
    var chat = state.chat, list = chatChanges(chat), box = el('div', 'changes')
    if (!chat) return box
    var notes = chat.reviewNotes = chat.reviewNotes || []
    var head = el('div', 'ch-head')
    head.appendChild(el('span', 'muted', list.length + ' ' + t('filesN')))
    head.appendChild(el('span', 'grow'))
    var send = el('button', 'btn small', '')
    send.type = 'button'
    function count() { send.textContent = t('sendNotes') + (notes.length ? ' (' + notes.length + ')' : ''); send.disabled = !notes.length }
    send.onclick = function () {
      if (!notes.length) return
      var text = t('notesIntro') + '\n' + notes.map(function (n) { return '- ' + n.path + ': «' + n.line.trim().slice(0, 120) + '» — ' + n.text }).join('\n')
      chat.reviewNotes = []
      renderChanges()
      reply(chat, text)
    }
    count()
    head.appendChild(send)
    box.appendChild(head)
    if (!list.length) box.appendChild(el('div', 'muted ch-empty', t('noChanges')))
    list.forEach(function (f) {
      var file = el('details', 'ch-file')
      file.open = list.length <= 6
      var sum = el('summary')
      sum.appendChild(el('span', 'ch-path', f.path))
      sum.appendChild(el('span', 'add', '+' + f.add))
      sum.appendChild(el('span', 'del', '−' + f.del))
      var open = iconBtn('external', t('openFile'), function (e) { e.preventDefault(); openPreview(f.path) })
      sum.appendChild(open)
      file.appendChild(sum)
      f.edits.forEach(function (lines) {
        var hunk = el('div', 'ch-hunk mono')
        lines.slice(0, 600).forEach(function (l) {
          var row = el('div', 'ch-line ' + (l[0] === '+' ? 'diff-add' : l[0] === '-' ? 'diff-del' : l[0] === '@' ? 'diff-hunk' : ''))
          row.appendChild(el('span', 'ch-sign', l[0] === ' ' ? '' : l[0]))
          row.appendChild(el('span', 'ch-text', l[1]))
          var plus = el('button', 'ch-add', '+')
          plus.type = 'button'
          plus.title = t('addNote')
          plus.onclick = function () {
            if (row.nextSibling && row.nextSibling.classList && row.nextSibling.classList.contains('ch-note')) return row.nextSibling.querySelector('textarea').focus()
            var form = el('div', 'ch-note')
            var area = el('textarea')
            area.rows = 2
            area.placeholder = t('notePlaceholder')
            var ok = el('button', 'btn small', t('add'))
            ok.type = 'button'
            ok.onclick = function () {
              if (!area.value.trim()) return
              notes.push({ path: f.path, line: l[1], text: area.value.trim() })
              form.replaceWith(el('div', 'ch-note done', '💬 ' + area.value.trim()))
              count()
            }
            area.onkeydown = function (e) { e.stopPropagation(); if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) ok.click() }
            form.appendChild(area)
            form.appendChild(ok)
            row.after(form)
            area.focus()
          }
          row.appendChild(plus)
          hunk.appendChild(row)
        })
        file.appendChild(hunk)
      })
      box.appendChild(file)
    })
    return box
  }
  // ── right panel: start page, workspace files, file previews, terminals ──
  var panel = { tabs: [], active: 'start', termN: 0 }
  // The side panel slides open and closed (its width, the chat beside it follows); its content keeps its final
  // width meanwhile, so a terminal is not resized frame by frame, and a native browser page waits until it stops.
  var panelAnim = null
  function togglePanel(force, instant) {
    var p = $('panel'), show = force !== undefined ? force : (p.hidden || p.classList.contains('closing'))
    $('panel-btn').classList.toggle('on', show)
    if (panelAnim) { panelAnim.cancel(); panelAnim = null }
    p.classList.remove('closing')
    if (show) {
      var was = p.hidden
      p.hidden = false
      renderPanel()
      if (was && !instant) slidePanel(true)
    } else {
      savePanel()
      if (p.hidden || instant) { p.hidden = true; return }
      p.classList.add('closing')
      slidePanel(false, function () { p.hidden = true; p.classList.remove('closing'); savePanel() })
    }
  }
  function slidePanel(open, done) {
    var p = $('panel'), w = p.getBoundingClientRect().width
    if (!p.animate || !w || matchMedia('(prefers-reduced-motion: reduce)').matches) { if (done) done(); return }
    p.style.setProperty('--panel-w', w + 'px')
    p.classList.add('sliding')
    syncBrowsers()
    var shut = { width: '0px', minWidth: '0px', opacity: 0.3 }, full = { width: w + 'px', minWidth: '0px', opacity: 1 }
    var a = panelAnim = p.animate(open ? [shut, full] : [full, shut], open
      ? { duration: 260, easing: 'cubic-bezier(0.2, 0.8, 0.2, 1)' }
      : { duration: 200, easing: 'cubic-bezier(0.4, 0, 0.2, 1)', fill: 'forwards' })
    a.onfinish = function () {
      if (done) done()
      if (!open) a.cancel()
      p.classList.remove('sliding')
      if (panelAnim === a) panelAnim = null
      syncBrowsers()
    }
    a.oncancel = function () { p.classList.remove('sliding'); if (panelAnim === a) panelAnim = null }
  }
  function addTab(tab, quiet) {
    var have = panel.tabs.filter(function (x) { return x.id === tab.id })[0]
    if (!have) panel.tabs.push(tab)
    if (quiet) return
    panel.active = tab.id
    togglePanel(true)
  }
  // The open tabs (files, previews, browser pages) come back after a reload; terminals still running on the server
  // are reattached.
  function savePanel() {
    setPref('panelTabs', panel.tabs.filter(function (x) { return x.keep }).map(function (x) { return x.keep }))
    setPref('panelActive', panel.active)
    setPref('panelOpen', !$('panel').hidden)
  }
  function restorePanel() {
    pref('panelTabs', []).forEach(function (k) {
      if (k.kind === 'files') openFilesAt(k.arg, true)
      else if (k.kind === 'file') openPreview(k.arg, true)
      else if (k.kind === 'browser') openBrowser(k.arg, true)
    })
    api('/api/pty-list').then(function (list) {
      (list || []).forEach(function (term) { openTerminal(null, term.id, true) })
      if (list && list.length && !$('panel').hidden) renderPanel()
    })
    var active = pref('panelActive', 'start')
    panel.active = panel.tabs.some(function (x) { return x.id === active }) ? active : 'start'
    togglePanel(pref('panelOpen', false), true)
  }
  var seenTabs = { start: 1 }          // tabs drawn before: only a new one pops in
  function renderPanel() {
    var tabs = $('panel-tabs'), body = $('panel-body')
    tabs.innerHTML = ''
    var all = [{ id: 'start', label: t('start'), icon: 'compass' }].concat(panel.tabs)
    all.forEach(function (tab) {
      var b = el('button', (tab.id === panel.active ? 'on' : '') + (seenTabs[tab.id] ? '' : ' fresh'))
      seenTabs[tab.id] = 1
      b.innerHTML = icon(tab.icon, 's14')
      b.appendChild(el('span', '', tab.label))
      if (tab.id !== 'start') {
        var x = el('span', 'x')
        x.innerHTML = icon('x', 's12')
        x.onclick = function (e) {
          e.stopPropagation()
          if (b._closing) return
          b._closing = true
          closeBrowser(tab.id)
          // the tab folds away, then the panel shows what is left
          var gone = function () {
            panel.tabs = panel.tabs.filter(function (y) { return y.id !== tab.id })
            if (tab.node && tab.node.dispose) tab.node.dispose()
            if (tab.node) tab.node.remove()
            if (panel.active === tab.id) panel.active = 'start'
            delete seenTabs[tab.id]
            renderPanel()
          }
          if (!b.animate) return gone()
          b.animate([{ opacity: 1, maxWidth: b.offsetWidth + 'px' }, { opacity: 0, maxWidth: '0px', paddingLeft: '0px', paddingRight: '0px' }],
            { duration: 150, easing: 'ease-in', fill: 'forwards' }).onfinish = gone
        }
        b.appendChild(x)
      }
      b.onclick = function () { panel.active = tab.id; renderPanel() }
      tabs.appendChild(b)
    })
    // Tab bodies stay in the DOM, hidden: an iframe moved in the DOM reloads its page, a terminal keeps its output.
    Array.prototype.forEach.call(body.children, function (n) { n.hidden = true })
    var old = body.querySelector('.start')
    if (old) old.remove()
    var active = all.filter(function (x) { return x.id === panel.active })[0] || all[0]
    if (active.id === 'start') body.appendChild(startPage())
    else {
      if (!active.node) active.node = active.make()
      if (active.node.parentNode !== body) body.appendChild(active.node)
      active.node.hidden = false
      if (active.node.focusTerm) setTimeout(active.node.focusTerm)
    }
    savePanel()
    syncBrowsers()
  }
  function startPage() {
    var box = el('div', 'start')
    var c = el('div', 'compass')
    c.innerHTML = icon('compass')
    box.appendChild(c)
    function card(ic, title, sub, key, run) {
      var b = el('button', 'start-card')
      b.innerHTML = ic
      var w = el('div')
      w.appendChild(el('div', 't', title))
      w.appendChild(el('div', 'd', sub))
      b.appendChild(w)
      b.appendChild(el('span', 'k', key))
      b.onclick = run
      box.appendChild(b)
    }
    card(FOLDER_COLOR, t('wsFiles'), t('wsFilesSub'), 'Ctrl + Alt + P', openFiles)
    card('<span style="color:#7aa2f7">' + icon('terminal') + '</span>', t('newTerm'), t('newTermSub'), 'Ctrl + `', openTerminal)
    card('<span style="color:#9ece6a">' + icon('globe') + '</span>', t('browser'), t('browserSub'), 'Ctrl + Alt + T', function () { openBrowser() })
    return box
  }
  function openFiles() { openFilesAt(chatFolder()) }
  function openFilesAt(root, quiet) {
    addTab({ id: 'files:' + root.toLowerCase(), label: t('files') + ' · ' + base(root), icon: 'folder', keep: { kind: 'files', arg: root },
      make: function () { return fileTree(root) } }, quiet)
  }
  // A path clicked in an answer: a folder opens as a file tree, a file in the preview.
  function openPath(path) {
    api('/api/mtime?path=' + encodeURIComponent(path)).then(function (r) {
      if (!r || r.mtime === null) return toast(t('notFound') + ': ' + path)
      if (r.dir) openFilesAt(path)
      else openPreview(path)
    })
  }
  var trees = []        // file trees on screen: they re-read their folders when a turn ends
  function refreshTrees() {
    trees = trees.filter(function (w) { return document.body.contains(w) })
    trees.forEach(function (w) { w._refresh() })
  }
  // The file panel: a small explorer. A path bar (click a part to go there, click the bar to type a path), back /
  // forward / up, a filter, hidden files on demand; folders open in place (a double click goes into one), files
  // open in the preview (a double click opens them with Windows); a right click has the rest: new file or folder,
  // rename (F2), delete to the Recycle Bin (Del), copy the path, show in Explorer, a terminal there, mention it in
  // the message. A row dragged into the input becomes an @mention.
  function fileTree(root) {
    var wrap = el('div', 'tree-wrap explorer'), cur = root, hist = [root], pos = 0, opened = {}, sel = null
    var hidden = pref('fsHidden', false), filter = ''
    var bar = el('div', 'tree-bar'), crumbs = el('div', 'crumbs'), box = el('div', 'tree'), foot = el('div', 'tree-foot')
    box.tabIndex = 0
    var back = iconBtn('chevronLeft', t('bBack'), function () { if (pos > 0) go(hist[--pos], true) })
    var fwd = iconBtn('chevronRight', t('bForward'), function () { if (pos < hist.length - 1) go(hist[++pos], true) })
    var up = iconBtn('arrowUp', t('fsUp'), function () { var p = parentOf(cur); if (p && !same(p, cur)) go(p.length === 2 ? p + '\\' : p) })
    bar.appendChild(back); bar.appendChild(fwd); bar.appendChild(up)
    bar.appendChild(crumbs)
    bar.appendChild(iconBtn('refresh', t('bReload'), function () { refresh() }))
    var tools = el('div', 'tree-bar tree-tools')
    var find = el('input', 'field tree-filter')
    find.placeholder = t('fsFilter')
    find.spellcheck = false
    find.oninput = function () { filter = find.value.trim().toLowerCase(); refresh() }
    tools.appendChild(find)
    tools.appendChild(iconBtn('filePlus', t('fsNewFile'), function () { create('touch', cur) }))
    tools.appendChild(iconBtn('folderPlus', t('fsNewFolder'), function () { create('mkdir', cur) }))
    var eye = iconBtn('eye', t('fsHidden'), function () { hidden = !hidden; setPref('fsHidden', hidden); eye.classList.toggle('on', hidden); refresh() })
    eye.classList.toggle('on', hidden)
    tools.appendChild(eye)
    tools.appendChild(iconBtn('folderColor', t('explorer'), function () { api('/api/open', { path: cur, app: 'explorer' }) }))
    wrap.appendChild(bar)
    wrap.appendChild(tools)
    wrap.appendChild(box)
    wrap.appendChild(foot)
    function drawCrumbs() {
      crumbs.innerHTML = ''
      var parts = String(cur).replace(/[\\/]+$/, '').split(/[\\/]/), acc = ''
      parts.forEach(function (p, i) {
        acc = i ? acc + '\\' + p : p
        var target = i ? acc : acc + '\\', b = el('button', 'crumb', i ? p : p + '\\')
        b.onclick = function (e) { e.stopPropagation(); if (!same(target, cur)) go(target) }
        if (i) crumbs.appendChild(el('span', 'sep', '›'))
        crumbs.appendChild(b)
      })
      crumbs.title = cur
      setTimeout(function () { crumbs.scrollLeft = crumbs.scrollWidth })
    }
    // a click on the bar's empty part: type a path
    crumbs.onclick = function () {
      var input = el('input', 'crumb-edit')
      input.value = cur
      input.spellcheck = false
      crumbs.innerHTML = ''
      crumbs.appendChild(input)
      input.focus()
      input.select()
      var done = false
      function finish(ok) {
        if (done) return
        done = true
        var v = input.value.trim().replace(/^"|"$/g, '')
        if (ok && v && !same(v, cur)) api('/api/mtime?path=' + encodeURIComponent(v)).then(function (x) {
          if (x && x.dir) go(v); else if (x && x.mtime) openPreview(v); else { toast(t('notFound') + ': ' + v); drawCrumbs() }
        })
        else drawCrumbs()
      }
      input.onkeydown = function (e) { e.stopPropagation(); if (e.key === 'Enter') finish(true); if (e.key === 'Escape') finish(false) }
      input.onblur = function () { finish(false) }
      input.onclick = function (e) { e.stopPropagation() }
    }
    function go(path, keepHist) {
      if (!keepHist) { hist = hist.slice(0, pos + 1).concat([path]).slice(-50); pos = hist.length - 1 }
      cur = path
      opened = {}
      sel = null
      refresh()
    }
    function pick(row, it) {
      if (sel && sel.row) sel.row.classList.remove('sel')
      sel = { row: row, it: it }
      row.classList.add('sel')
    }
    function fmtTime(ts) {
      if (!ts) return ''
      var d = new Date(ts * 1000), now = new Date()
      return d.toDateString() === now.toDateString() ? clock(d) : d.toLocaleDateString(lang === 'ru' ? 'ru-RU' : 'en-US', { day: 'numeric', month: 'short', year: d.getFullYear() === now.getFullYear() ? undefined : 'numeric' })
    }
    function level(path, depth, into, done) {
      api('/api/ls?root=' + encodeURIComponent(root) + '&path=' + encodeURIComponent(path) + (hidden ? '&hidden=1' : '')).then(function (r) {
        var items = (r.items || []).filter(function (it) { return !filter || depth > 0 || it.name.toLowerCase().indexOf(filter) >= 0 })
        if (!items.length && depth === 0) into.appendChild(el('div', 'muted pad', filter ? t('fsNoMatch') : t('fsEmpty')))
        items.forEach(function (it) {
          var row = el('div', 'tree-row')
          row.style.paddingLeft = (6 + depth * 16) + 'px'
          row.draggable = true
          row.ondragstart = function (e) { e.dataTransfer.setData('text/x-path', it.path); e.dataTransfer.setData('text/plain', it.path) }
          function draw(open) {
            row.innerHTML = '<span class="tw">' + (it.dir ? icon('chevronRight', 's12' + (open ? ' down' : '')) : '') + '</span>' +
              (it.dir ? icon(open ? 'folderOpen' : 'folder', 's14 fdir') : icon('file', 's14'))
            row.appendChild(el('span', 'n', it.name))
            row.appendChild(el('span', 'sz', it.dir ? '' : kfmt(it.size) + 'B'))
            row.appendChild(el('span', 'tm', fmtTime(it.mtime)))
          }
          draw(false)
          row.title = it.path
          into.appendChild(row)
          row.oncontextmenu = function (e) { e.preventDefault(); e.stopPropagation(); pick(row, it); itemMenu(e, it, row) }
          if (!it.dir) {
            row.onclick = function () { pick(row, it); openPreview(it.path) }
            row.ondblclick = function () { api('/api/fs', { do: 'start', path: it.path }) }
            return
          }
          var key = it.path.toLowerCase(), kids = null
          function toggle() {
            if (kids) { kids.remove(); kids = null; delete opened[key]; draw(false); return }
            opened[key] = true
            draw(true)
            kids = el('div', 'tree-kids')
            row.after(kids)
            level(it.path, depth + 1, kids)
          }
          row.onclick = function () { pick(row, it); toggle() }
          row.ondblclick = function (e) { e.preventDefault(); go(it.path) }
          if (opened[key]) toggle()          // a refresh keeps the folders that were open
        })
        if (depth === 0) foot.textContent = items.length + ' ' + t('fsItems')
        if (done) done()
      })
    }
    function point(e) { return { getBoundingClientRect: function () { return { left: e.clientX, right: e.clientX, top: e.clientY, bottom: e.clientY, width: 0, height: 0 } } } }
    function itemMenu(e, it, row) {
      var dir = it.dir ? it.path : parentOf(it.path)
      showMenu(point(e), [
        it.dir ? { label: t('fsOpenHere'), icon: 'folderOpen', run: function () { go(it.path) } }
          : { label: t('fsPreview'), icon: 'file', run: function () { openPreview(it.path) } },
        { label: t('fsOpenWith'), icon: 'external', run: function () { api('/api/fs', { do: 'start', path: it.path }) } },
        { label: t('fsReveal'), icon: 'folderColor', run: function () { api('/api/fs', { do: 'reveal', path: it.path }) } },
        { label: t('newTerm'), icon: 'terminal', run: function () { openTerminalAt(dir) } },
        '-',
        { label: t('fsMention'), icon: 'plus', run: function () { insertAtCursor(mention(it.path)); $('input').focus(); autosize() } },
        { label: t('copyAnyPath'), icon: 'copy', run: function () { copy(it.path); toast(t('copied')) } },
        '-',
        { label: t('fsNewFile'), icon: 'filePlus', run: function () { create('touch', dir) } },
        { label: t('fsNewFolder'), icon: 'folderPlus', run: function () { create('mkdir', dir) } },
        { label: t('rename') + '  F2', icon: 'pencil', run: function () { renameRow(row, it) } },
        { label: t('fsDelete') + '  Del', icon: 'trash', danger: true, run: function () { remove(it) } },
      ], { select: false })
    }
    box.oncontextmenu = function (e) {
      if (e.target !== box && !e.target.classList.contains('pad')) return
      e.preventDefault()
      showMenu(point(e), [
        { label: t('fsNewFile'), icon: 'filePlus', run: function () { create('touch', cur) } },
        { label: t('fsNewFolder'), icon: 'folderPlus', run: function () { create('mkdir', cur) } },
        '-',
        { label: t('newTerm'), icon: 'terminal', run: function () { openTerminalAt(cur) } },
        { label: t('fsReveal'), icon: 'folderColor', run: function () { api('/api/open', { path: cur, app: 'explorer' }) } },
        { label: t('copyPath'), icon: 'copy', run: function () { copy(cur); toast(t('copied')) } },
      ])
    }
    box.onkeydown = function (e) {
      if (!sel) return
      if (e.key === 'F2') { e.preventDefault(); renameRow(sel.row, sel.it) }
      else if (e.key === 'Delete') { e.preventDefault(); remove(sel.it) }
      else if (e.key === 'Enter') { e.preventDefault(); sel.it.dir ? go(sel.it.path) : openPreview(sel.it.path) }
    }
    function act(body, after) {
      return api('/api/fs', body).then(function (x) {
        if (!x || x.error) { toast(t('opFailed') + (x && x.error ? ': ' + x.error : '')); return }
        if (after) after(x)
        refresh()
      })
    }
    function renameRow(row, it) {
      var n = row.querySelector('.n')
      if (!n) return
      var input = el('input', 'rename')
      input.value = it.name
      row.replaceChild(input, n)
      input.focus()
      var dot = it.dir ? -1 : it.name.lastIndexOf('.')
      input.setSelectionRange(0, dot > 0 ? dot : it.name.length)
      var done = false
      function finish(ok) {
        if (done) return
        done = true
        var v = input.value.trim()
        if (ok && v && v !== it.name) act({ do: 'rename', path: it.path, name: v })
        else refresh()
      }
      input.onkeydown = function (e) { e.stopPropagation(); if (e.key === 'Enter') finish(true); if (e.key === 'Escape') finish(false) }
      input.onblur = function () { finish(true) }
      input.onclick = input.ondblclick = function (e) { e.stopPropagation() }
    }
    function remove(it) {
      if (!confirm(t('fsDeleteAsk') + '\n' + it.path)) return
      act({ do: 'delete', path: it.path })
    }
    // a new file or folder: a row with a name field at the top of its folder
    function create(kind, dir) {
      if (!same(dir, cur) && !opened[dir.toLowerCase()]) { go(dir) }
      var row = el('div', 'tree-row creating')
      row.innerHTML = '<span class="tw"></span>' + icon(kind === 'mkdir' ? 'folder' : 'file', 's14')
      var input = row.appendChild(el('input', 'rename'))
      input.placeholder = t(kind === 'mkdir' ? 'fsNewFolder' : 'fsNewFile')
      box.insertBefore(row, box.firstChild)
      input.focus()
      var done = false
      function finish(ok) {
        if (done) return
        done = true
        var v = input.value.trim()
        if (ok && v) act({ do: kind, path: dir, name: v }, function (x) { if (kind === 'touch' && x.path) openPreview(x.path) })
        else row.remove()
      }
      input.onkeydown = function (e) { e.stopPropagation(); if (e.key === 'Enter') finish(true); if (e.key === 'Escape') finish(false) }
      input.onblur = function () { finish(true) }
    }
    function refresh() {
      back.disabled = pos <= 0
      fwd.disabled = pos >= hist.length - 1
      drawCrumbs()
      var fresh = el('div')
      level(cur, 0, fresh, function () { box.innerHTML = ''; while (fresh.firstChild) box.appendChild(fresh.firstChild) })
    }
    wrap._refresh = refresh
    trees.push(wrap)
    refresh()
    return wrap
  }
  function openPreview(path, quiet) {
    addTab({ id: 'file:' + path.toLowerCase(), label: base(path), icon: 'file', keep: { kind: 'file', arg: path },
      make: function () { return preview(path) } }, quiet)
  }
  var MD_FILE = /\.(md|markdown)$/i, IMAGE_FILE = /\.(png|jpe?g|gif|webp|bmp|ico)$/i
  function preview(path) {
    var box = el('div', 'preview'), head = el('div', 'preview-head'), body = el('div', 'preview-body'), mtime = null, asMd = MD_FILE.test(path), shown = false
    head.appendChild(el('span', 'preview-path', path))
    if (asMd) {
      var flip = el('button', 'pick small', t('source'))
      flip.onclick = function () { asMd = !asMd; flip.textContent = t(asMd ? 'source' : 'rendered'); load() }
      head.appendChild(flip)
    }
    head.appendChild(iconBtn('refresh', t('bReload'), function () { load() }))
    box.appendChild(head)
    box.appendChild(body)
    body.appendChild(el('div', 'muted pad', t('loading')))
    function load() {
      var top = body.scrollTop
      api('/api/file?path=' + encodeURIComponent(path)).then(function (r) {
        mtime = r.mtime
        body.innerHTML = ''
        if (r.error) return body.appendChild(el('div', 'muted pad', r.error))
        if (r.binary && IMAGE_FILE.test(path)) {
          var img = el('img', 'img-preview appear')
          img.src = '/api/raw?path=' + encodeURIComponent(path) + '&m=' + r.mtime
          return body.appendChild(img)
        }
        if (r.binary) return body.appendChild(el('div', 'muted pad', t('binary') + ' · ' + r.size + ' B'))
        var view
        if (asMd) {
          view = el('div', 'text md-preview')
          view.innerHTML = md(r.text)
        } else view = codeView(r.text)
        if (!shown) view.classList.add('appear')
        shown = true
        body.appendChild(view)
        if (r.cut) body.appendChild(el('div', 'muted pad', t('cut')))
        body.scrollTop = top
      })
    }
    // The agent edits the file while it is open: it reloads when it changes on disk (checked while shown).
    var timer = setInterval(function () {
      if (!document.body.contains(box)) { if (mtime !== null) clearInterval(timer); return }
      if (box.hidden || $('panel').hidden || document.hidden) return
      api('/api/mtime?path=' + encodeURIComponent(path)).then(function (r) { if (r.mtime && mtime && r.mtime !== mtime) load() }, function () {})
    }, 2000)
    load()
    return box
  }
  function codeView(text) {
    var src = String(text).replace(/\r\n/g, '\n'), wrap = el('div', 'code-view'), nums = el('pre', 'ln'), code = el('pre', 'src')
    nums.textContent = src.split('\n').map(function (_, i) { return i + 1 }).join('\n')
    code.textContent = src
    wrap.appendChild(nums)
    wrap.appendChild(code)
    return wrap
  }
  // The old window's Browser tab: a page in an iframe (sandboxed by default), with the history this tab made.
  // A page that refuses to be framed (X-Frame-Options / CSP) shows the browser's own error: open it outside.
  var BROWSER_SANDBOX = 'allow-scripts allow-forms allow-same-origin allow-popups allow-popups-to-escape-sandbox'
  function parseAddress(input) {
    var text = String(input || '').trim()
    if (!text) return { error: 'bErrEmpty' }
    if (text.length > 16384) return { error: 'bErrInvalid' }
    var u
    try { u = new URL(/^[A-Za-z][A-Za-z\d+.-]*:(?!\d+(?:[/?#]|$))/.test(text) ? text : 'https://' + text) } catch (e) { return { error: 'bErrInvalid' } }
    if (u.username || u.password) return { error: 'bErrCredentials' }
    if (u.protocol !== 'https:' && u.protocol !== 'http:') return { error: 'bErrProtocol' }
    if (u.origin === location.origin) return { error: 'bErrSelf' }
    return { url: u.href, title: u.hostname }
  }
  // In the app window (desktop\app, WebView2) a Browser tab is a real browser of its own, laid by the window over
  // the tab's area: any site opens, not only those that allow an iframe. The page keeps the address bar and tells
  // the window where the area is (syncBrowsers); the window reports the address, title and history back.
  var HOST = !!(window.chrome && chrome.webview)
  var hostTabs = {}
  function hostPost(msg) { if (HOST) chrome.webview.postMessage(msg) }
  if (HOST) chrome.webview.addEventListener('message', function (ev) {
    var d = ev.data
    if (d && d.browser === 'state' && hostTabs[d.id]) hostTabs[d.id].state(d)
    if (d && d.dropped && drops[d.dropped]) { var x = drops[d.dropped]; delete drops[d.dropped]; x(d.paths || []) }
  })
  function covered() {
    return !$('menu').hidden || !$('submenu').hidden || !$('modal').hidden || !!panelAnim
  }
  function syncBrowsers() {
    Object.keys(hostTabs).forEach(function (id) {
      var b = hostTabs[id], r = b.area.getBoundingClientRect()
      var visible = !$('panel').hidden && panel.active === id && b.area.isConnected && r.width > 4 && r.height > 4 &&
        !b.blank && !covered()
      hostPost({ browser: 'bounds', id: id, visible: visible, x: r.left, y: r.top, w: r.width, h: r.height, dpr: window.devicePixelRatio || 1 })
    })
  }
  if (HOST) {
    window.addEventListener('resize', syncBrowsers)
    var watch = new MutationObserver(syncBrowsers)
    ;['menu', 'submenu', 'modal', 'panel'].forEach(function (id) { watch.observe($(id), { attributes: true, attributeFilter: ['hidden'] }) })
  }
  function closeBrowser(id) {
    if (!hostTabs[id]) return
    delete hostTabs[id]
    hostPost({ browser: 'close', id: id })
  }
  function nativeBrowserTab(tab, startUrl) {
    var id = tab.id, box = el('div', 'browser')
    var bar = el('form', 'b-bar')
    var back = iconBtn('chevronLeft', t('bBack'), function () { hostPost({ browser: 'back', id: id }) })
    var fwd = iconBtn('chevronRight', t('bForward'), function () { hostPost({ browser: 'forward', id: id }) })
    var reload = iconBtn('refresh', t('bReload'), function () { hostPost({ browser: me.loading ? 'stop' : 'reload', id: id }) })
    var addr = el('input', 'b-addr')
    addr.placeholder = t('bAddress')
    addr.spellcheck = false
    var go = iconBtn('link', t('bGo'))
    go.type = 'submit'
    var ext = iconBtn('external', t('bExternal'), function () { if (me.url) window.open(me.url, '_blank', 'noopener,noreferrer') })
    ;[back, fwd, reload].forEach(function (b) { b.type = 'button'; bar.appendChild(b) })
    var wrap = el('div', 'b-addr-box')
    wrap.appendChild(addr)
    wrap.appendChild(go)
    bar.appendChild(wrap)
    ext.type = 'button'
    bar.appendChild(ext)
    box.appendChild(bar)
    var fail = el('div', 'b-fail')
    fail.hidden = true
    box.appendChild(fail)
    var area = el('div', 'b-content')
    var hint = el('div', 'b-hint', t('bStart'))
    area.appendChild(hint)
    box.appendChild(area)
    var me = hostTabs[id] = { area: area, blank: true, url: '', loading: false }
    me.state = function (d) {
      me.url = d.url || ''
      me.loading = !!d.loading
      if (document.activeElement !== addr) addr.value = me.url
      back.disabled = !d.back
      fwd.disabled = !d.forward
      ext.disabled = !me.url
      reload.innerHTML = icon(me.loading ? 'x' : 'refresh')
      fail.hidden = !d.error
      fail.textContent = d.error ? t('bLoadFail') + ' (' + d.error + ')' : ''
      var title = d.title && !/^https?:/.test(d.title) ? d.title : (parseAddress(me.url).title || t('browser'))
      if (title !== tab.label) { tab.label = title; if (!$('panel').hidden) renderPanel() }
      tab.keep.arg = me.url
      if (me.url) setPref('browserLast', { url: me.url, title: title })
    }
    function open(input) {
      var r = parseAddress(input)
      if (r.error) { fail.textContent = t(r.error); fail.hidden = false; return }
      fail.hidden = true
      me.blank = false
      hint.hidden = true
      addr.value = r.url
      addr.blur()
      hostPost({ browser: 'open', id: id, url: r.url })
      syncBrowsers()
    }
    bar.onsubmit = function (e) { e.preventDefault(); open(addr.value) }
    back.disabled = fwd.disabled = ext.disabled = true
    new ResizeObserver(syncBrowsers).observe(area)
    var prev = pref('browserLast', null)
    if (startUrl) setTimeout(function () { open(startUrl) })
    else {
      if (prev && prev.url) {
        hint.hidden = true
        var card = el('div', 'b-restore')
        card.appendChild(el('div', 'muted', t('bPrevious')))
        card.appendChild(el('div', 't', prev.title))
        card.appendChild(el('div', 'muted u', prev.url))
        var again = el('button', 'btn primary', t('bRestore'))
        again.type = 'button'
        again.onclick = function () { card.remove(); open(prev.url) }
        card.appendChild(again)
        area.appendChild(card)
      }
      setTimeout(function () { addr.focus() })
    }
    return box
  }
  function openBrowser(url, quiet) {
    panel.browserN = (panel.browserN || 0) + 1
    var tab = { id: 'browser:' + panel.browserN, label: url ? (parseAddress(url).title || t('browser')) : t('browser'), icon: 'globe', keep: { kind: 'browser', arg: url || '' } }
    tab.make = function () { return HOST ? nativeBrowserTab(tab, url) : browserTab(tab, url) }
    addTab(tab, quiet)
  }
  function browserTab(tab, startUrl) {
    var box = el('div', 'browser'), hist = [], pos = -1, sandboxed = true
    var bar = el('form', 'b-bar')
    var back = iconBtn('chevronLeft', t('bBack'), function () { if (pos > 0) { pos--; show() } })
    var fwd = iconBtn('chevronRight', t('bForward'), function () { if (pos < hist.length - 1) { pos++; show() } })
    var reload = iconBtn('refresh', t('bReload'), function () { if (pos >= 0) show() })
    var addr = el('input', 'b-addr')
    addr.placeholder = t('bAddress')
    addr.spellcheck = false
    var go = iconBtn('link', t('bGo'))
    go.type = 'submit'
    var ext = iconBtn('external', t('bExternal'), function () { if (pos >= 0) window.open(hist[pos].url, '_blank', 'noopener,noreferrer') })
    var warn = el('div', 'b-warn', t('bSandboxWarn'))
    warn.hidden = true
    var shield = iconBtn('shield', t('bSandboxOff'), function () {
      sandboxed = !sandboxed
      shield.classList.toggle('off', !sandboxed)
      shield.title = t(sandboxed ? 'bSandboxOff' : 'bSandboxOn')
      warn.hidden = sandboxed
      if (pos >= 0) show()
    })
    ;[back, fwd, reload].forEach(function (b) { b.type = 'button'; bar.appendChild(b) })
    var wrap = el('div', 'b-addr-box')
    wrap.appendChild(addr)
    wrap.appendChild(go)
    bar.appendChild(wrap)
    ext.type = shield.type = 'button'
    bar.appendChild(ext)
    bar.appendChild(shield)
    box.appendChild(bar)
    box.appendChild(warn)
    var fail = el('div', 'b-fail')
    fail.hidden = true
    box.appendChild(fail)
    var content = el('div', 'b-content')
    var hint = el('div', 'b-hint', t('bStart'))
    content.appendChild(hint)
    box.appendChild(content)
    var prev = pref('browserLast', null)
    if (!startUrl && prev && prev.url) {
      hint.hidden = true
      var card = el('div', 'b-restore')
      card.appendChild(el('div', 'muted', t('bPrevious')))
      card.appendChild(el('div', 't', prev.title))
      card.appendChild(el('div', 'muted u', prev.url))
      var again = el('button', 'btn primary', t('bRestore'))
      again.type = 'button'
      again.onclick = function () { open(prev.url) }
      card.appendChild(again)
      content.appendChild(card)
    }
    function state() {
      back.disabled = pos <= 0
      fwd.disabled = pos >= hist.length - 1
      reload.disabled = ext.disabled = pos < 0
    }
    function show() {
      var cur = hist[pos]
      addr.value = cur.url
      fail.hidden = true
      content.innerHTML = ''
      hint.hidden = false
      hint.textContent = t('bLoading')
      content.appendChild(hint)
      var frame = el('iframe', 'b-frame')
      frame.src = cur.url
      frame.title = cur.title
      frame.referrerPolicy = 'no-referrer'
      if (sandboxed) frame.setAttribute('sandbox', BROWSER_SANDBOX)
      frame.onload = function () { hint.hidden = true }
      content.appendChild(frame)
      tab.label = cur.title
      tab.keep.arg = cur.url
      setPref('browserLast', cur)
      state()
      if (!$('panel').hidden) renderPanel()
    }
    function open(input) {
      var r = parseAddress(input)
      if (r.error) { fail.textContent = t(r.error); fail.hidden = false; return }
      if (pos >= 0 && hist[pos].url === r.url) return show()      // the same address again: reload
      hist = hist.slice(0, pos + 1).concat([r]).slice(-100)
      pos = hist.length - 1
      show()
    }
    bar.onsubmit = function (e) { e.preventDefault(); open(addr.value) }
    state()
    if (startUrl) setTimeout(function () { open(startUrl) })
    else setTimeout(function () { addr.focus() })
    return box
  }
  function openTerminal(command, attach, quiet, cwd) {
    panel.termN++
    var n = panel.termN
    addTab({ id: 'term:' + n, label: t('terminal') + ' ' + n, icon: 'terminal', make: function () { return terminal(cwd || chatFolder(), command, attach) } }, quiet)
  }
  function openTerminalAt(dir) { openTerminal(null, null, false, dir) }
  // A real terminal: the user's shell on a pseudo console of the server (webapp/pty.py), drawn by vt.js. The server
  // keeps it while the page reloads or the chat changes; closing the tab ends it.
  function terminal(cwd, command, attach) {
    var box = el('div', 'term'), id = attach || null, offset = 0, gone = false, queue = '', busy = false
    var vt = MxTerm(box, {
      send: function (data) { queue += data; flush() },
      resize: function (cols, rows) { if (id) api('/api/pty-resize', { id: id, cols: cols, rows: rows }) },
    })
    function flush() {
      if (!id || busy || !queue) return
      var data = queue
      queue = ''
      busy = true
      api('/api/pty-write', { id: id, data: data }).then(done, done)
    }
    function done() { busy = false; flush() }
    function read() {
      fetch('/api/pty-read?id=' + encodeURIComponent(id) + '&from=' + offset).then(function (r) {
        if (!r.ok) { gone = true; vt.write('\r\n\x1b[90m[' + t('termGone') + ']\x1b[m\r\n'); return }
        var reader = r.body.getReader(), dec = new TextDecoder(), buf = ''
        function pump() {
          return reader.read().then(function (x) {
            if (x.done) return
            buf += dec.decode(x.value, { stream: true })
            var lines = buf.split('\n')
            buf = lines.pop()
            lines.forEach(function (l) {
              var ev
              try { ev = JSON.parse(l) } catch (e) { return }
              if (ev.data) { vt.write(ev.data); offset = ev.at }
              if ('exit' in ev) { gone = true; vt.write('\r\n\x1b[90m[' + t('termExit') + ' ' + ev.exit + ']\x1b[m\r\n') }
            })
            return pump()
          })
        }
        return pump()
      }).then(function () { if (!gone) setTimeout(read, 1000) },
        function () { if (!gone) setTimeout(read, 2000) })
    }
    // the size is known once the tab is on screen
    requestAnimationFrame(function () {
      requestAnimationFrame(function () {
        vt.fit()
        if (id) { var sz = vt.size(); api('/api/pty-resize', { id: id, cols: sz.cols, rows: sz.rows }); return read() }
        var size = vt.size()
        api('/api/pty-open', { cwd: cwd, cols: size.cols, rows: size.rows }).then(function (r) {
          if (!r.id) { vt.write(t('termFail') + ': ' + (r.error || '?') + '\r\n'); return }
          id = r.id
          read()
          if (typeof command === 'string' && command) setTimeout(function () { queue += command; flush() }, 700)
          flush()
        })
      })
    })
    box.dispose = function () { gone = true; vt.dispose(); if (id) api('/api/pty-close', { id: id }) }
    box.focusTerm = vt.focus
    setTimeout(vt.focus, 60)
    return box
  }

  // ── settings ──
  function applyLook() {
    var theme = pref('theme', 'dark')
    var dark = theme === 'dark' || (theme === 'system' && matchMedia('(prefers-color-scheme: dark)').matches)
    document.documentElement.dataset.theme = dark ? 'dark' : 'light'
    // the app window (desktop\app\maxedcode.exe, WebView2) matches its title bar to the theme
    if (window.chrome && chrome.webview) chrome.webview.postMessage('theme:' + (dark ? 'dark' : 'light'))
    document.documentElement.lang = lang
    document.documentElement.style.setProperty('--chat-size', pref('fontSize', 14) + 'px')
    Array.prototype.forEach.call(document.querySelectorAll('[data-t]'), function (n) { n.textContent = t(n.dataset.t) })
    Array.prototype.forEach.call(document.querySelectorAll('[data-t-title]'), function (n) { n.title = t(n.dataset.tTitle) })
    Array.prototype.forEach.call(document.querySelectorAll('[data-icon]'), function (n) { n.innerHTML = icon(n.dataset.icon, n.dataset.iconCls) })
    $('views').hidden = !pref('trace', true)
    setHome($('app').classList.contains('home'))
  }
  matchMedia('(prefers-color-scheme: dark)').addEventListener('change', applyLook)
  function tipRow(label, hint, control) {
    var r = row(label, '', control)
    if (hint) { var l = r.querySelector('.lbl > div'); l.title = hint; l.classList.add('has-hint') }
    return r
  }
  function row(label, sub, control, wide) {
    var r = el('div', 'row' + (wide ? ' wide' : ''))
    var l = el('div', 'lbl')
    l.appendChild(el('div', '', label))
    if (sub) l.appendChild(el('div', 'sub', sub))
    r.appendChild(l)
    r.appendChild(control)
    return r
  }
  function pick(options, value, onChange) {
    var b = el('button', 'pick')
    function label() { var o = options.filter(function (x) { return x[0] === value })[0]; return o ? o[1] : value }
    b.appendChild(el('span', '', label()))
    b.insertAdjacentHTML('beforeend', icon('chevron', 's14'))
    b.onclick = function () {
      showMenu(b, options.map(function (o) {
        return { label: o[1], check: o[0] === value, run: function () { value = o[0]; b.firstChild.textContent = label(); onChange(o[0]) } }
      }), { align: 'right' })
    }
    return b
  }
  function toggle(on, onChange) {
    var b = el('button', 'toggle' + (on ? ' on' : ''))
    b.onclick = function () { on = !on; b.classList.toggle('on', on); onChange(on) }
    return b
  }
  function renderGeneral() {
    var tab = $('tab-general')
    sectionHead(tab, t('general'), '')
    tab = tab.appendChild(el('div', 'set-rows flat'))
    var shared = el('div', 'pick-row')
    shared.appendChild(el('span', 'path-val', state.workspace || ''))
    shared.appendChild(button(t('change'), function () {
      askFolder(state.workspace, t('sharedFolder')).then(function (r) {
        if (!r.path) return
        api('/api/workspace', { path: r.path }).then(function (x) {
          if (!x.workspace) return
          if (same(state.folder, state.workspace)) setFolder(x.workspace)
          state.workspace = x.workspace
          renderGeneral(); renderSide(); renderFolder()
        })
      })
    }))
    shared.appendChild(button(t('openFolder'), function () { api('/api/open', { path: state.workspace, app: 'explorer' }) }))
    tab.appendChild(row(t('sharedFolder'), t('sharedFolderSub'), shared, true))
    tab.appendChild(row(t('permission'), t('permissionSub'), pick([['manual', 'Manual'], ['accept-edits', 'Accept edits'], ['auto', 'Auto'], ['plan', 'Plan']], pref('mode', 'plan'), function (v) { setPref('mode', v) })))
    tab.appendChild(row(t('language'), '', pick([['ru', 'Русский'], ['en', 'English']], lang, function (v) {
      lang = v; setPref('lang', v); shareLang(); applyLook(); renderGeneral(); renderChat(); renderModelTabs(); renderCli(); renderCloud(); renderBrowser(); renderSide(); renderSeats(); renderStatus()
      relabel()
    })))
    var themes = el('div', 'themes'), cur = pref('theme', 'dark')
    ;[['light', 'sun'], ['dark', 'moon'], ['system', 'monitor']].forEach(function (x) {
      var b = el('button', cur === x[0] ? 'on' : '')
      b.innerHTML = icon(x[1])
      b.appendChild(el('span', '', t(x[0])))
      b.onclick = function () { setPref('theme', x[0]); applyLook(); renderGeneral() }
      themes.appendChild(b)
    })
    tab.appendChild(row(t('theme'), '', themes, true))
    var size = el('div', 'pick-row')
    var num = el('input', 'field num')
    num.type = 'number'
    num.min = 10
    num.max = 22
    num.value = pref('fontSize', 14)
    num.onchange = function () { var v = Math.max(10, Math.min(22, +num.value || 14)); num.value = v; setPref('fontSize', v); applyLook() }
    size.appendChild(num)
    size.appendChild(el('span', 'muted', ' ' + t('px')))
    tab.appendChild(row(t('fontSize'), t('fontSizeSub'), size))
    var u = updateInfo
    if (!u) return
    var vrow = el('div', 'pick-row')
    var check = button(t('updCheck'), function () {
      check.disabled = true
      check.textContent = t('updChecking')
      api('/api/update', { check: true }).then(function (x) { updateInfo = Object.assign({}, updateInfo, x); renderGeneral() },
        function () { renderGeneral() })
    })
    if (!u.installed) check.disabled = true
    vrow.appendChild(check)
    var sub = !u.installed ? t('updNoInstall') : u.ready ? u.ready + ' ' + t('updReady')
      : u.error ? u.error : u.latest ? t('updUpToDate') : ''
    tab.appendChild(row(t('updVersion') + ' ' + u.version, sub, vrow))
    if (u.installed) {
      tab.appendChild(row(t('updAuto'), t('updAutoSub'), toggle(u.auto, function (on) {
        api('/api/settings', { auto_update: on ? null : false }).then(function () { loadUpdate(false) })
      })))
    }
  }
  // ── Chat: how a turn is shown and what Enter does while the agent works ──
  function renderChat() {
    var tab = $('tab-chat')
    if (!tab) return
    sectionHead(tab, t('secChat'), '')
    tab = tab.appendChild(el('div', 'set-rows flat'))
    tab.appendChild(row(t('details'), t('detailsSub'), pick([['full', t('detailsFull')], ['terse', t('detailsTerse')]], pref('details', 'full'), function (v) { setPref('details', v) })))
    tab.appendChild(row(t('fold'), t('foldSub'), pick([['done', t('foldDone')], ['never', t('foldNever')]], pref('fold', 'done'), function (v) { setPref('fold', v) })))
    tab.appendChild(row(t('replies'), t('repliesSub'), pick([['all', t('repliesAll')], ['final', t('repliesFinal')]], pref('replies', 'all'), function (v) { setPref('replies', v) })))
    tab.appendChild(row(t('busySend'), t('busySendSub'), pick([['queue', t('busyQueue')], ['interrupt', t('busyInterrupt')]], pref('busySend', 'queue'), function (v) { setPref('busySend', v) })))
    tab.appendChild(row(t('traceToggle'), t('traceToggleSub'), toggle(pref('trace', true), function (v) { setPref('trace', v); applyLook(); if (!v) setView('chat') })))
    tab.appendChild(row(t('shortcuts'), t('shortcutsSub'), el('span')))
  }
  // ── Settings: building blocks (the DeepSeek Harness layout: a section title, then flat rows split by hairlines,
  // or cards of 20px radius for lists; one look in every section) ──
  // "Permissions — /permissions, /allow" -> ["Permissions", "/permissions, /allow"]: the CLI command goes in a tag
  function splitCmd(title) {
    var m = String(title || '').match(/^(.*?)\s*(?:—|:)\s*(\/.*)$/)
    return m ? [m[1], m[2]] : [String(title || ''), '']
  }
  function sectionHead(tab, title, intro) {
    tab.innerHTML = ''
    var p = splitCmd(title), h = el('h2', 'set-title', p[0])
    if (p[1]) h.appendChild(el('span', 'tag cmd', p[1]))
    tab.appendChild(h)
    if (intro) tab.appendChild(el('p', 'set-intro', intro))
  }
  function group(parent, title, sub) {
    if (title) {
      var p = splitCmd(title), g = el('div', 'set-group', p[0])
      if (p[1]) g.appendChild(el('span', 'tag cmd', p[1]))
      parent.appendChild(g)
    }
    if (sub) parent.appendChild(el('p', 'set-hint', sub))
    var box = el('div', 'set-card')
    parent.appendChild(box)
    return box
  }
  function listRow(box, main, sub, extra, title) {
    var r = el('div', 'set-item')
    var m = el('span', main === t('none') ? 'set-item-sub' : 'set-item-name', main)
    if (title) m.title = title
    r.appendChild(m)
    if (sub) r.appendChild(el('span', 'set-item-sub', sub))
    if (extra) { var x = el('span', 'set-item-act'); [].concat(extra).forEach(function (n) { x.appendChild(n) }); r.appendChild(x) }
    box.appendChild(r)
    return r
  }
  function addRow(parent, nodes) {
    var a = el('div', 'set-add')
    nodes.forEach(function (n) { a.appendChild(n) })
    parent.appendChild(a)
    return a
  }
  function inputEl(placeholder, value, cls) {
    var i = el('input', 'field' + (cls ? ' ' + cls : ''))
    i.placeholder = placeholder || ''
    i.value = value || ''
    i.spellcheck = false
    return i
  }
  function smallBtn(label, onclick, kind) {
    var b = el('button', 'btn small' + (kind ? ' ' + kind : ''), label)
    b.onclick = onclick
    return b
  }
  function filesNote(tab, paths) {
    var p = (paths || []).filter(Boolean)
    if (p.length) tab.appendChild(el('div', 'files', t('filesLbl') + ': ' + p.join(' · ')))
  }
  function clipText(x, n) { x = String(x || ''); return x.length > n ? x.slice(0, n) + '…' : x }

  // ── Models → Run a model on this PC (maxedcode/launch.py): a server, a model, a context, one Start button ──
  Object.assign(TEXT, {
    runHead: ['Запуск моделей', 'Start models'], runWindow: ['Запуск моделей', 'Start models'],
    runIntro: ['Необязательно: запускайте модели как привыкли — агент сам подключится к тому, что работает. Здесь можно запустить в один клик: программа, модель (или ваш скрипт) — «Запустить».',
      'Optional: start your models the way you always do, and the agent connects to what runs. Here it is one click: a program, a model (or your script), Start.'],
    runRunning: ['Работает', 'Running'], runOff: ['Не запущен', 'Not running'], runNotInstalled: ['Не установлен', 'Not installed'],
    runStart: ['Запустить', 'Start'], runRestart: ['Перезапустить с этой моделью', 'Restart with this model'],
    runStop: ['Остановить', 'Stop'], runStopping: ['Останавливаю…', 'Stopping…'], runCancel: ['Отменить', 'Cancel'],
    runActive: ['Запущено', 'Running'], runUseIt: ['Работать с ним', 'Use it'],
    runModel: ['Модель', 'Model'], runCtx: ['Контекст', 'Context'],
    runCtxSub: ['Сколько текста модель помнит за раз. Больше — дольше без сжатия, но нужно больше видеопамяти. Удобно 32K–64K.',
      'How much text the model holds at once. More: longer without compaction, but more graphics memory. 32K-64K is comfortable.'],
    runSetUp: ['настроена на', 'set up for'], runNoLoaded: ['модель не загружена', 'no model loaded'], runLoading: ['Запускаю', 'Starting'],
    runNoModels: ['Моделей пока нет', 'No models yet'], runLoaded: ['загружена', 'loaded'], runVision: ['видит картинки', 'reads pictures'],
    runUpTo: ['до', 'up to'], runScript: ['Свой скрипт запуска', 'Own start script'],
    runScriptSub: ['Если сервер запускается вашим .bat/.sh (свои флаги, пресеты), выберите его — он откроется в своём окне. «Нет» — обычный запуск с моделью и контекстом выше.',
      'If the server starts from your own .bat/.sh (own flags, presets), pick it: it opens in its own window. None: the usual start with the model and context above.'],
    runScriptNone: ['Нет — обычный запуск', 'None: the usual start'], runPickFile: ['Выбрать файл…', 'Pick a file…'],
    runDownload: ['Скачать', 'Download'], runRecheck: ['Проверить снова', 'Check again'],
    runNotInstalledSub: ['Установите программу, затем нажмите «Проверить снова».', 'Install it, then Check again.'],
    runNoCli: ['Не найдена команда запуска', 'Its start command is not found'],
    runOllamaPull: ['Скачайте модель в терминале, например:', 'Download a model in a terminal, for example:'],
    runDone: ['Готово', 'Ready'], runFailed: ['Не запустилось', 'Did not start'], runStopped: ['Сервер остановлен', 'Server stopped'],
    runScan: ['Ищу серверы и модели…', 'Looking for servers and models…'], runCurrent: ['сейчас у агента', 'the agent uses it'],
    runMenuHead: ['Локальная модель', 'Local model'], runMenuSettings: ['Модели и серверы…', 'Models and servers…'],
    runSecs: ['с', 's'], cmdLocal: ['Запустить модель на этом компьютере', 'Run a model on this computer'],
    cmdSettings: ['Все настройки', 'All settings'], connHead: ['Подключение', 'Connection'],
    connSub: ['Адрес сервера, который использует агент: этот компьютер, другой компьютер в сети или облачный API по ключу.',
      'The server the agent talks to: this computer, another one on the network, or a cloud API with a key.'],
  })
  var KIND_LABELS = { strata: 'Strata', lmstudio: 'LM Studio', ollama: 'Ollama', llamacpp: 'llama.cpp', vllm: 'vLLM', koboldcpp: 'KoboldCpp',
    jan: 'Jan', tgwui: 'text-generation-webui', openai: 'OpenAI', anthropic: 'Claude', gemini: 'Gemini', openrouter: 'OpenRouter',
    deepseek: 'DeepSeek', groq: 'Groq', mistral: 'Mistral', xai: 'xAI', together: 'Together AI', cerebras: 'Cerebras' }
  var launchData = null, launchKind = '', launchPick = {}, launchTimer = null
  function ctxLabel(n) { return n >= 1048576 ? Math.round(n / 1048576) + 'M' : Math.round(n / 1024) + 'K' }
  // Strata's sizes: "iq3_xxs" -> "IQ3_XXS", "coder-iq1_m" -> "Coder · IQ1_M", "ud-q4_k_xl" -> "UD-Q4_K_XL"
  function strataName(id) {
    var parts = String(id || '').split('-'), size = parts.pop().toUpperCase()
    if (parts.length && /^ud$/i.test(parts[parts.length - 1])) { parts.pop(); size = 'UD-' + size }
    var fam = parts.join('-')
    return fam ? fam.charAt(0).toUpperCase() + fam.slice(1) + ' · ' + size : size
  }
  function sizeLabel(b) { return b ? (b >= 1e9 ? (b / 1e9).toFixed(1) + ' GB' : Math.round(b / 1e6) + ' MB') : '' }
  function loadLaunch() {
    return api('/api/launch').then(function (d) {
      launchData = d
      if (d && d.job && d.job.result === null) followJob()
      autoFollow()
      return d
    }, function () { launchData = { servers: [], error: 'no answer' } })
  }
  // The agent's server on this PC does not answer and exactly one other one runs here (started from its own
  // window, a script, the tray): the agent follows it, once per address, and says so.
  var followedFrom = ''
  function autoFollow() {
    var d = state.settings, job = launchData && launchData.job
    if (!d || !d.server || d.server.online || !launchData || (job && job.result === null)) return
    var base = String((d.effective || {}).base_url || '')
    if (!/^https?:[/][/](127\.0\.0\.1|localhost|\[::1\])[:/]/i.test(base) || followedFrom === base) return
    var on = (launchData.servers || []).filter(function (x) { return x.running })
    if (on.length !== 1) return
    followedFrom = base
    var x = on[0], jit = x.kind === 'lmstudio' || x.kind === 'ollama'
    if (jit && !(x.loaded && x.loaded.model)) return
    api('/api/settings', { base_url: x.base, server: x.kind, model: jit ? x.loaded.model : null }).then(function (r) {
      if (r.error) return
      gotSettings(r)
      if (!$('modal').hidden) renderModelTabs()
      redrawLaunch()
      toast(t('followed').replace('{new}', x.label).replace('{old}', KIND_LABELS[d.server.kind] || base), 6000)
    })
  }
  function launchServer(kind) { return ((launchData && launchData.servers) || []).filter(function (s) { return s.kind === kind })[0] }
  // which program is shown: the chosen one, else the one the agent uses, a running one, the last started, one installed
  function launchDefault() {
    var list = (launchData && launchData.servers) || [], cur = ((state.settings && state.settings.server) || {}).kind
    var by = function (f) { return list.filter(f)[0] }
    var s = launchServer(launchKind) || by(function (x) { return x.kind === cur && x.installed }) || by(function (x) { return x.running }) ||
      launchServer(launchData && launchData.last) || by(function (x) { return x.installed }) || list[0]
    return s
  }
  function isCurrent(s) {
    var b = ((state.settings && state.settings.effective) || {}).base_url || ''
    return !!b && b.replace(/\/+$/, '').toLowerCase() === s.base.replace(/\/+$/, '').toLowerCase()
  }
  function followJob() {
    if (launchTimer) return
    launchTimer = setInterval(function () {
      api('/api/launch?job=1').then(function (r) {
        var j = r && r.job
        if (launchData) launchData.job = j
        if (!j || j.result !== null) {
          clearInterval(launchTimer)
          launchTimer = null
          if (j && j.result) {
            if (j.result.ok && j.do === 'start') toast(t('runDone') + ': ' + (j.result.model || j.model || '') + (j.result.ctx ? ' · ' + ctxLabel(j.result.ctx) : ''), 4000)
            else if (j.result.ok) toast(t('runStopped'), 2500)
          }
          foundServers = null
          return Promise.all([loadLaunch(), api('/api/settings').then(gotSettings)]).then(function () { if (!$('modal').hidden) renderModelTabs(); if ($('launcher') && !$('launcher').hidden) redrawLaunch() })
        }
        drawJob()
      }, function () {})
    }, 1000)
  }
  function launchAct(body) {
    api('/api/launch', body).then(function (r) {
      if (launchData) launchData.job = r.job
      if (r.job && r.job.error) { toast(r.job.error, 4000); return }
      redrawLaunch()
      followJob()
    })
  }
  var launchBox = null, jobBox = null
  function redrawLaunch() { if (launchBox && launchBox.isConnected) drawLaunch(); }
  function drawJob() {
    if (!jobBox || !jobBox.isConnected) return
    var j = launchData && launchData.job
    // a finished start of another program, or one that ended minutes ago: not shown
    if (j && j.result !== null && (j.kind !== launchKind || (j.age || 0) > 180)) j = null
    jobBox.innerHTML = ''
    jobBox.hidden = !j
    if (!j) return
    var busy = j.result === null, ok = j.result && j.result.ok
    var head = jobBox.appendChild(el('div', 'run-job-head'))
    head.appendChild(busy ? el('span', 'spin') : el('span', 'state-dot ' + (ok ? 'ok' : 'err')))
    // the program's own line (a command, "waiting for…", the log) goes under the bar; the head says it in words
    var raw = String(j.lines[0] || '').replace(/\s*…?\s*\d+ s(?= ·|$)/, '').replace(/^(loading|waiting for) [^·]*(· )?/, '')
    var what = busy ? (j.do === 'stop' ? t('runStopping') : t('runLoading') + (j.model ? ' ' + String(j.model).split(/[\/]/).pop() : '') +
      (j.pct !== null && j.pct !== undefined ? ' · ' + Math.round(j.pct) + '%' : '')) :
      ok ? (j.do === 'stop' ? t('runStopped') : t('runDone') + (j.result.model ? ': ' + j.result.model : '') + (j.result.ctx ? ' · ' + ctxLabel(j.result.ctx) : ''))
        : t('runFailed') + ': ' + (j.result.error || j.result.text || '')
    var txt = head.appendChild(el('span', 'run-job-text', what))
    txt.title = what
    head.appendChild(el('span', 'run-job-secs', j.secs + ' ' + t('runSecs')))
    if (busy) head.appendChild(smallBtn(t('runCancel'), function () { launchAct({ do: 'cancel' }) }, 'link'))
    if (busy) {
      if (raw) jobBox.appendChild(el('div', 'run-job-raw', raw)).title = raw
      var bar = jobBox.appendChild(el('div', 'run-bar' + (j.pct === null || j.pct === undefined ? ' wait' : '')))
      var fill = bar.appendChild(el('div', 'run-bar-fill'))
      if (j.pct !== null && j.pct !== undefined) fill.style.width = Math.max(3, Math.min(100, j.pct)) + '%'
    }
  }
  // ── the launch window: starting models from here is optional and lives apart from the settings, so working
  // with models started by hand is never in its way. Opened by the model menu and /local only. ──
  function openLauncher(kind) {
    var box = $('launcher')
    if (!box) {
      box = document.body.appendChild(el('div', 'modal'))
      box.id = 'launcher'
      box.addEventListener('mousedown', function (e) { if (e.target === box) closeLauncher() })
      document.addEventListener('keydown', function (e) { if (e.key === 'Escape' && !box.hidden) closeLauncher() })
    }
    if (kind) launchKind = kind
    box.hidden = false
    box.innerHTML = ''
    var card = box.appendChild(el('div', 'setup-card launch-card'))
    card.appendChild(iconBtn('x', '', closeLauncher)).classList.add('close')
    card.appendChild(el('h2', '', t('runWindow')))
    launchBox = card.appendChild(el('div', 'run-card'))
    drawLaunch()
    loadLaunch().then(redrawLaunch)       // fresh every time it opens: what runs now
  }
  // Settings -> Start models: the same as the launch window, as a tab of its own (apart from Connection)
  function renderLaunchTab() {
    var tab = $('tab-launch')
    if (!tab) return
    sectionHead(tab, t('runWindow'), '')
    launchBox = tab.appendChild(el('div', 'run-card'))
    drawLaunch()
    loadLaunch().then(redrawLaunch)
  }
  function closeLauncher() {
    var box = $('launcher')
    if (box) box.hidden = true
    launchBox = null
  }
  // the way to start (launch.ways): the one chosen here, else the last one started, else what the person set up
  // themselves (an own way, then a script found in the program's folder, then its presets), else the builtin start
  function pickOf(s) {
    var p = launchPick[s.kind] || {}, ways = s.ways || []
    var has = function (id) { return ways.some(function (w) { return w.id === id }) }
    var by = function (src) { return (ways.filter(function (w) { return w.source === src })[0] || {}).id }
    var way = has(p.way) ? p.way : has(s.last_way) ? s.last_way : by('own') || by('found') || by('program') || by('builtin') || ''
    var models = s.models || []
    var m = models.filter(function (x) { return x.id === p.model })[0] || models.filter(function (x) { return x.loaded })[0] ||
      models.filter(function (x) { return x.id === s.last_model })[0] || models[0] || null
    var ctx = p.ctx || (m && m.loaded && s.loaded && s.loaded.ctx) || (m && m.pick_ctx) || 32768
    var opts = p.opts || Object.assign({}, s.opts || {})
    return { way: way, wayObj: ways.filter(function (w) { return w.id === way })[0] || null, model: m, ctx: ctx, opts: opts }
  }
  var wayForm = null             // the "own way" form being filled: {kind, id?, name, command, base}
  function wayFormBox(body, s) {
    var f = wayForm, box = body.appendChild(el('div', 'way-form'))
    box.appendChild(el('div', 'way-form-title', f.id ? t('wayEdit') : t('wayNew')))
    var name = inputEl(t('wayName'), f.name), cmd = inputEl(t('wayCmdPh'), f.command), base = inputEl(s.base ? s.base.replace(/^https?:[/][/]/, '') : '127.0.0.1:5001', f.base)
    name.oninput = function () { f.name = name.value }
    cmd.oninput = function () { f.command = cmd.value }
    base.oninput = function () { f.base = base.value }
    var r1 = box.appendChild(el('div', 'way-field'))
    r1.appendChild(el('label', '', t('wayCmd')))
    var cr = r1.appendChild(el('div', 'pick-row'))
    cr.appendChild(cmd)
    cr.appendChild(button(t('runPickFile'), function () {
      api('/api/pick-file', { start: f.command ? parentOf(f.command.replace(/"/g, '')) : '' }).then(function (r) {
        var p = r && r.paths && r.paths[0]
        if (!p) return
        f.command = /\s/.test(p) ? '"' + p + '"' : p
        if (!f.name) f.name = p.split(/[\\/]/).pop().replace(/\.[^.]+$/, '')
        drawLaunch()
      })
    }))
    var r2 = box.appendChild(el('div', 'way-field'))
    r2.appendChild(el('label', '', t('wayNameLbl')))
    r2.appendChild(name)
    var r3 = box.appendChild(el('div', 'way-field'))
    r3.appendChild(el('label', '', t('wayBase')))
    r3.appendChild(base)
    base.title = s.kind === 'other' ? t('wayBaseOther') : t('wayBaseSub')
    cmd.title = s.kind === 'other' ? t('wayOtherSub') : t('wayNewSub')
    var acts = box.appendChild(el('div', 'run-actions'))
    var save = acts.appendChild(smallBtn(t('save'), function () {
      if (!String(f.command || '').trim()) return cmd.focus()
      if (s.kind === 'other' && !String(f.base || '').trim()) return base.focus()
      save.disabled = true
      api('/api/launch', { do: 'way-save', kind: s.kind, id: f.id || null, name: f.name || '', command: f.command, base: f.base || '' }).then(function (r) {
        if (r && r.servers) launchData = Object.assign(r, { job: launchData && launchData.job })
        var mine = (launchServer(s.kind) || {}).ways || []
        var added = mine.filter(function (w) { return w.source === 'own' && w.command === String(f.command).trim() })[0]
        wayForm = null
        if (added) { launchPick[s.kind] = Object.assign(launchPick[s.kind] || {}, { way: added.id }) }
        drawLaunch()
      })
    }))
    acts.appendChild(smallBtn(t('cancel'), function () { wayForm = null; drawLaunch() }, 'link'))
    setTimeout(function () { (f.command ? name : cmd).focus() }, 0)
  }
  // Settings -> Models -> More settings: this program's own switches (launch.OPTIONS), kept per program
  function optionsBox(body, s, p) {
    var list = s.options || []
    if (!list.length) return
    var open = !!(launchPick[s.kind] || {}).more
    var set = Object.keys(p.opts || {}).filter(function (k) { return p.opts[k] }).length
    var head = body.appendChild(el('button', 'run-more' + (open ? ' open' : '')))
    head.innerHTML = icon('chevronRight', 's14')
    head.appendChild(el('span', '', t('runMore') + ' ' + s.label))
    if (set) head.appendChild(el('span', 'tag', set + ' ' + t('runChanged')))
    head.onclick = function () { launchPick[s.kind] = Object.assign(launchPick[s.kind] || {}, { more: !open, opts: p.opts }); drawLaunch() }
    if (!open) return
    var rows = body.appendChild(el('div', 'set-rows run-opts'))
    var setOpt = function (k, v) { p.opts[k] = v; launchPick[s.kind] = Object.assign(launchPick[s.kind] || {}, { opts: p.opts }) }
    list.forEach(function (o) {
      var ctl
      if (o.type === 'text') {
        ctl = inputEl('', p.opts[o.key] || '')
        ctl.oninput = function () { setOpt(o.key, ctl.value) }
      } else {
        var cur = p.opts[o.key] !== undefined && p.opts[o.key] !== null ? String(p.opts[o.key]) : String(o.default || '')
        ctl = pick(o.choices.map(function (c) { return [c[0], tl(c[1])] }), cur, function (v) { setOpt(o.key, v) })
      }
      rows.appendChild(tipRow(tl(o.label), o.hint ? tl(o.hint) : '', ctl))
    })
    rows.appendChild(el('div', 'run-actions')).appendChild(smallBtn(t('runReset'), function () { p.opts = {}; launchPick[s.kind] = Object.assign(launchPick[s.kind] || {}, { opts: {} }); drawLaunch() }, 'link'))
  }
  function drawLaunch() {
    var box = launchBox
    box.innerHTML = ''
    if (!launchData) { var w = box.appendChild(el('div', 'run-empty')); w.appendChild(el('span', 'spin')); w.appendChild(el('span', 'muted', t('runScan'))); return }
    var list = launchData.servers || []
    var s = launchDefault()
    if (!s) { box.appendChild(el('div', 'error-box', launchData.error || t('setupNone'))); return }
    launchKind = s.kind
    var label = function (x) { return x.kind === 'other' ? t('otherKind') : x.label }
    var job = launchData.job, busy = !!(job && job.result === null)
    // the programs as segments: a dot says running (green), installed (grey), missing (hollow)
    var seg = box.appendChild(el('div', 'run-seg'))
    list.forEach(function (x) {
      var b = seg.appendChild(el('button', x.kind === s.kind ? 'on' : ''))
      if (x.kind !== 'other' || x.running) b.appendChild(el('span', 'state-dot ' + (x.running ? 'ok' : x.installed ? 'off' : 'none')))
      b.appendChild(el('span', '', label(x)))
      b.title = x.kind === 'other' ? t('about_other') : x.running ? t('runRunning') : x.installed ? t('runOff') : t('runNotInstalled')
      b.onclick = function () { launchKind = x.kind; wayForm = null; drawLaunch() }
    })
    var body = box.appendChild(el('div', 'run-body'))
    // the state of this program, its stop button
    var st = body.appendChild(el('div', 'run-state'))
    st.appendChild(el('span', 'state-dot ' + (s.running ? 'ok' : s.installed ? 'off' : 'none')))
    var lo = s.loaded || {}
    var line = s.running ? t('runRunning') + (lo.label ? ' · ' + lo.label : '') + (lo.model ? ' · ' + shortModel(lo.model) + (lo.ctx ? ' · ' + ctxLabel(lo.ctx) : '') : s.kind === 'other' ? '' : ' · ' + t('runNoLoaded'))
      : s.kind === 'other' ? t('about_other') : s.installed ? t('runOff') : t('runNotInstalled')
    st.appendChild(el('span', 'run-state-text', line))
    if (s.running && isCurrent(s)) st.appendChild(el('span', 'tag', t('runCurrent')))
    st.appendChild(el('span', 'grow'))
    if (s.running && !isCurrent(s)) st.appendChild(smallBtn(t('runUseIt'), function () {
      api('/api/settings', { base_url: s.base, server: s.kind === 'other' ? null : s.kind, model: s.kind === 'lmstudio' || s.kind === 'ollama' ? lo.model || null : null })
        .then(function (d) { if (!d.error) { gotSettings(d); renderModelTabs() } else toast(d.error, 4000) })
    }, 'outline'))
    if (s.running) {
      var stopB = st.appendChild(smallBtn(t('runStop'), function () { launchAct({ do: 'stop', kind: s.kind, base: s.base }) }, 'danger'))
      stopB.disabled = busy
    }
    var p = pickOf(s), ways = s.ways || [], models = s.models || []
    var save = function (k, v) { launchPick[s.kind] = Object.assign({}, launchPick[s.kind] || {}, { way: p.way, opts: p.opts }); launchPick[s.kind][k] = v; if (k === 'model') delete launchPick[s.kind].ctx }
    if (!s.installed && !ways.length) {
      body.appendChild(el('p', 'run-about', t('about_' + s.kind)))
      var acts = body.appendChild(el('div', 'run-actions'))
      acts.appendChild(smallBtn(t('runDownload') + ' ' + s.label, function () { window.open(s.download, '_blank', 'noopener,noreferrer') }))
      acts.appendChild(smallBtn(t('runRecheck'), function () { launchData = null; drawLaunch(); loadLaunch().then(redrawLaunch) }, 'outline'))
      acts.appendChild(smallBtn(t('wayAddShort'), function () { wayForm = { kind: s.kind }; drawLaunch() }, 'link'))
      if (wayForm && wayForm.kind === s.kind) wayFormBox(body, s)
      return
    }
    // the ways to start: one card each; the builtin start opens its model, context and settings below
    if (ways.length > 1 || (ways.length && ways[0].type !== 'builtin') || s.kind === 'other') {
      body.appendChild(el('div', 'run-label', t('runHow')))
      var wl = body.appendChild(el('div', 'run-models'))
      ways.forEach(function (w) {
        var r = wl.appendChild(el('button', 'run-model' + (w.id === p.way ? ' on' : '')))
        r.appendChild(el('span', 'run-radio'))
        var info = r.appendChild(el('span', 'run-model-info'))
        var title = w.type === 'builtin' ? t('wayBuiltin') : w.name
        var sub = w.type === 'builtin' ? t('wayBuiltinSub') : w.type === 'preset' ? t('wayPresetSub') : w.command + (w.base && w.source === 'own' ? ' → ' + w.base.replace(/^https?:[/][/]/, '') : '')
        info.appendChild(el('span', 'run-model-name', title))
        if (w.source === 'own') info.appendChild(el('span', 'run-model-sub', sub))      // a found one: its name says it
        r.title = sub
        var tag = { found: t('wayFound'), own: t('wayOwn'), program: t('wayPreset') }[w.source]
        if (tag) r.appendChild(el('span', 'tag', tag))
        if (w.source === 'own') {
          var ed = r.appendChild(iconBtn('pencil', t('wayEdit'), function (e) { e.stopPropagation(); wayForm = { kind: s.kind, id: w.id, name: w.name, command: w.command, base: w.base }; drawLaunch() }))
          ed.classList.add('run-icon')
          var rm = r.appendChild(iconBtn('x', t('remove'), function (e) {
            e.stopPropagation()
            api('/api/launch', { do: 'way-remove', id: w.id }).then(function (x) { if (x && x.servers) launchData = Object.assign(x, { job: launchData && launchData.job }); drawLaunch() })
          }))
          rm.classList.add('run-icon')
        }
        r.onclick = function () { save('way', w.id); wayForm = null; drawLaunch() }
      })
      if (!ways.length) wl.appendChild(el('div', 'run-none', t('wayOtherNone')))
    }
    if (wayForm && wayForm.kind === s.kind) wayFormBox(body, s)
    else body.appendChild(smallBtn('+ ' + t('wayAdd'), function () { wayForm = { kind: s.kind }; drawLaunch() }, 'link')).classList.add('run-add')
    var w0 = p.wayObj, builtin = w0 && w0.type === 'builtin'
    if (builtin) {
      // models: one radio row each, the facts that help choosing (size, quantization, the most context it takes)
      body.appendChild(el('div', 'run-label', t('runModel')))
      if (!models.length) {
        var none = body.appendChild(el('div', 'run-none', t('runNoModels')))
        if (s.kind === 'ollama') {
          none.appendChild(el('div', 'muted small', t('runOllamaPull')))
          var cmd = none.appendChild(el('div', 'pick-row'))
          cmd.appendChild(el('code', 'path-val', s.hint || 'ollama pull qwen3:8b'))
          cmd.appendChild(button(t('brCopy'), function () { copy(s.hint || 'ollama pull qwen3:8b') }))
        }
      }
      var ml = models.length ? body.appendChild(el('div', 'run-models')) : null
      models.forEach(function (m) {
        var r = ml.appendChild(el('button', 'run-model' + (p.model && p.model.id === m.id ? ' on' : '')))
        r.appendChild(el('span', 'run-radio'))
        var info = r.appendChild(el('span', 'run-model-info'))
        // Strata's sizes read best as "IQ3_XXS" / "Coder · IQ1_M"; the full name goes under it
        var strata = s.kind === 'strata'
        info.appendChild(el('span', 'run-model-name', strata ? strataName(m.id) : m.name))
        var facts = [strata ? m.name : '', m.params, m.quant, sizeLabel(m.size), m.max_ctx ? t('runUpTo') + ' ' + ctxLabel(m.max_ctx) : '',
          !m.max_ctx && m.ctx ? t('runSetUp') + ' ' + ctxLabel(m.ctx) : ''].filter(Boolean)
        if (facts.length) info.appendChild(el('span', 'run-model-sub', facts.join(' · ')))
        r.title = m.id
        if (m.vision) r.appendChild(el('span', 'tag', t('runVision')))
        if (m.loaded) r.appendChild(el('span', 'tag ok', t('runLoaded')))
        r.onclick = function () { save('model', m.id); drawLaunch() }
      })
      if (p.model) {
        var cr = body.appendChild(el('div', 'run-ctx'))
        var cl = cr.appendChild(el('div', 'run-label', t('runCtx')))
        cl.title = t('runCtxSub')
        var chips = cr.appendChild(el('div', 'run-chips'))
        ;(p.model.ctxs || [16384, 32768, 65536]).forEach(function (c) {
          var b = chips.appendChild(el('button', c === p.ctx ? 'on' : '', ctxLabel(c)))
          b.title = c.toLocaleString() + ' ' + t('tokens')
          b.onclick = function () { save('ctx', c); drawLaunch() }
        })
      }
      optionsBox(body, s, p)
    }
    // the one main action
    var foot = body.appendChild(el('div', 'run-foot'))
    jobBox = foot.appendChild(el('div', 'run-job'))
    drawJob()
    var same = builtin && s.running && p.model && p.model.loaded && (!lo.ctx || lo.ctx === p.ctx)
    var go = foot.appendChild(el('button', 'btn run-go', same ? '✓ ' + t('runActive') : s.running ? t('runRestart') : t('runStart')))
    go.disabled = busy || same || !w0 || (builtin && !p.model)
    go.title = !w0 ? t('wayNone') : builtin ? '' : w0.type === 'preset' ? t('wayPresetHint') : t('runScriptHint')
    go.onclick = function () {
      launchAct({ do: 'start', kind: s.kind, way: p.way, model: builtin && p.model ? p.model.id : null, ctx: builtin ? p.ctx : null, opts: builtin ? p.opts : null })
    }
  }

  // ── Models / Connection / Answers: three tabs over the same settings (appsettings) ──
  // Models: what the agent uses now + Run a model on this PC. Connection: servers found (one click), an address,
  // a cloud API with its key, the recent ones. Answers: sampling and the default effort.
  Object.assign(TEXT, {
    connTab: ['Подключение', 'Connection'],
    followed: ['{old} не отвечает, а {new} запущен — агент переключился на {new}', '{old} does not answer and {new} runs: the agent switched to {new}'],
    nowHead: ['Агент сейчас работает с', 'The agent works with'],
    nowOff: ['не отвечает', 'does not answer'],
    nowChange: ['Сменить подключение', 'Change connection'],
    connIntro: ['Куда агент отправляет запросы: сервер на этом компьютере, другой компьютер в сети или облачный API.',
      'Where the agent sends its requests: a server on this computer, another computer on the network, or a cloud API.'],
    foundHead: ['На этом компьютере', 'On this computer'], foundSub: ['Серверы моделей на этом компьютере. Нажмите «Подключить» — больше ничего не нужно.', 'Model servers on this computer. Press Connect: nothing else is needed.'],
    foundScan: ['Ищу серверы…', 'Looking for servers…'], foundAgain: ['Искать снова', 'Search again'],
    foundNone: ['Ничего не запущено. Запустите модель как обычно — агент подключится сам.', 'Nothing runs. Start a model as usual: the agent connects by itself.'], cloudProvider: ['Провайдер', 'Provider'],
    connect: ['Подключить', 'Connect'], connected: ['Подключено', 'Connected'], startIt: ['Запустить…', 'Start…'],
    addrHead: ['Адрес', 'Address'],
    addrSub: ['Сейчас используется этот. Другой компьютер или свой сервер — впишите IP или адрес в любом виде (192.168.0.10, 192.168.0.10:1234, http://host/v1), порт и /v1 подберутся сами.', 'This one is in use. Another computer or an own server: type an IP or an address in any form (192.168.0.10, 192.168.0.10:1234, http://host/v1); the port and /v1 are found.'],
    addrPh: ['например 192.168.0.10', 'e.g. 192.168.0.10'],
    cloudHead: ['Облачная модель', 'Cloud model'], cloudSub: ['OpenAI, Claude, Gemini, OpenRouter и другие — нужен ключ провайдера.', 'OpenAI, Claude, Gemini, OpenRouter and others: the provider\'s key is needed.'],
    answersIntro: ['Как модель отвечает. Пустое поле — значение по умолчанию для этой модели.', 'How the model answers. An empty field keeps this model\'s default.'],
    runHow: ['Как запускать', 'How to start'],
    otherKind: ['Другое', 'Other'],
    about_strata: ['Быстрый движок для больших MoE-моделей на одной видеокарте', 'A fast engine for big MoE models on one GPU'],
    about_lmstudio: ['Программа с каталогом моделей — самый простой старт', 'An app with a model catalog: the easiest start'],
    about_ollama: ['Маленькая фоновая служба: модели качаются командой ollama pull', 'A small background service: models come with ollama pull'],
    about_llamacpp: ['llama-server и файл .gguf', 'llama-server and a .gguf file'],
    about_other: ['KoboldCpp, vLLM, Jan, text-generation-webui или любой сервер с OpenAI-совместимым API', 'KoboldCpp, vLLM, Jan, text-generation-webui or any server with an OpenAI-compatible API'],
    wayBuiltin: ['Встроенный запуск', 'Builtin start'], wayBuiltinSub: ['Выбрать модель и контекст здесь', 'Pick a model and a context here'],
    wayPresetSub: ['пресет, сохранённый в Strata', 'a preset saved in Strata'], wayPreset: ['пресет', 'preset'],
    wayPresetHint: ['Пресет хранит модель, контекст и все флаги — как вы его сохранили в Strata.', 'The preset keeps the model, the context and every flag, as you saved it in Strata.'],
    wayFound: ['найден', 'found'], wayOwn: ['ваш', 'yours'],
    wayAdd: ['Свой способ запуска', 'Own way to start'], wayAddShort: ['Он стоит не там? Свой способ запуска', 'Somewhere else? Own way to start'],
    wayNew: ['Свой способ запуска', 'Own way to start'], wayEdit: ['Изменить способ запуска', 'Edit the way to start'],
    wayNewSub: ['Ваш .bat / .sh / .ps1 или строка команды — со своими флагами и моделью. Он откроется в своём окне, maxedcode дождётся сервера и подключится.',
      'Your .bat / .sh / .ps1 or a command line, with your own flags and model. It opens in its own window; maxedcode waits for the server and connects.'],
    wayOtherSub: ['Любой сервер с OpenAI-совместимым API: KoboldCpp, vLLM, Jan, text-generation-webui… Укажите, как вы его запускаете, и его адрес.',
      'Any server with an OpenAI-compatible API: KoboldCpp, vLLM, Jan, text-generation-webui… Say how you start it and its address.'],
    wayOtherNone: ['Добавьте, как вы запускаете свой сервер — дальше это одна кнопка.', 'Add how you start your server: from then on it is one button.'],
    wayCmd: ['Команда или файл', 'Command or file'], wayCmdPh: ['например D:\\AI\\start.bat или koboldcpp.exe --config my.kcpps', 'e.g. D:\\AI\\start.bat or koboldcpp.exe --config my.kcpps'],
    wayNameLbl: ['Название', 'Name'], wayName: ['например «Мой запуск 64K»', 'e.g. "My 64K start"'],
    wayBase: ['Адрес сервера', 'Server address'], wayBaseSub: ['Пусто — обычный адрес этой программы.', 'Empty: this program\'s usual address.'],
    wayBaseOther: ['Где сервер отвечает, например 127.0.0.1:5001 (KoboldCpp), :8000 (vLLM), :1337 (Jan).', 'Where the server answers, e.g. 127.0.0.1:5001 (KoboldCpp), :8000 (vLLM), :1337 (Jan).'],
    wayNone: ['Выберите способ запуска', 'Pick a way to start'],
    runMore: ['Дополнительно:', 'More settings:'], runChanged: ['изменено', 'changed'], runReset: ['Сбросить к обычным', 'Back to the defaults'], runHowScript: ['Ваш скрипт', 'Your script'], runHowModel: ['Модель и контекст', 'Model and context'],
    runScriptHint: ['Скрипт сам выбирает модель, контекст и флаги. Он откроется в своём окне, maxedcode дождётся сервера и сам подключится.',
      'The script picks the model, the context and the flags. It opens in its own window; maxedcode waits for the server and connects.'],
    runScriptsNone: ['Своих скриптов пока нет', 'No own scripts yet'], runAddScript: ['Добавить свой файл…', 'Add an own file…'],
    runYours: ['ваш запуск', 'your start'], runBig: ['Большая модель: первый запуск может занять несколько минут.', 'A big model: the first start can take a few minutes.'],
  })
  function renderModelTabs() { renderConn(); renderAnswers() }
  // a form over state.settings.values: drafts kept across redraws, one Save button per tab
  var draft = {}
  function formKit(data, rerender) {
    var saveBtn = el('button', 'btn', t('save')), status = el('span', 'muted', '')
    function saved(k) { var v = data.values[k]; return v === null || v === undefined ? '' : String(v) }
    function value(k) { return Object.prototype.hasOwnProperty.call(draft, k) ? draft[k] : saved(k) }
    function change(k, v) {
      if (v === saved(k)) delete draft[k]
      else draft[k] = v
      saveBtn.disabled = !Object.keys(draft).length
      status.textContent = ''
    }
    function input(k, placeholder) {
      var i = inputEl(placeholder === undefined || placeholder === null ? '' : String(placeholder), value(k))
      i.oninput = function () { change(k, i.value) }
      return i
    }
    function saveRow(tab, note) {
      var actions = tab.appendChild(el('div', 'save-row'))
      saveBtn.disabled = !Object.keys(draft).length
      status.textContent = note || ''
      saveBtn.onclick = function () {
        var body = {}
        Object.keys(draft).forEach(function (k) { body[k] = draft[k] === '' && k !== 'memory' ? null : draft[k] })
        saveBtn.disabled = true
        saveBtn.textContent = t('saving')
        api('/api/settings', body).then(function (d) {
          if (d.error) { saveBtn.textContent = t('save'); saveBtn.disabled = false; status.textContent = d.error; return }
          draft = {}
          gotSettings(d)
          rerender(t('saved'))
          renderMemory()
        })
      }
      actions.appendChild(saveBtn)
      actions.appendChild(status)
    }
    return { value: value, change: change, input: input, saveRow: saveRow, status: status }
  }
  // "The agent works with: LM Studio · twil-8b · 32K" — on Models and Connection, the first thing to see
  function connNow(tab, withChange) {
    var data = state.settings, server = data.server || {}, eff = data.effective || {}
    var card = tab.appendChild(el('div', 'conn-now' + (server.online ? ' ok' : ' off')))
    card.appendChild(el('span', 'state-dot ' + (server.online ? 'ok' : 'err')))
    var info = card.appendChild(el('div', 'conn-now-info'))
    var what = [KIND_LABELS[server.kind] || t('localServer'), eff.model && eff.model !== 'local' ? shortModel(eff.model) : '',
      server.online && server.ctx ? ctxLabel(server.ctx) : ''].filter(Boolean).join(' · ')
    info.appendChild(el('div', 'conn-now-main', what))
    info.appendChild(el('div', 'conn-now-sub', String(eff.base_url || '').replace(/^https?:[/][/]/, '') + (server.online ? '' : ' · ' + t('nowOff'))))
    if (withChange) card.appendChild(smallBtn(t('nowChange'), function () { openSection('conn') }, 'outline'))
  }
  function connectTo(x, b) {
    var models = x.models || []
    var body = { base_url: x.base, server: x.kind || null, model: null }
    // LM Studio / Ollama list every downloaded model: pin the loaded one (another id makes them load it)
    if ((x.kind === 'lmstudio' || x.kind === 'ollama') && (x.loaded || models.length > 1)) body.model = x.loaded || models[0]
    if (b) b.disabled = true
    return api('/api/settings', body).then(function (d) {
      if (d.error) { if (b) b.disabled = false; toast(d.error, 4000); return }
      gotSettings(d)
      renderModelTabs()
      toast(t('connected') + ': ' + (x.label || x.base), 2500)
    })
  }

  var foundServers = null
  function renderConn() {
    var tab = $('tab-conn'), data = state.settings
    if (!tab) return
    sectionHead(tab, t('connTab'), '')
    if (!data) { tab.appendChild(el('p', 'muted', t('loading'))); return }
    if (data.error) { tab.appendChild(el('div', 'error-box', data.error)); return }
    var server = data.server || {}, eff = data.effective || {}, kit = formKit(data, function () { renderModelTabs() })
    connNow(tab, false)
    // servers on this PC: found when the tab is drawn, connected with one click
    var g = tab.appendChild(el('div', 'set-group-row'))
    g.appendChild(el('div', 'set-group', t('foundHead')))
    g.appendChild(smallBtn(t('foundAgain'), function () { foundServers = null; renderConn() }, 'link'))
    var list = tab.appendChild(el('div', 'set-card found-list'))
    function draw() {
      list.innerHTML = ''
      if (!foundServers) { var w = list.appendChild(el('div', 'run-empty')); w.appendChild(el('span', 'spin')); w.appendChild(el('span', 'muted', t('foundScan'))); return }
      var any = false
      foundServers.forEach(function (x) {
        if (!x.running) return
        any = true
        var cur = String(eff.base_url || '').replace(/\/+$/, '').toLowerCase() === String(x.base || '').replace(/\/+$/, '').toLowerCase()
        var r = list.appendChild(el('div', 'found-row'))
        r.appendChild(el('span', 'state-dot ok'))
        var info = r.appendChild(el('div', 'found-info'))
        info.appendChild(el('div', 'found-name', x.label))
        var models = x.models || []
        info.appendChild(el('div', 'found-sub', [x.loaded || (models.length === 1 ? models[0] : models.length ? models.length + ' ' + t('setupModels') : ''),
          x.ctx ? ctxLabel(x.ctx) : '', String(x.base || '').replace(/^https?:[/][/]/, '')].filter(Boolean).join(' · ')))
        if (cur) r.appendChild(el('span', 'tag ok', '✓ ' + t('connected')))
        else { var b = r.appendChild(smallBtn(t('connect'), function () { connectTo(x, b) })) }
      })
      if (!any) list.appendChild(el('div', 'run-none', t('foundNone')))
    }
    draw()
    if (!foundServers) api('/api/servers').then(function (r) { foundServers = (r && r.servers) || []; draw() }, function () { foundServers = []; draw() })
    // an address in any form
    tab.appendChild(el('div', 'set-group', t('addrHead')))
    var addr = inputEl(t('addrPh'), String(eff.base_url || '').replace(/^https?:[/][/]/, '').replace(/[/]v1[/]?$/, ''))
    addr.title = t('addrSub')
    var go = smallBtn(t('connect'), function () {
      var v = addr.value.trim()
      if (!v) return addr.focus()
      go.disabled = true
      api('/api/settings', { base_url: v, server: null, model: null }).then(function (d) {
        go.disabled = false
        if (d.error) return toast(d.error, 4000)
        gotSettings(d)
        renderModelTabs()
        toast((d.server && d.server.online ? t('connected') : t('offline')) + ': ' + (d.effective || {}).base_url, 3000)
      })
    })
    addr.onkeydown = function (e) { if (e.key === 'Enter') go.onclick() }
    addRow(tab, [addr, go])
    // the recent ones: one click back
    var recent = server.recent || []
    if (recent.length) {
      tab.appendChild(el('div', 'set-group', t('recentServers')))
      var rbox = tab.appendChild(el('div', 'set-card'))
      recent.forEach(function (x) {
        var b = smallBtn(x.cloud ? t('apiKey') + '…' : t('connect'), function () {
          if (x.cloud) { kit.change('base_url', x.base_url); if (x.model) kit.change('model', x.model); renderConn(); return }
          b.disabled = true
          api('/api/settings', { base_url: x.base_url, server: x.server || null, model: x.model || null }).then(function (d) {
            if (d.error) { b.disabled = false; return toast(d.error, 4000) }
            gotSettings(d); renderModelTabs()
          })
        }, 'outline')
        listRow(rbox, x.label || x.base_url, x.base_url.replace(/^https?:[/][/]/, '') + (x.model ? ' · ' + x.model : ''), b)
      })
    }
    // a cloud API: provider, key, model
    tab.appendChild(el('div', 'set-group', t('cloudHead')))
    var rows = tab.appendChild(el('div', 'set-rows flat'))
    var clouds = server.clouds || [], shownUrl = String(kit.value('base_url') || eff.base_url || '').replace(/\/+$/, '').toLowerCase()
    var nowCloud = (clouds.filter(function (c) { return c[1].toLowerCase() === shownUrl })[0] || [])[1] || ''
    var keyInput = inputEl(server.has_key ? t('keySaved') + (server.key_end ? ' …' + server.key_end : '') : t('keyNone'), '')
    keyInput.type = 'password'
    keyInput.autocomplete = 'off'
    keyInput.oninput = function () { kit.change('api_key', keyInput.value) }    // emptied again: nothing changes
    rows.appendChild(row(t('cloudProvider'), '', pick([['', '—']].concat(clouds.map(function (c) { return [c[1], c[0]] })), nowCloud, function (v) {
      kit.change('base_url', v || String(eff.base_url || ''))
      renderConn()
    })))
    rows.appendChild(tipRow(t('apiKey'), t('apiKeySub'), keyInput))
    if (nowCloud) {
      var models = (server.models || []).slice()
      if (data.values.model && models.indexOf(data.values.model) < 0) models.push(data.values.model)
      rows.appendChild(tipRow(t('model'), t('modelCloud'), pick([['', t('auto') + (eff.model ? ' (' + eff.model + ')' : '')]].concat(models.map(function (m) { return [m, m] })),
        kit.value('model'), function (v) { kit.change('model', v) })))
    }
    kit.saveRow(tab)
  }

  function renderAnswers() {
    var tab = $('tab-answers'), data = state.settings
    if (!tab) return
    sectionHead(tab, t('answers'), '')
    if (!data) { tab.appendChild(el('p', 'muted', t('loading'))); return }
    if (data.error) { tab.appendChild(el('div', 'error-box', data.error)); return }
    var eff = data.effective || {}, kit = formKit(data, function () { renderAnswers() })
    var ans = tab.appendChild(el('div', 'set-rows flat'))
    ans.appendChild(tipRow(t('effortDefault'), t('effortSub'), pick([['', t('byDefault') + ' (' + (eff.effort || 'medium') + ')']].concat(EFFORTS.map(function (e) { return [e, e] })),
      kit.value('effort'), function (v) { kit.change('effort', v) })))
    ans.appendChild(tipRow(t('temperature'), t('temperatureSub'), kit.input('temperature', eff.temperature)))
    // the rest of the CLI's /sampling: empty = what model_settings / the defaults give
    ans.appendChild(tipRow('top_p', t('topPSub'), kit.input('top_p', eff.top_p)))
    ans.appendChild(tipRow('top_k', t('topKSub'), kit.input('top_k', eff.top_k)))
    ans.appendChild(tipRow('min_p', t('minPSub'), kit.input('min_p', eff.min_p)))
    kit.saveRow(tab)
  }

  // ── Memory: the shared MEMORY.md (every CLI and window session) and the memory files of the chat's folder ──
  var memDraft = null
  function renderMemory(note) {
    var tab = $('tab-memory'), data = state.settings, d = cliData
    if (!tab) return
    sectionHead(tab, t('secMemory'), t('memoryIntro'))
    if (note) tab.appendChild(el('div', 'error-box', note))
    if (data && !data.error) {
      var mem = el('textarea', 'field mem-text')
      mem.value = memDraft !== null ? memDraft : (data.values.memory || '')
      mem.spellcheck = false
      var saveBtn = el('button', 'btn', t('save')), status = el('span', 'muted', '')
      saveBtn.disabled = memDraft === null
      mem.oninput = function () { memDraft = mem.value; saveBtn.disabled = false; status.textContent = '' }
      saveBtn.onclick = function () {
        saveBtn.disabled = true
        api('/api/settings', { memory: memDraft || '' }).then(function (r) {
          if (r.error) { status.textContent = r.error; saveBtn.disabled = false; return }
          memDraft = null
          gotSettings(r)
          renderMemory()
        })
      }
      tab.appendChild(el('div', 'set-group', t('memory')))
      tab.appendChild(el('p', 'set-hint', t('memorySub')))
      tab.appendChild(mem)
      var sr = el('div', 'save-row')
      sr.appendChild(saveBtn)
      sr.appendChild(status)
      tab.appendChild(sr)
    }
    if (!d) return
    var box = group(tab, t('memHead'), t('memSub'))
    ;(d.memory || []).forEach(function (f) {
      var r = listRow(box, f.path, f.label + ' · ' + f.lines + ' ' + t('linesWord'))
      r.classList.add('link')
      r.onclick = function () { $('modal').hidden = true; openPreview(f.path) }
    })
    if (!(d.memory || []).length) listRow(box, t('none'), '')
    var noteIn = inputEl(t('notePh'))
    addRow(tab, [noteIn, smallBtn(t('saveNote'), function () { if (noteIn.value.trim()) cliAct({ do: 'note', text: noteIn.value.trim() }) })])
  }

  // ── the CLI-backed sections (tools, MCP, hooks, permissions): the same files the terminal's slash commands edit ──
  function cliAct(body) {
    body.cwd = chatFolder()
    api('/api/cli', body).then(function (r) {
      if (r.error) return renderCli(r.error)
      cliData = r
      renderCli()
    })
  }
  var toolsTab = 'skills'
  function renderCli(note) {
    renderTools(note)
    renderMcp(note)
    renderHooks(note)
    renderPerms(note)
    renderMemory()
  }
  function cliHead(tab, title, intro, note) {
    sectionHead(tab, title, intro)
    if (!cliData) { tab.appendChild(el('p', 'muted', t('loading'))); return false }
    if (note) tab.appendChild(el('div', 'error-box', note))
    return true
  }
  function renderTools(note) {
    var tab = $('tab-cli'), d = cliData
    if (!cliHead(tab, t('secTools'), t('toolsIntro'), note)) return
    var tabsDef = [['skills', t('skillsHead'), t('skillsSub'), d.skills, function (x) { return '/' + x.name }],
      ['commands', t('commandsHead'), t('commandsSub'), d.commands, function (x) { return '/' + x.name }],
      ['agents', t('agentsHead'), t('agentsSub'), d.agents, function (x) { return x.name }],
      ['tools', t('toolsHead'), t('toolsSub'), d.tools, function (x) { return x.name }]]
    var bar = el('div', 'set-tabs')
    tab.appendChild(bar)
    var body = el('div', '')
    tab.appendChild(body)
    function show(key) {
      toolsTab = key
      Array.prototype.forEach.call(bar.children, function (b) { b.classList.toggle('on', b.dataset.k === key) })
      body.innerHTML = ''
      var g = tabsDef.filter(function (x) { return x[0] === key })[0]
      body.appendChild(el('p', 'set-hint', g[2]))
      var box = el('div', 'set-card')
      body.appendChild(box)
      ;(g[3] || []).forEach(function (x) {
        var r = listRow(box, g[4](x), clipText(x.description || x.command || '', 160), null, x.path || '')
        if (x.path) { r.classList.add('link'); r.onclick = function () { $('modal').hidden = true; openPreview(x.path) } }
      })
      if (!(g[3] || []).length) listRow(box, t('none'), '')
    }
    tabsDef.forEach(function (g) {
      var b = el('button', '', splitCmd(g[1])[0])
      if ((g[3] || []).length) b.appendChild(el('span', 'count', String(g[3].length)))
      b.dataset.k = g[0]
      b.onclick = function () { show(g[0]) }
      bar.appendChild(b)
    })
    show(toolsTab)
    filesNote(tab, ['cwd: ' + d.cwd])
  }
  function renderMcp(note) {
    var tab = $('tab-mcp'), d = cliData
    if (!tab || !cliHead(tab, t('mcpHead'), t('mcpSub'), note)) return
    var box = el('div', 'set-card')
    tab.appendChild(box)
    ;(d.mcp || []).forEach(function (x) {
      listRow(box, x.name, x.url || x.command, iconBtn('x', t('remove'), function () { cliAct({ do: 'mcp-remove', name: x.name }) }))
    })
    if (!(d.mcp || []).length) listRow(box, t('none'), '')
    var mName = inputEl(t('mcpName'), '', 'short'), mCmd = inputEl(t('mcpCmd'))
    addRow(tab, [mName, mCmd, smallBtn(t('add'), function () {
      if (mName.value.trim() && mCmd.value.trim()) cliAct({ do: 'mcp-add', name: mName.value.trim(), command: mCmd.value.trim() })
    })])
    filesNote(tab, [d.files && d.files.mcp])
  }
  function renderHooks(note) {
    var tab = $('tab-hooks'), d = cliData
    if (!tab || !cliHead(tab, t('hooksHead'), t('hooksSub'), note)) return
    var box = el('div', 'set-card')
    tab.appendChild(box)
    ;(d.hooks || []).forEach(function (x) {
      listRow(box, x.event + (x.matcher ? ' · ' + x.matcher : ''), clipText(x.command || x.description || '', 160), null, x.path || '')
    })
    if (!(d.hooks || []).length) listRow(box, t('none'), '')
    filesNote(tab, [d.files && d.files.settings])
  }
  function renderPerms(note) {
    var tab = $('tab-perms'), d = cliData
    if (!tab || !cliHead(tab, t('rulesHead'), t('rulesSub'), note)) return
    ;['allow', 'deny', 'ask'].forEach(function (k) {
      var box = group(tab, t('rule_' + k))
      var any = false
      ;(d.rules[k] || []).forEach(function (r) {
        any = true
        var x = r.own ? iconBtn('x', t('remove'), function () { cliAct({ do: 'rule-remove', kind: k, rule: r.rule }) }) : el('span', 'set-item-sub', t('fromSettings'))
        listRow(box, clipText(r.rule.replace(/\\(.)/g, '$1'), 140), '', x, r.rule)
      })
      if (!any) listRow(box, t('none'), '')
    })
    var kindPick = 'allow', ruleIn = inputEl(t('rulePh'))
    addRow(tab, [pick([['allow', 'allow'], ['deny', 'deny'], ['ask', 'ask']], kindPick, function (v) { kindPick = v }), ruleIn,
      smallBtn(t('add'), function () { if (ruleIn.value.trim()) cliAct({ do: 'rule-add', kind: kindPick, rule: ruleIn.value.trim() }) })])
    filesNote(tab, [d.files && d.files.rules])
  }
  function renderBrowser() {
    var tab = $('tab-browser')
    if (!tab) return
    tab.innerHTML = ''              // browserSection draws its own title (brHead) and intro
    browserSection(tab)
  }
  // Settings → maxedcode → Browser: the user's own Chrome/Edge through the extension (maxedcode/chrome)
  function browserSection(tab) {
    var box = el('div', 'browser-set')
    tab.appendChild(box)
    function draw(s) {
      box.innerHTML = ''
      box.appendChild(el('h3', '', t('brHead')))
      box.appendChild(el('p', 'muted small', t('brIntro')))
      if (!s) { box.appendChild(el('p', 'muted', t('loading'))); return }
      if (s.error) { box.appendChild(el('div', 'error-box', s.error)); return }
      box.appendChild(row(t('brToggle'), t('brToggleSub'), toggle(s.on, function (v) {
        draw(null)
        api('/api/browser', { do: v ? 'on' : 'off' }).then(draw)
      })))
      if (!s.on) return
      if (s.connected && s.connected.length) {
        box.appendChild(el('div', 'br-state ok', t('brConnected') + ': ' + s.connected.join(', ')))
        return
      }
      box.appendChild(el('div', 'br-state', t('brWaiting')))
      var guide = el('div', 'pick-row br-guide')
      guide.appendChild(button(t('brGuide'), function () { api('/api/browser', { do: 'guide' }) }))
      guide.appendChild(el('span', 'muted small', t('brGuideSub')))
      box.appendChild(guide)
      var steps = el('ol', 'br-steps')
      var one = el('li', '', t('brStep1') + ' ')
      one.appendChild(el('code', '', 'chrome://extensions'))
      one.appendChild(document.createTextNode(' ' + t('brStep1b')))
      steps.appendChild(one)
      steps.appendChild(el('li', '', t('brStep2')))
      var three = el('li', '', t('brStep3'))
      three.appendChild(button(t('brReveal'), function () { api('/api/browser', { do: 'reveal' }) }))
      three.appendChild(el('div', 'br-or', t('brStep3b')))
      var path = el('div', 'pick-row')
      path.appendChild(el('span', 'path-val', s.extension))
      var copyBtn = button(t('brCopy'), function () { copy(s.extension) ; copyBtn.textContent = '✓ ' + t('brCopy') })
      path.appendChild(copyBtn)
      three.appendChild(path)
      steps.appendChild(three)
      var four = el('li', '', t('brStep4') + ' ')
      four.appendChild(button(t('brCheck'), function () { draw(null); api('/api/browser').then(draw) }))
      steps.appendChild(four)
      box.appendChild(steps)
    }
    draw(null)
    api('/api/browser').then(draw)
  }
  function gotSettings(d) {
    state.settings = d
    if (d && d.server && d.server.online) state.ctx = d.server.ctx
    renderSeats()
    renderStatus()
    // the server does not answer: look once whether another one runs on this PC (autoFollow)
    if (d && d.server && !d.server.online && !d.setup_needed) { if (launchData) autoFollow(); else loadLaunch() }
    if (d && d.setup_needed && !setupShown) openSetup()
  }
  // ── first start (a git clone opened as the window): which model server to use, found on this PC ──
  var setupShown = false
  function openSetup() {
    setupShown = true
    var box = $('setup')
    if (!box) {
      box = document.body.appendChild(el('div', 'modal'))
      box.id = 'setup'
      // a click on the dimmed backdrop closes it, like Settings
      box.addEventListener('mousedown', function (e) { if (e.target === box) box.hidden = true })
    }
    box.hidden = false
    box.innerHTML = ''
    var card = box.appendChild(el('div', 'setup-card'))
    card.appendChild(iconBtn('x', '', function () { box.hidden = true })).classList.add('close')
    card.appendChild(el('h2', '', t('setupTitle')))
    card.appendChild(el('p', 'intro', t('setupIntro')))
    var recent = ((state.settings && state.settings.server) || {}).recent || [], recentBtn = null
    if (recent.length) {
      card.appendChild(el('div', 'setup-group', t('setupRecent')))
      var rlist = card.appendChild(el('div', 'cli-list'))
      recent.forEach(function (x) {
        var r = rlist.appendChild(el('div', 'cli-row setup-server'))
        var info = r.appendChild(el('div', 'setup-info'))
        var top = info.appendChild(el('div', 'setup-top'))
        top.appendChild(el('span', 'm', x.label || x.base_url))
        top.appendChild(el('span', 's', x.base_url + (x.model ? ' · ' + x.model : '')))
        var b = r.appendChild(el('div', 'setup-acts')).appendChild(el('button', 'btn small outline', t('setupUse')))
        if (!recentBtn && !x.cloud) recentBtn = b
        b.onclick = function () {
          if (!x.cloud) return save({ base: x.base_url, kind: x.server }, [], x.model)
          // a cloud API: its key is not kept, so Settings -> Models opens with the address filled in
          box.hidden = true
          draft = { base_url: x.base_url }
          if (x.model) draft.model = x.model
          openSection('conn')
          setTimeout(function () { var k = document.querySelector('#tab-conn input[type=password]'); if (k) k.focus() }, 50)
        }
      })
      card.appendChild(el('div', 'setup-group', t('setupFound')))
    }
    var list = card.appendChild(el('div', 'cli-list'))
    var note = card.appendChild(el('div', 'muted small', t('setupScan')))
    function save(x, models, model) {
      var body = { base_url: x.base, server: x.kind || null }
      var many = (models || []).length > 1
      // LM Studio / Ollama list every downloaded model: pin the loaded one (another id makes them load it)
      if (many && (x.kind === 'lmstudio' || x.kind === 'ollama')) body.model = x.loaded || models[0]
      if (model) body.model = model
      api('/api/settings', body).then(function (d) {
        if (d.error) { note.textContent = d.error; return }
        box.hidden = true
        gotSettings(d)
      })
    }
    function draw(found) {
      list.innerHTML = ''
      note.textContent = found.length ? '' : t('setupNone')
      // one main action (filled): the first server running on this PC, else (none running) the last one used,
      // else the first one found. Any server can be chosen without starting it ("Use" keeps the address);
      // starting is a separate, quieter action.
      var main = found.filter(function (x) { return x.running })[0] || (recent.length ? null : found[0])
      if (recentBtn) recentBtn.className = 'btn small' + (main ? ' outline' : '')
      found.forEach(function (x) {
        var models = x.models || []
        var sub = x.running ? t('setupRunning') + ': ' + (x.loaded || (models.length === 1 ? models[0] : models.length + ' ' + t('setupModels'))) + (x.ctx ? ' · ' + x.ctx + ' ' + t('tokens') : '')
          : t('setupInstalled')
        var r = list.appendChild(el('div', 'cli-row setup-server'))
        var info = r.appendChild(el('div', 'setup-info'))
        var top = info.appendChild(el('div', 'setup-top'))
        top.appendChild(el('span', 'm', x.label))
        top.appendChild(el('span', 's', sub + ' · ' + x.base))
        // exactly what runs: the start command of an installed server, the program folder of a running one
        var starts = x.start && x.start.length, run = starts ? x.start.join(' ') : x.path || ''
        if (run) info.appendChild(el('div', 'setup-path', (starts ? t('setupRuns') : t('setupFrom')) + ': ' + run)).title = run
        var acts = r.appendChild(el('div', 'setup-acts')), use
        if (!x.running && starts) {
          var go = acts.appendChild(el('button', 'btn small link', t('setupStart')))
          go.title = t('setupStartHint')
          go.onclick = function () {
            go.disabled = use.disabled = true
            go.textContent = t('setupStarting')
            var failed = function () {
              go.disabled = use.disabled = false
              go.textContent = t('setupStart')
              note.textContent = t('setupNoStart') + (x.how || '')
            }
            api('/api/servers', { start: x.kind }).then(function (r) {
              if (r && r.base) { x.base = r.base; return save(x, []) }
              failed()
            }, failed)        // a dropped request too: the button no longer stays "Starting…"
          }
        }
        use = acts.appendChild(el('button', 'btn small' + (x === main ? '' : ' outline'), t('setupUse')))
        if (!x.running) use.title = t('setupUseHint') + (x.how ? ' (' + x.how + ')' : '')
        use.onclick = function () { save(x, models) }
      })
    }
    var add = card.appendChild(el('div', 'cli-add')), url = el('input', 'field')
    url.placeholder = t('setupUrl')
    url.spellcheck = false
    var go = el('button', 'btn small outline', t('setupUse'))
    go.onclick = function () {
      var v = url.value.trim()
      if (!v) return
      if (!/^https?:\/\//i.test(v)) v = 'http://' + v
      if (!/\/v1\/?$/.test(v) && !/:\/\/[^/]+\/./.test(v)) v = v.replace(/\/$/, '') + '/v1'
      save({ base: v, kind: '' }, [])
    }
    add.appendChild(url)
    add.appendChild(go)
    var actions = card.appendChild(el('div', 'save-row'))
    var again = el('button', 'btn small link', t('setupAgain'))
    again.onclick = scan
    actions.appendChild(again)
    function scan() {
      note.textContent = t('setupScan')
      api('/api/servers').then(function (r) { draw((r && r.servers) || []) }, function () { draw([]) })
    }
    scan()
  }
  var SECTIONS = ['general', 'chat', 'conn', 'launch', 'answers', 'cloud', 'cli', 'mcp', 'hooks', 'perms', 'memory', 'browser']
  function openSettings() {
    $('modal').hidden = false
    renderGeneral()
    renderChat()
    renderModelTabs()
    renderCli()
    renderBrowser()
    api('/api/settings').then(function (d) { gotSettings(d); renderModelTabs() })
    loadCli().then(function () { renderCli() })
    renderCloud()
    api('/api/cloud').then(function (d) { cloudData = d; renderCloud() })
  }
  // ── Settings -> Cloud: the same accounts, keys (DPAPI) and model notes as /cloud in the CLI; one card per key,
  // as the provider cards of DeepSeek Harness (name, a green/red key dot, actions on the right) ──
  var cloudData = null, cloudAdding = false
  function renderCloud(note, ok) {
    var tab = $('tab-cloud'), d = cloudData
    sectionHead(tab, t('cloudTab'), t('cloudIntro'))
    if (!d) { tab.appendChild(el('p', 'muted', t('loading'))); return }
    if (note || d.error) tab.appendChild(el('div', ok ? 'ok-box' : 'error-box', note || d.error))
    function act(body, busyBtn) {
      if (busyBtn) { busyBtn.disabled = true }
      api('/api/cloud', body).then(function (r) {
        cloudData = r
        if (!r.error && body.do === 'add') cloudAdding = false
        renderCloud(r.error || r.test || '', !r.error && !!r.test)
      })
    }
    var list = el('div', 'set-providers')
    tab.appendChild(list)
    ;(d.accounts || []).forEach(function (a, i) {
      var card = el('div', 'set-provider')
      var head = el('div', 'set-provider-head')
      head.appendChild(el('span', 'set-provider-name', a.label))
      if (i === 0) head.appendChild(el('span', 'tag', t('cloudDefaultTag')))
      head.appendChild(el('span', 'state-dot ' + (a.has_key ? 'ok' : 'err')))
      head.appendChild(el('span', 'set-item-sub', a.has_key ? t('keyOk') : t('cloudNoKey')))
      var acts = el('span', 'set-provider-acts')
      if (i > 0) acts.appendChild(smallBtn(t('cloudMakeDefault'), function (e) { act({ do: 'default', id: a.id }, e.target) }, 'outline'))
      acts.appendChild(smallBtn(t('cloudTest'), function (e) { e.target.textContent = '…'; act({ do: 'test', id: a.id }, e.target) }, 'outline'))
      acts.appendChild(smallBtn(t('remove'), function () { if (confirm(t('cloudRemoveAsk') + ' ' + a.label + '?')) act({ do: 'remove', id: a.id }) }, 'danger'))
      head.appendChild(acts)
      card.appendChild(head)
      var rows = el('div', 'set-rows')
      rows.appendChild(row(t('cloudModelLbl'), a.base_url, pick(a.models.map(function (m) { return [m, m] }), a.model, function (v) { act({ do: 'default', id: a.id, model: v }) })))
      card.appendChild(rows)
      var meta = el('div', 'set-provider-foot')
      meta.appendChild(el('span', 'set-item-sub', a.models.length + ' ' + t('setupModels')))
      meta.appendChild(smallBtn(t('cloudRefresh'), function (e) { act({ do: 'refresh', id: a.id }, e.target) }, 'link'))
      card.appendChild(meta)
      list.appendChild(card)
    })
    // add a key: a dashed button that opens the add card; the provider comes from the key itself on auto
    if (!cloudAdding) {
      var addBtn = el('button', 'set-add-dashed', '+ ' + t('addProvider'))
      addBtn.onclick = function () { cloudAdding = true; renderCloud() }
      tab.appendChild(addBtn)
    } else {
      var addCard = el('div', 'set-editor')
      tab.appendChild(addCard)
      addCard.appendChild(el('div', 'set-provider-name', t('addProvider')))
      var prov = ''
      var key = inputEl(t('cloudKeyPh')), model = inputEl(t('cloudModelPh'))
      key.type = 'password'
      key.autocomplete = 'off'
      function field(label, node) { var f = el('label', 'set-field'); f.appendChild(el('span', '', label)); f.appendChild(node); addCard.appendChild(f) }
      field(t('cloudProvider'), pick([['', t('cloudAuto')]].concat(Object.keys(d.providers || {}).map(function (p) { return [p, p] })), '', function (v) { prov = v }))
      field(t('cloudKeyLbl'), key)
      field(t('cloudModelLbl'), model)
      addCard.appendChild(el('p', 'set-hint', t('cloudKeySafe')))
      var foot = el('div', 'set-editor-acts')
      foot.appendChild(smallBtn(t('cancel'), function () { cloudAdding = false; renderCloud() }, 'outline'))
      var go = smallBtn(t('add'), function () { if (key.value.trim()) act({ do: 'add', provider: prov, key: key.value.trim(), model: model.value.trim() }, go) })
      foot.appendChild(go)
      addCard.appendChild(foot)
      setTimeout(function () { key.focus() }, 0)
    }
    // options: flat rows as in General
    tab.appendChild(el('div', 'set-group', t('cloudOptions')))
    var opts = el('div', 'set-rows flat')
    tab.appendChild(opts)
    opts.appendChild(row(t('cloudSub'), t('cloudSubSub'), toggle(!!d.allow_subagents, function (v) { act({ do: 'options', allow_subagents: v }) })))
    opts.appendChild(row(t('cloudAutoSend'), t('cloudAutoSendSub'), toggle(!!d.auto_approve, function (v) { act({ do: 'options', auto_approve: v }) })))
    var tok = el('input', 'field num')
    tok.value = d.max_tokens || ''
    tok.onchange = function () { act({ do: 'options', max_tokens: tok.value }) }
    opts.appendChild(row(t('cloudTokens'), t('cloudTokensSub'), tok))
    // what each model is good for: the agent writes these after a web search, the user may edit them
    // only the default models and those with a note by default: a provider may list dozens
    var seen = {}, keep = {}
    ;(d.accounts || []).forEach(function (a) { keep[a.model] = 1; a.models.forEach(function (m) { seen[m] = 1 }) })
    var all = Object.keys(seen).sort(), notes = d.notes || {}
    if (!all.length) return
    var shown = cloudAllNotes ? all : all.filter(function (m) { return keep[m] || notes[m] })
    var nb = group(tab, t('cloudNotes'), t('cloudNotesSub'))
    shown.forEach(function (m) {
      var inp = inputEl(t('cloudNotePh'), notes[m] || '', 'grow')
      inp.onchange = function () { act({ do: 'note', model: m, note: inp.value }) }
      inp.title = notes[m] || ''
      listRow(nb, m, '', inp, m).classList.add('note-row')
    })
    if (shown.length < all.length || cloudAllNotes) {
      var more = smallBtn(cloudAllNotes ? t('showLess') : t('showAll') + ' (' + all.length + ')', function () { cloudAllNotes = !cloudAllNotes; renderCloud() }, 'link')
      tab.appendChild(more)
    }
  }
  var cloudAllNotes = false
  Array.prototype.forEach.call(document.querySelectorAll('.tabs button'), function (b) {
    b.onclick = function () {
      Array.prototype.forEach.call(document.querySelectorAll('.tabs button'), function (x) { x.classList.toggle('active', x === b) })
      SECTIONS.forEach(function (k) { var s = $('tab-' + k); if (s) s.hidden = b.dataset.tab !== k })
      if (b.dataset.tab === 'launch') renderLaunchTab()     // looked up only when it is opened
    }
  })

  // ── files dropped or pasted into the input: uploaded, then the message points at the saved copy, as the old
  // window did ([File "name" (size, sha256:…): verbatim read-only copy saved at "path" …]) ──
  var attached = [], quoted = []
  function renderAttached() {
    var box = $('attached')
    box.innerHTML = ''
    quoted.forEach(function (q, i) { box.appendChild(quoteCard(q, function () { quoted.splice(i, 1); renderAttached() })) })
    attached.forEach(function (f, i) {
      var chip = el('span', 'chip')
      chip.innerHTML = icon('file', 's14')
      chip.appendChild(el('span', '', f.name + ' · ' + kfmt(f.size) + 'B'))
      chip.appendChild(iconBtn('x', '', function () { attached.splice(i, 1); renderAttached() }))
      box.appendChild(chip)
    })
    updateSend()
  }
  function uploadFiles(files) {
    Array.prototype.forEach.call(files, function (file) {
      fetch('/api/upload', { method: 'POST', headers: { 'x-file-name': encodeURIComponent(file.name) }, body: file })
        .then(function (r) { return r.json() })
        .then(function (r) { if (r.error) alert(r.error); else { attached.push(r); renderAttached() } })
    })
  }
  function attachmentNotes() {
    var notes = attached.map(function (f) {
      return '[File "' + f.name + '" (' + f.size + ' bytes, sha256:' + f.sha.slice(0, 8) + '): verbatim read-only copy saved at ' +
        JSON.stringify(f.path) + '. Read that path with your file tools when its contents are needed; copy it to a writable location before modifying it.]'
    })
    var quotes = quoted.map(function (q) { return q.split('\n').map(function (l) { return '> ' + l }).join('\n') + '\n\n' }).join('')
    attached = []
    quoted = []
    renderAttached()
    return quotes + (notes.length ? notes.join('\n') + '\n' : '')
  }
  // Something dropped from Windows anywhere in the window: a folder becomes its path in the message (dropped on the
  // sidebar it opens as a project), a file is attached as before. Only the app window knows the paths (dropPaths).
  var drops = {}
  function dropPaths(files, done) {
    var py = pyApi()
    if (!HOST && py && py.take_drop) {
      // the pywebview window: its Python side got the real paths of this drop (an older pywebview: none)
      return Promise.resolve(py.take_drop(files.length)).then(function (p) { done(p && p.length ? p : null) }, function () { done(null) })
    }
    if (!(HOST && chrome.webview.postMessageWithAdditionalObjects)) return done(null)
    var token = 'd' + Date.now() + Math.random().toString(36).slice(2, 6)
    drops[token] = done
    try { chrome.webview.postMessageWithAdditionalObjects({ dropped: token }, files) } catch (err) { delete drops[token]; done(null) }
  }
  function isDirEntry(item) { try { var en = item.webkitGetAsEntry && item.webkitGetAsEntry(); return !!(en && en.isDirectory) } catch (err) { return false } }
  function dropped(e, toSide) {
    var dt = e.dataTransfer
    if (!dt) return
    var inner = dt.getData('text/x-path')                 // a row of the file panel
    if (inner) { insertAtCursor(mention(inner)); $('input').focus(); autosize(); return }
    if (!dt.files.length) return
    var files = Array.prototype.slice.call(dt.files), dirs = Array.prototype.map.call(dt.items || [], isDirEntry)
    dropPaths(dt.files, function (paths) {
      var mentioned = false
      files.forEach(function (file, i) {
        var p = paths && paths[i], dir = dirs[i]
        if (dir && p && toSide) { setFolder(p); newChat(); renderSide(); return }
        if (dir && p) { insertAtCursor(mention(p)); mentioned = true; return }
        if (dir) return toast(t('dropFolderApp'))
        uploadFiles([file])
      })
      if (mentioned) { $('input').focus(); autosize(); keepDraft() }
    })
  }
  var composer = document.querySelector('.composer')
  document.addEventListener('dragover', function (e) {
    if (!e.dataTransfer || Array.prototype.indexOf.call(e.dataTransfer.types, 'Files') < 0 && Array.prototype.indexOf.call(e.dataTransfer.types, 'text/x-path') < 0) return
    e.preventDefault()                                   // the window never navigates to a dropped file
    var side = $('side').contains(e.target)
    composer.classList.toggle('drop', !side)
    $('side').classList.toggle('drop', side && Array.prototype.indexOf.call(e.dataTransfer.types, 'Files') >= 0)
  })
  document.addEventListener('dragleave', function (e) {
    if (e.relatedTarget) return
    composer.classList.remove('drop')
    $('side').classList.remove('drop')
  })
  document.addEventListener('drop', function (e) {
    composer.classList.remove('drop')
    $('side').classList.remove('drop')
    if (e.defaultPrevented) return                       // a sidebar row took it (a chat moved to a folder)
    e.preventDefault()
    dropped(e, $('side').contains(e.target))
  })
  $('input').addEventListener('paste', function (e) {
    var files = e.clipboardData && e.clipboardData.files
    if (files && files.length) { e.preventDefault(); uploadFiles(files) }
  })

  // ── wiring ──
  function setRail(on) { $('app').classList.toggle('rail-mode', on); setPref('rail', on) }
  $('toggle-side').onclick = function () { setRail(true) }
  document.querySelector('.brand .mark').onclick = function () { if ($('app').classList.contains('rail-mode')) setRail(false) }
  $('new-chat').onclick = newChat
  $('rail-new').onclick = newChat
  $('add-folder').onclick = function (e) { addProjectMenu(e.currentTarget) }
  $('rail-folder').onclick = function (e) { addProjectMenu(e.currentTarget) }
  function openSearch() {
    setRail(false)
    var s = $('search')
    s.hidden = !s.hidden || document.activeElement !== s ? false : true
    if (!s.hidden) s.focus()
    else { s.value = ''; ui.filter = ''; renderSide() }
  }
  $('search-btn').onclick = openSearch
  $('rail-search').onclick = openSearch
  var searchTimer = null
  $('search').oninput = function () {
    ui.filter = $('search').value
    renderSide()
    clearTimeout(searchTimer)
    var q = ui.filter.trim()
    if (q.length < 2) { ui.hits = []; return }
    searchTimer = setTimeout(function () {
      api('/api/search?q=' + encodeURIComponent(q)).then(function (r) {
        if (ui.filter.trim() !== q) return
        ui.hits = Array.isArray(r) ? r : []
        renderSide()
      })
    }, 250)
  }
  $('search').onkeydown = function (e) { if (e.key === 'Escape') { $('search').value = ''; ui.filter = ''; $('search').hidden = true; renderSide() } }
  $('sort-btn').onclick = function () {
    showMenu($('sort-btn'), [
      { label: t('sortRecent'), check: state.sort === 'recent', run: function () { state.sort = 'recent'; setPref('sort', 'recent'); renderSide() } },
      { label: t('sortName'), check: state.sort === 'name', run: function () { state.sort = 'name'; setPref('sort', 'name'); renderSide() } },
      '-',
      { label: t('showArchived'), icon: 'archive', check: ui.showArchived, run: function () { ui.showArchived = !ui.showArchived; renderSide() } },
    ], { align: 'below' })
  }
  $('open-settings').onclick = openSettings
  $('close-settings').onclick = function () { $('modal').hidden = true }
  $('modal').addEventListener('mousedown', function (e) { if (e.target === $('modal')) $('modal').hidden = true })
  $('send').onclick = function () { if (state.chat && state.chat.run && !$('input').value.trim()) stop(); else submit() }
  $('attach').onclick = attachMenu
  $('mode-btn').onclick = modeMenu
  $('model-btn').onclick = modelMenu
  $('hero-folder').onclick = function () { folderMenu($('hero-folder')) }
  $('chat-folder').onclick = chatFolderMenu
  $('changes-btn').onclick = openChanges
  $('open-default').onclick = function () { api('/api/open', { path: chatFolder(), app: 'explorer' }) }
  $('open-menu').onclick = openMenu
  $('more-btn').onclick = moreMenu
  $('panel-btn').onclick = function () { togglePanel() }
  $('panel-close').onclick = function () { togglePanel(false) }
  Array.prototype.forEach.call($('views').children, function (b) { b.onclick = function () { setView(b.dataset.view) } })
  $('input').addEventListener('input', function () { autosize(); inputMenus() })
  $('input').addEventListener('keydown', function (e) {
    if (!$('menu').hidden && menuState.keyboard && menuState.items.length) {
      if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
        e.preventDefault()
        menuState.sel = (menuState.sel + (e.key === 'ArrowDown' ? 1 : -1) + menuState.items.length) % menuState.items.length
        return markSel()
      }
      if ((e.key === 'Enter' || e.key === 'Tab') && menuState.sel >= 0) { e.preventDefault(); menuState.items[menuState.sel].click(); return }
      if (e.key === 'Escape') { e.preventDefault(); hideMenus(); return }
    }
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); submit() }
  })
  document.addEventListener('keydown', function (e) {
    if (e.ctrlKey && e.altKey && e.code === 'KeyN') { e.preventDefault(); newChat() }
    else if (e.ctrlKey && e.shiftKey && e.code === 'KeyB') { e.preventDefault(); togglePanel() }
    else if (e.ctrlKey && e.altKey && e.code === 'KeyP') { e.preventDefault(); openFiles() }
    else if (e.ctrlKey && e.code === 'Backquote') { e.preventDefault(); openTerminal() }
    else if (e.ctrlKey && e.altKey && e.code === 'KeyT') { e.preventDefault(); openBrowser() }
    else if (e.ctrlKey && e.altKey && e.code === 'KeyK') { e.preventDefault(); openSearch() }
    else if (e.ctrlKey && e.altKey && e.code === 'KeyB') { e.preventDefault(); setRail(!$('app').classList.contains('rail-mode')) }
    else if (e.ctrlKey && e.altKey && e.code === 'KeyG') { e.preventDefault(); renameCurrent() }
    else if (e.ctrlKey && e.shiftKey && e.code === 'KeyA') { e.preventDefault(); var cs = currentSession(); if (cs) archiveChat(cs, !cs.archived) }
    else if (e.ctrlKey && ((e.altKey && e.code === 'Comma') || e.code === 'Slash')) { e.preventDefault(); openSettings() }
    else if (e.ctrlKey && !e.altKey && (e.key === '=' || e.key === '+')) { e.preventDefault(); fontStep(1) }
    else if (e.ctrlKey && !e.altKey && e.key === '-') { e.preventDefault(); fontStep(-1) }
    else if (e.key === 'Escape') {
      if ($('setup') && !$('setup').hidden) $('setup').hidden = true
      else if (!$('modal').hidden) $('modal').hidden = true
      else if (!$('menu').hidden) hideMenus()
      else if (state.chat && state.chat.run) {
        if (Date.now() - lastEsc < 900) { lastEsc = 0; stop() }
        else { lastEsc = Date.now(); toast(t('escAgain')) }
      }
    }
  })
  var lastEsc = 0
  function fontStep(d) {
    setPref('fontSize', Math.max(10, Math.min(22, pref('fontSize', 14) + d)))
    applyLook()
  }
  // Quote: select text in the chat, a button puts it into the input as a quote.
  var quoteBtn = el('button', 'quote-btn', t('quote'))
  quoteBtn.hidden = true
  document.body.appendChild(quoteBtn)
  document.addEventListener('mouseup', function (e) {
    if (e.target === quoteBtn) return
    setTimeout(function () {
      var sel = window.getSelection(), text = sel ? String(sel).trim() : ''
      if (!text || !sel.rangeCount || !$('log').contains(sel.anchorNode)) { quoteBtn.hidden = true; return }
      var r = sel.getRangeAt(0).getBoundingClientRect()
      quoteBtn.innerHTML = icon('quoteMark', 's14')
      quoteBtn.appendChild(el('span', '', t('quote')))
      quoteBtn.style.top = Math.max(8, r.top - 36) + 'px'
      quoteBtn.style.left = Math.max(8, Math.min(innerWidth - 130, r.left)) + 'px'
      quoteBtn.hidden = false
      quoteBtn._text = text
    })
  })
  quoteBtn.onmousedown = function (e) { e.preventDefault() }
  quoteBtn.onclick = function () {
    quoted.push(quoteBtn._text)
    renderAttached()
    quoteBtn.hidden = true
    window.getSelection().removeAllRanges()
    $('input').focus()
  }
  // Drag the edge of the left sidebar or the right panel: the width is remembered.
  function resizer(handle, target, key, min, max, fromRight) {
    var saved = pref(key, 0)
    if (saved) { target.style.width = saved + 'px'; target.style.maxWidth = 'none' }
    handle.onmousedown = function (e) {
      e.preventDefault()
      var x0 = e.clientX, w0 = target.getBoundingClientRect().width
      document.body.classList.add('resizing')
      function move(ev) {
        target.style.width = Math.max(min, Math.min(max(), w0 + (fromRight ? x0 - ev.clientX : ev.clientX - x0))) + 'px'
        target.style.maxWidth = 'none'
      }
      function up() {
        document.removeEventListener('mousemove', move)
        document.removeEventListener('mouseup', up)
        document.body.classList.remove('resizing')
        setPref(key, Math.round(target.getBoundingClientRect().width))
      }
      document.addEventListener('mousemove', move)
      document.addEventListener('mouseup', up)
    }
  }
  resizer($('side-resize'), $('side'), 'sideW', 220, function () { return 420 }, false)
  resizer($('panel-resize'), $('panel'), 'panelW', 360, function () { return Math.max(360, innerWidth - 560) }, true)
  $('scroll').addEventListener('scroll', onChatScroll)
  // the wheel up stops following at once: new text arriving before the scroll event would pull the chat back
  // (not when the wheel scrolls a box of its own inside, such as the reasoning)
  $('scroll').addEventListener('wheel', function (e) {
    var s = $('scroll')
    if (e.deltaY >= 0 || e.ctrlKey || s.scrollTop <= 0) return
    for (var n = e.target; n && n !== s; n = n.parentElement) if (n.scrollTop > 0 && n.scrollHeight > n.clientHeight + 1) return
    stick = false
    updateToBottom()
  }, { passive: true })
  $('to-bottom').onclick = function () { scrollDown(true) }
  var draftTimer = null
  $('input').addEventListener('input', function () { clearTimeout(draftTimer); draftTimer = setTimeout(keepDraft, 400) })
  // No connection: the server stopped or was restarted (a new token); the page says so until it answers again.
  var connBox = el('div', 'conn', t('connLost'))
  connBox.hidden = true
  $('center').insertBefore(connBox, $('center').firstChild)

  $('app').classList.add('boot')                      // the saved collapsed sidebar is not animated at start
  setRail(pref('rail', false))
  requestAnimationFrame(function () { requestAnimationFrame(function () { $('app').classList.remove('boot') }) })
  applyLook()
  renderSeats()
  updateSend()
  api('/api/apps').then(function (a) { if (Array.isArray(a) && a.length) state.apps = ['explorer'].concat(a.filter(function (x) { return x !== 'explorer' })) })
  api('/api/info').then(function (info) {
    state.home = info.home
    state.workspace = info.workspace
    ;(info.gone || []).forEach(function (g) {
      state.folders = state.folders.filter(function (x) { return !same(x, g) })
      if (same(state.folder, g)) state.folder = ''
    })
    setPref('folders', state.folders)
    if (!state.folder || same(state.folder, info.home)) state.folder = info.workspace || info.home
    var back = ''
    try { back = sessionStorage.getItem('mx.chat') || '' } catch (e) {}     // read before newChat() resets it
    newChat()
    loadSessions().then(function () {
      var s = back && state.sessions.filter(function (x) { return x.id === back })[0]
      if (s) openChat(s)
    })
    restorePanel()
    loadCli()
  })
  api('/api/settings').then(gotSettings)
  var runningKey = '', runningNow = []
  setInterval(function () {
    api('/api/ping').then(function (r) {
      connBox.hidden = true
      var now = (r && r.running) || [], key = now.join(',')
      // a turn this page did not follow ended: its chat gets the "finished" dot
      runningNow.forEach(function (id) { if (now.indexOf(id) < 0 && !live[id]) markUnseen(id) })
      runningNow = now
      if (key !== runningKey) { runningKey = key; loadSessions() }
    }, function () { connBox.hidden = false })
  }, 10000)
})()
