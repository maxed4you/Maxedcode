// maxedcode browser bridge (service worker). The local maxedcode agent drives this browser through the native
// messaging host com.maxedcode.browser (maxedcode/chrome/host.py): Chrome starts it while the port is open, and
// the open port keeps this worker alive. Only what an agent needs: tabs, open, read, screenshot, click, type,
// key, scroll, back. The side panel (panel.html) is a chat with the same local maxedcode: the host runs its turns.
// No model calls of its own and no scripts from the agent (unlike OpenSidekick, which this trims down: its own LLM
// loop, providers, MCP, recording and debugger tools are left out).
'use strict'

const HOST = 'com.maxedcode.browser'
const SETTLE_MS = 500        // after an action: time for the page to react before checking whether it navigated
const LOAD_MS = 15000        // longest wait for a page load
const HIDE_MS = 12000        // the "working" frame goes away this long after the last action

let port = null
let ready = false            // the host answered: an agent can reach this browser
let lastError = ''
let agentTab = null          // the tab the agent works in (set by open/switch); none: the active tab
let opened = null            // {opener, id, at}: a tab a click opened (target=_blank), followed by the agent
const shown = new Map()      // tab id -> timer that hides its frame
const trail = new Map()      // tab id -> pages the agent read there, for back
const panels = new Set()     // open side panels (ports)
let current = null           // the panel's turn: {run, session, events, done}; a reopened panel replays it
const EVENTS_KEEP = 4000
let words = null             // messages of the maxedcode window's language (setLang); none: the browser's language

chrome.storage.local.get('appLang').then(function (s) { loadWords(s.appLang || '') })
chrome.storage.session.get('agentTab').then(function (s) { if (s.agentTab) agentTab = s.agentTab })
chrome.alarms.create('connect', { periodInMinutes: 1 })
chrome.alarms.onAlarm.addListener(connect)
chrome.runtime.onStartup.addListener(connect)
chrome.runtime.onInstalled.addListener(connect)
chrome.tabs.onRemoved.addListener(function (id) {
  trail.delete(id)
  if (id === agentTab) setAgentTab(null)
})
chrome.tabs.onCreated.addListener(function (tab) {
  if (tab.openerTabId) opened = { opener: tab.openerTabId, id: tab.id, at: Date.now() }
})
chrome.storage.onChanged.addListener(function (change, area) {
  if (area === 'local' && change.on) change.on.newValue === false ? disconnect() : connect()
})
chrome.runtime.onMessage.addListener(function (msg, _sender, reply) {
  // Stop on the page: ends the panel's turn if one runs, otherwise switches the extension off (no agent can act)
  if (msg.type === 'stop') {
    if (current && !current.done && port) port.postMessage({ type: 'stop', run: current.run })
    else chrome.storage.local.set({ on: false })
  }
  if (msg.type === 'status') { status().then(reply); return true }
})
// The user is in this browser now: the host marks it, so the agent uses the browser the user worked in last.
chrome.windows.onFocusChanged.addListener(function (id) {
  if (id !== chrome.windows.WINDOW_ID_NONE && port && ready) port.postMessage({ type: 'focus' })
})
chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true }).catch(function () {})
chrome.runtime.onConnect.addListener(function (p) {
  if (p.name !== 'panel') return
  panels.add(p)
  p.onDisconnect.addListener(function () { panels.delete(p) })
  p.onMessage.addListener(function (msg) { fromPanel(p, msg) })
  status().then(function (s) { p.postMessage(Object.assign({ type: 'state', current: current }, s)) })
})
connect()

// ── the side panel ──

function toPanels(msg) {
  panels.forEach(function (p) { try { p.postMessage(msg) } catch (e) { /* closed */ } })
}

async function fromPanel(p, msg) {
  if (msg.type === 'on') return chrome.storage.local.set({ on: !!msg.on })
  if (!port || !ready) return p.postMessage({ type: 'agent', run: msg.run, event: { type: 'exit', code: -1, error: 'not connected' } })
  if (msg.type === 'stop') return port.postMessage({ type: 'stop', run: msg.run })
  if (msg.type === 'approve') return port.postMessage({ type: 'approve', id: msg.id, decision: msg.decision })
  if (msg.type !== 'run') return
  // "this page" is the tab the user looks at when sending: the agent starts there
  const active = await chrome.tabs.query({ active: true, windowId: msg.windowId })
  const tab = active[0]
  if (tab && !blocked(tab.url)) setAgentTab(tab.id)
  current = { run: msg.run, session: msg.session, events: [], done: false }
  port.postMessage({ type: 'run', run: msg.run, session: msg.session, prompt: msg.prompt, mode: msg.mode,
    first: msg.first, tab: tab ? { id: tab.id, title: tab.title, url: tab.url } : null })
}

