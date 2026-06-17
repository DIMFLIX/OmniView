"""GUI-тесты: взаимоисключение sequential-режима и мультиплекса.

Покрытие:
  - ManagerBridge принудительно выключает мультиплекс, когда включён
    sequential-режим (мост сам реализует последовательное переключение).
  - SettingsPanel.current_settings() отдаёт multiplex_mode='off' при
    включённом sequential и блокирует контролы мультиплекса в UI.

Требует PyQt6; пропускается, если он недоступен. Qt работает в offscreen-режиме.
"""

import os

import pytest

pytest.importorskip("PyQt6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="module")
def qapp():
    """Единственный QApplication на модуль (offscreen)."""
    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


class TestBridgeSequentialDisablesMultiplex:
    """ManagerBridge: sequential forces multiplex off."""

    def test_sequential_forces_multiplex_off(self, qapp):
        """sequential_mode=True → multiplex_mode forced to 'off'.

        Sequential opens one camera at a time, so USB contention is
        impossible and the rotation scheduler is unnecessary.
        """
        from src.omniview.gui.manager_bridge import ManagerBridge

        bridge = ManagerBridge()
        bridge._pending_attrs = {"sequential_mode": True, "multiplex_mode": "auto"}
        bridge._create_managers()

        assert bridge._usb_manager.multiplex_mode == "off"

    def test_multiplex_passthrough_when_not_sequential(self, qapp):
        from src.omniview.gui.manager_bridge import ManagerBridge

        bridge = ManagerBridge()
        bridge._pending_attrs = {"sequential_mode": False, "multiplex_mode": "force"}
        bridge._create_managers()

        assert bridge._usb_manager.multiplex_mode == "force"


class TestSettingsPanelMutualExclusion:
    """SettingsPanel: sequential and multiplex are mutually exclusive."""

    def test_sequential_forces_multiplex_off(self, qapp):
        from src.omniview.gui.settings_panel import SettingsPanel

        panel = SettingsPanel()
        panel._check_multiplex.setChecked(True)
        panel._combo_multiplex.setCurrentIndex(
            panel._combo_multiplex.findData("force")
        )
        panel._check_sequential.setChecked(True)

        settings = panel.current_settings()
        assert settings["sequential_mode"] is True
        assert settings["multiplex_mode"] == "off"
        # UI reflects mutual exclusion
        assert panel._check_multiplex.isEnabled() is False
        assert panel._combo_multiplex.isEnabled() is False
        assert panel._spin_interval.isEnabled() is True

    def test_multiplex_active_when_sequential_off(self, qapp):
        from src.omniview.gui.settings_panel import SettingsPanel

        panel = SettingsPanel()
        panel._check_sequential.setChecked(False)
        panel._check_multiplex.setChecked(True)
        panel._combo_multiplex.setCurrentIndex(
            panel._combo_multiplex.findData("force")
        )

        settings = panel.current_settings()
        assert settings["multiplex_mode"] == "force"
        assert panel._combo_multiplex.isEnabled() is True
        assert panel._spin_interval.isEnabled() is False
