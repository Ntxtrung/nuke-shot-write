# -*- coding: utf-8 -*-
"""Install with runpy.run_path(r'path/to/shot_write.py')['install']() in Nuke."""

import json
import math
import os
import re
import struct
import tempfile
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


def _configure_output(root, node, frame):
    node["raw"].setValue(False)
    node["use_limit"].setValue(False)
    file_type = node["file_type"].value()
    colorspace = None
    if file_type in {"exr", "dpx"}:
        colorspace = _match_plate(root, node)
    colorspace = colorspace or _output_colorspace(root, node)
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
    if naming == "preRender_00x":
        naming = f"preRender_{slot:03d}"
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
    if naming == "preRender_00x":
        # Count across formats and frame modes so an output number identifies one export.
        pattern = re.compile(r"_preRender_(\d+)(?=[_ .\]}]|$)", re.IGNORECASE)
        numbers = [int(m.group(1)) for path in used_paths if path.startswith(_RENDER_JOIN) for m in pattern.finditer(path)]
        for ext in _PROFILES:
            try:
                folders = (shot_dir / "preRender" / ext).iterdir()
                for folder in folders:
                    if folder.name.casefold().startswith(f"{shot_dir.name}_prerender_".casefold()):
                        numbers.extend(int(m.group(1)) for m in pattern.finditer(folder.name))
            except FileNotFoundError:
                pass
        return max(numbers, default=0) + 1

    while frame is None and any(_file_pattern(file_type, slot, split, frame, naming) in used_paths for split in (False, True)):
        slot += 1
    return slot


def _next_still_slot(file_type, naming, slot, frame, shot_dir, used_paths):
    if naming == "preRender_00x":
        naming = f"preRender_{slot:03d}"
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


def _set_mov_codec(node):
    container = node.knob("mov64_format")
    if container is not None:
        container.setValue("mov (QuickTime / MOV)")
    node["mov64_codec"].setValue("h264")
    node["mov64_quality"].setValue("High")


def _create(file_type, frame=None, naming="toRunAI"):
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
        script_dir = Path(script_path).parent
        shot_dir = script_dir.parent if script_dir.name.casefold() == "nuke" else script_dir
        multiview = len(nuke.views()) > 1
        used_paths = {node["file"].getText() for node in nuke.allNodes("Write", root, True)}
        node_name, channels, datatype = _PROFILES[file_type]
        slot = _next_slot(file_type, naming, frame, shot_dir, used_paths)
        still_slot = 1 if frame is None else _next_still_slot(file_type, naming, slot, frame, shot_dir, used_paths)
        pattern = _file_pattern(file_type, slot, multiview, frame, naming, still_slot)
        node = nuke.nodes.Write()
        node.setName(node_name)
        node["file"].setValue(pattern)
        node["file_type"].setValue(file_type)
        node["channels"].setValue(channels)
        if datatype is not None:
            node["datatype"].setValue(datatype)
        node["create_directories"].setValue(True)
        if file_type == "exr":
            node["write_ACES_compliant_EXR"].setValue(False)
            node["compression"].setValue("Zip (1 scanline)")
        elif file_type == "mov":
            _set_mov_codec(node)
        _configure_output(root, node, frame)

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
    QtCore, QtGui, QtWidgets = _qt_modules()
    frame = int(nuke.frame())
    popup = _ring_widget(QtCore, QtGui, QtWidgets)(QtWidgets.QApplication.activeWindow(), frame, file_type)
    position = QtGui.QCursor.pos()
    screen = QtWidgets.QApplication.screenAt(position) or QtWidgets.QApplication.primaryScreen()
    bounds = screen.availableGeometry()
    half = popup.width() // 2
    popup.move(
        max(bounds.left(), min(position.x() - half, bounds.right() - popup.width() + 1)),
        max(bounds.top(), min(position.y() - half, bounds.bottom() - popup.height() + 1)),
    )
    if hasattr(popup, "exec"):
        popup.exec()
    else:
        popup.exec_()
    selected = popup.selected
    popup.deleteLater()

    if selected is not None:
        with group:
            return _create(*selected)


