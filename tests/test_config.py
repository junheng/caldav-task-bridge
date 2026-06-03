from __future__ import annotations

import os
import unittest
from contextlib import contextmanager
from typing import Iterator

from config import Settings


class ConfigTests(unittest.TestCase):
    def test_fns_client_and_ws_settings_are_loaded_from_env(self) -> None:
        env = {
            "RADICALE_URL": "http://radicale:5232",
            "RADICALE_USER": "diomgis",
            "RADICALE_PASSWORD": "secret",
            "FNS_API_URL": "https://fns.example.com",
            "FNS_API_TOKEN": "note-token",
            "FNS_VAULT": "Core",
            "FNS_WS_URL": "wss://fns.example.com/api/user/sync",
            "FNS_CLIENT_TYPE": "caldav-bridge",
            "FNS_CLIENT_NAME": "caldav-bridge",
            "FNS_USER_AGENT": "bridge-agent",
        }
        with patched_env(env):
            settings = Settings.from_env()

        self.assertEqual(settings.fns_ws_url, "wss://fns.example.com/api/user/sync")
        self.assertEqual(settings.fns_client_type, "caldav-bridge")
        self.assertEqual(settings.fns_client_name, "caldav-bridge")
        self.assertEqual(settings.fns_user_agent, "bridge-agent")


@contextmanager
def patched_env(values: dict[str, str]) -> Iterator[None]:
    old_values = {key: os.environ.get(key) for key in values}
    try:
        os.environ.update(values)
        yield
    finally:
        for key, old_value in old_values.items():
            if old_value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old_value


if __name__ == "__main__":
    unittest.main()
