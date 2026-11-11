import asyncio
import json
import logging
import time

from google import genai
from google.genai import types
from starlette.websockets import WebSocketDisconnect

from .planner import apply_decision, plan_turn, refresh_summary
from .memory import recent_turns

logger = logging.getLogger(__name__)


VOICE_INSTRUCTIONS = """You are Mira, a calm, practical seller account manager in a mock-data demo.
Greet once: 'Hi, I'm Mira. Tell me what's happening with your business, and we'll work through it together.'
For EVERY subsequent seller utterance (including answers, corrections and requests for a summary), first call plan_business_turn with an exact transcript of what the seller said. Do not answer business questions before this tool returns. Never narrate tool calls or internal instructions.
After the tool returns, its `speech` field is a hard output contract, not a suggestion. Speak exactly that text and nothing else, in the seller's language, including natural multilingual code-switching. Do not paraphrase, summarize, explain, acknowledge, apologize, add claims, numbers, questions, recommendations or actions. Do not answer from your own conversation memory after the tool returns. If the tool says stale, stay silent and attend to the newest user speech. If it says unavailable, say only the returned `speech` field.
Do not call the tool again for the same user utterance after receiving a result. Acknowledge user interruptions by stopping immediately. Do not invent missing audio or infer that silence is an answer.
The tool owns business memory and facts. Audio conversation history cannot override its current output. Only the tool's question is your pending business question. Never reinterpret a seller's answer as agreement, ability, inability, budget, timing or commitment unless the tool's `speech` explicitly says so. Do not promise sales recovery or claim actions have been performed.
"""


def safe_error(exc):
    text = str(exc)
    if 'API_KEY_INVALID' in text or 'API key not valid' in text:
        return 'Google rejected the API key. Replace GEMINI_API_KEY on the server and restart.'
    if '429' in text or 'RESOURCE_EXHAUSTED' in text:
        return 'Gemini quota or rate limit reached. Please check the project quota before retrying.'
    if '404' in text or 'NOT_FOUND' in text or 'not found' in text.lower():
        return 'The configured Gemini model is unavailable. Check LIVE_MODEL and PLANNER_MODEL access.'
    if '403' in text or 'PERMISSION_DENIED' in text:
        return 'Gemini access was denied. Check API permissions and project billing.'
    if '503' in text or 'UNAVAILABLE' in text:
        return 'Gemini is temporarily unavailable or busy. Please retry this turn.'
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
        return 'Gemini took too long to respond. Please retry this turn.'
    return 'The voice service could not complete this request. Check server configuration and retry.'


