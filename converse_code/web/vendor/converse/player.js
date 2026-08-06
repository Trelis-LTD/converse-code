import { SAMPLE_RATE } from './audio.js';

// Initial buffer before playback starts (and on recovery after an underrun) to absorb
// network jitter from the streamed TTS. Trades a little first-audio latency for gap-free
// playback. 100 ms suits the binary WebSocket transport.
const JITTER_LEAD = 0.1;

// How far ahead of the playhead we keep audio scheduled. Committing only a small horizon keeps
// clear() (barge/interrupt) responsive — little audio is locked into already-started sources.
const SCHEDULE_AHEAD = 0.2;
const TICK_MS = 25;

// clear() fades the master bus out over this long before stopping sources — a hard stop() cuts
// the waveform mid-sample and pops audibly (canceled/reset/reconnect). Must stay well under
// JITTER_LEAD so audio enqueued right after a clear starts at full gain.
const CLEAR_FADE = 0.025;
const PAUSE_FADE = 0.02;

// Streaming PCM player for gapless assistant audio.
//
//   enqueue(samples) — append assistant audio (16 kHz f32) to the play queue.
//   clear()          — discard queued audio (a barge/interrupt) and stop playing now.
//   stop()           — clear() + tear down (full stop / disconnect).
//
// The queue holds resampled PCM (context rate) drained gaplessly into short BufferSources.
//
// Far-end tap (SDK AEC): `onScheduled(samples16k, startAt)` fires when a chunk is committed to
// a BufferSource, with its playout time on this context's clock; `onCleared(cutAt)` fires on a
// barge/clear so the not-yet-played reference tail is dropped. See aec.js.
// SETTLED 2026-07-30 — no output-routing machinery belongs here. An 11-configuration on-device
// experiment (web/audio-route-test.html, iPhone 17, Chrome + Safari) showed modern iOS routes web
// audio to the loudspeakers in EVERY reachable configuration — bare destination, <audio>-element
// sink, navigator.audioSession types in every working order, EC-on and raw capture alike — and
// that "earpiece AND speaker at once" is simply normal iPhone stereo playback (the receiver is
// the second stereo speaker). Web pages get no earpiece lever, so the speaker/earpiece option
// (0.4.1-0.4.4) was unimplementable and is removed; the historical "quiet on iPhone" complaint
// was the per-voice gain spread, fixed server-side. Full record: docs/user-feedback.md.
export class StreamingPlayer {
  constructor() {
    this.onScheduled = null;
    this.onCleared = null;
    this.context = null;
    this.master = null;      // master gain bus — lets clear() fade out instead of popping
    this.ratio = 1;          // ctxRate / SAMPLE_RATE
    this.phase = 0;          // carried fractional read position across chunks (resampler)
    this.carry = 0;          // last input sample of the previous chunk (seamless interpolation)
    this.fadeEnd = 0;        // ctx time a clear()'s fade completes — new audio never starts inside it
    this.queue = [];         // Float32Array chunks at context rate, awaiting scheduling
    this.nextTime = 0;       // ctx time of the next sample to schedule
    this.sources = new Set(); // scheduled/playing BufferSources
    this.timer = null;
    this.paused = false;      // reversible server-side backchannel hold
    this.pauseGen = 0;        // invalidates a fade overtaken by clear/stop/resume
  }

  async ensureContext() {
    if (!this.context || this.context.state === 'closed') {
      this.context = new AudioContext();
      this.master = this.context.createGain();
      this.master.connect(this.context.destination);
      this.ratio = this.context.sampleRate / SAMPLE_RATE;
      this.phase = 0;
      this.carry = 0;
      this.fadeEnd = 0;
      this.nextTime = this.context.currentTime;
    }
    if (this.context.state === 'suspended' && !this.paused) await this.context.resume();
  }

