import { test, expect } from '@playwright/test';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

for (const rate of [44100, 48000]) {
  test(`capture preserves 16kHz rate from ${rate}Hz input`, () => {
    let Processor; const chunks = [];
    const context = vm.createContext({
      sampleRate: rate, Int16Array, Math,
      AudioWorkletProcessor: class { constructor() { this.port = { postMessage: value => chunks.push(value) }; } },
      registerProcessor: (name, implementation) => { Processor = implementation; },
    });
    vm.runInContext(readFileSync(new URL('../public/audio-worklet.js', import.meta.url), 'utf8'), context);
    const capture = new Processor();
    const seconds = 4;
    for (let offset = 0; offset < rate * seconds; offset += 128) {
      const block = new Float32Array(Math.min(128, rate * seconds - offset)).fill(.2);
      capture.process([[block]]);
    }
    const outputSamples = chunks.reduce((n, c) => n + c.pcm.byteLength / 2, 0) + capture.output.length;
    expect(Math.abs(outputSamples - 16000 * seconds)).toBeLessThanOrEqual(1);
    expect(chunks[0].pcm.byteLength).toBe(1280);
    expect(chunks[0].rms).toBeCloseTo(.2, 4);
  });
}
