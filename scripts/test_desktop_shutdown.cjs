'use strict';

// Run: node --test scripts/test_desktop_shutdown.cjs
// Exercise the actual desktop shutdown function with a private fake child and
// deterministic deadlines. No Electron window, backend, installer or taskkill runs.
const assert = require('node:assert/strict');
const { EventEmitter } = require('node:events');
const fs = require('node:fs');
const path = require('node:path');
const { test } = require('node:test');
const vm = require('node:vm');

function fixture() {
  const directory = path.resolve(__dirname, '..', 'desktop');
  const timers = new Map();
  const launches = [];
  const logs = [];
  let timerId = 0;
  const context = vm.createContext({
    require(name) {
      if (name === 'electron') return { app: { requestSingleInstanceLock: () => false, quit() {} } };
      if (name === 'node:child_process') return {
        spawn(command, args, options) {
          const process = new EventEmitter();
          launches.push({ command, args, options, process });
          return process;
        },
      };
      return require(name);
    },
    __dirname: directory,
    process: { platform: 'win32' },
    setTimeout(callback, delay) { const id = ++timerId; timers.set(id, { callback, delay }); return id; },
    clearTimeout(id) { timers.delete(id); },
  });
  vm.runInContext(fs.readFileSync(path.join(directory, 'main.cjs'), 'utf8') + `
    globalThis.shutdown = stopBackend;
    globalThis.attachChild = value => { backend = value; };
    globalThis.attachLog = value => { logStream = value; };
  `, context);
  context.attachLog({ destroyed: false, write: value => logs.push(value) });
  const child = new EventEmitter();
  Object.assign(child, {
    pid: 32456, exitCode: null, signalCode: null, writes: [], signals: [],
    kill(signal) { this.signals.push(signal); },
    stdin: { destroyed: false, writable: true, end: value => child.writes.push(value) },
  });
  context.attachChild(child);
  return {
    child, timers, launches, logs,
    shutdown: () => context.shutdown(),
    exit(code = 0) { child.exitCode = code; child.emit('exit', code, null); },
    deadline(delay) {
      const entry = [...timers].find(([, timer]) => timer.delay === delay);
      assert.ok(entry, `Expected a ${delay} ms deadline`);
      timers.delete(entry[0]);
      entry[1].callback();
    },
  };
}

test('normal exit asks the private stdin pipe to close and waits for backend exit', async () => {
  const run = fixture();
  let done = false;
  const stopping = run.shutdown().then(() => { done = true; });
  assert.deepEqual(run.child.writes, ['shutdown\n']);
  assert.deepEqual(run.child.signals, []);
  assert.equal(run.launches.length, 0);
  await Promise.resolve();
  assert.equal(done, false);
  run.exit();
  await stopping;
  assert.equal(run.timers.size, 0);
  assert.equal(run.child.listenerCount('exit'), 0);
});

test('a blocked backend is force-stopped only by its PID after the graceful deadline', async () => {
  const run = fixture();
  let done = false;
  const stopping = run.shutdown().then(() => { done = true; });
  run.deadline(5000);
  assert.equal(run.launches.length, 1);
  const killer = run.launches[0];
  assert.equal(killer.command, 'taskkill');
  assert.deepEqual(Array.from(killer.args), ['/PID', '32456', '/T', '/F']);
  assert.equal(killer.options.windowsHide, true);
  killer.process.emit('exit', 0);
  await Promise.resolve();
  assert.equal(done, false, 'taskkill completion alone does not prove the child has exited');
  run.exit(1);
  await stopping;
  assert.equal(run.timers.size, 0);
});

test('pipe and forced-cleanup errors are recorded and cannot block desktop exit indefinitely', async () => {
  const run = fixture();
  run.child.stdin.end = () => { throw new Error('control pipe closed'); };
  const stopping = run.shutdown();
  run.deadline(5000);
  run.launches[0].process.emit('error', new Error('cleanup denied'));
  run.deadline(1800);
  await stopping;
  assert.ok(run.logs.some(line => line.includes('control pipe closed')));
  assert.ok(run.logs.some(line => line.includes('cleanup denied')));
  assert.ok(run.logs.some(line => line.includes('exit could not be confirmed')));
  assert.equal(run.timers.size, 0);
  assert.equal(run.child.listenerCount('exit'), 0);
});

test('an already exited backend receives no shutdown or process-kill request', async () => {
  const run = fixture();
  run.child.exitCode = 0;
  await run.shutdown();
  assert.deepEqual(run.child.writes, []);
  assert.equal(run.launches.length, 0);
  assert.equal(run.timers.size, 0);
});
