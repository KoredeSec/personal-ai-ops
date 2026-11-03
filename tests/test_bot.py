"""End-to-end bot tests through the real python-telegram-bot Application.

A fake transport replaces HTTP, so every outgoing Bot API call is captured
instead of sent. This exercises gates, handlers, DB writes and the error
handler exactly as in production, minus the network.
"""

import json
import time

import pytest
from sqlalchemy import select
from telegram import Update
from telegram.request import BaseRequest

from app.bot.application import build_application
from app.core.llm import GeminiClient, LLMResult
from app.database.models import AuditLog, User
from app.database.session import session_scope

OWNER = 1001
STRANGER = 666
BOT_ID = 123456789


class FakeTelegram(BaseRequest):
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self._msg_id = 100

    @property
    def read_timeout(self):
        return None

    async def initialize(self):
        pass

    async def shutdown(self):
        pass

    async def do_request(self, url, method, request_data=None, **kwargs):
        endpoint = url.rsplit("/", 1)[-1]
        params = request_data.parameters if request_data else {}
        self.calls.append((endpoint, params))
        if endpoint == "getMe":
            result = {"id": BOT_ID, "is_bot": True, "first_name": "Ops", "username": "ops_test_bot",
                      "can_join_groups": False, "can_read_all_group_messages": False,
                      "supports_inline_queries": False}
        elif endpoint == "sendMessage":
            self._msg_id += 1
            result = {"message_id": self._msg_id, "date": int(time.time()),
                      "chat": {"id": params["chat_id"], "type": "private"},
                      "from": {"id": BOT_ID, "is_bot": True, "first_name": "Ops"},
                      "text": params.get("text", "")}
        else:
            result = True
        return 200, json.dumps({"ok": True, "result": result}).encode()

    def sent_texts(self):
        return [p["text"] for e, p in self.calls if e == "sendMessage"]


_update_id = 0


def make_update(app, text, user_id=OWNER, chat_type="private"):
    global _update_id
    _update_id += 1
    chat_id = user_id if chat_type == "private" else -100500
    msg = {"message_id": _update_id, "date": int(time.time()),
           "chat": {"id": chat_id, "type": chat_type, **({"title": "g"} if chat_type != "private" else {})},
           "from": {"id": user_id, "is_bot": False, "first_name": "Test"}, "text": text}
    if text.startswith("/"):
        msg["entities"] = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]
    return Update.de_json({"update_id": _update_id, "message": msg}, app.bot)


class StubLLM(GeminiClient):
    async def generate(self, prompt, **kw):
        return LLMResult(text="OK", model=self.model_for(kw.get("tier", "primary")), total_tokens=3)


@pytest.fixture()
async def bot(settings):
    fake = FakeTelegram()
    app = build_application(settings, request=fake, llm=StubLLM(settings))
    await app.initialize()
    yield app, fake
    await app.shutdown()


async def test_start_registers_user_and_replies(bot):
    app, fake = bot
    await app.process_update(make_update(app, "/start"))
    assert "Ready" in fake.sent_texts()[-1]
    assert "WAT" in fake.sent_texts()[-1]
    with session_scope() as s:
        u = s.scalar(select(User))
        assert u.telegram_user_id == OWNER and u.telegram_chat_id == OWNER


async def test_unauthorised_user_gets_no_response_and_is_audited(bot):
    app, fake = bot
    await app.process_update(make_update(app, "/start", user_id=STRANGER))
    await app.process_update(make_update(app, "hello", user_id=STRANGER))
    assert fake.sent_texts() == []
    with session_scope() as s:
        assert s.scalar(select(User)) is None
        events = s.scalars(select(AuditLog).where(AuditLog.event_type == "access_denied")).all()
        # Throttled: one audit row per stranger per hour, however many messages.
        assert len(events) == 1 and events[0].actor_telegram_id == STRANGER


async def test_owner_in_group_chat_is_ignored(bot):
    app, fake = bot
    await app.process_update(make_update(app, "/status", chat_type="group"))
    assert fake.sent_texts() == []


async def test_help_ping_status(bot):
    app, fake = bot
    for cmd in ("/help", "/ping", "/status"):
        await app.process_update(make_update(app, cmd))
    help_text, ping_text, status_text = fake.sent_texts()
    assert "/status" in help_text
    assert ping_text.startswith("pong")
    assert "✅ OK – Database" in status_text and "0002_delivery_state" in status_text
    assert "Milestone" not in help_text
    assert "AIza" not in status_text and "TEST_TOKEN" not in status_text  # no secrets