// ── connection to maxedcode ──

async function connect() {
  if (port) return
  const s = await chrome.storage.local.get('on')
  if (s.on === false || port) return
  let p
  try {
    p = chrome.runtime.connectNative(HOST)
  } catch (e) {
    lastError = String((e && e.message) || e)
    badge()
    return
  }
  port = p
  p.onMessage.addListener(fromHost)
  p.onDisconnect.addListener(function () {
    lastError = (chrome.runtime.lastError && chrome.runtime.lastError.message) || ''
    if (port === p) { port = null; ready = false; badge() }
    setTimeout(connect, 15000)
  })
  p.postMessage({ type: 'hello', browser: browserName(), version: chrome.runtime.getManifest().version })
  badge()
}

function disconnect() {
  if (port) port.disconnect()
  port = null
  ready = false
  shown.forEach(function (_t, id) { frame(id, false) })
  badge()
}

function fromHost(msg) {
  if (msg.type === 'agent') {
    if (current && current.run === msg.run) {
      if (msg.event.type !== 'delta' && current.events.length < EVENTS_KEEP) current.events.push(msg.event)
      if (msg.event.type === 'exit') current.done = true
    }
    toPanels(msg)
    return
  }
  if (msg.type === 'lang' || msg.type === 'ready') setLang(msg.lang)
  if (msg.type === 'lang') return
  if (msg.type === 'ready') {
    ready = true
    lastError = ''
    badge()
    chrome.windows.getLastFocused().then(function (w) { if (w && w.focused && port) port.postMessage({ type: 'focus' }) }, function () {})
    reloadIfUpdated(msg.version)
    return
  }
  if (!msg.id) return
  run(msg.action, msg.args || {})
    .catch(function (e) { return { ok: false, error: String((e && e.message) || e) } })
    .then(function (res) { if (port) port.postMessage(Object.assign({ id: msg.id }, res)) })
}

// The files on disk are newer than the running extension (maxedcode was updated): load them, once per version.
async function reloadIfUpdated(version) {
  // only for a newer version: an older one on the host's side (another copy of maxedcode) must not make this
  // extension reload itself, it did not come back after that
  if (!version || !newer(version, chrome.runtime.getManifest().version)) return
  const s = await chrome.storage.local.get('reloadedFor')
  if (s.reloadedFor === version) return
  await chrome.storage.local.set({ reloadedFor: version })
  chrome.runtime.reload()
}

function newer(a, b) {
  const x = String(a).split('.').map(Number), y = String(b).split('.').map(Number)
  for (let i = 0; i < Math.max(x.length, y.length); i++) {
    if ((x[i] || 0) !== (y[i] || 0)) return (x[i] || 0) > (y[i] || 0)
  }
  return false
}

function badge() {
  status().then(function (s) { toPanels(Object.assign({ type: 'status' }, s)) })
  chrome.storage.local.get('on').then(function (s) {
    const off = s.on === false
    chrome.action.setBadgeText({ text: off ? w('badgeOff') : ready ? '' : '!' })
    chrome.action.setBadgeBackgroundColor({ color: off ? '#777' : '#d97757' })
  })
}

function browserName() {
  const brands = ((navigator.userAgentData && navigator.userAgentData.brands) || []).map(function (b) { return b.brand })
  for (const name of ['Microsoft Edge', 'Google Chrome', 'Brave', 'YaBrowser', 'Yandex', 'Opera', 'Vivaldi']) {
    if (brands.indexOf(name) >= 0) return name
  }
  return 'Chromium'
}

// The panel speaks the maxedcode window's language (the host reads it); '' = the browser's own language
function setLang(lang) {
  lang = lang === 'ru' || lang === 'en' ? lang : ''
  chrome.storage.local.get('appLang').then(function (s) {
    if ((s.appLang || '') === lang) return
    chrome.storage.local.set({ appLang: lang })
    toPanels({ type: 'lang', lang: lang })
    loadWords(lang)
  })
}