  // Continuous linear resample of a 16 kHz chunk up to the context's native rate, carrying the
  // fractional read position AND the previous chunk's last sample across chunks — read positions
  // near a chunk seam interpolate across it instead of flat-holding (an audible tick per chunk).
  _resample(input) {
    if (this.ratio === 1) return Float32Array.from(input);
    const step = 1 / this.ratio;
    const n = input.length;
    const out = [];
    let pos = this.phase;
    while (pos < n) {
      // Read position pos sits between carry⌢input[pos-1] and input[pos] — position 0 is the seam.
      const i = Math.floor(pos);
      const frac = pos - i;
      const a = i === 0 ? this.carry : input[i - 1];
      const b = input[i];
      out.push(a + (b - a) * frac);
      pos += step;
    }
    this.phase = pos - n; // remainder feeds the start of the next chunk
    this.carry = input[n - 1];
    return Float32Array.from(out);
  }

  async enqueue(samples) {
    if (!samples || samples.length === 0) return;
    await this.ensureContext();
    const data = this._resample(samples);
    // Keep the pre-resample 16 kHz chunk alongside: the AEC far-end reference must match the
    // uplink rate, and only _schedule() knows the playout time.
    if (data.length > 0) this.queue.push({ data, src: Float32Array.from(samples) });
    this._ensureTimer();
    this._schedule();
  }

  // Schedule queued PCM up to SCHEDULE_AHEAD past the playhead. Each call drains whole chunks
  // off the front of the queue into a BufferSource.
  _schedule() {
    if (!this.context || this.paused) return;
    const now = this.context.currentTime;
    // Re-buffer JITTER_LEAD when the queue had drained (first chunk or underrun) — and never
    // start inside a clear()'s fade, however the two constants are tuned relative to each other.
    if (this.nextTime <= now) this.nextTime = Math.max(now + JITTER_LEAD, this.fadeEnd);
    while (this.queue.length && this.nextTime < now + SCHEDULE_AHEAD) {
      const { data, src } = this.queue.shift();
      const buffer = this.context.createBuffer(1, data.length, this.context.sampleRate);
      buffer.copyToChannel(data, 0);
      const source = this.context.createBufferSource();
      source.buffer = buffer;
      source.connect(this.master);
      source.onended = () => this.sources.delete(source);
      const startAt = Math.max(now, this.nextTime);
      source.start(startAt);
      this.onScheduled?.(src, startAt);
      this.nextTime = startAt + buffer.duration;
      this.sources.add(source);
    }
  }

  // Released-but-unplayed audio right now, in ms: scheduled-but-unplayed + queued-unscheduled.
  // On a barge this is what a drain would still play — and what a clear() throws away
  // (discarded_ms in the playback_stopped report, which re-truncates the server's heard text).
  pendingMs() {
    if (!this.context) return 0;
    const scheduled = Math.max(0, this.nextTime - this.context.currentTime);
    const queued = this.queue.reduce((s, c) => s + c.data.length, 0) / this.context.sampleRate;
    return (scheduled + queued) * 1000;
  }

  // Device output latency (context clock -> speaker), in ms.
  deviceLatencyMs() {
    if (!this.context) return 0;
    return (this.context.outputLatency || this.context.baseLatency || 0) * 1000;
  }

  // Time to actual silence at the speaker if nothing else happens: pending playout plus the
  // device output latency. This is what the server timestamps to measure the stop half of
  // barge latency.
  remainingMs() {
    return this.pendingMs() + this.deviceLatencyMs();
  }

  _ensureTimer() {
    if (this.timer != null) return;
    this.timer = setInterval(() => this._schedule(), TICK_MS);
  }

  _clearTimer() {
    if (this.timer != null) {
      clearInterval(this.timer);
      this.timer = null;
    }
  }

