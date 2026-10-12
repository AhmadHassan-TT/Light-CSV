"""Saved assistant chats: one JSON file per chat in the app's data folder."""
import json
import os
import time
import uuid

from PyQt6.QtCore import QStandardPaths

MAX_CHATS = 200


def chatsDir():
    base = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppDataLocation)
    path = os.path.join(base, 'chats')
    os.makedirs(path, exist_ok=True)
    return path


def toJson(value):
    """Plain JSON data from a conversation that may hold SDK content blocks."""
    if hasattr(value, 'model_dump'):
        # Only the fields the API actually returned, so the turn can be sent back unchanged
        return value.model_dump(mode='json', exclude_unset=True, exclude_none=True)
    if isinstance(value, dict):
        return {k: toJson(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [toJson(v) for v in value]
    return value


def newChat(provider, model, question):
    title = ' '.join(question.split())
    return {'id': time.strftime('%Y%m%d-%H%M%S-') + uuid.uuid4().hex[:6], 'title': title[:80] or 'Chat',
            'provider': provider, 'model': model, 'created': time.time(), 'updated': time.time(),
            'display': [], 'messages': []}


def save(chat):
    chat['updated'] = time.time()
    path = os.path.join(chatsDir(), chat['id'] + '.json')
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(toJson(chat), f, ensure_ascii=False)
    os.replace(tmp, path)  # atomic, so a crash never leaves half a chat
    prune()


def load(chatId):
    with open(os.path.join(chatsDir(), chatId + '.json'), encoding='utf-8') as f:
        return json.load(f)


def delete(chatId):
    try:
        os.remove(os.path.join(chatsDir(), chatId + '.json'))
    except FileNotFoundError:
        pass


def clear():
    for name in os.listdir(chatsDir()):
        if name.endswith('.json'):
            os.remove(os.path.join(chatsDir(), name))


def listChats():
    """Summaries, newest first."""
    chats = []
    for name in os.listdir(chatsDir()):
        if not name.endswith('.json'):
            continue
        try:
            with open(os.path.join(chatsDir(), name), encoding='utf-8') as f:
                data = json.load(f)
            chats.append({'id': data['id'], 'title': data.get('title', 'Chat'), 'provider': data.get('provider', 'claude'),
                          'updated': data.get('updated', 0)})
        except (OSError, ValueError, KeyError):
            continue  # skip a damaged file rather than failing the menu
    chats.sort(key=lambda c: c['updated'], reverse=True)
    return chats


def prune():
    for chat in listChats()[MAX_CHATS:]:
        delete(chat['id'])


def whenText(stamp):
    """Short label: "Today 14:05", "Yesterday 09:12" or "12 Oct"."""
    t, now = time.localtime(stamp), time.localtime()
    days = (time.mktime(now[:3] + (0, 0, 0, 0, 0, -1)) - time.mktime(t[:3] + (0, 0, 0, 0, 0, -1))) // 86400
    if days == 0:
        return time.strftime('Today %H:%M', t)
    if days == 1:
        return time.strftime('Yesterday %H:%M', t)
    return time.strftime('%d %b' if t.tm_year == now.tm_year else '%d %b %Y', t)
