#!/usr/bin/env node
// npm entry point (npm i -g maxedcode / npx maxedcode).
// The first run of this package opens the setup window (agent/install.py + install_gui.py, as Maxedcode_setup.exe):
// the install folder, the model server, the options; a maxedcode installed before (the zip, install.ps1) is offered
// as the folder to update. After an npm update to a version newer than the installed one, the next run updates that
// install quietly (install.py --update: the server, chats, settings, shortcuts and PATH stay); an install that
// updated itself from GitHub (maxedcode/update.py) to something newer is never taken back. Then it runs maxed with
// the same arguments: maxedcode, maxedcode app, maxedcode --print "..." ...
// Right after the setup window nothing more starts by itself: the window has its own "Open maxedcode" button.
// Files npm downloads carry no "from the internet" mark, so nothing here needs a paid code signature.
// One package for every system: on Linux / macOS it finds python3 3.10+ (and says how to get it when there is none),
// installs to $XDG_DATA_HOME/maxedcode (~/.local/share/maxedcode) and runs program/bin/maxed (a shell script);
// the folder note, the update and the uninstall work the same way.
'use strict'

const { spawnSync } = require('child_process')
const fs = require('fs')
const os = require('os')
const path = require('path')
const pkg = require('../package.json')

const ROOT = path.resolve(__dirname, '..')
const HOME = path.join(os.homedir(), '.maxedcode')
const STAMP = path.join(HOME, 'npm-install.json')
const WIN = process.platform === 'win32'
const LOG = WIN ? '%TEMP%\\maxedcode-setup.log' : path.join(os.tmpdir(), 'maxedcode-setup.log')

function fail (msg) {
  console.error('  maxedcode: ' + msg)
  process.exit(1)
}

function readStamp () {
  try { return JSON.parse(fs.readFileSync(STAMP, 'utf8')) } catch (e) { return {} }
}

// Linux / macOS: installer/bootstrap.sh finds a Python 3.10+ whose venv works, else gets uv and its Python 3.12
// into ~/.maxedcode/uv (no sudo); only when even that fails it prints how to install Python by hand
function findPosixPython () {
  const out = path.join(os.tmpdir(), 'maxedcode-npm-python-' + process.pid + '.txt')
  const r = spawnSync('sh', [path.join(ROOT, 'installer', 'bootstrap.sh')],
    { stdio: 'inherit', env: Object.assign({}, process.env, { MX_PYFILE: out }) })
  let py = ''
  try { py = fs.readFileSync(out, 'utf8').trim() } catch (e) {}
  fs.rmSync(out, { force: true })
  if (r.status !== 0 || !py) fail('no Python 3.10+ to install with (see above)')
  return py
}

// Python 3.10+ that is not the Microsoft Store one; installer/bootstrap.ps1 installs Python 3.12 when there is none
function findPython () {
  if (!WIN) return findPosixPython()
  const out = path.join(os.tmpdir(), 'maxedcode-npm-python-' + process.pid + '.txt')
  const r = spawnSync('powershell.exe', ['-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
    path.join(ROOT, 'installer', 'bootstrap.ps1')], { stdio: 'inherit', env: Object.assign({}, process.env, { MX_PYFILE: out }) })
  let py = ''
  try { py = fs.readFileSync(out, 'utf8').trim() } catch (e) {}
  fs.rmSync(out, { force: true })
  if (r.status !== 0 || !py) fail('Python 3.10+ was not found and could not be installed: https://www.python.org/downloads/')
  return py
}

const bat = d => path.join(d, 'program', 'bin', 'maxed.bat')
// the launcher of an install: maxed.bat on Windows, the shell script maxed on Linux / macOS
const launcher = d => WIN ? bat(d) : path.join(d, 'program', 'bin', 'maxed')

// the default install folder (install.py DEFAULT_DIR)
function defaultDir () {
  if (WIN) return path.join(process.env.LOCALAPPDATA || path.join(os.homedir(), 'AppData', 'Local'), 'Programs', 'maxedcode')
  return path.join(process.env.XDG_DATA_HOME || path.join(os.homedir(), '.local', 'share'), 'maxedcode')
}

// "1.2.10" -> [1, 2, 10]; newer(a, b): a is above b
const parse = v => (/^v?(\d+(\.\d+)*)$/.exec(String(v || '').trim()) || [null, ''])[1].split('.').filter(Boolean).map(Number)
function newer (a, b) {
  const x = parse(a); const y = parse(b)
  for (let i = 0; i < Math.max(x.length, y.length); i++) if ((x[i] || 0) !== (y[i] || 0)) return (x[i] || 0) > (y[i] || 0)
  return false
}

// the version of the program installed in dir (maxedcode/version.py); '0' for one older than that file
function installedVersion (dir) {
  try {
    const m = /VERSION\s*=\s*"([^"]+)"/.exec(fs.readFileSync(path.join(dir, 'program', 'maxedcode', 'version.py'), 'utf8'))
    if (m) return m[1]
  } catch (e) {}
  return '0'
}