// The texts this worker shows itself (the button's title and badge, the frame on the agent's tab) in that language
async function loadWords(lang) {
  let d = null
  if (lang) {
    try { d = await (await fetch(chrome.runtime.getURL('_locales/' + lang + '/messages.json'))).json() } catch (e) { d = null }
  }
  words = d
  chrome.action.setTitle({ title: w('actionTitle') })
  badge()
  shown.forEach(function (_t, id) {
    chrome.tabs.sendMessage(id, overlayMessage(true), { frameId: 0 }).catch(function () {})
  })
}

function w(k) { return (words && words[k] && words[k].message) || chrome.i18n.getMessage(k) || k }

async function status() {
  const s = await chrome.storage.local.get('on')
  let tab = null
  if (agentTab) tab = await chrome.tabs.get(agentTab).catch(function () { return null })
  return { on: s.on !== false, ready: ready, error: lastError, tab: tab && { title: tab.title, url: tab.url } }
}

// ── actions ──

async function run(action, a) {
  if (action === 'tabs') return tabs()
  if (action === 'open') return open(a.url, a.new_tab)
  if (action === 'switch') return switchTo(a.tab)
  const tab = await currentTab()
  if (!tab) return { ok: false, error: 'No tab to work in: open a url first.' }
  if (blocked(tab.url)) return { ok: false, error: `Tab ${tab.id} shows a browser page (${tab.url}) that extensions cannot read. Open a website instead.` }
  if (action === 'read') return page(tab.id, a.start || 0, a.find)
  if (action === 'back') return back(tab)
  if (action === 'screenshot') return screenshot(tab)
  if (['click', 'type', 'key', 'scroll'].indexOf(action) < 0) return { ok: false, error: 'Unknown action: ' + action }
  // type / key without a number: real input where the cursor is (Google Docs and other editors draw on a canvas
  // and take keys in a hidden frame, where page events do not reach)
  if ((action === 'type' || action === 'key') && !a.ref) return focused(tab, action, a)
  const started = Date.now()
  const res = await toPage(tab.id, { type: 'act', action: action, ref: a.ref, text: a.text, submit: a.submit, keys: a.keys, direction: a.direction })
  if (!res.ok) return res
  if (action === 'scroll') return { ok: true, text: res.did + '.' }
  const changed = await settle(tab.id, tab.url)
  // A page may open its new tab a moment later (Google Docs' "Blank document"): look a little longer for it.
  for (let i = 0; action === 'click' && i < 4 && !changed && !(opened && opened.opener === tab.id && opened.at >= started); i++) await sleep(250)
  if (opened && opened.opener === tab.id && opened.at >= started) {
    const id = opened.id
    opened = null
    group(id)
    setAgentTab(id)
    await waitForLoad(id)
    const p = await page(id, 0)
    if (p.ok) p.text = res.did + '. It opened a new tab, now the agent\'s tab:\n' + p.text
    return p
  }
  if (changed) {
    const p = await page(tab.id, 0)
    if (p.ok) p.text = res.did + '. The page changed:\n' + p.text
    return p
  }
  return { ok: true, text: res.did + '. Same page: read it again to see what changed.' }
}

async function tabs() {
  const all = await chrome.tabs.query({ windowType: 'normal' })
  const lines = all.slice(0, 50).map(function (t) {
    const marks = (t.id === agentTab ? ' (agent)' : '') + (t.active ? ' (active)' : '')
    return `${t.id}${marks}: ${clip(t.title || '', 80)} — ${clip(t.url || '', 120)}`
  })
  if (all.length > 50) lines.push(`… and ${all.length - 50} more`)
  return { ok: true, text: 'Open tabs (id: title — url):\n' + lines.join('\n') }
}

async function open(url, newTab) {
  url = String(url || '').trim()
  if (!url) return { ok: false, error: 'open needs a url.' }
  if (!/^[a-z][\w+.-]*:/i.test(url)) url = (/^(localhost|127\.|192\.168\.|10\.)/i.test(url) ? 'http://' : 'https://') + url
  if (!/^https?:/i.test(url)) return { ok: false, error: 'Only http and https addresses can be opened.' }
  // Without a tab of its own the agent opens a new one: the user's current page is never replaced.
  let tab = newTab ? null : await agentsTab()
  if (tab) tab = await chrome.tabs.update(tab.id, { url: url, active: true })
  else {
    tab = await chrome.tabs.create({ url: url, active: true })
    group(tab.id)
  }
  setAgentTab(tab.id)
  await waitForLoad(tab.id)
  return page(tab.id, 0)
}