def _ring_widget(core, gui, widgets):
    """Build the dialog class with Nuke's Qt binding."""
    qt = core.Qt

    class ShotWriteRing(widgets.QDialog):
        SIZE = 328
        INNER = 66
        OUTER = 159

        def __init__(self, parent, frame, file_type=None):
            super().__init__(parent, qt.Popup | qt.FramelessWindowHint)
            self.setFixedSize(self.SIZE, self.SIZE)
            self.setAttribute(qt.WA_TranslucentBackground)
            self.setMouseTracking(True)
            self.setAccessibleName("Shot Write Action Ring")
            self.selected = None
            self.frame = frame
            self.file_type = file_type
            self.render_frame = None
            center = self.SIZE / 2
            self.center = core.QPointF(center, center)
            self.set_level("formats" if file_type is None else self.format_level())

        def format_level(self):
            return "names" if self.file_type == "mov" else "frames"

        def set_level(self, level):
            self.level = level
            self.hovered = None
            if level == "formats":
                self.labels = (("PNG", "Reference"), ("EXR", "Precomp"), ("DPX", "Precomp"), ("MOV", "H.264"))
                angles, half_angle = (90, 0, -90, 180), 43
            elif level == "frames":
                self.labels = (("1 Frame", f"Frame {self.frame}"), ("Sequence", "Frame range"))
                angles, half_angle = (180, 0), 88
            else:
                self.labels = tuple((name, "Next number" if name == "preRender_00x" else "") for name in _NAMING_OPTIONS)
                angles, half_angle = (90, 18, -54, -126, 162), 34

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
            if self.level == "formats":
                keys = (qt.Key_Up, qt.Key_Right, qt.Key_Down, qt.Key_Left)
            elif self.level == "frames":
                keys = (qt.Key_Left, qt.Key_Right)
            else:
                keys = ()
            numbers = (qt.Key_1, qt.Key_2, qt.Key_3, qt.Key_4, qt.Key_5)[:len(self.labels)]
            if event.key() == qt.Key_Escape and self.level != "formats":
                self.set_level("frames" if self.level == "names" and self.format_level() == "frames" else "formats")
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

        def choose(self, index):
            if index is None:
                self.reject()
            elif self.level == "formats":
                self.file_type = _COMMANDS[index][1]
                self.render_frame = None
                self.set_level(self.format_level())
            elif self.level == "frames":
                self.render_frame = self.frame if index == 0 else None
                self.set_level("names")
            else:
                self.selected = (self.file_type, self.render_frame, _NAMING_OPTIONS[index])
                self.accept()

        def paintEvent(self, event):
            painter = gui.QPainter(self)
            painter.setRenderHint(gui.QPainter.Antialiasing)
            painter.setPen(gui.QColor("#465261"))
            painter.setBrush(gui.QColor("#171b21"))
            painter.drawEllipse(self.circle(self.OUTER + 3))

            for i, (path, (title, subtitle), (dx, dy)) in enumerate(zip(self.sectors, self.labels, self.offsets)):
                painter.setPen(gui.QColor("#465261"))
                painter.setBrush(gui.QColor("#344b61" if i == self.hovered else "#252c35"))
                painter.drawPath(path)
                if title == "preRender_00x":
                    dx -= 9
                size = 13 if title == "preRender_00x" else 14
                self.draw_centered(painter, dx, dy - 8 if subtitle else dy, title, size, "#f4f6f8", True)
                if subtitle:
                    self.draw_centered(painter, dx, dy + 11, subtitle, 11, "#b6c4d1")

            title = {"formats": "SHOT WRITE", "frames": self.file_type or "", "names": "OUTPUT NAME"}[self.level]
            self.draw_centered(painter, 0, -8 if self.level == "names" else 0, title.upper(), 13, "#f4f6f8", True)
            if self.level == "names":
                if self.file_type == "mov":
                    detail = "MOV · H.264"
                else:
                    mode = "Sequence" if self.render_frame is None else f"Frame {self.render_frame}"
                    detail = f"{self.file_type.upper()} · {mode}"
                self.draw_centered(painter, 0, 11, detail, 11, "#b6c4d1")

        def draw_centered(self, painter, x, y, text, size, color, bold=False):
            font = painter.font()
            font.setPixelSize(size)
            font.setBold(bold)
            painter.setFont(font)
            painter.setPen(gui.QColor(color))
            box = core.QRectF(self.center.x() + x - 52, self.center.y() + y - 10, 104, 20)
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