  // Freeze the output clock exactly where it is. AudioContext suspension preserves scheduled
  // BufferSource positions and lets incoming reply audio accumulate in `queue`; resume() continues
  // from the same sample instead of skipping the decision window or rebuilding source offsets.
  async pause() {
    if (!this.context || this.paused) return;
    const ctx = this.context;
    const gen = ++this.pauseGen;
    this.paused = true;
    try {
      const now = ctx.currentTime;
      const gain = this.master?.gain;
      if (gain) {
        gain.cancelScheduledValues(now);
        gain.setValueAtTime(gain.value, now);
        gain.linearRampToValueAtTime(0, now + PAUSE_FADE);
        await new Promise((resolve) => setTimeout(resolve, PAUSE_FADE * 1000));
      }
      if (this.context !== ctx || !this.paused || this.pauseGen !== gen) return;
      if (ctx.state === 'running' && typeof ctx.suspend === 'function') {
        await ctx.suspend();
        if (this.context !== ctx || !this.paused || this.pauseGen !== gen) {
          // clear()/resume() may have invalidated this pause while the browser was suspending.
          // Undo only our stale suspension; a newer pause generation still owns a paused context.
          if (this.context === ctx && !this.paused && ctx.state === 'suspended') {
            await ctx.resume();
          }
          return;
        }
      }
    } catch (error) {
      if (this.context === ctx && this.pauseGen === gen) {
        this.paused = false;
        throw error;
      }
    }
  }

  async resume() {
    if (!this.context || !this.paused) return;
    this.pauseGen++;
    this.paused = false;
    if (this.context.state === 'suspended') await this.context.resume();
    const now = this.context.currentTime;
    const gain = this.master?.gain;
    if (gain) {
      gain.cancelScheduledValues(now);
      gain.setValueAtTime(0, now);
      gain.linearRampToValueAtTime(1, now + PAUSE_FADE);
    }
    this._ensureTimer();
    this._schedule();
  }

  // Discard queued audio (barge/interrupt) and stop playing — via a short master-bus fade, so
  // the cut lands on silence instead of popping mid-waveform. `fadeS` overrides the fade for a
  // barge hard-clear (~150 ms reads as a yield, not a glitch); the default stays pop-guard short.
  clear(fadeS = CLEAR_FADE) {
    this.queue = [];
    this.phase = 0;
    this.carry = 0;
    const ctx = this.context;
    const wasPaused = this.paused;
    this.pauseGen++;
    this.paused = false;
    // A suspended context is already silent. Stop its sources at the frozen playhead and resume
    // only to apply that stop; fading would add no audible benefit and would overstate played audio.
    if (wasPaused && ctx?.state === 'suspended') ctx.resume().catch(() => {});
    if (ctx && this.sources.size) {
      const now = ctx.currentTime;
      const stopAt = now + (wasPaused ? 0 : fadeS);
      const gain = this.master.gain;
      gain.cancelScheduledValues(now);
      gain.setValueAtTime(gain.value, now);
      gain.linearRampToValueAtTime(0, stopAt);
      // Restore the instant the sources stop: same-time automation events apply in insertion
      // order, so the bus is back at 1 exactly when nothing is left playing. _schedule() keeps
      // any new audio out of the fade window via fadeEnd.
      gain.setValueAtTime(1, stopAt);
      for (const source of this.sources) {
        try { source.stop(stopAt); } catch { /* already stopped */ }
      }
      this.sources.clear();
      this.fadeEnd = stopAt;
      this.onCleared?.(stopAt);
    }
    this.nextTime = ctx?.currentTime || 0;
  }

  stop() {
    this.clear(0);
    this._clearTimer();
    // Full teardown must release the AudioContext: browsers cap live contexts per tab (~6 in
    // Chrome), and every session/connect-attempt builds a fresh player — leaking contexts here
    // made audio silently die after a few Start/Stop or provider switches until a page refresh.
    // ensureContext() recreates on next use (it already handles state === 'closed').
    const ctx = this.context;
    this.context = null;
    this.master = null;
    if (ctx && ctx.state !== 'closed' && typeof ctx.close === 'function') {
      ctx.close().catch(() => {});
    }
  }
}
