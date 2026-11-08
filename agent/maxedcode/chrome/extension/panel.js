// The maxedcode side panel: a chat with the local maxedcode agent, right in the browser. Each message is one turn
// of maxedcode that the native host runs (host.py Runs); its stream-json events come back through background.js.
// The chat is an ordinary maxedcode session ("app-…", in the shared folder): the maxedcode window shows it too.
'use strict'

// The maxedcode window's language when it is known (background.js setLang), else the browser's
let dict = null
const t = function (k) { return (dict && dict[k] && dict[k].message) || chrome.i18n.getMessage(k) || k }
const $ = function (id) { return document.getElementById(id) }
const texts = new WeakMap()    // node -> function that builds its text again when the language changes (tf)
const KEEP_TURNS = 30          // turns of the chat kept in storage for a reopened panel
const STORE_CHARS = 4000       // a stored event's text is cut to this

let port = null
let status = { on: true, ready: false }
let session = null             // maxedcode session id of this chat
let chat = []                  // [{prompt, run, events}] — what a reopened panel draws again
let mode = 'manual'
let turn = null                // the running turn (Turn)
let windowId = null

// ── setup ──

function translate(redraw) {
  document.documentElement.lang = (dict && dict.lang) || chrome.i18n.getUILanguage()
  document.querySelectorAll('[data-t]').forEach(function (n) { n.textContent = t(n.dataset.t) })
  document.querySelectorAll('[data-ph]').forEach(function (n) { n.placeholder = t(n.dataset.ph) })
  document.querySelectorAll('[data-title]').forEach(function (n) { n.title = t(n.dataset.title) })
  document.querySelectorAll('[data-tf]').forEach(function (n) { const f = texts.get(n); if (f) n.textContent = f() })
  if (redraw) setStatus(status)            // its texts are set from status, not from data-t
}

// A text built from translated parts (a step label, an error with details): kept as a function, so translate()
// builds it again in the new language
function tf(node, f) {
  texts.set(node, f)
  node.dataset.tf = ''
  node.textContent = f()
  return node
}

// el() whose text is one message: data-t, so translate() redraws it
function elt(tag, cls, key) {
  const n = el(tag, cls, t(key))
  n.dataset.t = key
  return n
}

async function useLang(lang) {
  dict = null
  if (lang) {
    try {
      dict = await (await fetch(chrome.runtime.getURL('_locales/' + lang + '/messages.json'))).json()
      dict.lang = lang
    } catch (e) { dict = null }
  }
  translate(true)
}
translate()
chrome.storage.local.get('appLang').then(function (s) { if (s.appLang) useLang(s.appLang) })
chrome.windows.getCurrent().then(function (w) { windowId = w.id })

chrome.storage.local.get(['panelSession', 'panelChat', 'panelMode']).then(function (s) {
  session = s.panelSession || null
  chat = Array.isArray(s.panelChat) ? s.panelChat : []
  setMode(s.panelMode || 'manual')
  chat.forEach(function (c) { drawStored(c) })
  connect()
})

function connect() {
  port = chrome.runtime.connect({ name: 'panel' })
  port.onMessage.addListener(onMessage)
  port.onDisconnect.addListener(function () { port = null; setTimeout(connect, 500) })
}

function onMessage(msg) {
  if (msg.type === 'state') {
    setStatus(msg)
    const c = msg.current
    // a turn that went on while the panel was closed: draw it from the background's copy
    if (c && session && c.session === session && !(turn && turn.id === c.run)) {
      const stored = chat.find(function (x) { return x.run === c.run })
      const box = stored && stored.el
      if (box) box.remove()
      const view = new Turn(stored ? stored.prompt : '', c.run)
      c.events.forEach(function (e) { view.event(e, true) })
      if (stored) stored.events = c.events.map(storable)
      if (!c.done) { turn = view; busy(true) }
    }
  } else if (msg.type === 'status') {
    setStatus(msg)
  } else if (msg.type === 'lang') {
    useLang(msg.lang)
  } else if (msg.type === 'agent' && turn && msg.run === turn.id) {
    turn.event(msg.event)
  }
}

