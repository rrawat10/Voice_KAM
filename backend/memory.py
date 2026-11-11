"""Retain complete conversation turns, rather than cutting by message count."""

HISTORY_TURNS = 15


def recent_turns(history):
    starts = []
    seen = set()
    for index, entry in enumerate(history):
        if entry['role'] == 'user' and entry['turn'] not in seen:
            starts.append(index)
            seen.add(entry['turn'])
    if len(starts) > HISTORY_TURNS:
        return history[starts[-HISTORY_TURNS]:]
    # Bound greeting-only reconnects too; they are not seller conversation turns.
    return history[:] if starts else history[-HISTORY_TURNS:]
