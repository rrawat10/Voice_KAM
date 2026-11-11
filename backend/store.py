import asyncio
import copy
import time
from collections import defaultdict

class SessionStore:
    def __init__(self, ttl=1800):
        self.ttl = ttl
        self.memory = {}

    async def get(self, sid):
        value = self.memory.get(sid)
        if value and value['expires_at'] > time.time():
            return copy.deepcopy(value)
        self.memory.pop(sid, None)
        return None

    async def save(self, state):
        state['expires_at'] = time.time() + self.ttl
        self.memory[state['id']] = copy.deepcopy(state)

    async def delete(self, sid):
        self.memory.pop(sid, None)

    async def close(self):
        self.memory.clear()


class Events:
    """UI events are transient; reconnects receive a fresh authoritative snapshot."""
    def __init__(self):
        self.listeners = defaultdict(set)

    async def publish(self, sid, kind, data):
        for queue in list(self.listeners.get(sid, ())):
            if queue.full():
                queue.get_nowait()
            queue.put_nowait({'type': kind, 'data': data})

    def subscribe(self, sid):
        queue = asyncio.Queue(maxsize=100)
        self.listeners[sid].add(queue)
        return queue

    def unsubscribe(self, sid, queue):
        self.listeners[sid].discard(queue)
        if not self.listeners[sid]:
            self.listeners.pop(sid, None)