// -> the folder installed to, or '' when the setup window was closed before installing
function install (dir, wizard) {
  // --dir: the folder the wizard offers first (it asks anyway); for an update, the one to update
  const args = [path.join(ROOT, 'agent', 'install.py'), '--dir', dir]
  // the wizard is the setup window (install.py falls back to the console wizard, and with no console to the defaults);
  // an update replaces the program files and asks nothing
  if (!wizard) args.push('--update')
  // extra setup options, as for install.ps1: MAXEDCODE_ARGS="--base-url 192.168.0.10:1234 --model qwen3"
  if (process.env.MAXEDCODE_ARGS) args.push.apply(args, process.env.MAXEDCODE_ARGS.split(/\s+/).filter(Boolean))
  console.log(wizard ? '\n  maxedcode: first start, opening the setup window ...' : '\n  maxedcode: updating to ' + pkg.version + ' ...')
  // the setup window may install to another folder: install.py writes the one it used to MX_DIRFILE
  const dirFile = path.join(os.tmpdir(), 'maxedcode-npm-dir-' + process.pid + '.txt')
  const r = spawnSync(findPython(), args, { stdio: 'inherit', env: Object.assign({}, process.env, { MX_DIRFILE: dirFile }) })
  let used = ''
  try { used = fs.readFileSync(dirFile, 'utf8').trim() } catch (e) {}
  fs.rmSync(dirFile, { force: true })
  if (r.status !== 0) fail('setup stopped (code ' + r.status + '); the log: ' + LOG)
  if (!used) return ''
  fs.mkdirSync(HOME, { recursive: true })
  fs.writeFileSync(STAMP, JSON.stringify({ version: pkg.version, dir: used }, null, 2))
  return used
}

// maxedcode uninstall [--purge]: the program (install.py --uninstall asks first), then this npm package.
// npm runs no uninstall scripts since v7, so npm uninstall alone would leave the program, shortcuts and PATH.
function uninstall (dir, rest) {
  if (fs.existsSync(path.join(dir, 'program', 'install.py'))) {
    const args = [path.join(ROOT, 'agent', 'install.py'), '--uninstall', '--dir', dir].concat(rest)
    const r = spawnSync(findPython(), args, { stdio: 'inherit' })
    if (r.status !== 0) process.exit(r.status === null ? 1 : r.status)   // cancelled: the npm package stays too
  }
  fs.rmSync(STAMP, { force: true })
  console.log('\n  maxedcode: removing the npm package (npm uninstall -g maxedcode) ...')
  const r = spawnSync('npm uninstall -g maxedcode', { stdio: 'inherit', shell: true })
  process.exit(r.status === null ? 1 : r.status)
}

function installed (dir) {
  if (!WIN) {
    return console.log('\n  maxedcode is installed in ' + dir +
      '\n    app:  maxedcode app (maxed app in a new terminal)' + (process.platform === 'darwin' ? ', maxedcode in Launchpad' : '') +
      '\n    CLI:  maxedcode (maxed in a new terminal)' +
      '\n    model server, folder again: maxedcode setup\n')
  }
  console.log('\n  maxedcode is installed in ' + dir +
    '\n    app:  the "maxedcode app" shortcut, or: maxedcode app' +
    '\n    CLI:  the "maxedcode CLI" shortcut, or: maxedcode (maxed in a new terminal)' +
    '\n    model server, folder again: maxedcode setup\n')
}

// One argument for cmd.exe -> a .bat -> Python's command line parser
function quote (a) {
  if (a !== '' && !/[\s"&|<>^()%!,;=]/.test(a)) return a
  return '"' + a.replace(/(\\*)"/g, '$1$1\\"').replace(/(\\+)$/, '$1$1') + '"'
}

function main () {
  if (!['win32', 'linux', 'darwin'].includes(process.platform)) fail('maxedcode runs on Windows, Linux and macOS.')
  let args = process.argv.slice(2)
  if (args[0] === '--npm-version') return console.log(pkg.version)
  const stamp = readStamp()
  let dir = process.env.MAXEDCODE_DIR || stamp.dir || defaultDir()
  if (args[0] === 'uninstall') return uninstall(dir, args.slice(1))
  const setup = args[0] === 'setup'                // maxedcode setup: the setup window again
  if (setup) args = args.slice(1)
  if (setup || !stamp.version || !fs.existsSync(launcher(dir))) {
    // the first run of this package (or its install is gone): the setup window, never a silent install
    const used = install(dir, true)
    if (used) {
      dir = used
      if (!args.length) return installed(dir)
    } else if (!fs.existsSync(launcher(dir))) {
      fail('nothing was installed (the setup window was closed); run maxedcode again to install it')
    }
  } else if (newer(pkg.version, installedVersion(dir))) {
    dir = install(dir, false) || dir
  }
  const maxed = launcher(dir)
  if (!fs.existsSync(maxed)) fail('setup did not create ' + maxed + '; the log: ' + LOG)
  if (!WIN) {                     // a shell script: the arguments go as they are, no cmd quoting
    const r = spawnSync(maxed, args, { stdio: 'inherit' })
    process.exit(r.status === null ? 1 : r.status)
  }
  const line = '"' + [quote(maxed)].concat(args.map(quote)).join(' ') + '"'
  const r = spawnSync('cmd.exe', ['/d', '/s', '/c', line], { stdio: 'inherit', windowsVerbatimArguments: true })
  process.exit(r.status === null ? 1 : r.status)
}

main()
