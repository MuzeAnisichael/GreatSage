"""Exercise the shipped playback controller without devices or a browser process."""
from pathlib import Path
import shutil
import subprocess

import pytest


def test_playback_receipts_follow_actual_media_events_and_stop_invalidates_fetch():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for the UI controller check")
    source = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const app = fs.readFileSync('ui/app.js', 'utf8');
const controller = app.slice(app.indexOf('class AudioPlayer {'), app.indexOf('const audioPlayer ='));
const receipts = [], media = [];
let finishFetch;
class Media {
  constructor() { this.events = {}; this.paused = true; media.push(this); }
  addEventListener(kind, listener) { this.events[kind] = listener; }
  async play() { this.paused = false; }
  pause() { this.paused = true; }
  removeAttribute() {}
  load() {}
}
const context = vm.createContext({
  Audio: Media, AbortController, performance, state: { status: { session_id: 'session' } },
  $: () => ({}), updateState() {}, toast(message) { throw Error(message); },
  URL: { createObjectURL: () => 'blob:fixture', revokeObjectURL() {} },
  client: {
    request: async (_, options) => { receipts.push(options.body); },
    audioBlob: () => new Promise(resolve => { finishFetch = resolve; })
  }
});
vm.runInContext(controller + '\nglobalThis.player = new AudioPlayer();', context);
const player = context.player;
const clip = { url: '/api/audio/a', audio_id: 'a', generation: 2, session_id: 'session' };
const tick = () => new Promise(resolve => setImmediate(resolve));
(async () => {
  player.enqueue({ ...clip, session_id: 'old-session' }, 'trace');
  assert.equal(finishFetch, undefined);
  player.enqueue(clip, 'trace');
  assert.equal(receipts.length, 0);
  finishFetch({});
  await tick();
  assert.equal(receipts.length, 0, 'downloading and play() are not an actual playing event');
  media[0].events.playing();
  assert.equal(receipts[0].playing, true);
  assert.equal(receipts[0].audio_id, 'a');
  assert.equal(receipts[0].generation, 2);
  assert.equal(receipts[0].session_id, 'session');
  assert.equal(receipts[0].sequence, 1);
  assert(receipts[0].client_elapsed_ms >= 0);
  player.stop({ interrupt_id: 'stop', generation: 3, session_id: 'session' });
  assert(media[0].paused);
  assert.equal(receipts[1].playing, false);
  assert.equal(receipts[1].sequence, 2);
  assert.equal(receipts[2].interrupt_id, 'stop');
  assert.equal(receipts[2].had_audio, true);
  assert(receipts[2].client_elapsed_ms >= 0);
  player.enqueue(clip, 'trace');
  await tick();
  assert.equal(media.length, 1, 'old-generation audio is not replayed');
  player.enqueue({ ...clip, audio_id: 'b', generation: 3 }, 'new-trace');
  player.stop();
  finishFetch({});
  await tick();
  assert.equal(media.length, 1, 'a late fetch cannot create audio after stopping');
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
    result = subprocess.run([node, "-e", source], cwd=Path(__file__).resolve().parents[1],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
