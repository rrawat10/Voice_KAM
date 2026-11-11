"""Exercise the real bridge against a fake transport/provider, without API credits."""
import asyncio
from collections import defaultdict
from contextlib import asynccontextmanager
from types import SimpleNamespace as NS

from backend.config import Settings
from backend.data import mock_data
from backend.live import run_live
from backend.planner import Decision
from backend.store import Events, SessionStore


def content(audio=None, transcript=None, complete=False):
    return NS(server_content=NS(interrupted=False, input_transcription=None,
        output_transcription=NS(text=transcript) if transcript else None,
        model_turn=NS(parts=[NS(inline_data=NS(data=audio))]) if audio else None,
        turn_complete=complete), tool_call=None, go_away=None)


class FakeSocket:
    def __init__(self):
        self.queue = asyncio.Queue()
        self.json = []
        self.audio = []
        self.closed = False
    async def receive(self):
        return await self.queue.get()
    async def send_json(self, message):
        self.json.append(message)
    async def send_bytes(self, data):
        self.audio.append(data)
    async def close(self, **kwargs):
        self.closed = True


class FakeSession:
    def __init__(self):
        self.queue = asyncio.Queue()
        self.responses = []
        self.started = False
    async def send_client_content(self, **kwargs):
        if not self.started:
            self.started = True
            await self.queue.put(content(audio=b'greeting', transcript='Hi.', complete=True))
        else:
            await self.queue.put(content(audio=b'unchecked', transcript='This should not be spoken.'))
            await self.queue.put(NS(server_content=None, go_away=None, tool_call=NS(function_calls=[NS(id='tool-1', name='plan_business_turn', args={'utterance': 'My supplier is late'})])))
    async def send_realtime_input(self, **kwargs):
        pass
    async def send_tool_response(self, function_responses):
        self.responses.extend(function_responses)
        await self.queue.put(content(audio=b'checked', transcript='When will they deliver?', complete=True))
    async def receive(self):
        while True:
            item = await self.queue.get()
            yield item
            if item.server_content and item.server_content.turn_complete:
                break


def test_voice_gate_accepts_only_planned_business_audio(monkeypatch):
    async def check():
        session = FakeSession()
        @asynccontextmanager
        async def connect(**kwargs):
            assert kwargs['config'].tools[0].function_declarations[0].behavior.value == 'BLOCKING'
            yield session
        async def close():
            pass
        client = NS(aio=NS(live=NS(connect=connect), aclose=close))
        monkeypatch.setattr('backend.live.genai.Client', lambda **kwargs: client)
        async def planner(client, settings, state, user_text, **kwargs):
            assert user_text == 'My supplier is late'
            return Decision(intent='clarify', speech='When will they deliver?', question='When will they deliver?', summary='The seller reports that their supplier is late; timing remains unknown.', facts=[{'key': 'supplier', 'text': 'Supplier is late', 'quote': 'My supplier is late'}])
        monkeypatch.setattr('backend.live.plan_turn', planner)
        store = SessionStore()
        await store.save({'id': 'demo', 'dataset': mock_data(), 'facts': {}, 'history': [], 'summary': '', 'plan': None, 'revision': 0})
        ws = FakeSocket()
        task = asyncio.create_task(run_live(ws, 'demo', store, Events(), Settings(gemini_api_key='test', max_call_seconds=5), defaultdict(asyncio.Lock)))
        for _ in range(100):
            if b'greeting' in ws.audio:
                break
            await asyncio.sleep(.01)
        await ws.queue.put({'type': 'websocket.receive', 'text': '{"type":"text","text":"My supplier is late"}'})
        for _ in range(100):
            if b'checked' in ws.audio:
                break
            await asyncio.sleep(.01)
        assert b'unchecked' not in ws.audio
        assert ws.audio == [b'greeting', b'checked']
        assert (await store.get('demo'))['facts']['supplier']['text'] == 'Supplier is late'
        await ws.queue.put({'type': 'websocket.disconnect'})
        await asyncio.wait_for(task, 2)
        assert ws.closed
    asyncio.run(check())


def test_interruption_discards_stale_plan(monkeypatch):
    async def check():
        entered, release = asyncio.Event(), asyncio.Event()
        session = FakeSession()
        @asynccontextmanager
        async def connect(**kwargs):
            yield session
        async def close():
            pass
        client = NS(aio=NS(live=NS(connect=connect), aclose=close))
        monkeypatch.setattr('backend.live.genai.Client', lambda **kwargs: client)
        async def planner(client, settings, state, user_text, **kwargs):
            entered.set()
            await release.wait()
            return Decision(intent='explain', speech='A stale answer.', facts=[{'key': 'supplier', 'text': 'Supplier is late', 'quote': 'My supplier is late'}])
        monkeypatch.setattr('backend.live.plan_turn', planner)
        store = SessionStore()
        await store.save({'id': 'demo', 'dataset': mock_data(), 'facts': {}, 'history': [], 'summary': '', 'plan': None, 'revision': 0})
        ws = FakeSocket()
        task = asyncio.create_task(run_live(ws, 'demo', store, Events(), Settings(gemini_api_key='test', max_call_seconds=5), defaultdict(asyncio.Lock)))
        for _ in range(100):
            if b'greeting' in ws.audio:
                break
            await asyncio.sleep(.01)
        await ws.queue.put({'type': 'websocket.receive', 'text': '{"type":"text","text":"My supplier is late"}'})
        await asyncio.wait_for(entered.wait(), 1)
        await ws.queue.put({'type': 'websocket.receive', 'text': '{"type":"speech_start"}'})
        await asyncio.sleep(.02)
        release.set()
        for _ in range(100):
            if session.responses:
                break
            await asyncio.sleep(.01)
        assert session.responses[0].response['status'] == 'stale'
        assert not (await store.get('demo'))['facts']
        assert ws.audio == [b'greeting']
        await ws.queue.put({'type': 'websocket.disconnect'})
        await asyncio.wait_for(task, 2)
    asyncio.run(check())
