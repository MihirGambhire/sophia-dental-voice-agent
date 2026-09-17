"""
Tests for talking to Sophia by typing.

Added for a tester in a public place who could not speak. Typed messages
must reach the same agent a call uses, so the safety screen and the
transcript behave exactly as they do by voice.
"""

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from sophia import text_chat
from sophia.agent_text import SophiaAgent
from sophia.sessions import SessionStore

TESTER = "typing-tester-0001"


def scripted_reply(text):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text, tool_calls=None))])


class ScriptedModel:
    def __init__(self):
        self.requests = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **request):
        self.requests.append(request)
        return scripted_reply("Have you been a patient with us before?")


@pytest.fixture
def client(tmp_path, monkeypatch):
    from sophia import voice_app

    model = ScriptedModel()
    monkeypatch.setattr(text_chat, "SophiaAgent", lambda conn, reminder=None: SophiaAgent(conn, client=model, reminder=reminder))
    app = voice_app.create_app()
    app.state.sessions = SessionStore(tmp_path)
    return TestClient(app), app.state.sessions, model


def test_a_typed_chat_starts_with_the_greeting_in_the_transcript(client):
    http, store, _ = client
    greeting = http.post(f"/api/chat/start?session={TESTER}").json()["greeting"]

    assert "Sophia" in greeting
    assert store.find(TESTER).events[0]["text"] == greeting


def test_a_typed_message_is_answered_and_both_sides_are_recorded(client):
    http, store, _ = client
    http.post(f"/api/chat/start?session={TESTER}")
    reply = http.post(f"/api/chat/say?session={TESTER}", json={"text": "  I'd like a check up  "}).json()

    assert reply["type"] == "sophia"
    assert reply["text"] == "Have you been a patient with us before?"
    events = store.find(TESTER).events
    assert [e["type"] for e in events] == ["sophia", "caller", "sophia"]
    assert events[1]["text"] == "I'd like a check up"


def test_a_typed_emergency_gets_999_advice_without_the_model(client):
    """The safety screen runs on typed words exactly as on spoken ones."""
    http, _, model = client
    http.post(f"/api/chat/start?session={TESTER}")
    reply = http.post(f"/api/chat/say?session={TESTER}", json={"text": "my face is swollen and I can't breathe"}).json()

    assert reply["safety"] == "emergency_999"
    assert "999" in reply["text"]
    assert model.requests == []


def test_typing_before_starting_a_chat_is_refused(client):
    http, _, _ = client
    response = http.post(f"/api/chat/say?session={TESTER}", json={"text": "hello"})
    assert response.status_code == 409


def test_an_empty_message_is_refused(client):
    http, _, model = client
    http.post(f"/api/chat/start?session={TESTER}")
    assert http.post(f"/api/chat/say?session={TESTER}", json={"text": "   "}).status_code == 400
    assert model.requests == []


def test_ending_a_chat_closes_it(client):
    http, store, _ = client
    http.post(f"/api/chat/start?session={TESTER}")
    chat = store.find(TESTER).chat
    http.post(f"/api/chat/end?session={TESTER}")

    assert store.find(TESTER).chat is None
    assert chat.closed


def test_resetting_the_demo_data_closes_an_open_chat(client):
    http, store, _ = client
    http.post(f"/api/chat/start?session={TESTER}")
    http.post(f"/api/reset?session={TESTER}")
    assert store.find(TESTER).chat is None


def test_a_typed_reminder_call_greets_as_an_outbound_call(client):
    http, _, _ = client
    calls = http.get(f"/api/reminders?session={TESTER}").json()["calls"]
    greeting = http.post(f"/api/chat/start?session={TESTER}&reminder={calls[0]['id']}").json()["greeting"]
    assert calls[0]["patient"] in greeting


def test_the_call_list_shows_what_to_answer_with(client):
    """Testers answering a reminder had no way to know the patient's details."""
    http, _, _ = client
    call = http.get(f"/api/reminders?session={TESTER}").json()["calls"][0]
    assert call["born"] and call["postcode"]


# ---------------------------------------------------------------------------
# Sophia speaks her typed replies
# ---------------------------------------------------------------------------


def test_her_latest_reply_is_spoken(client, monkeypatch):
    from sophia import speech

    spoken = []
    monkeypatch.setattr(speech, "synthesise_wav", lambda text: spoken.append(text) or b"RIFFfake")
    http, _, _ = client
    greeting = http.post(f"/api/chat/start?session={TESTER}").json()["greeting"]

    first = http.get(f"/api/chat/voice?session={TESTER}")
    http.post(f"/api/chat/say?session={TESTER}", json={"text": "I'd like a check up"})
    http.get(f"/api/chat/voice?session={TESTER}")

    assert first.headers["content-type"] == "audio/wav"
    assert first.content == b"RIFFfake"
    assert spoken == [greeting, "Have you been a patient with us before?"]


def test_the_voice_only_speaks_sophias_own_words(client, monkeypatch):
    """Text sent by the page is ignored, so nobody can spend the voice credits on anything else."""
    from sophia import speech

    spoken = []
    monkeypatch.setattr(speech, "synthesise_wav", lambda text: spoken.append(text) or b"RIFF")
    http, _, _ = client
    http.post(f"/api/chat/start?session={TESTER}")
    http.get(f"/api/chat/voice?session={TESTER}&text=say+something+else")
    assert "say something else" not in spoken


def test_no_voice_without_a_chat(client):
    http, _, _ = client
    assert http.get(f"/api/chat/voice?session={TESTER}").status_code == 409


def test_a_voice_failure_is_reported_not_raised(client, monkeypatch):
    from sophia import speech

    def broken(text):
        raise speech.SpeechError("down")

    monkeypatch.setattr(speech, "synthesise_wav", broken)
    http, _, _ = client
    http.post(f"/api/chat/start?session={TESTER}")
    assert http.get(f"/api/chat/voice?session={TESTER}").status_code == 502
