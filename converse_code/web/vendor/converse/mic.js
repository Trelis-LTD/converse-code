import { FRAME_SAMPLES, SAMPLE_RATE } from './audio.js';

// SDK-owned mic capture. Exists so the AEC-only front-end spec is the default, not a doc the app
// must re-read: echo cancellation ON, noise suppression + AGC OFF. Browser defaults enable BOTH —
// NS is neutral-to-harmful for the ASR (finding #80: bundled NS +12.5 WER at −15 dB) and AGC
// distorts levels — so an app that hand-rolls getUserMedia({audio:true}) silently degrades the
// loop. Frames come out as 16 kHz 512-sample Float32 via an AudioWorklet resampler.
// `processing:false` opens the mic fully raw (AEC+NS+AGC off): used for the optional raw ablation
// track, and by ConverseClient.startMic on WebKit where the SDK's own AEC3 cancels instead.
export class MicCapture {
  constructor({ onFrame, processing = true, workletUrl } = {}) {
    this.onFrame = onFrame;
    this.processing = processing;
    // The worklet ships with the SDK; apps only override this if their bundler relocates assets.
    this.workletUrl = workletUrl || new URL('./mic-worklet.js', import.meta.url);
    this.context = null;
    this.stream = null;
    this.source = null;
    this.worklet = null;
  }

  async start() {
    if (this.context) return;
    this.stream = await navigator.mediaDevices.getUserMedia({
      audio: {
        echoCancellation: this.processing,   // AEC on for the loop; off for the raw track
        noiseSuppression: false,             // AEC-only spec: NS off (neutral-to-harmful for ASR)
        autoGainControl: false,              // AEC-only spec: AGC off (distorts levels)
        channelCount: 1,
        sampleRate: SAMPLE_RATE,
      },
    });
    try {
      this.context = new AudioContext();
      await this.context.audioWorklet.addModule(this.workletUrl);
      this.source = this.context.createMediaStreamSource(this.stream);
      this.worklet = new AudioWorkletNode(this.context, 'voice-loop-mic', {
        processorOptions: { targetRate: SAMPLE_RATE, frameSize: FRAME_SAMPLES },
      });
      this.worklet.port.onmessage = (event) => {
        if (event.data?.type === 'frame' && this.onFrame) {
          const monotonic = globalThis.performance?.now?.();
          this.onFrame(event.data.frame, Number.isFinite(monotonic) ? monotonic : Date.now());
        }
      };
      const silentSink = this.context.createGain();
      silentSink.gain.value = 0;
      this.source.connect(this.worklet);
      this.worklet.connect(silentSink);
      silentSink.connect(this.context.destination);
      await this.context.resume();
    } catch (err) {
      // Partial start (e.g. addModule failed): release the live tracks and the AudioContext, or
      // the mic indicator stays lit and the leaked context counts against Chrome's per-page cap.
      // stop() failing here must not mask the original error or leave `context` set (which
      // would make the started-guard block a retry).
      try { await this.stop(); } catch { this.context = null; this.stream = null; }
      throw err;
    }
  }

  async stop() {
    this.worklet?.disconnect();
    this.source?.disconnect();
    for (const track of this.stream?.getTracks() || []) track.stop();
    if (this.context && this.context.state !== 'closed') await this.context.close();
    this.context = null;
    this.stream = null;
    this.source = null;
    this.worklet = null;
  }
}