function setStatus(s) {
  status = s
  $('dot').className = 'dot' + (s.on && s.ready ? ' on' : '')
  $('dot').title = s.on && s.ready ? t('connected') : t('notConnected')
  $('on').checked = s.on
  const banner = $('banner')
  banner.hidden = s.on && s.ready
  banner.textContent = ''
  if (!s.on) {
    banner.append(t('offBanner'))
    const b = document.createElement('button')
    b.textContent = t('turnOn')
    b.onclick = function () { port && port.postMessage({ type: 'on', on: true }) }
    banner.append(b)
  } else if (!s.ready) {
    banner.textContent = t('notConnected') + '. ' + t('notConnectedSub')
  }
  if (turn && !(s.on && s.ready)) turn.event({ type: 'exit', code: -1, error: 'not connected' })
  updateSend()
}

$('on').onchange = function () { port && port.postMessage({ type: 'on', on: $('on').checked }) }
$('menu-btn').onclick = function () { $('menu').hidden = !$('menu').hidden }
$('new').onclick = function () {
  if (turn) return
  session = null
  chat = []
  save()
  document.querySelectorAll('.turn, .user').forEach(function (n) { n.remove() })
  $('empty').hidden = false
  $('input').focus()
}
document.querySelectorAll('#modes button').forEach(function (b) { b.onclick = function () { setMode(b.dataset.mode) } })

function setMode(m) {
  mode = ['plan', 'manual', 'auto'].indexOf(m) >= 0 ? m : 'manual'
  document.querySelectorAll('#modes button').forEach(function (b) { b.classList.toggle('on', b.dataset.mode === mode) })
  $('mode-sub').dataset.t = { plan: 'planSub', manual: 'askSub', auto: 'autoSub' }[mode]
  $('mode-sub').textContent = t($('mode-sub').dataset.t)
  chrome.storage.local.set({ panelMode: mode })
}

// ── sending ──

const input = $('input')
input.addEventListener('keydown', function (e) {
  if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); send() }
})
input.addEventListener('input', function () {
  input.style.height = 'auto'
  input.style.height = Math.min(input.scrollHeight, 200) + 'px'
  updateSend()
})
$('send').onclick = send

function send() {
  if (turn) {                      // the button is Stop while a turn runs
    port && port.postMessage({ type: 'stop', run: turn.id })
    return
  }
  const text = input.value.trim()
  if (!text || !port || !status.ready) return
  const first = !session
  if (!session) {
    session = 'app-' + stamp()
    chrome.storage.local.set({ panelSession: session })
  }
  const id = 'p' + Date.now()
  const entry = { prompt: text, run: id, events: [] }
  chat.push(entry)
  turn = new Turn(text, id)
  entry.el = turn.box
  save()
  port.postMessage({ type: 'run', run: id, session: session, prompt: text, mode: mode, first: first, windowId: windowId })
  input.value = ''
  input.style.height = 'auto'
  busy(true)
}

function busy(on) {
  $('send').classList.toggle('busy', on)
  $('send').dataset.title = on ? 'stop' : 'send'
  $('send').title = t($('send').dataset.title)
  updateSend()
}

function updateSend() {
  $('send').disabled = !turn && (!input.value.trim() || !status.ready)
}

function stamp() {
  const d = new Date(), p = function (n) { return String(n).padStart(2, '0') }
  return d.getFullYear() + p(d.getMonth() + 1) + p(d.getDate()) + '-' + p(d.getHours()) + p(d.getMinutes()) + p(d.getSeconds())
}

function storable(e) {
  const c = JSON.parse(JSON.stringify(e))
  ;['content', 'result', 'error'].forEach(function (k) { if (typeof c[k] === 'string' && c[k].length > STORE_CHARS) c[k] = c[k].slice(0, STORE_CHARS) })
  if (c.message && typeof c.message.content === 'string') c.message.content = c.message.content.slice(0, STORE_CHARS)
  return c
}