// The tabs the agent opened sit in one "maxedcode" tab group, as Claude keeps its own: the user sees which are its.
async function group(tabId) {
  try {
    const tab = await chrome.tabs.get(tabId)
    const groups = await chrome.tabGroups.query({ title: 'maxedcode', windowId: tab.windowId })
    if (groups.length) await chrome.tabs.group({ tabIds: [tabId], groupId: groups[0].id })
    else {
      const id = await chrome.tabs.group({ tabIds: [tabId], createProperties: { windowId: tab.windowId } })
      await chrome.tabGroups.update(id, { title: 'maxedcode', color: 'orange' })
    }
  } catch (e) { /* a browser without tab groups: the tab just stays where it is */ }
}

// The visible part of the tab as a JPEG, with the numbers [N] drawn over its controls. Only the tab on screen can
// be captured, so the agent's tab is brought to the front of its window first.
async function screenshot(tab) {
  if (!tab.active) {
    await chrome.tabs.update(tab.id, { active: true })
    await sleep(300)
  }
  const marks = await toPage(tab.id, { type: 'marks', state: 'show' })
  if (!marks.ok) return marks
  await sleep(120)          // let the numbers paint
  let image
  try {
    image = await chrome.tabs.captureVisibleTab(tab.windowId, { format: 'jpeg', quality: 80 })
  } catch (e) {
    // the window is minimized or covered: the page can still draw itself through the debugger
    try {
      const shot = await debug(tab.id, 'Page.captureScreenshot', { format: 'jpeg', quality: 80 })
      image = 'data:image/jpeg;base64,' + shot.data
    } catch (err) {
      return { ok: false, error: 'No screenshot: ' + ((err && err.message) || err) + '. Use read.' }
    }
  } finally {
    await toPage(tab.id, { type: 'marks', state: 'hide' })
  }
  const fresh = await chrome.tabs.get(tab.id)
  const list = marks.marked.length ? '\nNumbered on the picture: ' + marks.marked.join(' ') : ''
  return { ok: true, image: image, text: `Screenshot of tab ${fresh.id}: ${fresh.title}\n${fresh.url} (scrolled ${marks.scrolled}% down)${list}` }
}

// Back to the page the agent read before. Chrome's own Back (and history.back) skips every entry that was left
// without a user gesture, and the agent's clicks have none: Back went to the start of the tab's history.
async function back(tab) {
  const list = trail.get(tab.id) || []
  if (list.length && list[list.length - 1] === tab.url) list.pop()
  const previous = list.pop()
  if (previous) {
    await chrome.tabs.update(tab.id, { url: previous })
    await waitForLoad(tab.id)
  } else {
    const res = await toPage(tab.id, { type: 'act', action: 'back' })
    if (!res.ok) return res
    await settle(tab.id, tab.url)
  }
  return page(tab.id, 0)
}

async function switchTo(id) {
  const tab = await chrome.tabs.get(+id).catch(function () { return null })
  if (!tab) return { ok: false, error: `No tab ${id}. List them with tabs.` }
  setAgentTab(tab.id)
  await waitForLoad(tab.id)       // a tab that is still opening reads as about:blank
  const now = await chrome.tabs.get(tab.id)
  if (blocked(now.url)) return { ok: true, text: `Now in tab ${now.id}: ${now.title} — ${now.url} (a browser page, it cannot be read).` }
  return page(now.id, 0)
}

async function agentsTab() {
  if (!agentTab) return null
  return chrome.tabs.get(agentTab).catch(function () { return null })
}

async function currentTab() {
  const own = await agentsTab()
  if (own) {
    if (own.status === 'loading' || own.pendingUrl) {
      await waitForLoad(own.id)
      return chrome.tabs.get(own.id)
    }
    return own
  }
  const active = await chrome.tabs.query({ active: true, lastFocusedWindow: true, windowType: 'normal' })
  return active[0] || null
}

