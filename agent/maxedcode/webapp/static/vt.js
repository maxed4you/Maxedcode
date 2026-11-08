/* maxedcode app: a small terminal emulator of our own (no xterm.js) for the side panel's terminal.
 * The server runs the shell on a Windows pseudo console (webapp/pty.py); ConPTY sends VT sequences, this file
 * keeps the screen (cells with colours), scrollback, the cursor and the alternate screen, draws it as DOM rows and
 * turns keys into the bytes a shell expects.
 *
 *   var vt = MxTerm(box, { send: function (data) {}, resize: function (cols, rows) {}, title: function (t) {} })
 *   vt.write(text)  vt.fit()  vt.focus()  vt.dispose()
 */
(function () {
  'use strict'
  var PALETTE = ['#1e1e1e', '#e06c75', '#98c379', '#e5c07b', '#61afef', '#c678dd', '#56b6c2', '#d4d4d4',
    '#7f848e', '#ff7b86', '#b5e890', '#ffd68a', '#7cc4ff', '#de9bf2', '#7fd8e3', '#ffffff']
  var SCROLLBACK = 3000
  var WIDE = /[ᄀ-ᅟ⺀-〾ぁ-㏿㐀-䶿一-鿿ꀀ-꓏가-힣豈-﫿︰-﹏＀-｠￠-￦]|[\ud83c-\ud83e][\udc00-\udfff]/

  function color256(n) {
    if (n < 16) return PALETTE[n]
    if (n >= 232) { var g = 8 + (n - 232) * 10; return 'rgb(' + g + ',' + g + ',' + g + ')' }
    n -= 16
    var v = [0, 95, 135, 175, 215, 255]
    return 'rgb(' + v[Math.floor(n / 36)] + ',' + v[Math.floor(n / 6) % 6] + ',' + v[n % 6] + ')'
  }
  function blankAttr() { return { fg: null, bg: null, bold: false, dim: false, italic: false, under: false, inverse: false } }
  function attrKey(a) {
    return [a.fg || '', a.bg || '', a.bold ? 1 : 0, a.dim ? 1 : 0, a.italic ? 1 : 0, a.under ? 1 : 0, a.inverse ? 1 : 0].join('|')
  }
  function esc(s) { return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;') }

  window.MxTerm = function (box, opts) {
    opts = opts || {}
    var cols = 80, rows = 24
    var attr = blankAttr(), styles = {}
    var screen, main = null, alt = false, scrollback = []
    var x = 0, y = 0, wrapPending = false, saved = { x: 0, y: 0, attr: blankAttr() }
    var top = 0, bottom = rows - 1, cursorOn = true, appKeys = false, bracketed = false
    var state = 'ground', params = '', osc = '', dirty = {}, sbAdded = 0, frame = 0, focused = false

    var view = document.createElement('div')
    view.className = 'vt'
    view.tabIndex = -1
    var sbBox = document.createElement('div')
    var scrBox = document.createElement('div')
    view.appendChild(sbBox)
    view.appendChild(scrBox)
    var input = document.createElement('textarea')
    input.className = 'vt-input'
    input.spellcheck = false
    input.setAttribute('autocomplete', 'off')
    var measure = document.createElement('span')
    measure.className = 'vt-measure'
    measure.textContent = 'MMMMMMMMMM'
    box.appendChild(view)
    box.appendChild(input)
    box.appendChild(measure)

    function styleId(a) {
      var k = attrKey(a)
      if (!styles[k]) styles[k] = { fg: a.fg, bg: a.bg, bold: a.bold, dim: a.dim, italic: a.italic, under: a.under, inverse: a.inverse, key: k }
      return styles[k]
    }
    var BLANK = styleId(blankAttr())
    function blankRow(st) {
      var r = { ch: new Array(cols), st: new Array(cols) }
      for (var i = 0; i < cols; i++) { r.ch[i] = ' '; r.st[i] = st || BLANK }
      return r
    }
    function newScreen() { var s = []; for (var i = 0; i < rows; i++) s.push(blankRow()); return s }
    screen = newScreen()
    for (var i = 0; i < rows; i++) {
      var d = document.createElement('div')
      d.className = 'vt-row'
      scrBox.appendChild(d)
    }
    function touch(r) { dirty[r] = true; schedule() }
    function touchAll() { for (var r = 0; r < rows; r++) dirty[r] = true; schedule() }
    function schedule() { if (!frame) frame = requestAnimationFrame(render) }
    function bgAttr() { var a = blankAttr(); a.bg = attr.bg; return styleId(a) }

    // ── screen operations ──
    function scrollUp(n) {
      for (var k = 0; k < n; k++) {
        var gone = screen.splice(top, 1)[0]
        if (top === 0 && !alt) { scrollback.push(gone); sbAdded++ }
        screen.splice(bottom, 0, blankRow(bgAttr()))
      }
      if (scrollback.length > SCROLLBACK) scrollback.splice(0, scrollback.length - SCROLLBACK)
      for (var r = top; r <= bottom; r++) dirty[r] = true
      schedule()
    }
    function scrollDown(n) {
      for (var k = 0; k < n; k++) {
        screen.splice(bottom, 1)
        screen.splice(top, 0, blankRow(bgAttr()))
      }
      for (var r = top; r <= bottom; r++) dirty[r] = true
      schedule()
    }
    function lineFeed() {
      if (y === bottom) scrollUp(1)
      else if (y < rows - 1) y++
    }
    function clamp() {
      x = Math.max(0, Math.min(cols - 1, x))
      y = Math.max(0, Math.min(rows - 1, y))
    }
    function put(ch) {
      var wide = WIDE.test(ch)
      if (wrapPending || (wide && x === cols - 1)) { x = 0; lineFeed(); wrapPending = false }
      var row = screen[y], st = styleId(attr)
      row.ch[x] = ch
      row.st[x] = st
      if (wide && x + 1 < cols) { row.ch[x + 1] = ''; row.st[x + 1] = st; x++ }
      touch(y)
      if (x >= cols - 1) wrapPending = true
      else x++
    }
    function erase(row, from, to) {
      var st = bgAttr(), r = screen[row]
      for (var i = Math.max(0, from); i < Math.min(cols, to); i++) { r.ch[i] = ' '; r.st[i] = st }
      touch(row)
    }
    function setAlt(on) {
      if (on === alt) return
      if (on) { main = { screen: screen, x: x, y: y }; screen = newScreen(); alt = true }
      else { screen = main ? main.screen : newScreen(); if (main) { x = main.x; y = main.y }; alt = false; main = null }
      touchAll()
    }

    function sgr(list) {
      if (!list.length) list = [0]
      for (var i = 0; i < list.length; i++) {
        var c = list[i]
        if (c === 0) attr = blankAttr()
        else if (c === 1) attr.bold = true
        else if (c === 2) attr.dim = true
        else if (c === 3) attr.italic = true
        else if (c === 4) attr.under = true
        else if (c === 7) attr.inverse = true
        else if (c === 22) { attr.bold = false; attr.dim = false }
        else if (c === 23) attr.italic = false
        else if (c === 24) attr.under = false
        else if (c === 27) attr.inverse = false
        else if (c >= 30 && c <= 37) attr.fg = PALETTE[c - 30]
        else if (c >= 90 && c <= 97) attr.fg = PALETTE[c - 90 + 8]
        else if (c === 39) attr.fg = null
        else if (c >= 40 && c <= 47) attr.bg = PALETTE[c - 40]
        else if (c >= 100 && c <= 107) attr.bg = PALETTE[c - 100 + 8]
        else if (c === 49) attr.bg = null
        else if (c === 38 || c === 48) {
          var value = null
          if (list[i + 1] === 5) { value = color256(list[i + 2] || 0); i += 2 }
          else if (list[i + 1] === 2) { value = 'rgb(' + (list[i + 2] || 0) + ',' + (list[i + 3] || 0) + ',' + (list[i + 4] || 0) + ')'; i += 4 }
          if (c === 38) attr.fg = value
          else attr.bg = value
        }
      }
    }

    function csi(final, raw) {
      var priv = /^[?>=]/.test(raw) ? raw[0] : ''
      var list = (priv ? raw.slice(1) : raw).split(';').map(function (v) { return v === '' ? NaN : parseInt(v, 10) })
      var p0 = isNaN(list[0]) ? 0 : list[0], n = p0 || 1
      if (priv === '?') {
        if (final === 'h' || final === 'l') {
          var on = final === 'h'
          list.forEach(function (m) {
            if (m === 25) { cursorOn = on; touch(y) }
            else if (m === 1) appKeys = on
            else if (m === 2004) bracketed = on
            else if (m === 1049 || m === 1047 || m === 47) {
              if (m === 1049 && on) saved = { x: x, y: y, attr: attr }
              setAlt(on)
              if (m === 1049 && !on) { x = saved.x; y = saved.y }
            }
          })
        }
        return
      }
      if (priv) return
      wrapPending = false
      switch (final) {
        case 'A': y = Math.max(y < top ? 0 : top, y - n); break
        case 'B': y = Math.min(y > bottom ? rows - 1 : bottom, y + n); break
        case 'C': x = Math.min(cols - 1, x + n); break
        case 'D': x = Math.max(0, x - n); break
        case 'E': x = 0; y = Math.min(rows - 1, y + n); break
        case 'F': x = 0; y = Math.max(0, y - n); break
        case 'G': case '`': x = n - 1; break
        case 'd': y = n - 1; break
        case 'H': case 'f': y = (isNaN(list[0]) ? 1 : list[0] || 1) - 1; x = (isNaN(list[1]) ? 1 : list[1] || 1) - 1; break
        case 'J':
          if (p0 === 0) { erase(y, x, cols); for (var r = y + 1; r < rows; r++) erase(r, 0, cols) }
          else if (p0 === 1) { erase(y, 0, x + 1); for (var r1 = 0; r1 < y; r1++) erase(r1, 0, cols) }
          else { for (var r2 = 0; r2 < rows; r2++) erase(r2, 0, cols); if (p0 === 3) { scrollback = []; sbBox.innerHTML = ''; sbAdded = 0 } }
          break
        case 'K':
          if (p0 === 0) erase(y, x, cols)
          else if (p0 === 1) erase(y, 0, x + 1)
          else erase(y, 0, cols)
          break
        case 'X': erase(y, x, x + n); break
        case 'P': case '@': {
          var row = screen[y], st = bgAttr()
          if (final === 'P') { row.ch.splice(x, n); row.st.splice(x, n); for (var a = 0; a < n; a++) { row.ch.push(' '); row.st.push(st) } }
          else { for (var b = 0; b < n; b++) { row.ch.splice(x, 0, ' '); row.st.splice(x, 0, st) } row.ch.length = cols; row.st.length = cols }
          touch(y)
          break
        }
        case 'L': case 'M':
          if (y < top || y > bottom) break
          var keep = top
          top = y
          if (final === 'L') scrollDown(n)
          else scrollUp(n)
          top = keep
          break
        case 'S': scrollUp(n); break
        case 'T': scrollDown(n); break
        case 'm': sgr(list.map(function (v) { return isNaN(v) ? 0 : v })); break
        case 'r':
          top = (isNaN(list[0]) ? 1 : list[0]) - 1
          bottom = (isNaN(list[1]) ? rows : list[1]) - 1
          if (top >= bottom || bottom >= rows) { top = 0; bottom = rows - 1 }
          x = 0; y = 0
          break
        case 's': saved = { x: x, y: y, attr: attr }; break
        case 'u': x = saved.x; y = saved.y; break
        case 'n':
          if (p0 === 6 && opts.send) opts.send('\x1b[' + (y + 1) + ';' + (x + 1) + 'R')
          else if (p0 === 5 && opts.send) opts.send('\x1b[0n')
          break
        case 'c': if (opts.send && !p0) opts.send('\x1b[?1;0c'); break
      }
      clamp()
    }

    function write(text) {
      var before = y
      for (var i = 0; i < text.length; i++) {
        var ch = text[i], code = text.charCodeAt(i)
        if (code >= 0xd800 && code <= 0xdbff && i + 1 < text.length) { ch = text.slice(i, i + 2); i++ }
        if (state === 'ground') {
          if (ch === '\x1b') state = 'esc'
          else if (ch === '\r') { x = 0; wrapPending = false }
          else if (ch === '\n' || ch === '\x0b' || ch === '\x0c') { lineFeed(); wrapPending = false }
          else if (ch === '\b') { x = Math.max(0, x - 1); wrapPending = false }
          else if (ch === '\t') { x = Math.min(cols - 1, (Math.floor(x / 8) + 1) * 8); wrapPending = false }
          else if (code < 32 || code === 127) { /* bell and other controls */ }
          else put(ch)
        } else if (state === 'esc') {
          state = 'ground'
          if (ch === '[') { state = 'csi'; params = '' }
          else if (ch === ']') { state = 'osc'; osc = '' }
          else if (ch === '(' || ch === ')' || ch === '*' || ch === '+') state = 'charset'
          else if (ch === '7') saved = { x: x, y: y, attr: attr }
          else if (ch === '8') { x = saved.x; y = saved.y; attr = saved.attr }
          else if (ch === 'D') lineFeed()
          else if (ch === 'E') { x = 0; lineFeed() }
          else if (ch === 'M') { if (y === top) scrollDown(1); else if (y > 0) y-- }
          else if (ch === 'c') { attr = blankAttr(); screen = newScreen(); x = y = 0; top = 0; bottom = rows - 1; touchAll() }
        } else if (state === 'charset') state = 'ground'
        else if (state === 'csi') {
          if (code >= 0x40 && code <= 0x7e) { state = 'ground'; csi(ch, params) }
          else params += ch
        } else if (state === 'osc') {
          if (ch === '\x07') { state = 'ground'; title(osc) }
          else if (ch === '\x1b') state = 'osc-esc'
          else osc += ch
        } else if (state === 'osc-esc') { state = 'ground'; title(osc) }
      }
      if (before !== y) { touch(before); touch(y) }
      else touch(y)
    }
    function title(text) {
      var m = /^[02];(.*)$/.exec(text)
      if (m && opts.title) opts.title(m[1])
    }

    // ── drawing ──
    function rowHtml(row, cursorAt) {
      var out = '', run = '', cur = null
      function flush() {
        if (!run) return
        out += cur === BLANK ? esc(run) : '<span style="' + css(cur) + '">' + esc(run) + '</span>'
        run = ''
      }
      for (var i = 0; i < row.ch.length; i++) {
        var st = row.st[i], ch = row.ch[i]
        if (i === cursorAt) {
          flush()
          out += '<span class="vt-cursor' + (focused ? '' : ' idle') + '">' + esc(ch || ' ') + '</span>'
          cur = null
          continue
        }
        if (st !== cur) { flush(); cur = st }
        run += ch
      }
      flush()
      return out.replace(/(\s|&nbsp;)+$/, '') || ' '
    }
    function css(st) {
      var fg = st.fg, bg = st.bg, s = ''
      if (st.inverse) { var t = fg || 'var(--vt-fg)'; fg = bg || 'var(--vt-bg)'; bg = t }
      if (fg) s += 'color:' + fg + ';'
      if (bg) s += 'background:' + bg + ';'
      if (st.bold) s += 'font-weight:600;'
      if (st.dim) s += 'opacity:.65;'
      if (st.italic) s += 'font-style:italic;'
      if (st.under) s += 'text-decoration:underline;'
      return s
    }
    function render() {
      frame = 0
      var stick = view.scrollHeight - view.scrollTop - view.clientHeight < 40
      if (sbAdded) {
        var start = Math.max(0, scrollback.length - sbAdded), html = ''
        for (var i = start; i < scrollback.length; i++) html += '<div class="vt-row">' + rowHtml(scrollback[i], -1) + '</div>'
        sbBox.insertAdjacentHTML('beforeend', html)
        while (sbBox.childNodes.length > SCROLLBACK) sbBox.removeChild(sbBox.firstChild)
        sbAdded = 0
      }
      var nodes = scrBox.childNodes
      for (var r in dirty) {
        r = +r
        if (r < rows && nodes[r]) nodes[r].innerHTML = rowHtml(screen[r], cursorOn && r === y ? x : -1)
      }
      dirty = {}
      if (stick) view.scrollTop = view.scrollHeight
    }

    // ── size ──
    function resize(c, r) {
      c = Math.max(10, c); r = Math.max(4, r)
      if (c === cols && r === rows) return
      var fit = function (s) {
        s.forEach(function (row) {
          while (row.ch.length < c) { row.ch.push(' '); row.st.push(BLANK) }
          row.ch.length = c; row.st.length = c
        })
        return s
      }
      cols = c
      fit(screen)
      if (main) fit(main.screen)
      while (screen.length > r) {           // the top lines leave for the scrollback, as a terminal does
        var gone = screen.shift()
        if (!alt) { scrollback.push(gone); sbAdded++ }
        y = Math.max(0, y - 1)
      }
      while (screen.length < r) screen.push(blankRow())
      rows = r
      top = 0; bottom = rows - 1
      scrBox.innerHTML = ''
      for (var i = 0; i < rows; i++) {
        var d = document.createElement('div')
        d.className = 'vt-row'
        scrBox.appendChild(d)
      }
      clamp()
      touchAll()
      if (opts.resize) opts.resize(cols, rows)
    }
    function fit() {
      var w = measure.getBoundingClientRect().width / 10, h = measure.getBoundingClientRect().height
      if (!w || !h || !view.clientWidth) return
      var style = getComputedStyle(view)
      var innerW = view.clientWidth - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight)
      var innerH = view.clientHeight - parseFloat(style.paddingTop) - parseFloat(style.paddingBottom)
      resize(Math.floor(innerW / w), Math.floor(innerH / h))
    }
    // one fit per frame; the scrollbar is always there (.vt overflow-y: scroll), so drawing rows cannot change the
    // width and start a resize loop
    var fitting = 0
    var observer = new ResizeObserver(function () {
      if (!fitting) fitting = requestAnimationFrame(function () { fitting = 0; fit() })
    })
    observer.observe(view)

    // ── keyboard and clipboard ──
    function send(data) { if (opts.send) opts.send(data) }
    var KEYS = { Enter: '\r', Backspace: '\x7f', Tab: '\t', Escape: '\x1b', Home: '\x1b[H', End: '\x1b[F', Insert: '\x1b[2~',
      Delete: '\x1b[3~', PageUp: '\x1b[5~', PageDown: '\x1b[6~', F1: '\x1bOP', F2: '\x1bOQ', F3: '\x1bOR', F4: '\x1bOS',
      F5: '\x1b[15~', F6: '\x1b[17~', F7: '\x1b[18~', F8: '\x1b[19~', F9: '\x1b[20~', F10: '\x1b[21~', F11: '\x1b[23~', F12: '\x1b[24~' }
    var ARROWS = { ArrowUp: 'A', ArrowDown: 'B', ArrowRight: 'C', ArrowLeft: 'D' }
    function selected() { var s = window.getSelection(); return s && !s.isCollapsed && box.contains(s.anchorNode) ? s.toString() : '' }
    input.addEventListener('keydown', function (e) {
      if (e.isComposing) return
      var mod = (e.shiftKey ? 1 : 0) + (e.altKey ? 2 : 0) + (e.ctrlKey ? 4 : 0)
      if (e.ctrlKey && (e.code === 'Backquote' || (e.shiftKey && e.code === 'KeyB'))) return   // the app's shortcuts
      e.stopPropagation()
      if (e.ctrlKey && (e.code === 'KeyV' || (e.shiftKey && e.code === 'KeyV'))) return          // the paste event
      if (e.ctrlKey && e.code === 'KeyC' && (e.shiftKey || selected())) {
        e.preventDefault()
        var text = selected()
        if (text) navigator.clipboard.writeText(text.replace(/ /g, ' ').replace(/[ \t]+$/gm, ''))
        return
      }
      var seq = null
      if (ARROWS[e.key]) seq = mod > 1 || (mod === 1) ? '\x1b[1;' + (mod + 1) + ARROWS[e.key] : (appKeys ? '\x1bO' : '\x1b[') + ARROWS[e.key]
      else if (e.key === 'Tab' && e.shiftKey) seq = '\x1b[Z'
      else if (KEYS[e.key]) seq = KEYS[e.key]
      else if (e.ctrlKey && !e.altKey && e.code && /^Key[A-Z]$/.test(e.code)) seq = String.fromCharCode(e.code.charCodeAt(3) - 64)
      else if (e.ctrlKey && e.code === 'Space') seq = '\x00'
      else if (e.ctrlKey && e.code === 'BracketLeft') seq = '\x1b'
      else if (e.ctrlKey && e.code === 'Backslash') seq = '\x1c'
      else if (e.altKey && !e.ctrlKey && e.key.length === 1) seq = '\x1b' + e.key
      if (seq !== null) {
        e.preventDefault()
        if (e.ctrlKey && e.key === 'Backspace') seq = '\x17'
        send(seq)
        view.scrollTop = view.scrollHeight
      }
    })
    input.addEventListener('input', function () {
      if (input.value) { send(input.value); input.value = ''; view.scrollTop = view.scrollHeight }
    })
    input.addEventListener('paste', function (e) {
      e.preventDefault()
      e.stopPropagation()
      var text = (e.clipboardData && e.clipboardData.getData('text/plain')) || ''
      if (!text) return
      text = text.replace(/\r?\n/g, '\r')
      send(bracketed ? '\x1b[200~' + text + '\x1b[201~' : text)
    })
    input.addEventListener('focus', function () { focused = true; box.classList.add('focus'); touch(y) })
    input.addEventListener('blur', function () { focused = false; box.classList.remove('focus'); touch(y) })
    // a click focuses the input unless it ended a text selection; right click: copy the selection or paste
    view.addEventListener('mouseup', function () { if (!selected()) input.focus({ preventScroll: true }) })
    view.addEventListener('contextmenu', function (e) {
      e.preventDefault()
      var text = selected()
      if (text) { navigator.clipboard.writeText(text.replace(/ /g, ' ').replace(/[ \t]+$/gm, '')); window.getSelection().removeAllRanges() }
      else navigator.clipboard.readText().then(function (t) { if (t) send(bracketed ? '\x1b[200~' + t.replace(/\r?\n/g, '\r') + '\x1b[201~' : t.replace(/\r?\n/g, '\r')) })
      input.focus({ preventScroll: true })
    })

    touchAll()
    return {
      write: write,
      fit: fit,
      focus: function () { input.focus({ preventScroll: true }) },
      size: function () { return { cols: cols, rows: rows } },
      dispose: function () { observer.disconnect(); if (frame) cancelAnimationFrame(frame) },
    }
  }
})()