let saving = null
function save() {
  clearTimeout(saving)
  saving = setTimeout(function () {
    const kept = chat.slice(-KEEP_TURNS).map(function (c) { return { prompt: c.prompt, run: c.run, events: c.events } })
    chrome.storage.local.set({ panelChat: kept })
  }, 300)
}

function drawStored(c) {
  const view = new Turn(c.prompt, c.run)
  ;(c.events || []).forEach(function (e) { view.event(e, true) })
  c.el = view.box
}

// ── one turn: the user's message, then the agent's text, steps, approval cards and answer ──

function Turn(prompt, id) {
  this.id = id
  this.steps = {}
  this.live = null              // the text block the streamed deltas go to
  this.texts = []               // every text shown, so the final answer is not drawn twice
  this.done = false
  $('empty').hidden = true
  this.box = el('div', 'turn')
  if (prompt) this.box.appendChild(el('div', 'user', prompt))
  this.thinking = elt('div', 'thinking', 'thinking')
  this.box.appendChild(this.thinking)
  $('log').appendChild(this.box)
  scroll()
}

Turn.prototype.event = function (e, replay) {
  if (this.done) return
  if (!replay && e.type !== 'delta') {
    const entry = chat.find(function (c) { return c.run === this.id }, this)
    if (entry) { entry.events.push(storable(e)); save() }
  }
  if (e.type === 'delta') return this.delta(e)
  if (e.type === 'assistant') return this.assistant(e.message || {})
  if (e.type === 'tool_result') return this.result(e)
  if (e.type === 'approval') return this.approval(e, replay)
  if (e.type === 'approval_result') return this.approved(e)
  if (e.type === 'notice') return this.add(tf(el('div', 'note'), function () { return noticeText(e) }))
  if (e.type === 'result') {
    fill(e)
    return this.answer(String(e.result || ''))
  }
  if (e.type === 'exit') return this.exit(e, replay)
}

Turn.prototype.delta = function (e) {
  if (e.reset) {
    if (this.live) { this.live.el.remove(); this.live = null }
    return
  }
  if (e.text) {
    if (!this.live) {
      this.live = { raw: '', el: el('div', 'text') }
      this.box.insertBefore(this.live.el, this.thinking)
    }
    this.live.raw += e.text
    this.live.el.innerHTML = markdown(clean(this.live.raw))
    scroll()
  }
}

Turn.prototype.assistant = function (m) {
  const text = clean(String(m.content || ''))
  if (text) {
    if (!this.live) {
      this.live = { raw: '', el: el('div', 'text') }
      this.box.insertBefore(this.live.el, this.thinking)
    }
    this.live.el.innerHTML = markdown(text)
    this.texts.push(text)
  } else if (this.live) {
    this.live.el.remove()
  }
  this.live = null
  ;(m.tool_calls || []).forEach(function (c) {
    const f = c.function || {}
    let a = {}
    try { a = JSON.parse(f.arguments || '{}') } catch (err) { a = {} }
    const row = el('div', 'step')
    const mark = el('span', 'mark')
    mark.appendChild(el('span', 'spin'))
    row.append(mark, tf(el('span', 'what'), function () { return label(f.name, a) }))
    this.box.insertBefore(row, this.thinking)
    this.steps[c.id] = { row: row, mark: mark, detail: null }
    row.onclick = function () {
      const s = this.steps[c.id]
      if (s.detail) s.detail.hidden = !s.detail.hidden
    }.bind(this)
  }, this)
  scroll()
}

Turn.prototype.result = function (e) {
  const s = this.steps[e.id]
  if (!s) return
  const text = String(e.content || '')
  const failed = /^Error/.test(text) || /^The user rejected/.test(text)
  s.mark.textContent = failed ? '✕' : '✓'
  s.row.classList.toggle('err', failed)
  s.detail = el('div', 'detail', text.length > 3000 ? text.slice(0, 3000) + '\n…' : text)
  s.detail.hidden = true
  s.row.after(s.detail)
}