function setAgentTab(id) {
  if (agentTab && agentTab !== id) frame(agentTab, false)
  agentTab = id
  chrome.storage.session.set({ agentTab: id })
}

async function page(tabId, start, find) {
  let res = await toPage(tabId, { type: 'read', start: start, find: find })
  // a page that draws itself with scripts (shops, apps) is empty right after loading: give it up to 3 s
  for (let i = 0; i < 6 && res.ok && !find && !start && !String(res.text || '').trim(); i++) {
    await sleep(500)
    res = await toPage(tabId, { type: 'read', start: start, find: find })
  }
  if (!res.ok) return res
  const tab = await chrome.tabs.get(tabId)
  const list = trail.get(tabId) || []
  if (list[list.length - 1] !== tab.url) list.push(tab.url)
  trail.set(tabId, list.slice(-30))
  const text = res.text.trim() || '(nothing readable on the page: it is still loading, shows a bot check, or draws on a canvas; take a screenshot to see it)'
  const where = res.scrolled ? ` (scrolled ${res.scrolled}% down)` : ''
  return { ok: true, text: `Tab ${tab.id}: ${tab.title}\n${tab.url}${where}\n\n${text}` }
}

// After an action: did the page navigate (a new url or a load)? Waits for that load.
async function settle(tabId, before) {
  await sleep(SETTLE_MS)
  const tab = await chrome.tabs.get(tabId).catch(function () { return null })
  if (!tab) return false
  if (tab.status === 'loading') await waitForLoad(tabId)
  const now = await chrome.tabs.get(tabId).catch(function () { return tab })
  return tab.status === 'loading' || now.url !== before
}

function waitForLoad(tabId) {
  return new Promise(function (resolve) {
    let done = false
    function finish() {
      if (done) return
      done = true
      chrome.tabs.onUpdated.removeListener(listener)
      clearTimeout(timer)
      setTimeout(resolve, 300)    // let client-side rendering start
    }
    function listener(id, info) { if (id === tabId && info.status === 'complete') finish() }
    chrome.tabs.onUpdated.addListener(listener)
    const timer = setTimeout(finish, LOAD_MS)
    // a tab just created or sent to a url can still report the old page as complete: pendingUrl tells
    chrome.tabs.get(tabId).then(function (t) { if (t.status === 'complete' && !t.pendingUrl) finish() }, finish)
  })
}

// A message to page.js in the tab's top frame; the script is injected on first use.
async function toPage(tabId, msg) {
  try {
    await chrome.tabs.sendMessage(tabId, { type: 'ping' }, { frameId: 0 })
  } catch (e) {
    if (!(await inject(tabId))) {
      // a page in the middle of loading refuses the script: wait for it once and try again
      await waitForLoad(tabId)
      if (!(await inject(tabId))) {
        const tab = await chrome.tabs.get(tabId).catch(function () { return null })
        return { ok: false, error: tab ? `Tab ${tabId} (${tab.url}) cannot be read by extensions (a browser page, the web store or a PDF).`
          : `Tab ${tabId} was closed. List tabs or open the page again.` }
      }
    }
  }
  frame(tabId, true)
  try {
    return (await chrome.tabs.sendMessage(tabId, msg, { frameId: 0 })) || { ok: false, error: 'The page did not answer.' }
  } catch (e) {
    return { ok: false, error: 'The page did not answer (it may have navigated away): read it again.' }
  }
}

function inject(tabId) {
  return chrome.scripting.executeScript({ target: { tabId: tabId }, files: ['page.js'] }).then(function () { return true }, function () { return false })
}

// The orange frame and "Stop" on the agent's tab while it works.
function frame(tabId, on) {
  clearTimeout(shown.get(tabId))
  shown.delete(tabId)
  chrome.tabs.sendMessage(tabId, overlayMessage(on), { frameId: 0 }).catch(function () {})
  if (on) shown.set(tabId, setTimeout(function () { frame(tabId, false) }, HIDE_MS))
}

// page.js has no say in the language: its texts come with the message
function overlayMessage(on) {
  return on ? { type: 'overlay', state: 'show', text: w('working'), stop: w('stop') } : { type: 'overlay', state: 'hide' }
}

