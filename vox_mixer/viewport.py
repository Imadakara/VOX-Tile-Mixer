"""Interactive GPU voxel preview; geometry is meshed once per model variant."""
from __future__ import annotations

import math

import numpy as np
from OpenGL import GL
from PySide6.QtCore import Qt
from PySide6.QtOpenGLWidgets import QOpenGLWidget

from .core import Model, VoxScene


VERTEX_SHADER = """
#version 330 core
layout(location=0) in vec3 position;
layout(location=1) in vec3 normal;
layout(location=2) in vec3 color;
uniform mat4 mvp;
uniform mat4 model;
out vec3 fragmentColor;
void main() {
    vec3 n = normalize(mat3(model) * normal);
    float light = max(dot(n, normalize(vec3(-0.45, -0.6, 1.0))), 0.0);
    fragmentColor = color * (0.50 + 0.48 * light);
    gl_Position = mvp * vec4(position, 1.0);
}
"""
FRAGMENT_SHADER = """
#version 330 core
in vec3 fragmentColor;
out vec4 frag;
void main() { frag = vec4(fragmentColor, 1.0); }
"""

# Two triangles, with outward normals. Coordinates are relative to a voxel corner.
FACES = (
    ((1, 0, 0), ((1, 0, 0), (1, 1, 0), (1, 1, 1), (1, 0, 1))),
    ((-1, 0, 0), ((0, 0, 0), (0, 0, 1), (0, 1, 1), (0, 1, 0))),
    ((0, 1, 0), ((0, 1, 0), (0, 1, 1), (1, 1, 1), (1, 1, 0))),
    ((0, -1, 0), ((0, 0, 0), (1, 0, 0), (1, 0, 1), (0, 0, 1))),
    ((0, 0, 1), ((0, 0, 1), (1, 0, 1), (1, 1, 1), (0, 1, 1))),
    ((0, 0, -1), ((0, 0, 0), (0, 1, 0), (1, 1, 0), (1, 0, 0))),
)


def mesh_vertices(model: Model, palette: bytes):
    occupied = {(x, y, z) for x, y, z, _ in model.voxels}
    result = []
    half = tuple(s / 2 for s in model.size)
    for x, y, z, color_index in model.voxels:
        rgba = palette[(color_index - 1) * 4:color_index * 4 + 4]
        color = tuple(v / 255 for v in rgba[:3])
        for normal, corners in FACES:
            if (x + normal[0], y + normal[1], z + normal[2]) in occupied:
                continue
            for i in (0, 1, 2, 0, 2, 3):
                corner = corners[i]
                result.extend((x + corner[0] - half[0], y + corner[1] - half[1],
                               z + corner[2] - half[2], *normal, *color))
    return np.asarray(result, dtype=np.float32)


def compile_shader(source, shader_type):
    shader = GL.glCreateShader(shader_type)
    GL.glShaderSource(shader, source)
    GL.glCompileShader(shader)
    if not GL.glGetShaderiv(shader, GL.GL_COMPILE_STATUS):
        raise RuntimeError(GL.glGetShaderInfoLog(shader).decode(errors="replace"))
    return shader


def perspective(fov, aspect, near, far):
    f = 1.0 / math.tan(math.radians(fov) / 2)
    return np.array([[f / aspect, 0, 0, 0], [0, f, 0, 0],
                     [0, 0, (far + near) / (near - far), 2 * far * near / (near - far)],
                     [0, 0, -1, 0]], dtype=np.float32)


def look_at(eye, target):
    forward = target - eye
    forward /= np.linalg.norm(forward)
    side = np.cross(forward, np.array([0, 0, 1], dtype=np.float32))
    side /= np.linalg.norm(side)
    up = np.cross(side, forward)
    view = np.eye(4, dtype=np.float32)
    view[:3, :3] = np.stack((side, up, -forward))
    view[:3, 3] = -view[:3, :3] @ eye
    return view