Turn.prototype.approval = function (e, replay) {
  const card = el('div', 'ask')
  card.appendChild(elt('div', 'head', 'apHead'))
  if (e.why) card.appendChild(tf(el('div', 'why'), function () { return whyText(e.why) }))
  const a = e.input || {}
  if (e.tool === 'browser') card.appendChild(tf(el('pre'), function () { return label('browser', a) }))
  else card.appendChild(el('pre', '', String(a.command || a.code || a.path || a.task || JSON.stringify(a, null, 1)).slice(0, 2000)))
  const row = el('div', 'row')
  ;[['allow', 'apYes', 'yes'], ['deny', 'apNo', ''], ['always', 'apAlways', '']].forEach(function (x) {
    const b = elt('button', x[2], x[1])
    b.onclick = function () {
      row.querySelectorAll('button').forEach(function (n) { n.disabled = true })
      port && port.postMessage({ type: 'approve', id: e.id, decision: x[0] })
    }
    row.appendChild(b)
  })
  card.appendChild(row)
  card.dataset.id = e.id
  this.add(card)
  if (!replay) notifyAsk()
}

Turn.prototype.approved = function (e) {
  const card = this.box.querySelector('.ask[data-id="' + e.id + '"]')
  if (!card) return
  const row = card.querySelector('.row')
  if (row) row.replaceWith(elt('div', 'state', e.decision === 'deny' ? 'apDenied' : 'apAllowed'))
}

Turn.prototype.answer = function (text) {
  text = clean(text)
  this.gotAnswer = true
  if (!text || this.texts.some(function (x) { return x.trim() === text.trim() })) return
  const block = el('div', 'text')
  block.innerHTML = markdown(text)
  this.texts.push(text)
  this.add(block)
}

Turn.prototype.exit = function (e, replay) {
  if (e.code && !this.gotAnswer) {
    this.add(tf(el('div', 'error'), function () { return exitText(e) }))
  }
  this.finish()
  if (!replay) save()
}

Turn.prototype.finish = function () {
  this.done = true
  this.thinking.remove()
  this.box.querySelectorAll('.step .spin').forEach(function (n) { n.parentNode.textContent = '·' })
  if (turn === this) { turn = null; busy(false) }
}

Turn.prototype.add = function (node) {
  this.box.insertBefore(node, this.thinking.parentNode ? this.thinking : null)
  scroll()
}

// The chat's context is filling up (page texts stay in it): past BIG_FILL a line offers a new chat, which is faster
// than the automatic summary at 90%.
const BIG_FILL = 0.6
function fill(e) {
  const used = e.usage && e.usage.prompt_tokens, ctx = e.ctx
  const big = $('big')
  if (!used || !ctx || used / ctx < BIG_FILL) { big.hidden = true; return }
  big.hidden = false
  const pct = Math.round(used / ctx * 100)
  tf($('big-text'), function () { return t('bigChat').replace('{pct}', pct) })
}
$('big-new').onclick = function () { $('new').onclick(); $('big').hidden = true }

// ── helpers ──

function el(tag, cls, text) {
  const n = document.createElement(tag)
  if (cls) n.className = cls
  if (text !== undefined) n.textContent = text
  return n
}

function scroll() {
  const log = $('log')
  if (log.scrollHeight - log.scrollTop - log.clientHeight < 160) log.scrollTop = log.scrollHeight
}

function clip(s, n) { s = String(s || ''); return s.length > n ? s.slice(0, n - 1) + '…' : s }

// a model's <think> block is not part of the answer
function clean(s) { return String(s).replace(/<think>[\s\S]*?(<\/think>|$)/g, '').trim() }