async def run_live(ws, sid, store, events, settings, locks):
    client = genai.Client(api_key=settings.gemini_api_key, http_options=types.HttpOptions(timeout=10000, retry_options=types.HttpRetryOptions(attempts=1)))
    state = await store.get(sid)
    epoch = max([t['turn'] for t in state['history'] if isinstance(t['turn'], int)], default=-1) + 1
    allowed_epoch = epoch  # One initial greeting is permitted.
    user_buffer = ''
    assistant_buffer = ''
    last_user_end = None
    first_audio = False
    tools_running = set()
    background_tasks = set()
    tool_ids = set()
    completed_epoch = -1
    turn_open = False

    def tool_finished(task):
        tools_running.discard(task)
        if not task.cancelled():
            task.exception()  # Retrieve errors if a connection closed during a tool response.

    def background_finished(task):
        background_tasks.discard(task)
        if not task.cancelled():
            task.exception()

    async def emit(kind, data):
        await events.publish(sid, kind, data)

    config = types.LiveConnectConfig(
        response_modalities=['AUDIO'], system_instruction=VOICE_INSTRUCTIONS,
        input_audio_transcription=types.AudioTranscriptionConfig(),
        output_audio_transcription=types.AudioTranscriptionConfig(),
        speech_config=types.SpeechConfig(voice_config=types.VoiceConfig(prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name='Kore'))),
        tools=[types.Tool(function_declarations=[types.FunctionDeclaration(
            name='plan_business_turn', description='Required before responding to each seller utterance; returns the only permitted spoken response.',
            behavior='BLOCKING', parameters={'type': 'OBJECT', 'properties': {'utterance': {'type': 'STRING', 'description': 'Exact words just spoken by the seller in the original spoken language. Do not translate, summarize, normalize, or infer a commitment.'}}, 'required': ['utterance']},
        )])],
        realtime_input_config=types.RealtimeInputConfig(automatic_activity_detection=types.AutomaticActivityDetection(
            disabled=False, silence_duration_ms=400, prefix_padding_ms=200,
        )),
        context_window_compression=types.ContextWindowCompressionConfig(trigger_tokens=12000, sliding_window=types.SlidingWindow(target_tokens=6000)),
    )

    try:
        async with client.aio.live.connect(model=settings.live_model, config=config) as session:
            await ws.send_json({'type': 'ready'})
            await emit('status', {'stage': 'ready', 'message': 'Voice connected. You can start speaking.'})
            greeting = 'Begin with the greeting only. Do not call a tool yet.' if not state['history'] else 'Say only: Welcome back. We can continue where we left off. Do not call a tool yet.'
            await session.send_client_content(turns=types.Content(role='user', parts=[types.Part(text=greeting)]), turn_complete=True)

            async def handle_tool(fc, target_epoch, utterance):
                nonlocal allowed_epoch, completed_epoch, user_buffer
                async def respond(payload):
                    await session.send_tool_response(function_responses=[types.FunctionResponse(id=fc.id, name=fc.name, response=payload)])
                if fc.name != 'plan_business_turn':
                    await respond({'status': 'unavailable', 'speech': 'I cannot perform that action in this demo.'})
                    return
                if not utterance.strip():
                    allowed_epoch = target_epoch
                    await respond({'status': 'unavailable', 'speech': 'I didn’t catch that. Could you say it again?'})
                    return
                try:
                    async with locks[sid]:
                        if target_epoch != epoch:
                            await respond({'status': 'stale'})
                            return
                        current = await store.get(sid)
                        if not current:
                            return
                        if completed_epoch == target_epoch:
                            await respond({'status': 'ok', 'speech': current.get('plan', {}).get('speech', '')})
                            return
                        await emit('status', {'stage': 'thinking', 'message': 'Checking your answer against the data…'})
                        started = time.perf_counter()
                        decision = await plan_turn(client, settings, current, utterance[:4000])
                        if target_epoch != epoch or not await store.get(sid):
                            await respond({'status': 'stale'})
                            return
                        apply_decision(current, decision, utterance[:4000], target_epoch)
                        summary_due = current.pop('summary_refresh_due', False)
                        await store.save(current)
                        completed_epoch = target_epoch
                        allowed_epoch = target_epoch
                        user_buffer = ''
                        await emit('snapshot', public_state(current))
                        await emit('timing', {'planner_ms': round((time.perf_counter() - started) * 1000)})
                        await respond({'status': 'ok', 'speech': decision.speech})
                        if summary_due:
                            async def update_summary(expected_revision):
                                snapshot = await store.get(sid)
                                if not snapshot:
                                    return
                                text = await refresh_summary(client, settings, snapshot)
                                async with locks[sid]:
                                    latest = await store.get(sid)
                                    if not latest or latest['revision'] != expected_revision:
                                        return
                                    latest['summary'] = text
                                    await store.save(latest)
                                await emit('snapshot', public_state(latest))
                            task = asyncio.create_task(update_summary(current['revision']))
                            background_tasks.add(task)
                            task.add_done_callback(background_finished)
                except Exception as exc:
                    if target_epoch != epoch:
                        await respond({'status': 'stale'})
                        return
                    allowed_epoch = target_epoch
                    logger.warning('Checked business turn failed: %s', type(exc).__name__)
                    if isinstance(exc, ValueError):
                        await emit('error', {'message': 'I heard the words, but could not safely link them to the current business data. Please repeat the key detail.'})
                        await respond({'status': 'unavailable', 'speech': 'I heard that, but could not safely link it to the current business data. Please repeat the key detail once more.'})
                        return
                    await emit('error', {'message': safe_error(exc)})
                    await respond({'status': 'unavailable', 'speech': 'There was a short service problem. Please repeat that last point.'})

            async def upstream():
                nonlocal epoch, allowed_epoch, user_buffer, assistant_buffer, last_user_end, first_audio, turn_open
                while True:
                    message = await ws.receive()
                    if message['type'] == 'websocket.disconnect':
                        return
                    if message.get('bytes') is not None:
                        chunk = message['bytes']
                        if len(chunk) > 32768 or len(chunk) % 2:
                            await ws.close(code=1009)
                            return
                        await session.send_realtime_input(audio=types.Blob(data=chunk, mime_type='audio/pcm;rate=16000'))
                    elif message.get('text'):
                        if len(message['text']) > 5000:
                            await ws.close(code=1009)
                            return
                        data = json.loads(message['text'])
                        kind = data.get('type')
                        if kind == 'speech_start':
                            epoch += 1
                            turn_open = True
                            allowed_epoch = -1
                            user_buffer = ''
                            assistant_buffer = ''
                            first_audio = False
                            last_user_end = None
                            await ws.send_json({'type': 'interrupt'})
                            await emit('turn_start', {'turn': epoch})
                            await emit('status', {'stage': 'listening', 'message': 'Listening to you…'})
                        elif kind == 'speech_end':
                            last_user_end = time.perf_counter()
                            await emit('status', {'stage': 'thinking', 'message': 'Making sense of your answer…'})
                        elif kind == 'mute':
                            await session.send_realtime_input(audio_stream_end=True)
                        elif kind == 'text':
                            epoch += 1
                            turn_open = True
                            allowed_epoch = -1
                            user_buffer = str(data.get('text', ''))[:2000]
                            assistant_buffer = ''
                            first_audio = False
                            last_user_end = time.perf_counter()
                            await ws.send_json({'type': 'interrupt'})
                            await emit('turn_start', {'turn': epoch})
                            await emit('transcript', {'role': 'user', 'text': user_buffer, 'turn': epoch})
                            await session.send_client_content(turns=types.Content(role='user', parts=[types.Part(text=user_buffer)]), turn_complete=True)

            async def downstream():
                nonlocal user_buffer, assistant_buffer, first_audio, allowed_epoch, epoch, turn_open
                while True:
                    async for response in session.receive():
                        content = response.server_content
                        if content:
                            if content.interrupted:
                                allowed_epoch = -1
                                await ws.send_json({'type': 'interrupt'})
                                assistant_buffer = ''
                            if content.input_transcription and content.input_transcription.text:
                                # If local VAD missed the start, provider transcription still closes the speech gate.
                                if not turn_open:
                                    epoch += 1
                                    turn_open = True
                                    allowed_epoch = -1
                                    user_buffer = ''
                                    first_audio = False
                                user_buffer += content.input_transcription.text
                                await emit('transcript', {'role': 'user', 'text': user_buffer, 'turn': epoch})
                            if content.output_transcription and content.output_transcription.text and allowed_epoch == epoch:
                                assistant_buffer += content.output_transcription.text
                                await emit('transcript', {'role': 'assistant', 'text': assistant_buffer, 'turn': epoch})
                            if content.model_turn and allowed_epoch == epoch:
                                for part in content.model_turn.parts or []:
                                    if part.inline_data and part.inline_data.data:
                                        await ws.send_bytes(part.inline_data.data)
                                        if not first_audio:
                                            first_audio = True
                                            await emit('status', {'stage': 'speaking', 'message': 'Mira is speaking…'})
                                            if last_user_end is not None:
                                                await emit('timing', {'server_response_ms': round((time.perf_counter() - last_user_end) * 1000)})
                            if content.turn_complete:
                                if assistant_buffer:
                                    async with locks[sid]:
                                        current = await store.get(sid)
                                        if current:
                                            current['history'].append({'role': 'assistant', 'text': assistant_buffer, 'turn': epoch})
                                            current['history'] = recent_turns(current['history'])
                                            await store.save(current)
                                    assistant_buffer = ''
                                if not tools_running:
                                    turn_open = False
                                    if allowed_epoch != epoch:
                                        await emit('error', {'message': 'The voice model did not produce a checked answer. Please repeat your question.'})
                                    await emit('status', {'stage': 'listening', 'message': 'Your turn. Speak naturally.'})
                        if response.tool_call:
                            for fc in response.tool_call.function_calls:
                                if fc.id in tool_ids:
                                    continue
                                tool_ids.add(fc.id)
                                # The input transcription is what the UI shows the seller and
                                # is the closest representation of the words actually heard.
                                # Live may translate or paraphrase its generated function
                                # argument, which can fail exact-fact validation in Hindi/Hinglish.
                                utterance = user_buffer.strip() or str((fc.args or {}).get('utterance', '')).strip()
                                task = asyncio.create_task(handle_tool(fc, epoch, utterance))
                                tools_running.add(task)
                                task.add_done_callback(tool_finished)
                        if response.go_away:
                            await emit('error', {'message': 'The voice connection is ending. Reconnect to continue with your saved session context.'})
                            return

            tasks = [asyncio.create_task(upstream()), asyncio.create_task(downstream())]
            try:
                done, pending = await asyncio.wait(tasks, timeout=settings.max_call_seconds, return_when=asyncio.FIRST_COMPLETED)
                if not done:
                    await emit('status', {'stage': 'disconnected', 'message': 'The five-minute call limit was reached. Your session data is still available.'})
                for task in done:
                    task.result()
            finally:
                for task in [*tasks, *tools_running, *background_tasks]:
                    task.cancel()
                await asyncio.gather(*tasks, *tools_running, *background_tasks, return_exceptions=True)
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        await emit('error', {'message': safe_error(exc)})
    finally:
        await client.aio.aclose()
        try:
            await ws.close()
        except RuntimeError:
            pass
        await emit('status', {'stage': 'disconnected', 'message': 'Voice disconnected. Reconnect to continue this session.'})


def public_state(state):
    return {k: state.get(k) for k in ['dataset', 'facts', 'summary', 'plan', 'history', 'revision']}