class VoxViewport(QOpenGLWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.scene: VoxScene | None = None
        self.choices = {}
        self.fitted = {}
        self.yaw = math.radians(55)
        self.pitch = math.radians(30)
        self.distance = 400.0
        self.center = np.zeros(3, dtype=np.float32)
        self.last_mouse = None
        self.cpu_meshes = {}
        self.layer_buffers = {}
        self.outline_buffer = None
        self.rebuild_required = True
        self.outline_required = True
        self.selected_link = None
        self.visible_layers = set()
        self.invert_horizontal = True
        self.invert_vertical = True
        self.program = None
        self.setMinimumSize(440, 360)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def initializeGL(self):
        vertex = compile_shader(VERTEX_SHADER, GL.GL_VERTEX_SHADER)
        fragment = compile_shader(FRAGMENT_SHADER, GL.GL_FRAGMENT_SHADER)
        self.program = GL.glCreateProgram()
        GL.glAttachShader(self.program, vertex)
        GL.glAttachShader(self.program, fragment)
        GL.glLinkProgram(self.program)
        if not GL.glGetProgramiv(self.program, GL.GL_LINK_STATUS):
            raise RuntimeError(GL.glGetProgramInfoLog(self.program).decode(errors="replace"))
        GL.glDeleteShader(vertex)
        GL.glDeleteShader(fragment)
        GL.glEnable(GL.GL_DEPTH_TEST)
        GL.glDisable(GL.GL_CULL_FACE)
        self.rebuild_required = True
        self.outline_required = True

    def set_scene(self, scene: VoxScene):
        self.scene = scene
        self.choices = {}
        self.fitted = {}
        self.cpu_meshes.clear()
        self.selected_link = None
        self.visible_layers = {instance.layer for instance in scene.instances}
        self.rebuild_required = True
        self.outline_required = True
        if scene.instances:
            positions = np.array([instance.origin for instance in scene.instances], dtype=np.float32)
            low, high = positions.min(axis=0), positions.max(axis=0)
            self.center = (low + high) / 2
            self.distance = max(80.0, float(np.linalg.norm(high - low)) * 1.15)
        self.update()

    def set_mix(self, choices: dict, fitted: dict):
        self.choices = choices
        self.fitted = fitted
        self.rebuild_required = True
        self.outline_required = True
        self.update()

    def set_layer_visible(self, name: str, visible: bool):
        if visible:
            self.visible_layers.add(name)
        else:
            self.visible_layers.discard(name)
        self.update()

    def highlight_link(self, model_id: int | None, layer: str | None = None):
        self.selected_link = (model_id, layer) if model_id is not None else None
        self.outline_required = True
        self.update()

    def _buffer(self, values):
        vao = GL.glGenVertexArrays(1)
        vbo = GL.glGenBuffers(1)
        GL.glBindVertexArray(vao)
        GL.glBindBuffer(GL.GL_ARRAY_BUFFER, vbo)
        GL.glBufferData(GL.GL_ARRAY_BUFFER, values.nbytes, values, GL.GL_STATIC_DRAW)
        for location in range(3):
            GL.glEnableVertexAttribArray(location)
            GL.glVertexAttribPointer(location, 3, GL.GL_FLOAT, GL.GL_FALSE, 36,
                                     GL.ctypes.c_void_p(location * 12))
        GL.glBindVertexArray(0)
        return vao, vbo, len(values) // 9

    def _delete_outline(self):
        if self.outline_buffer:
            vao, vbo, _ = self.outline_buffer
            GL.glDeleteVertexArrays(1, [vao])
            GL.glDeleteBuffers(1, [vbo])
            self.outline_buffer = None

    def _delete_buffers(self):
        for vao, vbo, _ in self.layer_buffers.values():
            GL.glDeleteVertexArrays(1, [vao])
            GL.glDeleteBuffers(1, [vbo])
        self.layer_buffers.clear()
        self._delete_outline()

    def _rebuild(self):
        self._delete_buffers()
        if self.scene is None:
            return
        layers = {}
        for instance in self.scene.instances:
            key = self.choices.get(instance.shape_id)
            variant = self.fitted.get(key) if key is not None else None
            model = variant.model if variant is not None else self.scene.models[instance.model_id]
            mesh_key = key if variant is not None else ("base", instance.model_id)
            if mesh_key not in self.cpu_meshes:
                self.cpu_meshes[mesh_key] = mesh_vertices(model, self.scene.palette).reshape((-1, 9))
            source = self.cpu_meshes[mesh_key]
            axes = np.asarray(instance.axes, dtype=np.float32)
            origin = np.asarray(instance.origin, dtype=np.float32)
            if variant is not None:
                origin = origin + axes @ np.asarray(variant.pivot_shift, dtype=np.float32)
            if len(source):
                transformed = source.copy()
                transformed[:, :3] = source[:, :3] @ axes.T + origin
                transformed[:, 3:6] = source[:, 3:6] @ axes.T
                layers.setdefault(instance.layer, []).append(transformed)
        for layer, meshes in layers.items():
            self.layer_buffers[layer] = self._buffer(np.ascontiguousarray(np.concatenate(meshes).ravel()))
        self.rebuild_required = False
        self.outline_required = True

    def _rebuild_outline(self):
        self._delete_outline()
        outlines = []
        if self.scene is None or self.selected_link is None:
            self.outline_required = False
            return
        for instance in self.scene.instances:
            if (instance.model_id, instance.layer) != self.selected_link:
                continue
            key = self.choices.get(instance.shape_id)
            variant = self.fitted.get(key) if key is not None else None
            model = variant.model if variant is not None else self.scene.models[instance.model_id]
            if not model.voxels:
                continue
            axes = np.asarray(instance.axes, dtype=np.float32)
            origin = np.asarray(instance.origin, dtype=np.float32)
            if variant is not None:
                origin = origin + axes @ np.asarray(variant.pivot_shift, dtype=np.float32)
            low, high = model.bounds
            half = np.asarray(model.size, dtype=np.float32) / 2
            low = np.asarray(low, dtype=np.float32) - half - 0.2
            high = np.asarray(high, dtype=np.float32) + 1 - half + 0.2
            corners = np.array([[x, y, z] for x in (low[0], high[0])
                                for y in (low[1], high[1]) for z in (low[2], high[2])], dtype=np.float32)
            corners = corners @ axes.T + origin
            for a, b in ((0, 1), (0, 2), (0, 4), (1, 3), (1, 5), (2, 3),
                         (2, 6), (3, 7), (4, 5), (4, 6), (5, 7), (6, 7)):
                for corner in (corners[a], corners[b]):
                    outlines.extend((*corner, 0, 0, 1, 0.12, 0.95, 1.0))
        if outlines:
            self.outline_buffer = self._buffer(np.asarray(outlines, dtype=np.float32))
        self.outline_required = False

    def paintGL(self):
        GL.glViewport(0, 0, self.width(), self.height())
        GL.glClearColor(0.095, 0.115, 0.15, 1)
        GL.glClear(GL.GL_COLOR_BUFFER_BIT | GL.GL_DEPTH_BUFFER_BIT)
        if self.scene is None or self.program is None:
            return
        if self.rebuild_required:
            self._rebuild()
        if self.outline_required:
            self._rebuild_outline()
        eye = self.center + self.distance * np.array((math.cos(self.pitch) * math.cos(self.yaw),
                    math.cos(self.pitch) * math.sin(self.yaw), math.sin(self.pitch)), dtype=np.float32)
        view = look_at(eye, self.center)
        projection = perspective(42, max(1, self.width()) / max(1, self.height()), 0.5, max(2000, self.distance * 8))
        GL.glUseProgram(self.program)
        mvp_location = GL.glGetUniformLocation(self.program, "mvp")
        GL.glUniformMatrix4fv(mvp_location, 1, GL.GL_TRUE, projection @ view)
        GL.glUniformMatrix4fv(GL.glGetUniformLocation(self.program, "model"), 1, GL.GL_TRUE,
                              np.eye(4, dtype=np.float32))
        for layer, (vao, _, count) in self.layer_buffers.items():
            if layer not in self.visible_layers:
                continue
            GL.glBindVertexArray(vao)
            GL.glDrawArrays(GL.GL_TRIANGLES, 0, count)
        if self.outline_buffer and self.selected_link and self.selected_link[1] in self.visible_layers:
            GL.glDisable(GL.GL_DEPTH_TEST)
            GL.glLineWidth(2)
            vao, _, count = self.outline_buffer
            GL.glBindVertexArray(vao)
            GL.glDrawArrays(GL.GL_LINES, 0, count)
            GL.glEnable(GL.GL_DEPTH_TEST)
        GL.glBindVertexArray(0)
        GL.glUseProgram(0)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.last_mouse = event.position()

    def mouseMoveEvent(self, event):
        if self.last_mouse is not None and event.buttons() & Qt.MouseButton.LeftButton:
            delta = event.position() - self.last_mouse
            self.yaw += delta.x() * 0.009 * (-1 if self.invert_horizontal else 1)
            self.pitch = max(-1.4, min(1.4, self.pitch + delta.y() * 0.009 *
                                       (-1 if self.invert_vertical else 1)))
            self.last_mouse = event.position()
            self.update()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.last_mouse = None

    def wheelEvent(self, event):
        self.distance *= 0.86 ** (event.angleDelta().y() / 120)
        self.distance = max(15, min(5000, self.distance))
        self.update()
