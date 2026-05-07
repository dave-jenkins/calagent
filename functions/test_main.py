import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import MagicMock, patch


MAIN_PATH = Path(__file__).with_name("main.py")


class FakeRequest:
    def __init__(self, payload):
        self._payload = payload

    def get_json(self, silent=True):
        return self._payload


def load_main_module():
    """Load main.py with Google SDK modules stubbed for local unit tests."""
    fake_db = MagicMock()
    fake_db.collection.return_value.document.return_value.get.return_value.exists = True
    fake_db.collection.return_value.stream.return_value = []

    google_module = types.ModuleType("google")
    cloud_module = types.ModuleType("google.cloud")
    firestore_module = types.ModuleType("google.cloud.firestore")
    oauth2_module = types.ModuleType("google.oauth2")
    service_account_module = types.ModuleType("google.oauth2.service_account")
    apiclient_module = types.ModuleType("googleapiclient")
    discovery_module = types.ModuleType("googleapiclient.discovery")
    functions_framework_module = types.ModuleType("functions_framework")

    firestore_module.Client = lambda *args, **kwargs: fake_db
    discovery_module.build = lambda *args, **kwargs: MagicMock()
    oauth2_module.service_account = service_account_module
    cloud_module.firestore = firestore_module
    google_module.cloud = cloud_module
    google_module.oauth2 = oauth2_module
    apiclient_module.discovery = discovery_module

    module_stubs = {
        "functions_framework": functions_framework_module,
        "google": google_module,
        "google.cloud": cloud_module,
        "google.cloud.firestore": firestore_module,
        "google.oauth2": oauth2_module,
        "google.oauth2.service_account": service_account_module,
        "googleapiclient": apiclient_module,
        "googleapiclient.discovery": discovery_module,
    }

    with patch.dict(sys.modules, module_stubs):
        spec = importlib.util.spec_from_file_location("calendar_main_under_test", MAIN_PATH)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module


class TestMain(unittest.TestCase):
    def setUp(self):
        self.main = load_main_module()

    def test_is_calendar_intent_detects_keywords(self):
        self.assertTrue(self.main.MessageParser.is_calendar_intent("Please create event for Friday"))
        self.assertFalse(self.main.MessageParser.is_calendar_intent("What is for dinner tonight?"))

    def test_calendar_agent_returns_400_when_json_missing(self):
        body, status = self.main.calendar_agent(FakeRequest(None))
        self.assertEqual(status, 400)
        self.assertEqual(body["status"], "error")

    def test_calendar_agent_help_command_sends_response(self):
        class FakeHandler:
            def handle_admin_help(self):
                return "ADMIN HELP"

            def handle_help(self):
                return "HELP TEXT"

        sent_messages = []
        with patch.object(self.main, "CommandHandler", FakeHandler):
            with patch.object(
                self.main.GroupMeManager,
                "send_message",
                side_effect=lambda message: sent_messages.append(message) or True,
            ):
                req = FakeRequest(
                    {
                        "text": "help",
                        "user_id": "user-1",
                        "group_id": "group-1",
                        "name": "Alice",
                        "sender_id": "12345",
                        "sender_type": "user",
                    }
                )
                body, status = self.main.calendar_agent(req)

        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "success")
        self.assertEqual(sent_messages, ["HELP TEXT"])

    def test_calendar_agent_ignores_its_own_bot_messages(self):
        with patch.object(
            self.main.GroupMeManager,
            "send_message",
            side_effect=AssertionError("should not send"),
        ):
            req = FakeRequest(
                {
                    "text": "help",
                    "user_id": "user-1",
                    "group_id": "group-1",
                    "name": "CalendarMgr",
                    "sender_id": "909774",
                    "sender_type": "bot",
                }
            )
            body, status = self.main.calendar_agent(req)

        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ignored")


if __name__ == "__main__":
    unittest.main()