async def test_aitest_uses_light_model(bot, settings):
    app, fake = bot
    await app.process_update(make_update(app, "/aitest"))
    assert settings.gemini_model_light in fake.sent_texts()[-1]


async def test_unknown_command_and_plain_text_fallbacks(bot):
    app, fake = bot
    await app.process_update(make_update(app, "/nonsense"))
    await app.process_update(make_update(app, "remind me tomorrow"))
    unknown, text = fake.sent_texts()
    assert "don't know that command" in unknown and "free-text" in text
    assert "Milestone" not in unknown + text


async def test_known_command_does_not_also_trigger_unknown_handler(bot):
    app, fake = bot
    await app.process_update(make_update(app, "/ping"))
    assert len(fake.sent_texts()) == 1


async def test_rate_limit_warns_once_then_stays_silent(settings):
    settings.rate_limit_per_minute = 2
    fake = FakeTelegram()
    app = build_application(settings, request=fake, llm=StubLLM(settings))
    await app.initialize()
    try:
        for _ in range(5):
            await app.process_update(make_update(app, "/ping"))
    finally:
        await app.shutdown()
    texts = fake.sent_texts()
    assert texts[:2] == [t for t in texts[:2] if t.startswith("pong")]
    assert len(texts) == 3 and texts[2].startswith("Too many messages")
    assert "WAT" in texts[2]  # exact local unblock time


async def test_handler_exception_returns_reference_not_details(bot, monkeypatch):
    app, fake = bot

    def explode(*a, **k):
        raise RuntimeError("secret internal detail")

    monkeypatch.setattr("app.bot.handlers.basic.collect_health", explode)
    await app.process_update(make_update(app, "/status"))
    reply = fake.sent_texts()[-1]
    assert "ref" in reply and "secret internal detail" not in reply
    with session_scope() as s:
        assert s.scalar(select(AuditLog).where(AuditLog.event_type == "handler_error"))


async def test_post_init_sets_commands_and_registers_jobs(bot):
    app, fake = bot
    await app.job_queue.start()
    try:
        await app.post_init(app)
        assert any(e == "setMyCommands" for e, _ in fake.calls)
        names = sorted(j.name for j in app.job_queue.jobs())
        assert names == ["backup", "heartbeat", "housekeeping", "reminder_dispatch"]
    finally:
        await app.job_queue.stop()


async def test_slow_ai_call_does_not_block_other_commands(settings):
    """With concurrent updates, /ping answers while /aitest is still waiting on Gemini."""
    import asyncio

    release = asyncio.Event()

    class SlowLLM(StubLLM):
        async def generate(self, prompt, **kw):
            await release.wait()
            return await super().generate(prompt, **kw)

    fake = FakeTelegram()
    app = build_application(settings, request=fake, llm=SlowLLM(settings))
    await app.initialize()
    await app.start()
    try:
        await app.update_queue.put(make_update(app, "/aitest"))
        await app.update_queue.put(make_update(app, "/ping"))
        for _ in range(100):
            if any(t.startswith("pong") for t in fake.sent_texts()):
                break
            await asyncio.sleep(0.02)
        assert any(t.startswith("pong") for t in fake.sent_texts()), "ping blocked by slow AI call"
        assert not any("Gemini is working" in t for t in fake.sent_texts())
        assert any(e == "sendChatAction" for e, _ in fake.calls)  # typing indicator shown
        release.set()
        for _ in range(100):
            if any("Gemini is working" in t for t in fake.sent_texts()):
                break
            await asyncio.sleep(0.02)
        assert any("Gemini is working" in t for t in fake.sent_texts())
    finally:
        release.set()
        await app.stop()
        await app.shutdown()


async def test_long_replies_are_chunked(bot):
    from app.bot.reply import reply_text

    app, fake = bot
    update = make_update(app, "/ping")
    await reply_text(update, "line\n" * 2000)  # ~10k chars
    sent = fake.sent_texts()
    assert len(sent) >= 3 and all(len(t) <= 4096 for t in sent)


async def test_status_reports_problems_first(bot):
    app, fake = bot
    await app.process_update(make_update(app, "/status"))
    text = fake.sent_texts()[-1]
    # Fresh test env: no heartbeat written and no backup yet -> listed as problems up top.
    assert text.startswith("<b>2 problem(s):</b>")
    assert "heartbeat stale" in text and "No backup" in text
