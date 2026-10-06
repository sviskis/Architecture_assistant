from types import SimpleNamespace
from architecture_assistant_gui.app import GuiApp
from architecture_assistant_gui.core import CoreWorker
from architecture_assistant.composition import CompositionConfig
from architecture_assistant.composition.provider_settings import load_provider_settings


def test_agent_settings_survive_worker_restart(tmp_path):
    config = CompositionConfig(database_path=tmp_path / "project.db", provider_settings_path=tmp_path / "providers.json", exchange_dir=tmp_path / "exchange", report_dir=tmp_path / "reports")
    worker = CoreWorker(config)
    worker.open()
    try:
        assert worker.save_provider_settings({"agent_a": {"provider": "gemini", "model": "gemini-3.1-pro-preview", "api_key": "offline-test-key"}})["saved"]
    finally:
        worker.close()
    worker = CoreWorker(config)
    worker.open()
    try:
        assert worker.save_provider_settings({"agent_a": {"model": "custom-model", "api_key": None}})["saved"]
    finally:
        worker.close()
    settings = load_provider_settings(config.provider_settings_path).settings
    assert settings.agent_a.provider == "gemini"
    assert settings.agent_a.model == "custom-model"
    assert settings.agent_a.api_key == "offline-test-key"


def test_close_saves_current_fields_before_destroying_window():
    saved = []
    finished = []
    class Runner:
        is_busy = False
        def poll(self):
            result = getattr(self, "result", None)
            self.result = None
            return [result] if result else []
        def submit(self, label, action):
            worker = SimpleNamespace(save_provider_settings=lambda payload: saved.append(payload) or {"saved": True})
            self.result = SimpleNamespace(label=label, error=None, payload=action(worker))
            return True
    app = GuiApp.__new__(GuiApp)
    app._runner = Runner()
    app._finish_close = lambda: finished.append(True)
    app._save_settings_before_close({"agent_a": {"model": "chosen-model", "api_key": "offline-key"}})
    assert saved[0]["agent_a"]["model"] == "chosen-model"
    assert finished == [True]
