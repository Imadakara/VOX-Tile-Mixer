from __future__ import annotations

from collections import Counter
from pathlib import Path
import sys

from PySide6.QtCore import Qt, QSettings
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
    QGroupBox, QHBoxLayout, QLabel, QMainWindow, QMessageBox, QPushButton,
    QScrollArea, QSlider, QSpinBox, QSplitter, QTableWidget, QTableWidgetItem, QToolButton,
    QVBoxLayout, QWidget,
)

from .core import (
    Link, VoxScene, assign_link, build_links, choose_variants, export_scene,
    load_project, load_tiles, project_path, save_project,
)
from .viewport import VoxViewport


STYLE = """
QWidget { background: #171d28; color: #e7edf7; font: 10pt 'Segoe UI'; }
QGroupBox { border: 1px solid #354154; border-radius: 8px; margin-top: 12px; padding: 10px; font-weight: 600; }
QGroupBox::title { subcontrol-origin: margin; left: 12px; padding: 0 5px; }
QPushButton { background: #28364b; border: 1px solid #40516a; border-radius: 6px; padding: 7px 11px; }
QPushButton:hover { background: #344861; }
QPushButton:disabled { background: #222b38; color: #788395; }
QPushButton#primary { background: #3975b8; border-color: #5796d7; font-weight: 600; }
QTableWidget, QScrollArea, QComboBox, QSpinBox { background: #202a39; border: 1px solid #354154; }
QTableWidget { alternate-background-color: #263449; }
QTableWidget::item { color: #e7edf7; }
QHeaderView::section { background: #28364b; padding: 5px; border: 0; }
QTableWidget::item:selected { background: #365777; }
QSlider::groove:horizontal { height: 6px; background: #38475d; border-radius: 3px; }
QSlider::handle:horizontal { background: #77b8f8; width: 14px; margin: -5px 0; border-radius: 7px; }
"""


class LinkDialog(QDialog):
    def __init__(self, link: Link, tiles, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Link model #{link.model_id}")
        self.setMinimumWidth(440)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(f"Model #{link.model_id} on {link.layer}. Geometry candidates: {', '.join(link.candidates) or 'none'}"))
        form = QFormLayout()
        self.family = QComboBox()
        self.family.addItem("Keep unchanged", None)
        for tile in tiles:
            self.family.addItem(tile.key, tile.key)
        index = self.family.findData(link.reference_key)
        self.family.setCurrentIndex(max(0, index))
        self.rotation = QComboBox()
        for turns in range(4):
            self.rotation.addItem(f"{turns * 90}°", turns)
        self.rotation.setCurrentIndex(link.rotation)
        form.addRow("Replacement tile", self.family)
        form.addRow("Rotation", self.rotation)
        layout.addLayout(form)
        layout.addWidget(QLabel("Manual choices are checked against the model canvas when mixing."))
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


