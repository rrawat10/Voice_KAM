import asyncio
from collections import defaultdict
from contextlib import asynccontextmanager
import json
from pathlib import Path
import secrets
import time
from urllib.parse import urlparse

from fastapi import FastAPI, File, HTTPException, Request, UploadFile, WebSocket
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .config import settings
from .data import excel_template, mock_data, parse_excel
from .live import public_state, run_live
from .store import Events, SessionStore

store = SessionStore(settings.session_ttl_seconds)
events = Events()
locks = defaultdict(asyncio.Lock)
active = {}
rate_limits = {}


async def reap_sessions():
    while True:
        await asyncio.sleep(5)
        now = time.time()
        expired = [sid for sid, state in list(store.memory.items()) if state['expires_at'] <= now]
        for sid in expired:
            await clear_session(sid, 'expired')


@asynccontextmanager
async def lifespan(app):
    cleanup = asyncio.create_task(reap_sessions())
    try:
        yield
    finally:
        cleanup.cancel()
        await asyncio.gather(cleanup, return_exceptions=True)
        await store.close()


app = FastAPI(title='Seller Studio · Voice KAM', lifespan=lifespan)


def valid_origin(connection):
    origin = connection.headers.get('origin')
    if not origin:
        return True
    parsed = urlparse(origin)
    host = connection.headers.get('host', '')
    return parsed.netloc == host or (not settings.secure_cookies and origin in {'http://127.0.0.1:5173', 'http://localhost:5173'})


@app.middleware('http')
async def headers(request, call_next):
    if request.method in {'POST', 'PUT', 'PATCH', 'DELETE'} and not valid_origin(request):
        return JSONResponse({'detail': 'Cross-origin request rejected.'}, status_code=403)
    try:
        response = await call_next(request)
    except Exception:
        return JSONResponse({'detail': 'The server could not complete the request. Please retry.'}, status_code=503)
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Referrer-Policy'] = 'no-referrer'
    if request.url.path.startswith('/api/'):
        response.headers['Cache-Control'] = 'no-store'
    return response


async def current_session(request):
    sid = request.cookies.get('kam_session', '')
    state = await store.get(sid) if sid else None
    if not state:
        if sid:
            raise HTTPException(410, 'Your previous session expired or was cleared. Load your data to start again.')
        raise HTTPException(401, 'Choose your data to start a new session.')
    return sid, state


async def create_session(request, data):
    ip = request.client.host if request.client else 'local'
    now = time.monotonic()
    for key in list(rate_limits):
        rate_limits[key] = [t for t in rate_limits[key] if now - t < 60]
        if not rate_limits[key]:
            rate_limits.pop(key)
    times = rate_limits.setdefault(ip, [])
    if len(times) >= 12:
        raise HTTPException(429, 'Too many session creations. Please wait a minute.')
    times.append(now)
    previous = request.cookies.get('kam_session')
    if previous:
        await clear_session(previous)
    sid = secrets.token_urlsafe(32)
    state = {'id': sid, 'dataset': data, 'facts': {}, 'summary': '', 'plan': None, 'history': [], 'revision': 0}
    await store.save(state)
    response = JSONResponse(public_state(state))
    # Locator outlives server data so a returning browser can be told its session expired.
    response.set_cookie('kam_session', sid, httponly=True, secure=settings.secure_cookies, samesite='strict', max_age=86400)
    return response


async def clear_session(sid, reason='ended'):
    ws = active.get(sid)
    if ws:
        try:
            await ws.close(code=1000)
        except RuntimeError:
            pass
    async with locks[sid]:
        await store.delete(sid)
    await events.publish(sid, 'ended', {'reason': reason})
    # A running task can still reference its lock; remove only once the connection is gone.
    if sid not in active:
        locks.pop(sid, None)


@app.get('/api/health')
async def health():
    return {'ok': True, 'voice_configured': bool(settings.gemini_api_key), 'storage': 'Temporary memory · 30-minute inactivity expiry', 'max_call_seconds': settings.max_call_seconds}


@app.post('/api/sessions/mock')
async def select_mock(request: Request):
    return await create_session(request, mock_data())


@app.post('/api/sessions/upload')
async def upload(request: Request, file: UploadFile = File(...)):
    content = await file.read(2_000_001)
    await file.close()
    try:
        data = await asyncio.to_thread(parse_excel, content, file.filename or '')
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    return await create_session(request, data)


@app.get('/api/template')
async def template():
    return Response(excel_template(), media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', headers={'Content-Disposition': 'attachment; filename="seller-metrics-template.xlsx"'})


@app.get('/api/session')
async def get_session(request: Request):
    _, state = await current_session(request)
    return public_state(state)


@app.get('/api/events')
async def sse(request: Request):
    sid, state = await current_session(request)
    if len(events.listeners.get(sid, ())) >= 3:
        raise HTTPException(429, 'Too many open tabs for this session.')
    async def stream():
        queue = events.subscribe(sid)
        try:
            latest = await store.get(sid)
            yield 'data: ' + json.dumps({'type': 'snapshot', 'data': public_state(latest or state)}) + '\n\n'
            while not await request.is_disconnected():
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                    yield 'data: ' + json.dumps(event, ensure_ascii=False) + '\n\n'
                    if event['type'] == 'ended':
                        return
                except asyncio.TimeoutError:
                    if not await store.get(sid):
                        yield 'data: {"type":"ended","data":{"reason":"expired"}}\n\n'
                        return
                    yield ': keepalive\n\n'
        finally:
            events.unsubscribe(sid, queue)
    return StreamingResponse(stream(), media_type='text/event-stream', headers={'X-Accel-Buffering': 'no', 'Cache-Control': 'no-cache, no-transform'})


@app.websocket('/api/voice')
async def voice(ws: WebSocket):
    if not valid_origin(ws):
        await ws.close(code=1008)
        return
    sid = ws.cookies.get('kam_session', '')
    if not sid or not await store.get(sid):
        await ws.close(code=1008)
        return
    await ws.accept()
    if not settings.gemini_api_key:
        await ws.send_json({'type': 'error', 'message': 'Set GEMINI_API_KEY in the server .env to enable voice. Data preview is ready.'})
        await ws.close(code=1011)
        return
    if sid in active or len(active) >= settings.max_concurrent_calls:
        await ws.send_json({'type': 'error', 'message': 'A voice session is already active, or the demo is at capacity. Please retry shortly.'})
        await ws.close(code=1013)
        return
    active[sid] = ws
    try:
        await run_live(ws, sid, store, events, settings, locks)
    finally:
        active.pop(sid, None)
        if not await store.get(sid):
            locks.pop(sid, None)


DIST = Path(__file__).resolve().parent.parent / 'frontend' / 'dist'
if DIST.is_dir():
    app.mount('/assets', StaticFiles(directory=DIST / 'assets'), name='assets')
    @app.get('/audio-worklet.js')
    async def audio_worklet():
        return FileResponse(DIST / 'audio-worklet.js', media_type='application/javascript')

    @app.get('/favicon.svg')
    async def favicon():
        return FileResponse(DIST / 'favicon.svg', media_type='image/svg+xml')

    @app.get('/')
    async def index():
        return FileResponse(DIST / 'index.html')
