#!/usr/bin/env node
// npm entry point (npm i -g maxedcode / npx maxedcode).
// The first run installs maxedcode from this package with the usual setup (agent/install.py); after an npm update
// the next run updates the installed program quietly (the server, chats and settings are kept). Then it runs maxed
// with the same arguments: maxedcode, maxedcode app, maxedcode --print "...", maxedcode configure ...
// Files npm downloads carry no "from the internet" mark, so nothing here needs a paid code signature.
'use strict'

const { spawnSync } = require('child_process')
const fs = require('fs')
const os = require('os')
const path = require('path')
const pkg = require('../package.json')

const ROOT = path.resolve(__dirname, '..')
const HOME = path.join(os.homedir(), '.maxedcode')
const STAMP = path.join(HOME, 'npm-install.json')

function fail (msg) {
  console.error('  maxedcode: ' + msg)
  process.exit(1)
}

function readStamp () {
  try { return JSON.parse(fs.readFileSync(STAMP, 'utf8')) } catch (e) { return {} }
}

// Python 3.10+ that is not the Microsoft Store one; installer/bootstrap.ps1 installs Python 3.12 when there is none
function findPython () {
  const out = path.join(os.tmpdir(), 'maxedcode-npm-python-' + process.pid + '.txt')
  const r = spawnSync('powershell.exe', ['-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
    path.join(ROOT, 'installer', 'bootstrap.ps1')], { stdio: 'inherit', env: Object.assign({}, process.env, { MX_PYFILE: out }) })
  let py = ''
  try { py = fs.readFileSync(out, 'utf8').trim() } catch (e) {}
  fs.rmSync(out, { force: true })
  if (r.status !== 0 || !py) fail('Python 3.10+ was not found and could not be installed: https://www.python.org/downloads/')
  return py
}

function install (dir, wizard) {
  const args = [path.join(ROOT, 'agent', 'install.py'), '--dir', dir]
  args.push(wizard && process.stdin.isTTY ? '--console' : '--yes')
  // extra setup options, as for install.ps1: MAXEDCODE_ARGS="--base-url 192.168.0.10:1234 --model qwen3"
  if (process.env.MAXEDCODE_ARGS) args.push.apply(args, process.env.MAXEDCODE_ARGS.split(/\s+/).filter(Boolean))
  console.log(wizard ? '\n  maxedcode: first start, setting it up ...' : '\n  maxedcode: updating to ' + pkg.version + ' ...')
  const r = spawnSync(findPython(), args, { stdio: 'inherit' })
  if (r.status !== 0) fail('setup stopped (code ' + r.status + '); the log: %TEMP%\\maxedcode-setup.log')
  fs.mkdirSync(HOME, { recursive: true })
  fs.writeFileSync(STAMP, JSON.stringify({ version: pkg.version, dir: dir }, null, 2))
}

// One argument for cmd.exe -> a .bat -> Python's command line parser
function quote (a) {
  if (a !== '' && !/[\s"&|<>^()%!,;=]/.test(a)) return a
  return '"' + a.replace(/(\\*)"/g, '$1$1\\"').replace(/(\\+)$/, '$1$1') + '"'
}

function main () {
  if (process.platform !== 'win32') fail('maxedcode runs on Windows 10 / 11 for now.')
  let args = process.argv.slice(2)
  if (args[0] === '--npm-version') return console.log(pkg.version)
  const stamp = readStamp()
  const local = process.env.LOCALAPPDATA || path.join(os.homedir(), 'AppData', 'Local')
  const dir = process.env.MAXEDCODE_DIR || stamp.dir || path.join(local, 'Programs', 'maxedcode')
  const maxed = path.join(dir, 'program', 'bin', 'maxed.bat')
  if (args[0] === 'setup') {                       // maxedcode setup: the setup wizard again
    install(dir, true)
    args = args.slice(1)
    if (!args.length) return
  } else if (!fs.existsSync(maxed)) {
    install(dir, true)
  } else if (stamp.version !== pkg.version) {
    install(dir, false)
  }
  if (!fs.existsSync(maxed)) fail('setup did not create ' + maxed)
  const line = '"' + [quote(maxed)].concat(args.map(quote)).join(' ') + '"'
  const r = spawnSync('cmd.exe', ['/d', '/s', '/c', line], { stdio: 'inherit', windowsVerbatimArguments: true })
  process.exit(r.status === null ? 1 : r.status)
}

main()
