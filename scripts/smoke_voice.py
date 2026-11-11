"""One real Gemini voice/tool smoke test. Uses mock data and never prints keys.

Run from the project root: python -m scripts.smoke_voice
This makes a small live API request and can consume quota.
"""
import asyncio
from collections import defaultdict
import json
import time

from backend.config import settings
from backend.data import mock_data
from backend.live import run_live
from backend.store import Events, SessionStore


class ProbeSocket:
    def __init__(self):
        self.incoming = asyncio.Queue()
        self.audio_bytes = 0
        self.closed = asyncio.Event()

    async def receive(self):
        return await self.incoming.get()

    async def send_json(self, data):
        if data.get('type') == 'ready':
            print('Live connection: READY', flush=True)

    async def send_bytes(self, data):
        self.audio_bytes += len(data)

    async def close(self, **kwargs):
        self.closed.set()


class ProbeEvents(Events):
    def __init__(self):
        super().__init__()
        self.waiting = asyncio.Event()
        self.errors = []
        self.snapshots = 0

    async def publish(self, sid, kind, data):
        if kind == 'error':
            self.errors.append(data['message'])
            print('ERROR:', data['message'], flush=True)
            self.waiting.set()
        elif kind == 'status':
            print('Status:', data['stage'], flush=True)
            if data['stage'] in {'listening', 'disconnected'}:
                self.waiting.set()
        elif kind == 'snapshot':
            self.snapshots += 1
            print('Accepted plan:', data['plan']['intent'], flush=True)
            print('Planned speech:', data['plan']['speech'], flush=True)
        elif kind == 'timing':
            print('Timing:', json.dumps(data), flush=True)


async def main():
    if not settings.gemini_api_key:
        raise SystemExit('GEMINI_API_KEY is missing.')
    store, events, ws = SessionStore(), ProbeEvents(), ProbeSocket()
    await store.save({'id': 'smoke', 'dataset': mock_data(), 'facts': {},
                      'history': [], 'summary': '',
                      'plan': None, 'revision': 0})
    config = settings.model_copy(update={'max_call_seconds': 40})
    task = asyncio.create_task(run_live(ws, 'smoke', store, events, config, defaultdict(asyncio.Lock)))
    try:
        await asyncio.wait_for(events.waiting.wait(), 15)
        if events.errors or ws.closed.is_set():
            raise RuntimeError('Live greeting failed; see sanitized status above.')
        print('Greeting audio bytes:', ws.audio_bytes, flush=True)
        before = ws.audio_bytes
        events.waiting.clear()
        started = time.perf_counter()
        await ws.incoming.put({'type': 'websocket.receive', 'text': json.dumps({
            'type': 'text', 'text': 'My sales dropped this month. Both periods are full months. Can you help me understand why?'
        })})
        await asyncio.wait_for(events.waiting.wait(), 22)
        state = await store.get('smoke')
        print('Turn duration seconds:', round(time.perf_counter() - started, 2), flush=True)
        print('Business response audio bytes:', ws.audio_bytes - before, flush=True)
        print('Saved history entries:', len(state['history']), flush=True)
        if events.errors or events.snapshots != 1 or ws.audio_bytes <= before:
            raise RuntimeError('Business audio/tool test did not pass.')
        print('PASS: real Live connection, business planner, session update and generated audio.', flush=True)
        print('Microphone recognition and subjective audio quality still need a browser test.', flush=True)
    finally:
        await ws.incoming.put({'type': 'websocket.disconnect'})
        try:
            await asyncio.wait_for(task, 5)
        except asyncio.TimeoutError:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await store.close()


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except (RuntimeError, TimeoutError) as error:
        print('FAIL:', str(error) or 'Provider response deadline exceeded.', flush=True)
        raise SystemExit(1) from None