class MixerWindow(QMainWindow):
    MAX_VARIANT_FOLDERS = 5

    def __init__(self):
        super().__init__()
        self.setWindowTitle("VOX Tile Mixer")
        self.resize(1430, 900)
        self.scene: VoxScene | None = None
        self.original_folder: Path | None = None
        self.variant_folders: list[Path] = []
        self.original_tiles = []
        self.variant_tiles = []
        self.links: list[Link] = []
        self.choices = {}
        self.fitted = {}
        self.sliders = {}
        self.mix_applied = False
        self.settings = QSettings("RC-survival", "VOX Tile Mixer")
        settings_menu = self.menuBar().addMenu("Settings")
        horizontal = settings_menu.addAction("Invert horizontal drag")
        horizontal.setCheckable(True)
        horizontal.setChecked(self.settings.value("invert_horizontal", True, type=bool))
        horizontal.toggled.connect(lambda value: self.set_axis_setting("horizontal", value))
        vertical = settings_menu.addAction("Invert vertical drag")
        vertical.setCheckable(True)
        vertical.setChecked(self.settings.value("invert_vertical", True, type=bool))
        vertical.toggled.connect(lambda value: self.set_axis_setting("vertical", value))

        left = QSplitter(Qt.Orientation.Vertical)
        preview_box = QGroupBox("Model preview")
        preview_layout = QVBoxLayout(preview_box)
        actions = QHBoxLayout()
        self.open_button = QPushButton("＋  Open VOX model")
        self.open_button.clicked.connect(self.open_dialog)
        actions.addWidget(self.open_button)
        self.model_label = QLabel("Choose a native .vox scene")
        actions.addWidget(self.model_label, 1)
        preview_layout.addLayout(actions)
        self.viewport = VoxViewport()
        self.viewport.invert_horizontal = horizontal.isChecked()
        self.viewport.invert_vertical = vertical.isChecked()
        preview_layout.addWidget(self.viewport, 1)
        layer_row = QHBoxLayout()
        self.layer_toggle = QToolButton()
        self.layer_toggle.setText("Layers ▸")
        self.layer_toggle.setCheckable(True)
        self.layer_toggle.toggled.connect(self.toggle_layers)
        layer_row.addWidget(self.layer_toggle)
        layer_row.addStretch()
        self.highlight_label = QLabel("")
        layer_row.addWidget(self.highlight_label)
        preview_layout.addLayout(layer_row)
        self.layer_panel = QScrollArea()
        self.layer_panel.setWidgetResizable(True)
        self.layer_panel.setMaximumHeight(150)
        self.layer_panel.setVisible(False)
        self.layer_content = QWidget()
        self.layer_list = QVBoxLayout(self.layer_content)
        self.layer_list.addStretch()
        self.layer_panel.setWidget(self.layer_content)
        preview_layout.addWidget(self.layer_panel)
        help_label = QLabel("Left drag: rotate  •  wheel: zoom  •  Settings: invert drag axes")
        help_label.setStyleSheet("color:#a6b5c8")
        preview_layout.addWidget(help_label)
        left.addWidget(preview_box)

        links_box = QGroupBox("Model → tile links")
        links_layout = QVBoxLayout(links_box)
        self.link_summary = QLabel("Open a model and add a tile folder to start.")
        links_layout.addWidget(self.link_summary)
        self.link_table = QTableWidget(0, 6)
        self.link_table.setHorizontalHeaderLabels(["Model", "Layer", "Uses", "Canvas", "Linked tile / status", "Change"])
        self.link_table.horizontalHeader().setStretchLastSection(True)
        self.link_table.setAlternatingRowColors(True)
        self.link_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.link_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.link_table.itemSelectionChanged.connect(self.highlight_selected_link)
        links_layout.addWidget(self.link_table)
        left.addWidget(links_box)
        left.setSizes([570, 280])

        right = QSplitter(Qt.Orientation.Vertical)
        library_box = QGroupBox("Tile libraries")
        library_layout = QVBoxLayout(library_box)
        self.add_original_button = QPushButton("＋  Choose original tile set")
        self.add_original_button.clicked.connect(lambda: self.add_folder_dialog("original"))
        library_layout.addWidget(self.add_original_button)
        self.original_label = QLabel("No original set selected")
        self.original_label.setWordWrap(True)
        library_layout.addWidget(self.original_label)
        self.add_variant_button = QPushButton("＋  Add mix-in set")
        self.add_variant_button.clicked.connect(lambda: self.add_folder_dialog("variant"))
        library_layout.addWidget(self.add_variant_button)
        self.folder_label = QLabel("Add up to five alternative tile sets.")
        self.folder_label.setWordWrap(True)
        library_layout.addWidget(self.folder_label)
        self.folder_area = QScrollArea()
        self.folder_area.setWidgetResizable(True)
        self.folder_content = QWidget()
        self.folder_list = QVBoxLayout(self.folder_content)
        self.folder_list.addStretch()
        self.folder_area.setWidget(self.folder_content)
        library_layout.addWidget(self.folder_area, 1)
        self.link_button = QPushButton("Link tiles")
        self.link_button.setObjectName("primary")
        self.link_button.setEnabled(False)
        self.link_button.clicked.connect(self.link_tiles)
        library_layout.addWidget(self.link_button)
        right.addWidget(library_box)

        mix_box = QGroupBox("Mix variants")
        mix_layout = QVBoxLayout(mix_box)
        mix_layout.addWidget(QLabel("Set each mix-in from 0 to 100%. The remaining share stays original."))
        self.slider_area = QScrollArea()
        self.slider_area.setWidgetResizable(True)
        self.slider_content = QWidget()
        self.slider_layout = QVBoxLayout(self.slider_content)
        self.slider_layout.addStretch()
        self.slider_area.setWidget(self.slider_content)
        mix_layout.addWidget(self.slider_area, 1)
        seed_row = QHBoxLayout()
        seed_row.addWidget(QLabel("Seed"))
        self.seed = QSpinBox()
        self.seed.setRange(0, 2_147_483_647)
        self.seed.setValue(42)
        self.seed.valueChanged.connect(lambda _value: self.mark_mix_dirty())
        seed_row.addWidget(self.seed)
        reroll = QPushButton("New seed")
        reroll.clicked.connect(lambda: self.seed.setValue(self.seed.value() + 1))
        seed_row.addWidget(reroll)
        mix_layout.addLayout(seed_row)
        self.share_label = QLabel("Original: 100%")
        self.share_label.setWordWrap(True)
        mix_layout.addWidget(self.share_label)
        self.mix_button = QPushButton("Mix")
        self.mix_button.setObjectName("primary")
        self.mix_button.setEnabled(False)
        self.mix_button.clicked.connect(self.recalculate)
        mix_layout.addWidget(self.mix_button)
        self.status_label = QLabel("No mix yet")
        self.status_label.setWordWrap(True)
        mix_layout.addWidget(self.status_label)
        self.export_button = QPushButton("Export mixed VOX…")
        self.export_button.setObjectName("primary")
        self.export_button.setEnabled(False)
        self.export_button.clicked.connect(self.export_dialog)
        mix_layout.addWidget(self.export_button)
        right.addWidget(mix_box)
        right.setSizes([460, 380])

        workspace = QSplitter(Qt.Orientation.Horizontal)
        workspace.addWidget(left)
        workspace.addWidget(right)
        workspace.setStretchFactor(0, 3)
        workspace.setStretchFactor(1, 2)
        self.setCentralWidget(workspace)
        self.statusBar().showMessage("Ready")

    def error(self, title, exc):
        QMessageBox.critical(self, title, str(exc))

    def set_axis_setting(self, axis, value):
        setattr(self.viewport, f"invert_{axis}", value)
        self.settings.setValue(f"invert_{axis}", value)

    def toggle_layers(self, expanded):
        self.layer_panel.setVisible(expanded)
        self.layer_toggle.setText("Layers ▾" if expanded else "Layers ▸")

    def refresh_layers(self):
        while self.layer_list.count() > 1:
            item = self.layer_list.takeAt(0)
            if item.widget():
                item.widget().hide()
                item.widget().deleteLater()
        if not self.scene:
            return
        all_layers = sorted({instance.layer for instance in self.scene.instances})
        for layer in all_layers:
            checkbox = QCheckBox(layer)
            checkbox.setChecked(True)
            checkbox.toggled.connect(lambda checked, name=layer: self.viewport.set_layer_visible(name, checked))
            self.layer_list.insertWidget(self.layer_list.count() - 1, checkbox)

    def highlight_selected_link(self):
        row = self.link_table.currentRow()
        if 0 <= row < len(self.links):
            link = self.links[row]
            count = sum(instance.model_id == link.model_id and instance.layer == link.layer
                        for instance in self.scene.instances)
            self.viewport.highlight_link(link.model_id, link.layer)
            self.highlight_label.setText(f"Outlined: {count} tile(s) on {link.layer}")
        else:
            self.viewport.highlight_link(None)
            self.highlight_label.setText("")

    @property
    def tiles(self):
        return self.original_tiles + self.variant_tiles

    def open_dialog(self):
        path, _ = QFileDialog.getOpenFileName(self, "Open MagicaVoxel scene", "", "VOX files (*.vox)")
        if path:
            self.open_scene(Path(path))

    def open_scene(self, path: Path):
        try:
            scene = VoxScene.load(path)
        except Exception as exc:
            self.error("Cannot open VOX scene", exc)
            return
        self.scene = scene
        self.viewport.set_scene(scene)
        self.refresh_layers()
        self.model_label.setText(f"{path.name}  •  {len(scene.instances)} objects")
        self.model_label.setToolTip(str(path))
        self.links = []
        self.choices = {}
        self.fitted = {}
        self.mix_applied = False
        self.refresh_link_table()
        saved = project_path(scene.path)
        if saved.exists():
            try:
                profile = load_project(saved, scene)
                self.original_folder = None
                self.variant_folders = []
                self.original_tiles = []
                self.variant_tiles = []
                self.add_folder(Path(profile["original_folder"]), "original", refresh=False)
                for folder in profile["variant_folders"]:
                    self.add_folder(Path(folder), "variant", refresh=False)
                self.link_tiles()
                saved_links = {(link.model_id, link.layer): link for link in self.links}
                for key, value in profile.get("links", {}).items():
                    number, layer = key.split("|", 1)
                    link = saved_links[(int(number), layer)]
                    if value["family"] is not None:
                        assign_link(scene, link, value.get("reference_key") or value["family"], self.tiles)
                        link.rotation = int(value["rotation"])
                    else:
                        link.family = None
                        link.status = "unmatched"
                self.seed.setValue(profile.get("seed", 42))
                self.refresh_sliders(profile.get("weights", {}))
                self.refresh_link_table()
                self.mark_mix_dirty()
                self.statusBar().showMessage(f"Loaded links from {saved.name}", 8000)
            except Exception as exc:
                self.error("Cannot load saved links", exc)
        self.update_buttons()

    def add_folder_dialog(self, role):
        if role == "variant" and len(self.variant_folders) >= self.MAX_VARIANT_FOLDERS:
            return
        folder = QFileDialog.getExistingDirectory(self, "Choose original tile set" if role == "original"
                                                  else "Choose a mix-in tile set")
        if folder:
            try:
                self.add_folder(Path(folder), role)
            except Exception as exc:
                self.error("Cannot load tile folder", exc)

    def add_folder(self, path: Path, role: str, refresh=True):
        path = path.resolve()
        if path == self.original_folder or path in self.variant_folders:
            return
        if any(path.name == folder.name for folder in ([self.original_folder] if self.original_folder else []) +
               self.variant_folders):
            raise ValueError("Tile folders must have distinct names")
        if role == "variant" and len(self.variant_folders) >= self.MAX_VARIANT_FOLDERS:
            raise ValueError("The limit is five mix-in sets")
        tiles = load_tiles(path)
        if role == "original":
            self.original_folder = path
            self.original_tiles = tiles
        else:
            self.variant_folders.append(path)
            self.variant_tiles.extend(tiles)
        self.refresh_folders()
        self.refresh_sliders()
        if refresh:
            if role == "original":
                self.links = []
                self.refresh_link_table()
            self.mark_mix_dirty(clear_preview=True)
        self.update_buttons()

    def remove_folder(self, path):
        if path == self.original_folder:
            self.original_folder = None
            self.original_tiles = []
            self.links = []
        else:
            self.variant_folders.remove(path)
            self.variant_tiles = [tile for tile in self.variant_tiles if tile.path.parent != path]
        self.refresh_folders()
        self.refresh_sliders()
        self.refresh_link_table()
        self.mark_mix_dirty(clear_preview=True)
        self.update_buttons()

    def refresh_folders(self):
        while self.folder_list.count() > 1:
            item = self.folder_list.takeAt(0)
            if item.widget():
                item.widget().hide()
                item.widget().deleteLater()
        for folder in ([self.original_folder] if self.original_folder else []) + self.variant_folders:
            row = QWidget()
            row.setMinimumHeight(80)
            column = QVBoxLayout(row)
            line = QHBoxLayout()
            names = [tile for tile in self.tiles if tile.path.parent == folder]
            role = "Original" if folder == self.original_folder else "Mix-in"
            label = QLabel(f"{role}: {folder.name}  •  {len(names)} tiles")
            label.setToolTip(str(folder))
            line.addWidget(label, 1)
            remove = QPushButton("Remove")
            remove.setFixedWidth(72)
            remove.setStyleSheet("QPushButton { color:#ffcad0; background:#422733; border:1px solid #764353; "
                                 "border-radius:5px; padding:3px; }")
            remove.clicked.connect(lambda _checked=False, p=folder: self.remove_folder(p))
            line.addWidget(remove)
            column.addLayout(line)
            toggle = QToolButton()
            toggle.setText("Show tiles ▸")
            toggle.setCheckable(True)
            toggle.setStyleSheet("border:0;color:#9db7d5;text-align:left;padding:3px")
            details = QLabel("\n".join(tile.path.name for tile in names))
            details.setStyleSheet("color:#aebcd0;padding-left:12px")
            details.hide()
            toggle.toggled.connect(lambda opened, panel=details, button=toggle:
                                   (panel.setVisible(opened), button.setText("Hide tiles ▾" if opened else "Show tiles ▸")))
            column.addWidget(toggle)
            column.addWidget(details)
            self.folder_list.insertWidget(self.folder_list.count() - 1, row)
        self.add_variant_button.setEnabled(len(self.variant_folders) < self.MAX_VARIANT_FOLDERS)
        self.original_label.setText(f"Original: {self.original_folder.name}" if self.original_folder else
                                    "No original set selected")
        self.folder_label.setText(f"{len(self.variant_folders)} of {self.MAX_VARIANT_FOLDERS} mix-in sets loaded")

    def refresh_sliders(self, values=None):
        if values is None:
            values = {name: slider.value() for name, slider in self.sliders.items()}
        while self.slider_layout.count() > 1:
            item = self.slider_layout.takeAt(0)
            if item.widget():
                item.widget().hide()
                item.widget().deleteLater()
        self.sliders = {}
        for name in [p.name for p in self.variant_folders]:
            row = QWidget()
            line = QHBoxLayout(row)
            title = QLabel(name)
            title.setMinimumWidth(116)
            slider = QSlider(Qt.Orientation.Horizontal)
            slider.setRange(0, 100)
            slider.setValue(int(values.get(name, 0)))
            amount = QLabel(f"{slider.value()}%")
            amount.setFixedWidth(40)
            slider.valueChanged.connect(lambda value, out=amount: (out.setText(f"{value}%"), self.mark_mix_dirty()))
            line.addWidget(title)
            line.addWidget(slider, 1)
            line.addWidget(amount)
            self.slider_layout.insertWidget(self.slider_layout.count() - 1, row)
            self.sliders[name] = slider
        self.update_share_label()

    def update_share_label(self):
        weights = {name: slider.value() for name, slider in self.sliders.items()}
        total = sum(weights.values())
        original = max(0, 100 - total)
        if total > 100:
            shares = "  •  ".join(f"{name}: {100 * value / total:.0f}%" for name, value in weights.items())
            self.share_label.setText(f"Effective mix: Original: 0%  •  {shares} (normalized)")
        else:
            self.share_label.setText(f"Original: {original}%")

    def mark_mix_dirty(self, clear_preview=False):
        self.mix_applied = False
        if clear_preview:
            self.choices = {}
            self.fitted = {}
            self.viewport.set_mix({}, {})
        self.update_share_label()
        self.status_label.setText("Settings changed. Press Mix to update the preview.")
        self.update_buttons()

    def update_buttons(self):
        self.link_button.setEnabled(self.scene is not None and self.original_folder is not None)
        self.mix_button.setEnabled(self.scene is not None and bool(self.links))
        self.export_button.setEnabled(self.scene is not None and self.mix_applied)

    def link_tiles(self):
        if not self.scene or not self.original_tiles:
            return
        self.links = build_links(self.scene, self.original_tiles)
        self.refresh_link_table()
        self.mark_mix_dirty(clear_preview=True)
        self.update_buttons()

    def refresh_link_table(self):
        selected = self.link_table.currentRow()
        selected_key = ((self.links[selected].model_id, self.links[selected].layer)
                        if 0 <= selected < len(self.links) else None)
        self.link_table.blockSignals(True)
        self.link_table.setRowCount(0)
        if not self.scene or not self.links:
            self.link_summary.setText("Press Link tiles to identify models. Ambiguous shapes need a manual choice.")
            self.link_table.blockSignals(False)
            self.highlight_selected_link()
            return
        counts = Counter((instance.model_id, instance.layer) for instance in self.scene.instances)
        self.link_table.setRowCount(len(self.links))
        for row, link in enumerate(self.links):
            model = self.scene.models[link.model_id]
            for column, value in enumerate((str(link.model_id), link.layer, str(counts[(link.model_id, link.layer)]),
                                            "×".join(map(str, model.size)))):
                cell = QTableWidgetItem(value)
                cell.setFlags(cell.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self.link_table.setItem(row, column, cell)
            status_cell = QWidget()
            status_cell.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            line = QHBoxLayout(status_cell)
            line.setContentsMargins(4, 2, 4, 2)
            name = QLabel(link.reference_key or "—")
            name.setToolTip(link.reference_key or "No tile linked")
            line.addWidget(name, 1)
            plaque = QLabel(link.status)
            palette = {"unmatched": ("#5d2029", "#ffb8c1"),
                       "ambiguous": ("#644e14", "#ffe29b"),
                       "linked": ("#164a37", "#b0f2c9"),
                       "manual": ("#164a37", "#b0f2c9")}
            background, foreground = palette[link.status]
            plaque.setStyleSheet(f"background:{background};color:{foreground};border-radius:5px;padding:3px 7px;font-weight:600")
            line.addWidget(plaque)
            self.link_table.setCellWidget(row, 4, status_cell)
            change = QPushButton("Link other…")
            change.clicked.connect(lambda _checked=False, i=row: self.change_link(i))
            self.link_table.setCellWidget(row, 5, change)
        self.link_table.resizeColumnsToContents()
        self.link_table.blockSignals(False)
        if selected_key:
            for row, link in enumerate(self.links):
                if (link.model_id, link.layer) == selected_key:
                    self.link_table.selectRow(row)
                    break
        self.highlight_selected_link()
        status = Counter(link.status for link in self.links)
        self.link_summary.setText(f"{status['linked']} linked  •  {status['ambiguous']} ambiguous  •  "
                                  f"{status['unmatched']} unmatched  •  {status['manual']} manual")

    def change_link(self, index):
        self.link_table.selectRow(index)
        link = self.links[index]
        dialog = LinkDialog(link, self.tiles, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        family = dialog.family.currentData()
        if family is None:
            link.family = None
            link.reference_key = None
            link.status = "unmatched"
        else:
            assign_link(self.scene, link, family, self.tiles)
            link.rotation = dialog.rotation.currentData()
        self.refresh_link_table()
        self.mark_mix_dirty()

    def recalculate(self):
        if not self.scene or not self.links:
            return
        weights = {name: slider.value() for name, slider in self.sliders.items()}
        self.choices, self.fitted, issues = choose_variants(self.scene, self.links, self.variant_tiles,
                                                            weights, self.seed.value())
        self.viewport.set_mix(self.choices, self.fitted)
        self.mix_applied = True
        self.update_buttons()
        message = f"{len(self.choices)} of {len(self.scene.instances)} objects replaced"
        if issues:
            message += f"  •  {len(issues)} incompatible variant(s) skipped"
            self.status_label.setToolTip("\n".join(issues))
        else:
            self.status_label.setToolTip("")
        self.status_label.setText(message)

    def export_dialog(self):
        if not self.scene or not self.mix_applied:
            return
        default = self.scene.path.with_name(self.scene.path.stem + "_mixed.vox")
        name, _ = QFileDialog.getSaveFileName(self, "Export mixed scene", str(default), "VOX files (*.vox)")
        if not name:
            return
        output = Path(name).resolve()
        if output == self.scene.path:
            self.error("Choose another filename", "The source VOX is preserved. Export to a new file.")
            return
        try:
            export_scene(self.scene, output, self.choices, self.fitted)
            exported = VoxScene.load(output)
            exported_links = build_links(exported, self.original_tiles)
            source_links = {(link.model_id, link.layer): link for link in self.links}
            original_instances = {instance.shape_id: instance for instance in self.scene.instances}
            for link in exported_links:
                if link.model_id < len(self.scene.models):
                    source = source_links.get((link.model_id, link.layer))
                else:
                    exemplar = next((instance for instance in exported.instances
                                     if instance.model_id == link.model_id and instance.layer == link.layer), None)
                    old = original_instances[exemplar.shape_id] if exemplar else None
                    source = source_links.get((old.model_id, old.layer)) if old else None
                if source:
                    link.family = source.family
                    link.rotation = source.rotation
                    link.reference_key = source.reference_key
            weights = {name: slider.value() for name, slider in self.sliders.items()}
            # The exported scene already contains the mix. Reopening it starts
            # from exactly those pixels instead of applying the recipe twice.
            exported_weights = {name: 0 for name in weights}
            save_project(project_path(output), exported, self.original_folder, self.variant_folders,
                         exported_links, exported_weights, self.seed.value())
            # Also preserve links for the input scene for a later return to this source.
            save_project(project_path(self.scene.path), self.scene, self.original_folder, self.variant_folders,
                         self.links, weights, self.seed.value())
            self.statusBar().showMessage(f"Exported {output.name} and its link file", 10000)
            QMessageBox.information(self, "Export complete", f"Saved:\n{output}\n{project_path(output)}")
        except Exception as exc:
            self.error("Export failed", exc)


def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setStyleSheet(STYLE)
    window = MixerWindow()
    window.show()
    if len(sys.argv) > 1 and Path(sys.argv[1]).is_file():
        window.open_scene(Path(sys.argv[1]))
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
