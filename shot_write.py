# -*- coding: utf-8 -*-
"""Install with runpy.run_path(r'path/to/shot_write.py')['install']() in Nuke."""

import json
import math
import os
import re
import struct
import tempfile
import uuid
from functools import partial
from pathlib import Path

import nuke


_SCRIPT_PATH = r"[string map {\\ /} [value root.name]]"
_SCRIPT_DIR = f"[file dirname {_SCRIPT_PATH}]"
_SHOT_DIR = (
    "[expr {[string equal -nocase [file tail " + _SCRIPT_DIR + "] nuke] ? "
    "[file dirname " + _SCRIPT_DIR + "] : " + _SCRIPT_DIR + "}]"
)
_SHOT_NAME = f"[file tail {_SHOT_DIR}]"
_RENDER_JOIN = f"[file join {_SHOT_DIR} preRender "
_RING_SHORTCUT = "Shift+Alt+W"

_PROFILES = {
    "png": ("Write_REF_PNG", "rgb", "8 bit"),
    "exr": ("Write_COMP_EXR", "rgba", "16 bit half"),
    "dpx": ("Write_COMP_DPX", "rgb", "10 bit"),
    "mov": ("Write_REF_MOV", "rgb", None),
}

_COMMANDS = (
    ("PNG Reference", "png", "Ctrl+Alt+1"),
    ("EXR Precomp", "exr", "Ctrl+Alt+2"),
    ("DPX Precomp", "dpx", "Ctrl+Alt+3"),
    ("MOV H.264", "mov", "Ctrl+Alt+W"),
)
_NAMING_OPTIONS = ("toRunAI", "preRender_00x", "endComp", "endPaint", "toTrack")
_EXR_COMPRESSION = {
    0: "none", 1: "RLE", 2: "Zip (1 scanline)", 3: "Zip (16 scanline)",
    4: "PIZ Wavelet (32 scanlines)", 5: "PXR24", 6: "B44", 7: "B44A", 8: "DWAA", 9: "DWAB",
}
_FRAME_TOKEN = re.compile(r"#+|%0?\d*d")

_CLIPBOARD_TYPE = "application/x-nuke-shot-write"
_AFTER_RENDER = (
    "import nuke\n"
    "if nuke.GUI:\n"
    "    try:\n"
    "        import shot_write\n"
    "        shot_write.copy_render_path(nuke.thisNode())\n"
    "    except Exception as error:\n"
    "        nuke.tprint('Shot Write clipboard: ' + str(error))"
)


def _qt_modules():
    try:
        from PySide6 import QtCore, QtGui, QtWidgets
    except ImportError:
        from PySide2 import QtCore, QtGui, QtWidgets
    return QtCore, QtGui, QtWidgets


def _settings_file():
    return Path(os.environ.get("HOME") or Path.home()) / ".nuke" / "shot_write_settings.json"


def _format_preset(file_type, label=None):
    _, channels, datatype = _PROFILES[file_type]
    return {
        "id": uuid.uuid4().hex, "label": label or file_type.upper(), "enabled": True,
        "file_type": file_type, "channels": channels, "datatype": datatype or "",
        "compression": "Zip (1 scanline)" if file_type == "exr" else "",
        "codec": "h264" if file_type == "mov" else "", "output_transform": "",
        "match_plate": file_type in {"exr", "dpx"},
        "choose_frames": file_type != "mov", "default_mode": "sequence",
    }


def _default_settings():
    return {
        "version": 2,
        "formats": [_format_preset(ext) for ext in _PROFILES],
        "modes": [
            {"id": "frame", "label": "1 Frame", "enabled": True, "kind": "frame"},
            {"id": "sequence", "label": "Sequence", "enabled": True, "kind": "sequence"},
        ],
        "names": [
            {"id": uuid.uuid4().hex, "label": name, "suffix": name, "enabled": True}
            for name in _NAMING_OPTIONS
        ],
    }


def _validate_naming_options(options):
    if not isinstance(options, (list, tuple)) or not options:
        raise ValueError("Keep at least one naming suffix.")
    names = []
    for option in options:
        if not isinstance(option, str):
            raise ValueError("Each suffix must be text.")
        name = option.strip()
        if not name or not all(char.isalnum() or char in "_-" for char in name):
            raise ValueError("Use letters, numbers, underscores or hyphens in each suffix.")
        names.append(name)
    if len({name.casefold() for name in names}) != len(names):
        raise ValueError("Each suffix must have a different name.")
    return tuple(names)


def _validate_settings(settings):
    if not isinstance(settings, dict):
        raise ValueError("Invalid Action Ring settings.")
    defaults = _default_settings()
    if "formats" not in settings and "naming_options" in settings:
        names = _validate_naming_options(settings["naming_options"])
        defaults["names"] = [
            {"id": uuid.uuid4().hex, "label": name, "suffix": name, "enabled": True}
            for name in names
        ]
        return defaults

    clean = {"version": 2}
    datatypes = {
        "png": {"8 bit", "16 bit"}, "exr": {"16 bit half", "32 bit float"},
        "dpx": {"8 bit", "10 bit", "12 bit", "16 bit"}, "mov": {""},
    }
    for level in ("formats", "modes", "names"):
        entries = settings.get(level, defaults[level])
        if not isinstance(entries, list) or not entries:
            raise ValueError(f"Keep at least one item in {level}.")
        labels, ids, result = set(), set(), []
        for raw in entries:
            if not isinstance(raw, dict):
                raise ValueError(f"Invalid item in {level}.")
            label = raw.get("label", "")
            if not isinstance(label, str) or not label.strip():
                raise ValueError(f"Every item in {level} needs a display name.")
            label = label.strip()
            if label.casefold() in labels:
                raise ValueError(f"Display names in {level} must be different.")
            labels.add(label.casefold())
            entry_id = raw.get("id") or uuid.uuid4().hex
            if not isinstance(entry_id, str) or entry_id in ids:
                raise ValueError(f"Duplicate or invalid item ID in {level}.")
            ids.add(entry_id)
            enabled = raw.get("enabled", True)
            if not isinstance(enabled, bool):
                raise ValueError("Enabled must be true or false.")
            entry = {"id": entry_id, "label": label, "enabled": enabled}
            if level == "names":
                entry["suffix"] = _validate_naming_options([raw.get("suffix", "")])[0]
            elif level == "modes":
                if raw.get("kind") not in {"frame", "sequence"}:
                    raise ValueError("Render mode must be frame or sequence.")
                entry["kind"] = raw["kind"]
            else:
                ext = raw.get("file_type")
                if ext not in _PROFILES:
                    raise ValueError("Choose PNG, EXR, DPX or MOV as the file type.")
                preset = _format_preset(ext)
                preset.update(raw)
                for key in ("datatype", "compression", "codec", "output_transform", "channels"):
                    if not isinstance(preset[key], str):
                        raise ValueError(f"Invalid {key} in {label}.")
                    preset[key] = preset[key].strip()
                if preset["datatype"] not in datatypes[ext]:
                    raise ValueError(f"Choose a valid datatype for {label}.")
                if preset["channels"] not in {"rgb", "rgba", "all"}:
                    raise ValueError(f"Choose RGB, RGBA or all channels for {label}.")
                if ext == "exr" and not preset["compression"]:
                    raise ValueError(f"Choose EXR compression for {label}.")
                if ext == "mov" and not preset["codec"]:
                    raise ValueError(f"Choose a MOV codec for {label}.")
                for key in ("match_plate", "choose_frames"):
                    if not isinstance(preset[key], bool):
                        raise ValueError(f"Invalid {key} in {label}.")
                if preset["default_mode"] not in {"frame", "sequence"}:
                    raise ValueError(f"Choose a default render mode for {label}.")
                entry = {key: preset[key] for key in _format_preset(ext)}
                entry.update(id=entry_id, label=label, enabled=enabled)
            result.append(entry)
        if not any(item["enabled"] for item in result):
            raise ValueError(f"Enable at least one item in {level}.")
        clean[level] = result
    _validate_naming_options([item["suffix"] for item in clean["names"]])
    return clean