function blocked(url) {
  return !url || /^(chrome|edge|brave|opera|vivaldi|browser|about|chrome-extension|devtools|view-source):/i.test(url) ||
    /^https:\/\/(chrome\.google\.com\/webstore|chromewebstore\.google\.com|microsoftedge\.microsoft\.com\/addons)/i.test(url)
}

// ── real keyboard input through the debugger (Chrome shows "started debugging this browser" while attached) ──

const DEBUG_MS = 20000                 // detach this long after the last input, so the bar goes away
const debugged = new Map()             // tab id -> detach timer
const KEYS = { enter: 13, tab: 9, escape: 27, esc: 27, backspace: 8, delete: 46, arrowup: 38, arrowdown: 40, arrowleft: 37,
  arrowright: 39, home: 36, end: 35, pageup: 33, pagedown: 34, space: 32 }
const MODS = { alt: 1, control: 2, ctrl: 2, meta: 4, cmd: 4, shift: 8 }
chrome.debugger.onDetach.addListener(function (source) {
  clearTimeout(debugged.get(source.tabId))
  debugged.delete(source.tabId)
})

async function debug(tabId, method, params) {
  if (!debugged.has(tabId)) {
    await chrome.debugger.attach({ tabId: tabId }, '1.3')
    debugged.set(tabId, 0)
  }
  clearTimeout(debugged.get(tabId))
  debugged.set(tabId, setTimeout(function () {
    debugged.delete(tabId)
    chrome.debugger.detach({ tabId: tabId }).catch(function () {})
  }, DEBUG_MS))
  return chrome.debugger.sendCommand({ tabId: tabId }, method, params || {})
}

async function press(tabId, combo) {
  const parts = String(combo).split('+').map(function (k) { return k.trim() }).filter(Boolean)
  const name = parts.pop() || ''
  const modifiers = parts.reduce(function (m, p) { return m | (MODS[p.toLowerCase()] || 0) }, 0)
  const lower = name.toLowerCase()
  const single = name.length === 1
  const code = KEYS[lower] || (single ? name.toUpperCase().charCodeAt(0) : 0)
  if (!code) throw new Error('Unknown key: ' + name)
  const key = single ? name : name[0].toUpperCase() + name.slice(1)
  const text = lower === 'enter' ? '\r' : single && !(modifiers & 7) ? name : undefined
  const base = { modifiers: modifiers, windowsVirtualKeyCode: code, key: key,
    code: single ? (/\d/.test(name) ? 'Digit' + name : 'Key' + name.toUpperCase()) : key }
  await debug(tabId, 'Input.dispatchKeyEvent', Object.assign({ type: text ? 'keyDown' : 'rawKeyDown', text: text }, base))
  await debug(tabId, 'Input.dispatchKeyEvent', Object.assign({ type: 'keyUp' }, base))
}

async function focused(tab, action, a) {
  if (!tab.active) {
    await chrome.tabs.update(tab.id, { active: true })
    await sleep(300)
  }
  frame(tab.id, true)
  try {
    if (action === 'type') {
      // nothing takes typing yet (the window was in the background, the editor never got the caret): a real click
      // into the page's editor first, as a person would
      const spot = await toPage(tab.id, { type: 'editor' })
      if (spot.ok && spot.x !== undefined) {
        for (const kind of ['mousePressed', 'mouseReleased']) {
          await debug(tab.id, 'Input.dispatchMouseEvent', { type: kind, x: spot.x, y: spot.y, button: 'left', clickCount: 1 })
        }
        await sleep(200)
      }
      await debug(tab.id, 'Input.insertText', { text: String(a.text == null ? '' : a.text) })
      if (a.submit) await press(tab.id, 'Enter')
    } else {
      await press(tab.id, a.keys)
    }
  } catch (e) {
    return { ok: false, error: 'Real keyboard input failed: ' + ((e && e.message) || e) + '. Click into the field first, or use a number from read.' }
  }
  await sleep(SETTLE_MS)
  const did = action === 'type' ? 'typed where the cursor is' + (a.submit ? ' and pressed Enter' : '') : 'pressed ' + a.keys
  return { ok: true, text: did + '. Check the result with read or screenshot (an editor like Google Docs shows its text only on a screenshot).' }
}

function sleep(ms) { return new Promise(function (r) { setTimeout(r, ms) }) }
function clip(s, n) { s = String(s); return s.length > n ? s.slice(0, n - 1) + '…' : s }
