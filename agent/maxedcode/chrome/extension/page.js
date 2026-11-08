// maxedcode page script. background.js injects it into a tab only when the agent works there (never into every
// page). It reads the page as plain text with interactive elements numbered [N] and acts on them by number.
// Element naming, clicking and typing are adapted from OpenSidekick (MIT, see LICENSE-OpenSidekick.txt).
;(function () {
  'use strict'
  // Already here, unless that copy is orphaned: after the extension reloads (an update) the old script stays in
  // the tab without its connection (chrome.runtime.id is gone) and every request to that tab went unanswered.
  if (typeof window.__maxedcodePage === 'function' && window.__maxedcodePage()) return
  window.__maxedcodePage = function () { try { return !!chrome.runtime.id } catch (e) { return false } }

  const LIMIT = 6000           // characters per read; "start" continues
  const OVERLAY = 'maxedcode-overlay'
  const byNumber = new Map()   // N -> WeakRef(element)
  const numberOf = new WeakMap()   // element -> N: numbers stay the same across reads of the same page
  let last = 0

  const SKIP = new Set(['script', 'style', 'noscript', 'template', 'svg', 'canvas', 'iframe', 'head', 'meta', 'link', 'object', 'video', 'audio'])
  const BLOCK = new Set(['address', 'article', 'aside', 'blockquote', 'br', 'caption', 'dd', 'details', 'dialog', 'div', 'dl', 'dt',
    'fieldset', 'figcaption', 'figure', 'footer', 'form', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'header', 'hr', 'li', 'main', 'nav',
    'ol', 'p', 'pre', 'section', 'table', 'tbody', 'thead', 'tfoot', 'tr', 'ul'])
  const ROLES = new Set(['button', 'link', 'checkbox', 'radio', 'tab', 'menuitem', 'menuitemcheckbox', 'menuitemradio', 'switch',
    'option', 'combobox', 'textbox', 'searchbox', 'slider', 'spinbutton', 'treeitem'])
  const INPUT_KIND = { checkbox: 'checkbox', radio: 'radio', submit: 'button', button: 'button', reset: 'button', image: 'button',
    search: 'search', email: 'input', password: 'password', number: 'number', file: 'file', range: 'slider', date: 'date' }
  const NESTED = 'a[href],button,input:not([type=hidden]),select,textarea,[role=button],[role=link],[role=checkbox],' +
    '[role=tab],[role=menuitem],[role=option],[contenteditable=""],[contenteditable=true]'

  chrome.runtime.onMessage.addListener(function (msg, _sender, reply) {
    try {
      if (msg.type === 'ping') reply({ ok: true })
      else if (msg.type === 'read') reply(read(msg.start || 0, msg.find))
      else if (msg.type === 'act') reply(act(msg))
      else if (msg.type === 'overlay') reply(overlay(msg))
      else if (msg.type === 'marks') reply(msg.state === 'hide' ? unmark() : mark())
      else if (msg.type === 'editor') reply(editor())
      else reply({ ok: false, error: 'unknown message' })
    } catch (e) {
      reply({ ok: false, error: String((e && e.message) || e) })
    }
  })

  // ── reading ──

  function read(start, find) {
    const body = document.body || document.documentElement
    // The main content first, then the menus around it: the first part of a read is what the page is about.
    const main = document.querySelector('main, [role=main]')
    let text = main && main !== body && visible(main)
      ? render(main) + '\n\n--- the rest of the page (menus, header, footer) ---\n' + render(body, main)
      : render(body)
    if (find) text = matching(text, String(find))
    start = Math.max(0, Math.min(start, text.length))
    let part = text.slice(start, start + LIMIT)
    if (start + LIMIT < text.length) {
      const cut = part.lastIndexOf('\n')
      if (cut > LIMIT / 2) part = part.slice(0, cut)
    }
    const end = start + part.length
    const where = scrollState()
    let note = ''
    if (start > 0 || end < text.length) note = `\n[characters ${start}-${end} of ${text.length}` + (end < text.length ? `; more: read with start=${end}]` : ']')
    return { ok: true, text: part + note, scrolled: where }
  }

  // Lines that contain the text, each with the line before it (a heading or a label).
  function matching(text, find) {
    const lines = text.split('\n'), q = find.toLowerCase(), keep = []
    lines.forEach(function (l, i) {
      if (l.toLowerCase().indexOf(q) < 0) return
      if (i > 0 && keep[keep.length - 1] !== i - 1 && keep[keep.length - 1] !== i) keep.push(i - 1)
      if (keep[keep.length - 1] !== i) keep.push(i)
    })
    if (!keep.length) return `(no line on this page contains "${find}")`
    return `Lines with "${find}":\n` + keep.map(function (i) { return lines[i] }).join('\n')
  }

  function render(root, skip) {
    const lines = []
    let line = ''
    function flush() {
      const s = line.replace(/\s+/g, ' ').trim()
      if (s) lines.push(s)
      line = ''
    }
    function walk(node) {
      if (node.nodeType === 3) {
        line += node.data
        return
      }
      if (node.nodeType !== 1) return
      const el = node
      const tag = el.localName
      if (SKIP.has(tag) || el.id === OVERLAY || el === skip) return
      if (!visible(el)) return
      if (tag === 'a' && /^\[\d+\]$/.test((el.textContent || '').trim())) {
        line += el.textContent      // a footnote mark: not worth a number of its own
        return
      }
      if (tag === 'label') {
        // a label around its field: the field's entry already carries the label text
        const field = el.querySelector('input:not([type=hidden]),select,textarea')
        if (field && el.querySelectorAll('input:not([type=hidden]),select,textarea').length === 1) {
          line += ' ' + describe(field) + ' '
          return
        }
      }
      if (isInteractive(el)) {
        line += ' ' + describe(el) + ' '
        // its text is already its name; go inside only for controls nested in it (a card link with buttons)
        if (!el.querySelector(NESTED)) return
      }
      const block = BLOCK.has(tag)
      if (block) flush()
      if (/^h[1-6]$/.test(tag)) line += '#'.repeat(+tag[1]) + ' '
      else if (tag === 'li') line += '- '
      else if (tag === 'td' || tag === 'th') line += ' | '
      else if (tag === 'img' && el.alt && el.alt.trim()) line += ` [image: ${clip(el.alt.trim(), 80)}] `
      if (el.shadowRoot) for (const child of el.shadowRoot.childNodes) walk(child)
      for (const child of el.childNodes) walk(child)
      if (block) flush()
    }
    walk(root)
    flush()
    return lines.join('\n')
  }

  function visible(el) {
    if (el.checkVisibility) {
      if (el.checkVisibility({ visibilityProperty: true, opacityProperty: true })) return true
      // display: contents has no box of its own, but its children do
      return getComputedStyle(el).display === 'contents'
    }
    return !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
  }

  function isInteractive(el) {
    const tag = el.localName
    if (tag === 'a') return el.hasAttribute('href')
    if (tag === 'button' || tag === 'select' || tag === 'textarea' || tag === 'summary') return true
    if (tag === 'input') return el.type !== 'hidden'
    const role = el.getAttribute('role')
    if (role && ROLES.has(role)) return true
    if (el.isContentEditable) return !(el.parentElement && el.parentElement.isContentEditable)
    return false
  }

  function number(el) {
    let n = numberOf.get(el)
    if (!n) {
      n = ++last
      numberOf.set(el, n)
      byNumber.set(n, new WeakRef(el))
    }
    return n
  }

  function kindOf(el) {
    const tag = el.localName, role = el.getAttribute('role')
    if (role) return role
    if (tag === 'a') return 'link'
    if (tag === 'input') return INPUT_KIND[(el.getAttribute('type') || '').toLowerCase()] || 'input'
    if (tag === 'textarea') return 'input'
    if (tag === 'summary') return 'button'
    if (el.isContentEditable) return 'editor'
    return tag
  }

  function describe(el) {
    const tag = el.localName, kind = kindOf(el)
    let s = `[${number(el)}] ${kind}`
    const name = clip(clean(accessibleName(el)), 100)
    if (name) s += ` "${name}"`
    if (kind === 'checkbox' || kind === 'radio' || kind === 'switch') {
      const on = el.checked || el.getAttribute('aria-checked') === 'true'
      s += on ? ' (checked)' : ' (unchecked)'
    } else if (tag === 'input' || tag === 'textarea') {
      if (el.type === 'password') s += el.value ? ' =(filled)' : ''
      else if (el.value) s += ` ="${clip(el.value, 80)}"`
      if (el.placeholder && el.placeholder !== name) s += ` (${clip(el.placeholder, 60)})`
    } else if (tag === 'select') {
      const chosen = el.options[el.selectedIndex]
      s += ` ="${chosen ? clip(chosen.text.trim(), 60) : ''}" options: ` +
        Array.from(el.options).slice(0, 25).map(function (o) { return clip(o.text.trim(), 40) }).join(' | ')
    } else if (el.isContentEditable) {
      const v = clean(el.innerText || '')
      if (v && v !== name) s += ` ="${clip(v, 200)}"`
    }
    if (el.getAttribute('aria-expanded')) s += el.getAttribute('aria-expanded') === 'true' ? ' (open)' : ' (closed)'
    if (el.getAttribute('aria-selected') === 'true') s += ' (selected)'
    if (el.disabled || el.getAttribute('aria-disabled') === 'true') s += ' (disabled)'
    return s
  }

  function accessibleName(el) {
    const aria = el.getAttribute('aria-label')
    if (aria && aria.trim()) return aria
    const by = el.getAttribute('aria-labelledby')
    if (by) {
      const parts = by.split(/\s+/).map(function (id) { return document.getElementById(id) }).filter(Boolean).map(function (n) { return n.textContent })
      if (parts.length) return parts.join(' ')
    }
    if (el.isContentEditable) return el.getAttribute('aria-placeholder') || el.getAttribute('title') || ''
    const tag = el.localName
    if (tag === 'input' || tag === 'textarea' || tag === 'select') {
      if (el.id) {
        const label = document.querySelector(`label[for="${CSS.escape(el.id)}"]`)
        if (label) return label.textContent
      }
      const wrap = el.closest('label')
      if (wrap) return wrap.textContent
      if (tag === 'input' && /^(submit|button|reset)$/i.test(el.type)) return el.value
      return el.getAttribute('placeholder') || el.getAttribute('title') || el.getAttribute('name') || ''
    }
    const text = el.innerText || el.textContent || ''
    if (text.trim()) return text
    const img = el.querySelector && el.querySelector('img[alt]')
    return el.getAttribute('title') || (img && img.alt) || ''
  }

  function scrollState() {
    const max = Math.max(1, document.documentElement.scrollHeight - innerHeight)
    return Math.round(Math.min(1, scrollY / max) * 100)
  }

  // ── acting ──

  function act(msg) {
    if (msg.action === 'scroll' && !msg.ref) return scroll(msg.direction)
    // history.back() from the page: the browser's own Back (chrome.tabs.goBack) skips entries a script added
    // without a user gesture, which is every page the agent reached by clicking
    if (msg.action === 'back') { setTimeout(function () { history.back() }, 0); return { ok: true, did: 'went back' } }
    if (msg.action === 'key' && !msg.ref) return key(document.activeElement || document.body, msg.keys)
    const ref = byNumber.get(+msg.ref)
    const el = ref && ref.deref()
    if (!el) return { ok: false, error: `No element [${msg.ref}] on this page. Read the page again: numbers are only valid for the page they came from.` }
    if (!el.isConnected) return { ok: false, error: `Element [${msg.ref}] is gone (the page changed). Read the page again.` }
    try { el.scrollIntoView({ block: 'center', inline: 'center' }) } catch (e) { /* ignore */ }
    if (msg.action === 'click') return click(el)
    if (msg.action === 'type') return type(el, String(msg.text == null ? '' : msg.text), !!msg.submit)
    if (msg.action === 'key') return key(el, msg.keys)
    if (msg.action === 'scroll') return { ok: true, did: `scrolled to [${msg.ref}]` }
    return { ok: false, error: 'unknown action ' + msg.action }
  }

  function label(el) {
    const name = clip(clean(accessibleName(el)), 60)
    return `[${number(el)}] ${kindOf(el)}` + (name ? ` "${name}"` : '')
  }

  function click(el) {
    const opts = { bubbles: true, cancelable: true, view: window }
    el.dispatchEvent(new PointerEvent('pointerdown', opts))
    el.dispatchEvent(new MouseEvent('mousedown', opts))
    try { el.focus({ preventScroll: true }) } catch (e) { /* ignore */ }
    el.dispatchEvent(new PointerEvent('pointerup', opts))
    el.dispatchEvent(new MouseEvent('mouseup', opts))
    // One click: el.click() fires the event and runs the default action (follow a link, toggle a box, submit).
    if (typeof el.click === 'function') el.click()
    else el.dispatchEvent(new MouseEvent('click', opts))
    return { ok: true, did: 'clicked ' + label(el) }
  }

  function type(el, text, submit) {
    const tag = el.localName
    if (tag === 'select') return choose(el, text)
    if (tag === 'input' && /^(checkbox|radio)$/i.test(el.type)) return click(el)
    try { el.focus({ preventScroll: true }) } catch (e) { /* ignore */ }
    if (tag === 'input' || tag === 'textarea') {
      try { el.select() } catch (e) { /* ignore */ }
      let done = false
      // insertText goes through the page's own input handling (React, Vue and editors see a real edit)
      try { done = document.execCommand('insertText', false, text) } catch (e) { done = false }
      if (!done || el.value !== text) {
        const proto = tag === 'textarea' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype
        Object.getOwnPropertyDescriptor(proto, 'value').set.call(el, text)
        el.dispatchEvent(new Event('input', { bubbles: true }))
      }
      el.dispatchEvent(new Event('change', { bubbles: true }))
    } else if (el.isContentEditable) {
      const range = document.createRange()
      range.selectNodeContents(el)
      const sel = getSelection()
      sel.removeAllRanges()
      sel.addRange(range)
      let done = false
      try { done = document.execCommand('insertText', false, text) } catch (e) { done = false }
      if (!done) {
        el.textContent = text
        el.dispatchEvent(new InputEvent('input', { bubbles: true, data: text, inputType: 'insertText' }))
      }
    } else {
      return { ok: false, error: `${label(el)} is not a text field.` }
    }
    if (submit) enter(el)
    return { ok: true, did: `typed into ${label(el)}` + (submit ? ' and pressed Enter' : '') }
  }

  function choose(el, value) {
    const wanted = String(value).trim().toLowerCase()
    const options = Array.from(el.options)
    const match = options.find(function (o) { return o.value.toLowerCase() === wanted || o.text.trim().toLowerCase() === wanted }) ||
      options.find(function (o) { return o.text.trim().toLowerCase().indexOf(wanted) >= 0 })
    if (!match) return { ok: false, error: `No option "${value}" in ${label(el)}.` }
    el.value = match.value
    el.dispatchEvent(new Event('input', { bubbles: true }))
    el.dispatchEvent(new Event('change', { bubbles: true }))
    return { ok: true, did: `chose "${match.text.trim()}" in ${label(el)}` }
  }

  function keyEvent(target, type, init) {
    const ev = new KeyboardEvent(type, Object.assign({ bubbles: true, cancelable: true }, init))
    target.dispatchEvent(ev)
    return ev
  }

  // Enter in a field: the page's own key handler first; a form nobody handled is submitted.
  function enter(el) {
    const init = { key: 'Enter', code: 'Enter', keyCode: 13, which: 13 }
    const down = keyEvent(el, 'keydown', init)
    keyEvent(el, 'keypress', init)
    keyEvent(el, 'keyup', init)
    const form = el.form || (el.closest && el.closest('form'))
    if (!down.defaultPrevented && form) {
      try { if (form.requestSubmit) form.requestSubmit(); else form.submit() } catch (e) { /* ignore */ }
    }
  }

  function key(el, keys) {
    const parts = String(keys || '').split('+').map(function (k) { return k.trim() }).filter(Boolean)
    if (!parts.length) return { ok: false, error: 'No keys given (e.g. Enter, Escape, Control+a).' }
    const main = parts[parts.length - 1], mods = parts.slice(0, -1).map(function (m) { return m.toLowerCase() })
    try { el.focus({ preventScroll: true }) } catch (e) { /* ignore */ }
    if (main.toLowerCase() === 'enter' && !mods.length) {
      enter(el)
      return { ok: true, did: 'pressed Enter' }
    }
    const name = main.length === 1 ? main : main[0].toUpperCase() + main.slice(1)
    const init = {
      key: name,
      code: name.length === 1 ? (/\d/.test(name) ? 'Digit' + name : 'Key' + name.toUpperCase()) : name,
      ctrlKey: mods.indexOf('control') >= 0 || mods.indexOf('ctrl') >= 0,
      shiftKey: mods.indexOf('shift') >= 0,
      altKey: mods.indexOf('alt') >= 0,
      metaKey: mods.indexOf('meta') >= 0,
    }
    keyEvent(el, 'keydown', init)
    keyEvent(el, 'keyup', init)
    return { ok: true, did: 'pressed ' + parts.join('+') }
  }

  function scroll(direction) {
    const d = direction || 'down'
    if (d === 'top') scrollTo({ top: 0 })
    else if (d === 'bottom') scrollTo({ top: document.documentElement.scrollHeight })
    else scrollBy({ top: (d === 'up' ? -1 : 1) * Math.round(innerHeight * 0.8) })
    return { ok: true, did: `scrolled ${d} (now ${scrollState()}% down the page)` }
  }

  // ── the "maxedcode is working" frame: the user always sees which tab the agent uses ──

  function overlay(msg) {
    let box = document.getElementById(OVERLAY)
    if (msg.state === 'hide') {
      if (box) box.remove()
      return { ok: true }
    }
    // the texts come in the maxedcode window's language (background.js); the browser's own language without them
    const words = [msg.text || chrome.i18n.getMessage('working'), msg.stop || chrome.i18n.getMessage('stop')]
    if (box) {
      // already shown: only its words, the language may have changed
      const pill = box.firstChild
      if (pill && pill.childNodes.length === 3) { pill.childNodes[1].textContent = words[0]; pill.childNodes[2].textContent = words[1] }
      return { ok: true }
    }
    box = document.createElement('div')
    box.id = OVERLAY
    // inline styles only: the page's CSP may refuse an injected <style>
    box.style.cssText = 'position:fixed;inset:0;pointer-events:none;z-index:2147483647;' +
      'box-shadow:inset 0 0 0 2px rgba(217,119,87,.9),inset 0 0 24px 6px rgba(217,119,87,.18);'
    const pill = document.createElement('div')
    pill.style.cssText = 'position:fixed;bottom:14px;left:50%;transform:translateX(-50%);pointer-events:auto;display:flex;' +
      'align-items:center;gap:8px;background:#1f1e1d;color:#eeeceb;border:1px solid rgba(255,255,255,.14);' +
      'padding:4px 4px 4px 12px;border-radius:999px;box-shadow:0 8px 24px -10px rgba(0,0,0,.6);' +
      'font:600 12px/16px system-ui,"Segoe UI",sans-serif;'
    const dot = document.createElement('span')
    dot.style.cssText = 'width:7px;height:7px;border-radius:50%;background:#d97757;flex:none;'
    const text = document.createElement('span')
    text.textContent = words[0]
    const stop = document.createElement('button')
    stop.textContent = words[1]
    stop.style.cssText = 'background:transparent;color:#eeeceb;border:1px solid rgba(255,255,255,.3);border-radius:999px;' +
      'padding:3px 12px;cursor:pointer;font:inherit;'
    stop.onclick = function () { chrome.runtime.sendMessage({ type: 'stop' }) }
    pill.append(dot, text, stop)
    box.appendChild(pill)
    document.documentElement.appendChild(box)
    return { ok: true }
  }

  // ── numbers drawn over the controls for a screenshot: the model sees [N] and acts by it, as after a read ──

  const MARKS = 'maxedcode-marks'
  const MARK_MAX = 150

  function mark() {
    unmark()
    const layer = document.createElement('div')
    layer.id = MARKS
    layer.style.cssText = 'position:fixed;inset:0;pointer-events:none;z-index:2147483646;'
    const listed = []
    const all = document.querySelectorAll(NESTED + ',summary,[role]')
    for (const el of all) {
      if (listed.length >= MARK_MAX) break
      if (!isInteractive(el) || !visible(el) || el.closest('#' + OVERLAY)) continue
      const r = el.getBoundingClientRect()
      if (r.width < 2 || r.height < 2 || r.bottom < 0 || r.right < 0 || r.top > innerHeight || r.left > innerWidth) continue
      const n = number(el)
      const box = document.createElement('div')
      box.style.cssText = `position:fixed;left:${r.left}px;top:${r.top}px;width:${r.width}px;height:${r.height}px;` +
        'outline:1px solid rgba(217,119,87,.9);border-radius:2px;'
      const tag = document.createElement('div')
      tag.textContent = n
      tag.style.cssText = `position:fixed;left:${Math.max(0, r.left)}px;top:${Math.max(0, r.top - 14)}px;` +
        'background:#d97757;color:#fff;font:700 11px/13px Consolas,monospace;padding:0 3px;border-radius:3px;'
      layer.append(box, tag)
      listed.push(describe(el))
    }
    document.documentElement.appendChild(layer)
    const frame = document.getElementById(OVERLAY)
    if (frame) frame.style.visibility = 'hidden'      // the Stop pill must not cover the page in the picture
    return { ok: true, marked: listed, scrolled: scrollState() }
  }

  function unmark() {
    const layer = document.getElementById(MARKS)
    if (layer) layer.remove()
    const frame = document.getElementById(OVERLAY)
    if (frame) frame.style.visibility = ''
    return { ok: true }
  }

  // Where to click so typing lands somewhere, when nothing editable has the focus: the largest visible editor
  // (Google Docs' page, a contenteditable, a textbox). {} when something already takes typing.
  function editor() {
    const active = document.activeElement
    if (active && active !== document.body && (active.isContentEditable || active.localName === 'iframe' ||
        active.localName === 'textarea' || (active.localName === 'input' && !/^(button|submit|checkbox|radio)$/i.test(active.type)))) {
      return { ok: true }
    }
    let best = null, area = 0
    // Google Docs: its white page (the editor around it is mostly grey margin); elsewhere any editable area
    const docs = document.querySelectorAll('.kix-page-paginated, .kix-page')
    const pool = docs.length ? docs : document.querySelectorAll('[contenteditable=""], [contenteditable=true], [role=textbox], textarea')
    pool.forEach(function (el) {
      if (!visible(el)) return
      const r = el.getBoundingClientRect()
      const w = Math.min(r.right, innerWidth) - Math.max(r.left, 0), h = Math.min(r.bottom, innerHeight) - Math.max(r.top, 0)
      if (w > 20 && h > 20 && w * h > area) { area = w * h; best = r }
    })
    if (!best) return { ok: true }
    // near the top left of its visible part: the start of a document, not the middle of its text
    return { ok: true, x: Math.round(Math.max(best.left, 0) + Math.min(80, best.width / 2)), y: Math.round(Math.max(best.top, 0) + Math.min(60, best.height / 2)) }
  }

  function clean(s) { return String(s || '').replace(/\s+/g, ' ').trim() }
  function clip(s, n) { s = String(s || ''); return s.length > n ? s.slice(0, n - 1) + '…' : s }
})()
