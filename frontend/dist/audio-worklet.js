// PCM16, mono, 16kHz output. Fractional resampling also works at 44.1kHz.
class Capture extends AudioWorkletProcessor {
  constructor() {
    super(); this.input = []; this.cursor = 0; this.output = [];
  }
  process(inputs) {
    const samples = inputs[0]?.[0];
    if (!samples) return true;
    for (const sample of samples) this.input.push(sample);
    const step = sampleRate / 16000;
    while (this.cursor + 1 < this.input.length) {
      const i = Math.floor(this.cursor), frac = this.cursor - i;
      this.output.push(this.input[i] * (1 - frac) + this.input[i + 1] * frac);
      this.cursor += step;
      if (this.output.length === 640) {
        const pcm = new Int16Array(640); let sum = 0;
        this.output.forEach((v, j) => { sum += v * v; pcm[j] = Math.max(-1, Math.min(1, v)) * 32767; });
        this.port.postMessage({ pcm: pcm.buffer, rms: Math.sqrt(sum / 640) }, [pcm.buffer]);
        this.output = [];
      }
    }
    const consumed = Math.min(Math.floor(this.cursor), this.input.length - 1);
    this.input.splice(0, consumed); this.cursor -= consumed;
    return true;
  }
}
registerProcessor('capture', Capture);
