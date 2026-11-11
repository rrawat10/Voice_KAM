export class VoiceConnection {
  constructor({ onStatus, onError, onLevel, onPlayback, onTiming }) {
    Object.assign(this, { onStatus, onError, onLevel, onPlayback, onTiming });
    this.sources = new Set(); this.nextTime = 0; this.muted = false;
    this.speaking = false; this.lastLoud = 0; this.loudFrames = 0; this.closed = false;
  }
  async connect() {
    try {
      this.onStatus('connecting');
      this.context = new AudioContext();
      await this.context.resume();
      this.stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 } });
      if (this.closed) { this.stream.getTracks().forEach(t => t.stop()); return; }
      await this.context.audioWorklet.addModule('/audio-worklet.js');
      this.capture = new AudioWorkletNode(this.context, 'capture');
      this.input = this.context.createMediaStreamSource(this.stream);
      this.silent = this.context.createGain(); this.silent.gain.value = 0;
      this.input.connect(this.capture); this.capture.connect(this.silent); this.silent.connect(this.context.destination);
      this.socket = new WebSocket(`${location.protocol === 'https:' ? 'wss:' : 'ws:'}//${location.host}/api/voice`);
      this.socket.binaryType = 'arraybuffer';
      this.socket.onmessage = event => {
        if (typeof event.data !== 'string') { this.play(event.data); return; }
        const msg = JSON.parse(event.data);
        if (msg.type === 'ready') { this.ready = true; this.onStatus('listening'); }
        if (msg.type === 'interrupt') this.interrupt();
        if (msg.type === 'error') this.onError(msg.message);
      };
      this.socket.onclose = () => { this.close(); this.onStatus('disconnected'); };
      this.socket.onerror = () => this.onError('Could not open the voice connection. Check your network and server configuration.');
      this.capture.port.onmessage = ({ data }) => {
        if (!this.ready || this.closed || this.muted || this.socket.readyState !== WebSocket.OPEN) return;
        this.onLevel(Math.min(1, data.rms * 8));
        const now = performance.now();
        if (data.rms > 0.023) { this.lastLoud = now; this.loudFrames++; } else this.loudFrames = 0;
        if (!this.speaking && this.loudFrames >= 3) {
          this.speaking = true; this.lastEndAt = null; this.latencyReported = false; this.interrupt(); this.send({ type: 'speech_start' });
        }
        if (this.speaking && now - this.lastLoud > 400) {
          this.speaking = false; this.lastEndAt = now; this.send({ type: 'speech_end' });
        }
        if (this.socket.bufferedAmount < 128000) this.socket.send(data.pcm);
        else { this.onError('The connection is too slow for live audio. Reconnect when your network improves.'); this.close(); }
      };
    } catch (error) {
      this.onError(error.name === 'NotAllowedError' ? 'Microphone permission was denied. Allow microphone access in your browser and try again.' : 'Could not start audio. Use Chrome or Edge on HTTPS or localhost, then retry.');
      this.close();
    }
  }
  send(value) { if (this.socket?.readyState === WebSocket.OPEN) this.socket.send(JSON.stringify(value)); }
  text(text) { this.interrupt(); this.lastEndAt = performance.now(); this.latencyReported = false; this.send({ type: 'text', text }); }
  toggleMute() {
    this.muted = !this.muted;
    if (this.muted) {
      if (this.speaking) { this.lastEndAt = performance.now(); this.send({ type: 'speech_end' }); }
      this.speaking = false; this.send({ type: 'mute' }); this.onLevel(0);
    }
    return this.muted;
  }
  play(data) {
    if (!this.context || this.closed) return;
    const pcm = new Int16Array(data), buffer = this.context.createBuffer(1, pcm.length, 24000);
    const channel = buffer.getChannelData(0);
    for (let i = 0; i < pcm.length; i++) channel[i] = pcm[i] / 32768;
    const source = this.context.createBufferSource(); source.buffer = buffer; source.connect(this.context.destination);
    const start = Math.max(this.context.currentTime + 0.02, this.nextTime);
    if (this.lastEndAt != null && !this.latencyReported) {
      this.latencyReported = true;
      this.onTiming?.({ response_ms: Math.round(performance.now() - this.lastEndAt + (start - this.context.currentTime) * 1000) });
    }
    this.nextTime = start + buffer.duration; this.sources.add(source);
    source.onended = () => { this.sources.delete(source); if (!this.sources.size) this.onPlayback(false); };
    source.start(start); this.onPlayback(true);
  }
  interrupt() {
    for (const source of this.sources) { try { source.stop(); } catch {} }
    this.sources.clear(); this.nextTime = 0; this.onPlayback(false);
  }
  close() {
    this.closed = true; this.ready = false; this.interrupt();
    this.stream?.getTracks().forEach(track => track.stop());
    this.capture?.disconnect(); this.input?.disconnect(); this.silent?.disconnect();
    if (this.context?.state !== 'closed') this.context?.close();
    if (this.socket) {
      this.socket.onclose = null; this.socket.onerror = null; this.socket.onmessage = null;
      if (this.socket.readyState < WebSocket.CLOSING) this.socket.close();
    }
    this.onLevel(0); this.onStatus('disconnected');
  }
}
