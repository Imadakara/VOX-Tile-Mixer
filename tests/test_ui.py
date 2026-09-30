import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPointF, Qt, QEvent
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QApplication

from test_core import tiny_vox
from vox_mixer.app import MixerWindow
from vox_mixer.core import Model, VoxScene, load_project, project_path


class MixerUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_roles_mix_button_selection_and_drag(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model = Model((1, 1, 1), ((0, 0, 0, 2),))
            source = root / "scene.vox"
            original = root / "tiles"
            variant = root / "tiles_damaged_A"
            original.mkdir()
            variant.mkdir()
            tiny_vox(source, [model], [0, 0])
            tiny_vox(original / "slab.vox", [model], [0])
            tiny_vox(variant / "slab_damaged_A.vox", [model], [0])
            window = MixerWindow()
            try:
                window.open_scene(source)
                window.add_folder(original, "original")
                window.add_folder(variant, "variant")
                self.assertEqual(list(window.sliders), [variant.name])
                window.link_tiles()
                self.assertEqual(window.links[0].status, "linked")
                self.assertFalse(window.export_button.isEnabled())
                window.sliders[variant.name].setValue(100)
                window.recalculate()
                self.assertEqual(len(window.choices), 2)
                self.assertTrue(window.export_button.isEnabled())
                output = root / "mixed.vox"
                with patch("vox_mixer.app.QFileDialog.getSaveFileName", return_value=(str(output), "")), \
                     patch("vox_mixer.app.QMessageBox.information"):
                    window.export_dialog()
                self.assertTrue(output.exists())
                saved = load_project(project_path(output), VoxScene.load(output))
                self.assertEqual(saved["original_folder"], str(original))
                self.assertEqual(saved["variant_folders"], [str(variant)])
                self.assertEqual(saved["weights"], {variant.name: 0})
                window.link_table.selectRow(0)
                self.assertEqual(window.viewport.selected_link, (0, "Structure_Walls"))
                window.sliders[variant.name].setValue(0)
                self.assertFalse(window.export_button.isEnabled())

                viewport = window.viewport
                viewport.invert_horizontal = True
                viewport.invert_vertical = True
                start_yaw, start_pitch = viewport.yaw, viewport.pitch
                viewport.mousePressEvent(QMouseEvent(QEvent.Type.MouseButtonPress, QPointF(0, 0),
                                                    QPointF(0, 0), QPointF(0, 0),
                                                    Qt.MouseButton.MiddleButton, Qt.MouseButton.MiddleButton,
                                                    Qt.KeyboardModifier.NoModifier))
                viewport.mouseMoveEvent(QMouseEvent(QEvent.Type.MouseMove, QPointF(20, 20),
                                                   QPointF(20, 20), QPointF(20, 20),
                                                   Qt.MouseButton.NoButton, Qt.MouseButton.MiddleButton,
                                                   Qt.KeyboardModifier.NoModifier))
                self.assertEqual((viewport.yaw, viewport.pitch), (start_yaw, start_pitch))
                viewport.mousePressEvent(QMouseEvent(QEvent.Type.MouseButtonPress, QPointF(0, 0),
                                                    QPointF(0, 0), QPointF(0, 0),
                                                    Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
                                                    Qt.KeyboardModifier.NoModifier))
                viewport.mouseMoveEvent(QMouseEvent(QEvent.Type.MouseMove, QPointF(20, 20),
                                                   QPointF(20, 20), QPointF(20, 20),
                                                   Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton,
                                                   Qt.KeyboardModifier.NoModifier))
                self.assertLess(viewport.yaw, start_yaw)
                self.assertLess(viewport.pitch, start_pitch)
            finally:
                window.close()
