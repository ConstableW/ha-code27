"""Standalone CODE27 split gates; no Home Assistant installation required."""

from __future__ import annotations

import ast
import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "custom_components" / "code27"


class DistributionTests(unittest.TestCase):
    def test_cards_ship_inside_the_integration_and_are_registered(self):
        for card in ("code27-card.js", "haseamp-card.js", "portrait.png"):
            self.assertTrue((COMPONENT / "cards" / card).is_file(), card)
        init = (COMPONENT / "__init__.py").read_text(encoding="utf-8")
        self.assertIn('"/code27-cards"', init)
        self.assertIn("add_extra_js_url", init)

    def test_runtime_modules_have_no_hase_imports(self):
        for path in COMPONENT.glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    self.assertNotIn("hase", node.module or "", path.name)
                    self.assertLess(node.level, 2, path.name)
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        self.assertNotIn("hase", alias.name, path.name)

    def test_service_selectors_and_package_boundary(self):
        services = (COMPONENT / "services.yaml").read_text(encoding="utf-8")
        self.assertIn("integration: code27", services)
        self.assertNotIn("integration: hase", services)
        for stale in ("bridge.py", "conversation.py", "spellbook.py", "spellbooker_ha.py"):
            self.assertFalse((COMPONENT / stale).exists(), stale)
        for stale in ("core", "adapters", "backends", "vacuum_providers"):
            self.assertFalse((COMPONENT / stale).exists(), stale)


class ExternalHubTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        package = types.ModuleType("split_standalone")
        package.__path__ = [str(COMPONENT)]
        sys.modules[package.__name__] = package
        coordinator_module = types.ModuleType("split_standalone.coordinator")
        coordinator_module.EVENT_USER_MESSAGE = "code27_user_message"
        coordinator_module.EVENT_ASSISTANT_MESSAGE = "code27_assistant_message"
        sys.modules[coordinator_module.__name__] = coordinator_module
        speech = types.ModuleType("split_standalone.speech")
        speech.async_speak_text = AsyncMock()
        sys.modules[speech.__name__] = speech
        spec = importlib.util.spec_from_file_location(
            "split_standalone.external_hub", COMPONENT / "external_hub.py"
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        cls.module = module
        cls.speech = speech

    @classmethod
    def tearDownClass(cls):
        for name in ("split_standalone.external_hub", "split_standalone.speech",
                     "split_standalone.coordinator", "split_standalone"):
            sys.modules.pop(name, None)

    async def test_facade_uses_existing_coordinator_and_scopes_chat(self):
        callbacks = {}
        removed = []
        def listen(kind, callback):
            callbacks[kind] = callback
            return lambda: removed.append(kind)
        hass = types.SimpleNamespace(bus=types.SimpleNamespace(async_listen=listen))
        entry = types.SimpleNamespace(entry_id="device-a")
        data = types.SimpleNamespace(
            current_slot={"slot_number": 1}, system={"ai_language": "de"},
            character_slots=[{"slot_number": 1}],
        )
        coordinator = types.SimpleNamespace(
            data=data, last_update_success=True, stopping=False,
            notification_active=False,
            async_add_listener=lambda callback: lambda: None,
            async_send_chat_reply=AsyncMock(return_value="reply-1"),
            async_send_chat_image_reply=AsyncMock(return_value="image-1"),
            async_cleanup_assist_exchange=AsyncMock(return_value=[]),
            async_trigger_notification=AsyncMock(),
            async_acknowledge_notification=AsyncMock(),
        )
        hub = self.module.Code27ExternalHub(hass, entry, coordinator)
        self.assertEqual(hub.contract_version, 1)
        self.assertEqual(hub.entry_id, "device-a")
        self.assertTrue(hub.snapshot.available)
        data.current_slot["slot_number"] = 2
        self.assertEqual(hub.snapshot.current_slot["slot_number"], 2)
        events = []
        unsubscribe = hub.subscribe_chat(lambda kind, payload: events.append((kind, payload)))
        callbacks["code27_user_message"](types.SimpleNamespace(data={"config_entry_id": "other"}))
        callbacks["code27_user_message"](types.SimpleNamespace(data={"config_entry_id": "device-a"}))
        self.assertEqual([kind for kind, _ in events], ["user"])
        self.assertEqual(await hub.send_chat_reply("hi", 1), "reply-1")
        await hub.speak_text("hello", expected_slot=1)
        self.speech.async_speak_text.assert_awaited_once()
        unsubscribe()
        self.assertEqual(set(removed), {"code27_user_message", "code27_assistant_message"})


class StandaloneSetupTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        ha = types.ModuleType("homeassistant")
        ha.__path__ = []
        config = types.ModuleType("homeassistant.config_entries")
        config.ConfigEntry = object
        const = types.ModuleType("homeassistant.const")
        const.CONF_HOST, const.CONF_PORT = "host", "port"
        const.Platform = types.SimpleNamespace(**{
            name: name for name in (
                "BINARY_SENSOR", "BUTTON", "IMAGE", "LIGHT", "MEDIA_PLAYER", "NUMBER",
                "SELECT", "SENSOR", "SWITCH", "TODO",
            )
        })
        core = types.ModuleType("homeassistant.core")
        core.HomeAssistant = object
        helpers = types.ModuleType("homeassistant.helpers")
        helpers.__path__ = []
        aiohttp = types.ModuleType("homeassistant.helpers.aiohttp_client")
        aiohttp.async_get_clientsession = lambda hass: object()
        cv = types.ModuleType("homeassistant.helpers.config_validation")
        cv.config_entry_only_config_schema = lambda domain: object()
        helpers.config_validation = cv
        for module in (ha, config, const, core, helpers, aiohttp, cv):
            sys.modules[module.__name__] = module

        cls.created_clients = []
        cls.created_coordinators = []
        cls.service_calls = []
        package = "split_setup"
        fake_const = types.ModuleType(f"{package}.const")
        for key, value in {
            "CONF_LOCAL_SECRET": "local_secret", "CONF_MACHINE_ID": "machine_id",
            "CONF_SCAN_INTERVAL": "scan_interval", "CONF_USER_ID": "user_id",
            "DEFAULT_SCAN_INTERVAL": 15, "DOMAIN": "code27",
        }.items():
            setattr(fake_const, key, value)
        api = types.ModuleType(f"{package}.api")
        def client_factory(*args):
            client = object()
            cls.created_clients.append(client)
            return client
        api.Code27Client = client_factory
        coordinator_module = types.ModuleType(f"{package}.coordinator")
        class Coordinator:
            def __init__(self, hass, entry, client, interval):
                self.hass, self.entry, self.client = hass, entry, client
                self.interval = interval
                self.refresh = AsyncMock()
                self.stop = AsyncMock()
                cls.created_coordinators.append(self)
            async def async_config_entry_first_refresh(self):
                await self.refresh()
            def async_start_realtime(self):
                self.started = True
            async def async_stop_realtime(self):
                await self.stop()
        coordinator_module.Code27Coordinator = Coordinator
        hub = types.ModuleType(f"{package}.external_hub")
        hub.Code27ExternalHub = lambda hass, entry, coordinator: types.SimpleNamespace(
            contract_version=1, entry_id=entry.entry_id, coordinator=coordinator,
        )
        music = types.ModuleType(f"{package}.music_api")
        music.async_setup_music_entry = AsyncMock()
        services = types.ModuleType(f"{package}.services")
        services.async_register_services = lambda hass: cls.service_calls.append("register")
        services.async_unregister_services = lambda hass: cls.service_calls.append("unregister")
        for module in (fake_const, api, coordinator_module, hub, music, services):
            sys.modules[module.__name__] = module
        spec = importlib.util.spec_from_file_location(
            package, COMPONENT / "__init__.py", submodule_search_locations=[str(COMPONENT)]
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[package] = module
        spec.loader.exec_module(module)
        cls.module = module
        cls.music = music

    @classmethod
    def tearDownClass(cls):
        for name in (
            "split_setup", "split_setup.const", "split_setup.api",
            "split_setup.coordinator", "split_setup.external_hub",
            "split_setup.music_api", "split_setup.services", "homeassistant",
            "homeassistant.config_entries", "homeassistant.const", "homeassistant.core",
            "homeassistant.helpers", "homeassistant.helpers.aiohttp_client",
            "homeassistant.helpers.config_validation",
        ):
            sys.modules.pop(name, None)

    async def test_setup_creates_one_runtime_and_unload_releases_it(self):
        callbacks = []
        entry = types.SimpleNamespace(
            entry_id="device-a", data={
                "host": "192.0.2.10", "port": 8081, "user_id": "u",
                "local_secret": "s", "machine_id": "m",
            }, options={},
            add_update_listener=lambda callback: lambda: None,
            async_on_unload=callbacks.append,
        )
        manager = types.SimpleNamespace(
            async_forward_entry_setups=AsyncMock(),
            async_unload_platforms=AsyncMock(return_value=True),
        )
        hass = types.SimpleNamespace(data={}, config_entries=manager)
        self.assertTrue(await self.module.async_setup_entry(hass, entry))
        self.assertEqual(len(self.created_clients), 1)
        self.assertEqual(len(self.created_coordinators), 1)
        coordinator = self.created_coordinators[0]
        coordinator.refresh.assert_awaited_once()
        self.assertTrue(coordinator.started)
        self.assertIs(hass.data["code27"][entry.entry_id], coordinator)
        self.assertIs(entry.runtime_data.coordinator, coordinator)
        self.assertEqual(entry.runtime_data.entry_id, entry.entry_id)
        self.assertEqual(self.service_calls, ["register"])
        self.music.async_setup_music_entry.assert_awaited_once()
        self.assertTrue(await self.module.async_unload_entry(hass, entry))
        self.assertNotIn(entry.entry_id, hass.data["code27"])
        coordinator.stop.assert_awaited_once()
        self.assertEqual(self.service_calls, ["register", "unregister"])
