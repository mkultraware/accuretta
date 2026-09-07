"""Conversation search and safe-boundary delivery of user task updates."""

from collections import OrderedDict
import threading


class SteeringInbox:
    def __init__(self):
        self.lock = threading.RLock()
        self.active = set()
        self.queues = {}
        self.receipts = OrderedDict()

    def begin(self, chat_id):
        with self.lock:
            self.active.add(chat_id)
            self.queues[chat_id] = []

    def submit(self, chat_id, message):
        with self.lock:
            key = (chat_id, message["id"])
            if key in self.receipts:
                return self.receipts[key]
            if chat_id not in self.active:
                return "inactive"
            queue = self.queues.setdefault(chat_id, [])
            if len(queue) >= 20:
                return "full"
            queue.append(dict(message))
            self.receipts[key] = "pending"
            while len(self.receipts) > 1000:
                old = next((k for k, status in self.receipts.items() if status != "pending"), None)
                if old is None:
                    break
                del self.receipts[old]
            return "pending"

    def pending(self, chat_id):
        with self.lock:
            return bool(self.queues.get(chat_id))

    def take(self, chat_id, close_if_empty=False):
        with self.lock:
            messages = self.queues.get(chat_id, [])
            self.queues[chat_id] = []
            if not messages and close_if_empty:
                self.active.discard(chat_id)
            return messages

    def applied(self, chat_id, message_id):
        with self.lock:
            self.receipts[(chat_id, message_id)] = "applied"

    def status(self, chat_id, message_id):
        with self.lock:
            return self.receipts.get((chat_id, message_id), "unknown")

    def finish(self, chat_id):
        with self.lock:
            self.active.discard(chat_id)
            messages = self.queues.pop(chat_id, [])
            for key, status in self.receipts.items():
                if key[0] == chat_id and status == "pending":
                    self.receipts[key] = "deferred"
            return messages


def message_text(message):
    content = message.get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(part.get("text", "") for part in content
                         if isinstance(part, dict) and part.get("type") == "text")
    return ""


def search_conversations(chats, query, limit=30):
    query = str(query or "").strip()[:200]
    if len(query) < 2:
        return []
    terms = query.casefold().split()
    results = []
    records = chats.get("chats", {})
    order = sorted(records, key=lambda key: records[key].get("updated", 0), reverse=True)
    for chat_id in order:
        chat = records[chat_id]
        messages = chat.get("messages", [])
        visible = [i for i, m in enumerate(messages)
                   if (m.get("role") in {"user", "assistant"} or m.get("_note"))
                   and not m.get("_internal")]
        hits = 0
        for index, message in enumerate(messages):
            if message.get("role") not in {"user", "assistant", "tool"} or message.get("invisible"):
                continue
            content = message_text(message)
            folded = content.casefold()
            if not all(term in folded for term in terms):
                continue
            position = folded.find(terms[0])
            start = max(0, position - 75)
            excerpt = ("…" if start else "") + content[start:start + 250].replace("\n", " ")
            if start + 250 < len(content):
                excerpt += "…"
            anchor = next((i for i in visible if i >= index), visible[-1] if visible else None)
            results.append({"chat_id": chat_id, "title": chat.get("title") or "Untitled",
                            "message_index": index, "visible_index": visible.index(anchor) if anchor is not None else None,
                            "role": message.get("role"), "internal": bool(message.get("_internal") or message.get("role") == "tool"),
                            "excerpt": excerpt})
            hits += 1
            if len(results) >= limit:
                return results
            if hits >= 3:
                break
    return results