def _load_settings():
    try:
        return _validate_settings(json.loads(_settings_file().read_text(encoding="utf-8")))
    except FileNotFoundError:
        return _default_settings()
    except (OSError, ValueError, TypeError, KeyError) as error:
        nuke.tprint(f"Shot Write settings: {error}. Using default Action Ring settings.")
        return _default_settings()


def _save_settings(settings):
    clean = _validate_settings(settings)
    path = _settings_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(path, (json.dumps(clean, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))


def _load_naming_options():
    return tuple(item["suffix"] for item in _load_settings()["names"] if item["enabled"])


def _save_naming_options(options):
    names = _validate_naming_options(options)
    settings = _load_settings()
    settings["names"] = [
        {"id": uuid.uuid4().hex, "label": name, "suffix": name, "enabled": True} for name in names
    ]
    _save_settings(settings)


def _editor_widget(core, gui, widgets):
    qt = core.Qt

    class ActionRingEditor(widgets.QDialog):
        LEVELS = ("formats", "modes", "names")

        def __init__(self, parent=None, initial_level="formats"):
            super().__init__(parent)
            self.setWindowTitle("Shot Write — Edit Action Ring")
            self.resize(830, 530)
            self.loading = False
            self.entries = {}
            self.lists = {}
            layout = widgets.QVBoxLayout(self)
            body = widgets.QSplitter()
            layout.addWidget(body)
            self.levels = widgets.QListWidget()
            self.levels.addItems(("Formats", "Render modes", "Naming suffixes"))
            self.levels.setMaximumWidth(160)
            body.addWidget(self.levels)
            right = widgets.QWidget()
            right_layout = widgets.QVBoxLayout(right)
            body.addWidget(right)
            split = widgets.QSplitter()
            right_layout.addWidget(split)
            self.pages = widgets.QStackedWidget()
            for level in self.LEVELS:
                listing = widgets.QListWidget()
                listing.setDragDropMode(widgets.QAbstractItemView.InternalMove)
                listing.setDefaultDropAction(qt.MoveAction)
                listing.setAccessibleName(level)
                listing.currentItemChanged.connect(lambda current, previous: self.select_item())
                self.lists[level] = listing
                self.pages.addWidget(listing)
            split.addWidget(self.pages)
            properties = widgets.QWidget()
            form = widgets.QFormLayout(properties)
            self.label = widgets.QLineEdit()
            self.label.setAccessibleName("Display name")
            form.addRow("Display name", self.label)
            self.properties = widgets.QStackedWidget()
            form.addRow(self.properties)
            split.addWidget(properties)
            split.setStretchFactor(1, 1)

            format_page = widgets.QWidget()
            format_form = widgets.QFormLayout(format_page)
            self.file_type = widgets.QComboBox()
            self.file_type.addItems(tuple(_PROFILES))
            self.channels = widgets.QComboBox()
            self.channels.addItems(("rgb", "rgba", "all"))
            self.datatype = widgets.QComboBox()
            self.compression = widgets.QComboBox()
            self.compression.setEditable(True)
            self.compression.addItems(tuple(_EXR_COMPRESSION.values()))
            self.codec = widgets.QComboBox()
            self.codec.setEditable(True)
            self.codec.addItems(("h264", "appr"))
            self.output = widgets.QLineEdit()
            self.output.setPlaceholderText("Auto")
            self.match_plate = widgets.QCheckBox("Match Read_Plate")
            self.match_plate.setToolTip("Match datatype, EXR compression and output transform from Read_Plate.")
            self.choose_frames = widgets.QCheckBox("Ask for render mode")
            self.default_mode = widgets.QComboBox()
            self.default_mode.addItems(("sequence", "frame"))
            for label, control in (
                ("File type", self.file_type), ("Channels", self.channels), ("Datatype", self.datatype),
                ("Compression", self.compression), ("MOV codec", self.codec), ("Output transform", self.output),
                ("", self.match_plate), ("", self.choose_frames), ("Default mode", self.default_mode),
            ):
                format_form.addRow(label, control)
            self.properties.addWidget(format_page)

            mode_page = widgets.QWidget()
            mode_form = widgets.QFormLayout(mode_page)
            self.kind = widgets.QComboBox()
            self.kind.addItems(("frame", "sequence"))
            mode_form.addRow("Render behavior", self.kind)
            self.properties.addWidget(mode_page)

            name_page = widgets.QWidget()
            name_form = widgets.QFormLayout(name_page)
            self.suffix = widgets.QLineEdit()
            self.suffix.setAccessibleName("Filename suffix")
            name_form.addRow("Filename suffix", self.suffix)
            hint = widgets.QLabel("End with _00x to increment the output number.")
            hint.setWordWrap(True)
            name_form.addRow(hint)
            self.properties.addWidget(name_page)

            actions = widgets.QHBoxLayout()
            self.add = widgets.QPushButton("Add")
            self.remove = widgets.QPushButton("Remove")
            actions.addWidget(self.add)
            actions.addWidget(self.remove)
            actions.addStretch()
            right_layout.addLayout(actions)
            hint = widgets.QLabel("Check items to show them in the ring. Drag items to change their order.")
            hint.setWordWrap(True)
            right_layout.addWidget(hint)
            buttons = widgets.QDialogButtonBox(
                widgets.QDialogButtonBox.Save | widgets.QDialogButtonBox.Cancel | widgets.QDialogButtonBox.RestoreDefaults
            )
            layout.addWidget(buttons)
            self.levels.currentRowChanged.connect(self.change_level)
            self.label.textEdited.connect(self.change_values)
            self.suffix.textEdited.connect(self.change_values)
            self.output.textEdited.connect(self.change_values)
            for combo in (self.channels, self.datatype, self.compression, self.codec, self.default_mode, self.kind):
                combo.currentTextChanged.connect(self.change_values)
            self.match_plate.toggled.connect(self.change_values)
            self.choose_frames.toggled.connect(self.change_values)
            self.file_type.currentTextChanged.connect(self.change_file_type)
            self.add.clicked.connect(self.add_item)
            self.remove.clicked.connect(self.remove_item)
            buttons.accepted.connect(self.save)
            buttons.rejected.connect(self.reject)
            buttons.button(widgets.QDialogButtonBox.RestoreDefaults).clicked.connect(lambda: self.populate(_default_settings()))
            self.populate(_load_settings())
            self.levels.setCurrentRow(self.LEVELS.index(initial_level))

        def current_level(self):
            return self.LEVELS[max(0, self.levels.currentRow())]

        def populate(self, settings):
            self.loading = True
            self.entries.clear()
            for level in self.LEVELS:
                listing = self.lists[level]
                listing.clear()
                for entry in settings[level]:
                    self.append_item(level, dict(entry))
                listing.setCurrentRow(0)
            self.loading = False
            self.select_item()

        def append_item(self, level, entry):
            # The item stores a key so edits and drag order refer to the same Python data.
            key = uuid.uuid4().hex
            self.entries[key] = entry
            item = widgets.QListWidgetItem(entry["label"])
            item.setData(qt.UserRole, key)
            item.setFlags(item.flags() | qt.ItemIsUserCheckable | qt.ItemIsDragEnabled | qt.ItemIsDropEnabled)
            item.setCheckState(qt.Checked if entry["enabled"] else qt.Unchecked)
            self.lists[level].addItem(item)
            return item

        def change_level(self, index):
            self.pages.setCurrentIndex(index)
            self.properties.setCurrentIndex(index)
            self.select_item()

        def select_item(self):
            if self.loading:
                return
            level = self.current_level()
            listing = self.lists[level]
            item = listing.currentItem()
            self.remove.setEnabled(listing.count() > 1)
            if item is None:
                return
            entry = self.entries[item.data(qt.UserRole)]
            self.loading = True
            self.label.setText(entry["label"])
            if level == "formats":
                self.file_type.setCurrentText(entry["file_type"])
                self.channels.setCurrentText(entry["channels"])
                self.datatype.clear()
                choices = {
                    "png": ("8 bit", "16 bit"), "exr": ("16 bit half", "32 bit float"),
                    "dpx": ("8 bit", "10 bit", "12 bit", "16 bit"), "mov": ("",),
                }
                self.datatype.addItems(choices[entry["file_type"]])
                self.datatype.setCurrentText(entry["datatype"])
                self.compression.setCurrentText(entry["compression"])
                self.codec.setCurrentText(entry["codec"])
                self.output.setText(entry["output_transform"])
                self.match_plate.setChecked(entry["match_plate"])
                self.choose_frames.setChecked(entry["choose_frames"])
                self.default_mode.setCurrentText(entry["default_mode"])
                self.update_controls()
            elif level == "modes":
                self.kind.setCurrentText(entry["kind"])
            else:
                self.suffix.setText(entry["suffix"])
            self.loading = False

        def update_controls(self):
            ext = self.file_type.currentText()
            self.datatype.setEnabled(ext != "mov")
            self.compression.setEnabled(ext == "exr")
            self.codec.setEnabled(ext == "mov")
            self.match_plate.setEnabled(ext in {"exr", "dpx"})
            self.default_mode.setEnabled(not self.choose_frames.isChecked())

        def change_file_type(self, ext):
            if self.loading:
                return
            item = self.lists["formats"].currentItem()
            if item is None:
                return
            entry = self.entries[item.data(qt.UserRole)]
            defaults = _format_preset(ext)
            for key in ("file_type", "channels", "datatype", "compression", "codec", "match_plate", "choose_frames", "default_mode"):
                entry[key] = defaults[key]
            self.select_item()

        def change_values(self, *args):
            if self.loading:
                return
            level = self.current_level()
            item = self.lists[level].currentItem()
            if item is None:
                return
            entry = self.entries[item.data(qt.UserRole)]
            entry["label"] = self.label.text()
            item.setText(entry["label"])
            if level == "formats":
                entry.update(
                    channels=self.channels.currentText(), datatype=self.datatype.currentText(),
                    compression=self.compression.currentText(), codec=self.codec.currentText(),
                    output_transform=self.output.text(), match_plate=self.match_plate.isChecked(),
                    choose_frames=self.choose_frames.isChecked(), default_mode=self.default_mode.currentText(),
                )
                self.update_controls()
            elif level == "modes":
                entry["kind"] = self.kind.currentText()
            else:
                entry["suffix"] = self.suffix.text()

        def add_item(self):
            level = self.current_level()
            listing = self.lists[level]
            if level == "formats":
                entry = _format_preset("exr", "New EXR")
            elif level == "modes":
                entry = {"id": uuid.uuid4().hex, "label": "New mode", "kind": "sequence", "enabled": True}
            else:
                entry = {"id": uuid.uuid4().hex, "label": "New suffix", "suffix": "newOutput", "enabled": True}
            existing = {self.entries[listing.item(i).data(qt.UserRole)]["label"].casefold() for i in range(listing.count())}
            label = entry["label"]
            number = 2
            while entry["label"].casefold() in existing:
                entry["label"] = f"{label} {number}"
                number += 1
            if level == "names":
                suffixes = {value.get("suffix") for value in self.entries.values()}
                while entry["suffix"] in suffixes:
                    entry["suffix"] += "_new"
            item = self.append_item(level, entry)
            listing.setCurrentItem(item)
            self.label.setFocus()
            self.label.selectAll()

        def remove_item(self):
            listing = self.lists[self.current_level()]
            if listing.count() <= 1:
                return
            item = listing.currentItem()
            if item is not None:
                self.entries.pop(item.data(qt.UserRole))
                listing.takeItem(listing.row(item))
                self.select_item()

        def save(self):
            settings = {"version": 2}
            for level, listing in self.lists.items():
                settings[level] = []
                for index in range(listing.count()):
                    item = listing.item(index)
                    entry = dict(self.entries[item.data(qt.UserRole)])
                    entry["enabled"] = item.checkState() == qt.Checked
                    settings[level].append(entry)
            try:
                _save_settings(settings)
            except (OSError, ValueError) as error:
                widgets.QMessageBox.warning(self, "Shot Write", str(error))
                return
            self.accept()

    return ActionRingEditor


def edit_action_ring(initial_level="formats"):
    core, gui, widgets = _qt_modules()
    dialog = _editor_widget(core, gui, widgets)(widgets.QApplication.activeWindow(), initial_level)
    result = dialog.exec() if hasattr(dialog, "exec") else dialog.exec_()
    dialog.deleteLater()
    return result == widgets.QDialog.Accepted


def edit_naming_suffixes():
    return edit_action_ring("names")


def _numbered_prefix(naming):
    return naming[:-3] if naming.endswith("_00x") else None


def _output_colorspace(root, node):
    file_type = node["file_type"].value()
    if file_type == "exr":
        return root["workingSpaceLUT"].value() or "linear"
    if file_type == "dpx":
        lut = root.knob("logLut") or root.knob("logLUT")
        return lut.value() if lut else "default"

    viewer = nuke.activeViewer()
    process = viewer.node()["viewerProcess"].value() if viewer else ""
    if root["colorManagement"].value() == "Nuke":
        native = {"rec1886": "Gamma2.4"}.get(process, process)
        available = nuke.getColorspaceList(node["colorspace"])
        return native if native in available and native.casefold() not in {"none", "raw"} else "sRGB"

    monitor = root.knob("monitorLut") or root.knob("monitorLUT")
    for label in (process, monitor.value() if monitor else ""):
        match = re.fullmatch(r"(.+) \((.+)\)", label or "")
        if match:
            view, display = match.groups()
            try:
                colorspace = root.getOCIOColorspaceFromViewTransform(display, view)
            except (RuntimeError, ValueError):
                continue
            if colorspace and colorspace.casefold() not in {"raw", "none", "default"}:
                return colorspace

    available = nuke.getColorspaceList(node["colorspace"])
    fallback = next((name for name in ("Output - sRGB", "sRGB - Display", "sRGB") if name in available), "default")
    nuke.tprint(f"Shot Write: Viewer transform could not be mapped. {file_type.upper()} uses {fallback}.")
    return fallback


def _plate_read(root):
    plate = root.node("Read_Plate")
    return plate if plate is not None and plate.Class() == "Read" else None


def _sample_plate_file(read, filename):
    # With a Read outside the current timeline, try its original first frame as well.
    if not _FRAME_TOKEN.search(filename.name) and filename.is_file():
        return filename
    first = int(read["origfirst"].value())

    def substitute(token, frame):
        spec = token[0]
        width = len(spec) if spec.startswith("#") else int(spec[1:-1] or "0")
        return f"{frame:0{width}d}"

    for frame in dict.fromkeys((int(nuke.frame()), first)):
        name = _FRAME_TOKEN.sub(lambda token: substitute(token, frame), filename.name)
        for view in nuke.views():
            path = filename.with_name(name.replace("%V", view).replace("%v", view[0]))
            if path.is_file():
                return path
    return None


def _exr_header(stream):
    if stream.read(4) != b"\x76\x2f\x31\x01":
        raise ValueError("invalid OpenEXR header")
    version, = struct.unpack("<I", stream.read(4))
    attrs = {}

    def read_name():
        name = bytearray()
        while len(name) <= 255:
            byte = stream.read(1)
            if byte == b"\x00":
                return bytes(name)
            if not byte:
                break
            name.extend(byte)
        raise ValueError("invalid OpenEXR header name")

    # A Write has one datatype/compression choice. Use the first image part's settings.
    while name := read_name():
        read_name()  # Attribute type.
        size, = struct.unpack("<I", stream.read(4))
        if name in {b"channels", b"compression", b"chromaticities", b"oiio:ColorSpace", b"ocio:colorspace"} and size <= 1024 * 1024:
            attrs[name] = stream.read(size)
        else:
            stream.seek(size, os.SEEK_CUR)
    channels = attrs.get(b"channels", b"")
    types = set()
    position = 0
    while position < len(channels) and channels[position] != 0:
        end = channels.index(b"\x00", position)
        name = channels[position:end].rsplit(b".", 1)[-1].upper()
        if name in {b"R", b"G", b"B", b"A", b"Y", b"RY", b"BY"}:
            types.add(struct.unpack_from("<I", channels, end + 1)[0])
        position = end + 17
    datatype = "32 bit float" if 2 in types else ("16 bit half" if types == {1} else None)
    compression = _EXR_COMPRESSION.get((attrs.get(b"compression") or b"\xff")[0])
    color_tag = (attrs.get(b"ocio:colorspace") or attrs.get(b"oiio:ColorSpace", b"")).decode("utf-8", "replace").strip("\x00 ")
    return datatype, compression, color_tag, attrs.get(b"chromaticities"), bool(version & 0x1000)


def _dpx_header(stream):
    header = stream.read(804)
    if len(header) != 804 or header[:4] not in (b"SDPX", b"XPDS"):
        raise ValueError("invalid DPX header")
    bit_depth = header[803]
    datatype = f"{bit_depth} bit" if bit_depth in (8, 10, 12, 16) else None
    return datatype, header[801]


def _match_plate(root, node):
    file_type = node["file_type"].value()
    read = _plate_read(root)
    if read is None:
        return None
    # Inspect the full-resolution plate even when the project is in proxy mode.
    filename = Path(read["file"].getEvaluatedValue() or "")
    if filename.suffix.casefold() != f".{file_type}":
        return None

    declared = read["colorspace"].value() or ""
    declared = "" if read["raw"].value() or declared.casefold().startswith("default") else declared
    available = nuke.getColorspaceList(node["colorspace"])
    colorspace = declared if declared in available and declared.casefold() not in {"raw", "none"} else None
    settings = []
    try:
        path = _sample_plate_file(read, filename)
        if path is None:
            raise FileNotFoundError("no readable frame from this sequence")
        with path.open("rb") as stream:
            if file_type == "exr":
                datatype, compression, color_tag, chromaticities, multipart = _exr_header(stream)
                compression_knob = node["compression"]
                if compression in compression_knob.values():
                    compression_knob.setValue(compression)
                    settings.append(compression)
                elif compression:
                    nuke.tprint(f"Shot Write: {compression} is unavailable; using the Write's compression preset.")
                if multipart:
                    nuke.tprint("Shot Write: matched datatype and compression from the first EXR part.")
                if not colorspace and color_tag in available and color_tag.casefold() not in {"raw", "none", "default"}:
                    colorspace = color_tag
                if not colorspace and chromaticities and len(chromaticities) == 32:
                    primaries = struct.unpack("<8f", chromaticities)
                    ap0 = (0.7347, 0.2653, 0.0, 1.0, 0.0001, -0.077, 0.32168, 0.33767)
                    ap1 = (0.713, 0.293, 0.165, 0.830, 0.128, 0.044, 0.32168, 0.33767)
                    for expected, names in ((ap0, ("ACES - ACES2065-1", "ACES2065-1")), (ap1, ("ACES - ACEScg", "ACEScg"))):
                        if all(abs(a - b) < 0.0002 for a, b in zip(primaries, expected)):
                            colorspace = next((name for name in names if name in available), None)
                            break
            else:
                datatype, transfer = _dpx_header(stream)
                # Transfer codes describe the curve; use the Read's explicit transform first.
                if not colorspace and not read["raw"].value():
                    if transfer in (1, 3):
                        colorspace = "Cineon" if "Cineon" in available else None
                    elif transfer == 2 and root["colorManagement"].value() == "Nuke":
                        colorspace = "linear" if "linear" in available else None
                    elif transfer == 6:
                        colorspace = next((name for name in ("Utility - Rec.709 - Camera", "rec709", "Rec.709") if name in available), None)
        if datatype:
            node["datatype"].setValue(datatype)
            settings.append(datatype)
    except (OSError, ValueError, struct.error, IndexError) as error:
        nuke.tprint(f"Shot Write: could not inspect {read.fullName()}: {error}. Using format defaults.")

    settings.append(colorspace or "project output transform")
    nuke.tprint(f"Shot Write: {read.fullName()} → {', '.join(settings)}")
    return colorspace


def _configure_output(root, node, frame, preset=None):
    if preset is None:
        saved = node.knob("shot_write_preset")
        preset = json.loads(saved.value()) if saved is not None else _format_preset(node["file_type"].value())
    node["raw"].setValue(False)
    node["use_limit"].setValue(False)
    file_type = node["file_type"].value()
    colorspace = None
    if file_type in {"exr", "dpx"} and preset["match_plate"]:
        colorspace = _match_plate(root, node)
    colorspace = preset["output_transform"] or colorspace or _output_colorspace(root, node)
    if colorspace == "default":
        node["colorspace"].fromScript("default")
    else:
        node["colorspace"].setValue(colorspace)

    callback = (node["afterRender"].value() or "").rstrip()
    if _AFTER_RENDER not in callback:
        node["afterRender"].setValue(f"{callback}\n\n{_AFTER_RENDER}" if callback else _AFTER_RENDER)

    if frame is not None:
        node["first"].setValue(frame)
        node["last"].setValue(frame)
        if node.knob("shot_write_render_frame") is None:
            if node.knob("shot_write_settings") is None:
                node.addKnob(nuke.Tab_Knob("shot_write_settings", "Shot Write"))
            node.addKnob(nuke.PyScript_Knob(
                "shot_write_render_frame", f"Render Frame {frame}",
                "import nuke\nnode = nuke.thisNode()\nframe = int(node['first'].value())\n"
                "nuke.execute(node, frame, frame)",
            ))


def copy_render_path(node):
    """Copy the rendered Read from Write > Python > After Render."""
    path = nuke.filename(node)
    if not path:
        return
    path = path.replace("\\", "/")
    clip = {"path": path, "colorspace": node["colorspace"].value(), "raw": node["raw"].value()}
    if node.knob("shot_write_render_frame") is not None and node["file_type"].value() != "mov":
        clip["frame"] = int(node["first"].value())

    def copy():
        try:
            rendered = _rendered_sequence(path)
            if rendered is None:
                nuke.tprint(f"Shot Write clipboard: no rendered files found for {path}")
                return
            _copy_read_to_clipboard(clip, rendered)
        except Exception as error:
            nuke.tprint(f"Shot Write clipboard: {error}")

    nuke.executeInMainThread(copy)


def _rendered_sequence(path):
    directory, name = os.path.split(path)
    views = nuke.views() if "%V" in path or "%v" in path else ("",)
    first_file = None
    first = last = None
    for view in views:
        view_directory = directory.replace("%V", view).replace("%v", view[:1])
        view_name = name.replace("%V", view).replace("%v", view[:1])
        token = _FRAME_TOKEN.search(view_name)
        if token is None:
            filename = os.path.join(view_directory, view_name)
            if os.path.isfile(filename):
                return filename, None
            continue

        spec = token[0]
        width = len(spec) if spec.startswith("#") else int(spec[1:-1] or "0")
        prefix, suffix = view_name[:token.start()], view_name[token.end():]
        pattern = re.compile(
            re.escape(prefix) + r"(-?[0-9]+)" + re.escape(suffix),
            re.IGNORECASE if os.name == "nt" else 0,
        )
        try:
            with os.scandir(view_directory or ".") as entries:
                for entry in entries:
                    match = pattern.fullmatch(entry.name)
                    if not match:
                        continue
                    frame = int(match[1])
                    if f"{frame:0{width}d}" != match[1] or not entry.is_file():
                        continue
                    if first is None or frame < first:
                        first, first_file = frame, entry.path
                    last = frame if last is None else max(last, frame)
        except FileNotFoundError:
            continue

    return (first_file, (first, last)) if first_file is not None else None


def _load_render_read(node, clip, rendered):
    path = clip["path"].replace("\\", "/")
    sample, frames = rendered
    if frames is None and "frame" in clip:
        frames = (clip["frame"], clip["frame"])

    file = node["file"]
    # Read a concrete file so its display window and pixel aspect seed the native format.
    file.fromUserText(sample.replace("\\", "/"))
    format_knob = node["format"]
    image_format = format_knob.toScript(False)
    if frames is not None and _FRAME_TOKEN.search(os.path.basename(path)):
        first, last = frames
        file.fromUserText(f"{path} {first}-{last}")
    elif "%V" in path or "%v" in path:
        file.setValue(path)
    if frames is not None:
        first, last = frames
        for name, value in (("first", first), ("last", last), ("origfirst", first), ("origlast", last)):
            node[name].setValue(value)
        node["origset"].setValue(True)
    format_knob.fromScript(image_format)

    colorspace = clip.get("colorspace")
    if colorspace and not colorspace.casefold().startswith("default"):
        node["colorspace"].setValue(colorspace)
    node["raw"].setValue(clip.get("raw", False))


def _copy_read_to_clipboard(clip, rendered):
    root = nuke.root()
    was_modified = root.modified()
    undo = nuke.Undo()
    undo.disable()
    try:
        with root:
            selection = nuke.selectedNodes()
            read = None
            try:
                for node in selection:
                    node.setSelected(False)
                read = nuke.nodes.Read()
                _load_render_read(read, clip, rendered)
                read.setSelected(True)
                if not nuke.nodeCopy("%clipboard%"):
                    raise RuntimeError("Nuke could not copy the rendered Read node.")
            finally:
                if read is not None:
                    nuke.delete(read)
                for node in selection:
                    node.setSelected(True)
    finally:
        root.setModified(was_modified)
        undo.enable()

    core, _, widgets = _qt_modules()
    clipboard = widgets.QApplication.clipboard()
    native = clipboard.mimeData()
    # Keep Nuke's clipboard payload intact and attach metadata for Paste Render as Read.
    mime = core.QMimeData()
    for name in native.formats():
        mime.setData(name, native.data(name))
    mime.setData(_CLIPBOARD_TYPE, json.dumps(clip).encode("utf-8"))
    clipboard.setMimeData(mime)


def paste_render_as_read():
    _, _, widgets = _qt_modules()
    mime = widgets.QApplication.clipboard().mimeData()
    if mime is None or not mime.hasFormat(_CLIPBOARD_TYPE):
        nuke.message("Render a Shot Write to copy its Read before using Paste Render as Read.")
        return
    node = None
    try:
        clip = json.loads(bytes(mime.data(_CLIPBOARD_TYPE)))
        path = clip["path"].replace("\\", "/")
        rendered = _rendered_sequence(path)
        if rendered is None:
            nuke.message("Shot Write could not find the rendered files:\n" + path)
            return
        with nuke.lastHitGroup():
            node = nuke.nodes.Read()
            _load_render_read(node, clip, rendered)
            node.autoplace()
    except (OSError, RuntimeError, ValueError, TypeError, KeyError) as error:
        if node is not None:
            nuke.delete(node)
        nuke.message(f"Shot Write could not create the Read node:\n{error}")
        return
    return node


def _file_pattern(file_type, slot, multiview, frame=None, naming="toRunAI", still_slot=1):
    numbered = _numbered_prefix(naming)
    if numbered is not None:
        naming = f"{numbered}{slot:03d}"
        slot = 1
    suffix = f"_{slot:02d}" if slot > 1 else ""
    stem = f"{_SHOT_NAME}_{naming}{suffix}"
    if frame is not None:
        stem += f"_{frame:04d}_{still_slot:03d}"
    view = ".%V" if multiview else ""
    number = ".####" if frame is None and file_type != "mov" else ""
    return f"{_RENDER_JOIN}{file_type} {stem} {stem}]{view}{number}.{file_type}"


def _next_slot(file_type, naming, frame, shot_dir, used_paths):
    slot = 1
    numbered = _numbered_prefix(naming)
    if numbered is not None:
        # Count across formats and frame modes so an output number identifies one export.
        pattern = re.compile("_" + re.escape(numbered) + r"(\d+)(?=[_ .\]}]|$)", re.IGNORECASE)
        numbers = [int(m.group(1)) for path in used_paths if path.startswith(_RENDER_JOIN) for m in pattern.finditer(path)]
        for ext in _PROFILES:
            try:
                folders = (shot_dir / "preRender" / ext).iterdir()
                for folder in folders:
                    if folder.name.casefold().startswith(f"{shot_dir.name}_{numbered}".casefold()):
                        numbers.extend(int(m.group(1)) for m in pattern.finditer(folder.name))
            except FileNotFoundError:
                pass
        return max(numbers, default=0) + 1

    while frame is None and any(_file_pattern(file_type, slot, split, frame, naming) in used_paths for split in (False, True)):
        slot += 1
    return slot


def _next_still_slot(file_type, naming, slot, frame, shot_dir, used_paths):
    numbered = _numbered_prefix(naming)
    if numbered is not None:
        naming = f"{numbered}{slot:03d}"
    prefix = f"{_RENDER_JOIN}{file_type} "
    counter = re.compile(rf"_{re.escape(naming)}_{frame:04d}_(\d+)" r"(?=[ .\]]|$)", re.IGNORECASE)
    numbers = [int(m[1]) for path in used_paths if path.startswith(prefix) for m in counter.finditer(path)]
    try:
        for folder in (shot_dir / "preRender" / file_type).iterdir():
            if folder.name.casefold().startswith(f"{shot_dir.name}_".casefold()):
                numbers.extend(int(m[1]) for m in counter.finditer(folder.name))
    except FileNotFoundError:
        pass
    return max(numbers, default=0) + 1


def _set_mov_codec(node, codec="h264"):
    container = node.knob("mov64_format")
    if container is not None:
        container.setValue("mov (QuickTime / MOV)")
    node["mov64_codec"].setValue(codec)
    node["mov64_quality"].setValue("High")


def _create(file_type, frame=None, naming="toRunAI", preset=None):
    root = nuke.root()
    script_path = root.name()
    if not os.path.isabs(script_path) or not os.path.isfile(script_path):
        nuke.message("Save the Nuke script before creating a Shot Write.")
        return

    sources = nuke.selectedNodes()
    if len(sources) > 1:
        nuke.message("Select a single source node for Shot Write.")
        return
    source = sources[0] if sources else None
    if source is not None and source.Class() in {"Viewer", "BackdropNode", "StickyNote"}:
        nuke.message("Select an image node as the source for Shot Write.")
        return

    node = None
    try:
        save_preset = preset is not None
        script_dir = Path(script_path).parent
        shot_dir = script_dir.parent if script_dir.name.casefold() == "nuke" else script_dir
        multiview = len(nuke.views()) > 1
        used_paths = {node["file"].getText() for node in nuke.allNodes("Write", root, True)}
        preset = preset or _format_preset(file_type)
        node_name = _PROFILES[file_type][0]
        slot = _next_slot(file_type, naming, frame, shot_dir, used_paths)
        still_slot = 1 if frame is None else _next_still_slot(file_type, naming, slot, frame, shot_dir, used_paths)
        pattern = _file_pattern(file_type, slot, multiview, frame, naming, still_slot)
        node = nuke.nodes.Write()
        node.setName(node_name)
        node["file"].setValue(pattern)
        node["file_type"].setValue(file_type)
        node["channels"].setValue(preset["channels"])
        if preset["datatype"]:
            node["datatype"].setValue(preset["datatype"])
        node["create_directories"].setValue(True)
        if file_type == "exr":
            node["write_ACES_compliant_EXR"].setValue(False)
            node["compression"].setValue(preset["compression"])
        elif file_type == "mov":
            _set_mov_codec(node, preset["codec"])
        _configure_output(root, node, frame, preset)
        if save_preset:
            saved = nuke.String_Knob("shot_write_preset", "")
            saved.setVisible(False)
            saved.setValue(json.dumps(preset))
            node.addKnob(saved)

        if source is not None and not node.setInput(0, source):
            raise RuntimeError("The selected node cannot feed an image Write.")
        node.autoplace()
        if source is not None:
            node.setSelected(False)
    except Exception as exc:
        if node is not None:
            nuke.delete(node)
        if source is not None:
            source.setSelected(True)
        nuke.message(f"Shot Write could not create the preset.\n\n{exc}")
        return

    if source is not None:
        source.setSelected(True)
    return node


def _update_selected_writes():
    with nuke.lastHitGroup():
        nodes = [
            node for node in nuke.selectedNodes()
            if node.Class() == "Write" and node["file_type"].value() in _PROFILES
        ]
        if not nodes:
            nuke.message("Select the PNG, EXR, DPX or MOV Write nodes to update.")
            return
        root = nuke.root()
        for node in nodes:
            path = node["file"].getText()
            is_still = node.knob("shot_write_render_frame") is not None or (
                path.startswith(_RENDER_JOIN) and "_1frame" in path
                and node["first"].value() == node["last"].value()
            )
            frame = int(node["first"].value()) if is_still else None
            _configure_output(root, node, frame)


def _open_ring(file_type=None):
    group = nuke.lastHitGroup()
    core, gui, widgets = _qt_modules()
    while True:
        settings = _load_settings()
        if file_type and not any(item["enabled"] and item["file_type"] == file_type for item in settings["formats"]):
            nuke.message(f"Enable a {file_type.upper()} preset in Edit Action Ring.")
            return
        frame = int(nuke.frame())
        popup = _ring_widget(core, gui, widgets)(widgets.QApplication.activeWindow(), frame, file_type)
        position = gui.QCursor.pos()
        screen = widgets.QApplication.screenAt(position) or widgets.QApplication.primaryScreen()
        bounds = screen.availableGeometry()
        half = popup.width() // 2
        popup.move(
            max(bounds.left(), min(position.x() - half, bounds.right() - popup.width() + 1)),
            max(bounds.top(), min(position.y() - half, bounds.bottom() - popup.height() + 1)),
        )
        if popup.selected is None:
            popup.exec() if hasattr(popup, "exec") else popup.exec_()
        selected, edit_requested = popup.selected, popup.edit_requested
        popup.deleteLater()
        if edit_requested:
            edit_action_ring()
            continue
        if selected is not None:
            with group:
                return _create(*selected)
        return


def _ring_widget(core, gui, widgets):
    """Build the dialog class with Nuke's Qt binding."""
    qt = core.Qt

    class ShotWriteRing(widgets.QDialog):
        SIZE = 328
        INNER = 66
        OUTER = 159
        ITEMS_PER_PAGE = 6

        def __init__(self, parent, frame, file_type=None):
            super().__init__(parent, qt.Popup | qt.FramelessWindowHint)
            self.setFixedSize(self.SIZE, self.SIZE)
            self.setAttribute(qt.WA_TranslucentBackground)
            self.setMouseTracking(True)
            self.setAccessibleName("Shot Write Action Ring")
            self.selected = None
            self.edit_requested = False
            self.settings = _load_settings()
            self.active = {
                level: [item for item in self.settings[level] if item["enabled"]]
                for level in ("formats", "modes", "names")
            }
            self.naming_options = tuple(item["suffix"] for item in self.active["names"])
            self.frame = frame
            self.preset = next((item for item in self.active["formats"] if item["file_type"] == file_type), None)
            self.file_type = self.preset["file_type"] if self.preset else None
            self.render_frame = None
            self.history = []
            center = self.SIZE // 2
            self.center = core.QPointF(center, center)
            self.previous_page = widgets.QPushButton("‹", self)
            self.previous_page.setAccessibleName("Previous ring page")
            self.previous_page.setGeometry(center - 49, center + 28, 25, 23)
            self.previous_page.clicked.connect(lambda: self.change_page(-1))
            self.next_page = widgets.QPushButton("›", self)
            self.next_page.setAccessibleName("Next ring page")
            self.next_page.setGeometry(center + 24, center + 28, 25, 23)
            self.next_page.clicked.connect(lambda: self.change_page(1))
            self.settings_button = widgets.QPushButton("⚙", self)
            self.settings_button.setAccessibleName("Edit Action Ring")
            self.settings_button.setToolTip("Edit Action Ring")
            self.settings_button.setGeometry(center - 13, center - 50, 26, 23)
            self.settings_button.clicked.connect(self.request_editor)
            if self.preset and not self.preset["choose_frames"]:
                self.render_frame = frame if self.preset["default_mode"] == "frame" else None
            self.set_level("formats" if self.preset is None else self.format_level())

        def format_level(self):
            return "frames" if self.preset["choose_frames"] else "names"

        def set_level(self, level):
            self.page = 0
            self.level = level
            self.items = self.active["modes" if level == "frames" else level]
            self.update_sectors()
            if len(self.items) == 1:
                self.choose(0)

        def change_page(self, step):
            self.page = max(0, min(self.page + step, self.page_count - 1))
            self.update_sectors()

        def update_sectors(self):
            self.hovered = None
            self.page_count = math.ceil(len(self.items) / self.ITEMS_PER_PAGE)
            self.previous_page.setVisible(self.page_count > 1)
            self.next_page.setVisible(self.page_count > 1)
            self.previous_page.setEnabled(self.page > 0)
            self.next_page.setEnabled(self.page + 1 < self.page_count)
            start = self.page * self.ITEMS_PER_PAGE
            visible = self.items[start:start + self.ITEMS_PER_PAGE]
            self.labels = []
            for item in visible:
                if self.level == "formats":
                    subtitle = item["codec"].upper() if item["file_type"] == "mov" else item["file_type"].upper()
                elif self.level == "frames":
                    subtitle = f"Frame {self.frame}" if item["kind"] == "frame" else "Frame range"
                else:
                    subtitle = "Next number" if _numbered_prefix(item["suffix"]) is not None else ""
                self.labels.append((item["label"], subtitle))
            if self.level == "formats" and len(visible) == 4:
                angles = (90, 0, -90, 180)
            elif self.level == "frames" and len(visible) == 2:
                angles = (180, 0)
            else:
                angles = tuple(90 - index * 360 / len(visible) for index in range(len(visible)))
            half_angle = 180 / len(visible) - 2
            self.offsets = [(112 * math.cos(math.radians(a)), -112 * math.sin(math.radians(a))) for a in angles]
            self.sectors = []
            for angle in angles:
                path = gui.QPainterPath()
                path.arcMoveTo(self.circle(self.OUTER), angle - half_angle)
                path.arcTo(self.circle(self.OUTER), angle - half_angle, 2 * half_angle)
                inner_arc = gui.QPainterPath()
                inner_arc.arcMoveTo(self.circle(self.INNER), angle + half_angle)
                path.lineTo(inner_arc.currentPosition())
                path.arcTo(self.circle(self.INNER), angle + half_angle, -2 * half_angle)
                path.closeSubpath()
                self.sectors.append(path)
            self.update()

        def circle(self, radius):
            return core.QRectF(self.center.x() - radius, self.center.y() - radius, 2 * radius, 2 * radius)

        def sector_at(self, event):
            point = event.position() if hasattr(event, "position") else event.localPos()
            return next((i for i, path in enumerate(self.sectors) if path.contains(point)), None)

        def mouseMoveEvent(self, event):
            hovered = self.sector_at(event)
            if self.hovered != hovered:
                self.hovered = hovered
                self.setToolTip(self.labels[hovered][0] if hovered is not None else "")
                self.update()

        def leaveEvent(self, event):
            self.hovered = None
            self.update()

        def mousePressEvent(self, event):
            if event.button() == qt.RightButton or not self.rect().contains(event.pos()):
                self.reject()
            else:
                event.accept()

        def mouseReleaseEvent(self, event):
            if event.button() == qt.LeftButton:
                self.choose(self.sector_at(event))

        def keyPressEvent(self, event):
            if event.isAutoRepeat():
                return
            if event.key() in (qt.Key_PageUp, qt.Key_PageDown):
                self.change_page(1 if event.key() == qt.Key_PageDown else -1)
                return
            if self.level == "formats" and len(self.labels) == 4:
                keys = (qt.Key_Up, qt.Key_Right, qt.Key_Down, qt.Key_Left)
            elif self.level == "frames" and len(self.labels) == 2:
                keys = (qt.Key_Left, qt.Key_Right)
            else:
                keys = ()
            numbers = (qt.Key_1, qt.Key_2, qt.Key_3, qt.Key_4, qt.Key_5, qt.Key_6)[:len(self.labels)]
            if event.key() == qt.Key_Escape and self.history:
                level, self.preset, self.render_frame = self.history.pop()
                self.file_type = self.preset["file_type"] if self.preset else None
                self.set_level(level)
            elif event.key() in numbers:
                self.choose(numbers.index(event.key()))
            elif event.key() in keys:
                self.choose(keys.index(event.key()))
            elif event.key() in (qt.Key_Return, qt.Key_Enter):
                self.choose(self.hovered)
            else:
                super().keyPressEvent(event)

        def reject(self):
            self.selected = None
            super().reject()

        def request_editor(self):
            self.edit_requested = True
            self.reject()

        def wheelEvent(self, event):
            if event.angleDelta().y():
                self.change_page(1 if event.angleDelta().y() < 0 else -1)
                event.accept()
            else:
                super().wheelEvent(event)

        def choose(self, index):
            if index is None:
                self.reject()
                return
            item = self.items[self.page * self.ITEMS_PER_PAGE + index]
            if self.level != "names" and len(self.items) > 1:
                self.history.append((self.level, self.preset, self.render_frame))
            if self.level == "formats":
                self.preset = item
                self.file_type = item["file_type"]
                self.render_frame = self.frame if item["default_mode"] == "frame" else None
                self.set_level(self.format_level())
            elif self.level == "frames":
                self.render_frame = self.frame if item["kind"] == "frame" else None
                self.set_level("names")
            else:
                self.selected = (self.file_type, self.render_frame, item["suffix"], self.preset)
                self.accept()

        def paintEvent(self, event):
            painter = gui.QPainter(self)
            painter.setRenderHint(gui.QPainter.Antialiasing)
            painter.setPen(gui.QColor("#465261"))
            painter.setBrush(gui.QColor("#171b21"))
            painter.drawEllipse(self.circle(self.OUTER + 3))
            for index, (path, (title, subtitle), (dx, dy)) in enumerate(zip(self.sectors, self.labels, self.offsets)):
                painter.setPen(gui.QColor("#465261"))
                painter.setBrush(gui.QColor("#344b61" if index == self.hovered else "#252c35"))
                painter.drawPath(path)
                if dx > 70:
                    dx -= 10
                elif dx < -70:
                    dx += 10
                self.draw_centered(painter, dx, dy - 8 if subtitle else dy, title, 13, "#f4f6f8", True)
                if subtitle:
                    self.draw_centered(painter, dx, dy + 11, subtitle, 11, "#b6c4d1")

            title = {"formats": "SHOT WRITE", "frames": self.preset["label"] if self.preset else "", "names": "OUTPUT NAME"}[self.level]
            self.draw_centered(painter, 0, -8 if self.level == "names" else 0, title.upper(), 13, "#f4f6f8", True)
            if self.level == "names":
                mode = "Sequence" if self.render_frame is None else f"Frame {self.render_frame}"
                self.draw_centered(painter, 0, 11, f"{self.file_type.upper()} · {mode}", 11, "#b6c4d1")
            if self.page_count > 1:
                self.draw_centered(painter, 0, 39, f"{self.page + 1}/{self.page_count}", 11, "#b6c4d1")

        def draw_centered(self, painter, x, y, text, size, color, bold=False):
            font = painter.font()
            font.setPixelSize(size)
            font.setBold(bold)
            painter.setFont(font)
            painter.setPen(gui.QColor(color))
            box = core.QRectF(self.center.x() + x - 52, self.center.y() + y - 10, 104, 20)
            text = painter.fontMetrics().elidedText(text, qt.ElideRight, int(box.width()))
            painter.drawText(box, qt.AlignCenter, text)

    return ShotWriteRing


def register_menu(replace=False):
    toolbar = nuke.menu("Nodes")
    if toolbar.findItem("Shot Write") is not None:
        if not replace:
            return
        toolbar.removeItem("Shot Write")

    menu = toolbar.addMenu("Shot Write", icon="Write.png")
    menu.addCommand("Action Ring", _open_ring, _RING_SHORTCUT, icon="Write.png", shortcutContext=2)
    for label, file_type, shortcut in _COMMANDS:
        menu.addCommand(label, partial(_open_ring, file_type), shortcut, icon="Write.png", shortcutContext=2)
    menu.addSeparator()
    menu.addCommand("Edit Action Ring", edit_action_ring, icon="Write.png", shortcutContext=2)
    menu.addCommand("Paste Render as Read", paste_render_as_read, "Ctrl+Alt+R", icon="Read.png", shortcutContext=2)
    menu.addCommand("Update Selected Writes", _update_selected_writes, icon="Write.png", shortcutContext=2)


_MENU_MARKER = b"# Shot Write startup"
_MENU_ENTRY = _MENU_MARKER + b"\nimport shot_write\nshot_write.register_menu()\n"


def _atomic_write(path, data):
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as temp:
        temp_path = Path(temp.name)
        try:
            temp.write(data)
            temp.close()
            os.replace(temp_path, path)
        finally:
            temp.close()
            temp_path.unlink(missing_ok=True)


def _install_files(folder):
    folder.mkdir(parents=True, exist_ok=True)
    menu_file = folder / "menu.py"
    previous = menu_file.read_bytes() if menu_file.exists() else b""
    update_menu = _MENU_MARKER not in previous.splitlines()
    if update_menu:
        newline = b"\r\n" if b"\r\n" in previous else b"\n"
        separator = newline * 2 if previous else b""
        updated = previous + separator + _MENU_ENTRY.replace(b"\n", newline)
        compile(updated, str(menu_file), "exec")

    module_file = folder / "shot_write.py"
    payload = Path(__file__).read_bytes()
    if not module_file.exists() or module_file.read_bytes() != payload:
        _atomic_write(module_file, payload)

    if update_menu:
        if previous:
            with tempfile.NamedTemporaryFile(prefix="menu.py.", suffix=".bak", dir=folder, delete=False) as backup:
                backup.write(previous)
        _atomic_write(menu_file, updated)


def install():
    """Install for this user and register the shortcuts in the current Nuke session."""
    folder = Path(os.environ.get("HOME") or Path.home()) / ".nuke"
    _install_files(folder)
    register_menu(replace=True)
    nuke.message(f"Shot Write is installed. Press {_RING_SHORTCUT} in the Node Graph to open the Action Ring.")