function label(name, a) {
  if (name !== 'browser') return name + ' ' + clip(a.command || a.path || a.query || a.url || a.pattern || '', 80)
  if (a.task) return t('sTask') + ': ' + clip(a.task, 100)
  const n = a.ref ? ' [' + a.ref + ']' : ''
  switch (a.action) {
    case 'read': return t('sRead') + (a.find ? ' "' + clip(a.find, 40) + '"' : '')
    case 'screenshot': return t('sShot')
    case 'click': return t('sClick') + n
    case 'type': return t('sType') + n + ' "' + clip(a.text, 40) + '"'
    case 'key': return t('sKey') + ' ' + (a.keys || '')
    case 'scroll': return t('sScroll') + (DIRS[a.direction] ? ' ' + t(DIRS[a.direction]) : a.direction ? ' ' + a.direction : n)
    case 'open': return t('sOpen') + ' ' + clip(a.url, 60)
    case 'tabs': return t('sTabs')
    case 'switch': return t('sSwitch')
    case 'back': return t('sBack')
  }
  return 'browser ' + (a.action || '')
}

const DIRS = { up: 'dirUp', down: 'dirDown', top: 'dirTop', bottom: 'dirBottom' }

// Why a turn ended without an answer: 'not connected' comes from background.js, why 'start' from host.py
function exitText(e) {
  if (e.error === 'not connected') return t('notConnected')
  if (e.why === 'start') return t('notStarted') + (e.error ? '\n' + e.error : '')
  if (e.code === 1 && !e.error) return t('stopped')
  return t('failed') + (e.error ? '\n' + e.error.slice(-600) : '')
}

// headless `notice` events by kind, as the maxedcode window shows them; an unknown kind keeps its own text
function noticeText(e) {
  const key = { cut: 'noticeCut', empty: 'noticeEmpty', loop: 'noticeLoop' }[e.kind]
  return key ? t(key) : e.text || e.kind || ''
}

// An approval's reason comes in English from maxedcode (permissions.explain): its general sentences are
// translated, a specific one (why a command is dangerous) is shown as it is
const WHY = {
  'changes a file in the project': 'whyWrite',
  'only reads': 'whyRead',
  'works in your own browser, where you are logged in: it can open pages, click and type': 'whyBrowser',
  'calls an external MCP tool, which can do things outside this project': 'whyMcp',
  'runs a command': 'whyRun',
}
function whyText(why) { return WHY[why] ? t(WHY[why]) : why }

function notifyAsk() {
  if (document.visibilityState !== 'visible') document.title = '● maxedcode'
}
document.addEventListener('visibilitychange', function () { if (document.visibilityState === 'visible') document.title = 'maxedcode' })

// Small Markdown: paragraphs, lists, headings, code, bold, italics, links. Everything is escaped first.
function markdown(src) {
  const parts = String(src).split(/```[^\n]*\n?/)
  return parts.map(function (p, i) {
    return i % 2 ? '<pre><code>' + esc(p.replace(/\n$/, '')) + '</code></pre>' : blocks(p)
  }).join('')
}

function blocks(text) {
  const out = []
  let list = null
  text.split('\n').forEach(function (line) {
    const item = line.match(/^\s*(?:[-*•]|\d+[.)])\s+(.*)$/)
    if (item) {
      const tag = /^\s*\d/.test(line) ? 'ol' : 'ul'
      if (!list || list.tag !== tag) { if (list) out.push('</' + list.tag + '>'); list = { tag: tag }; out.push('<' + tag + '>') }
      out.push('<li>' + inline(item[1]) + '</li>')
      return
    }
    if (list) { out.push('</' + list.tag + '>'); list = null }
    const head = line.match(/^(#{1,6})\s+(.*)$/)
    if (head) out.push('<h3>' + inline(head[2]) + '</h3>')
    else if (line.trim()) out.push('<p>' + inline(line) + '</p>')
  })
  if (list) out.push('</' + list.tag + '>')
  return out.join('')
}

function inline(s) {
  return esc(s)
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\*\*([^*]+)\*\*/g, '<b>$1</b>')
    .replace(/(^|[^*])\*([^*\s][^*]*)\*/g, '$1<i>$2</i>')
    .replace(/\[([^\]]+)\]\((https?:[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>')
    .replace(/(^|[\s(])(https?:\/\/[^\s<)]+)/g, '$1<a href="$2" target="_blank" rel="noopener">$2</a>')
}

function esc(s) {
  return String(s).replace(/[&<>"]/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c] })
}
