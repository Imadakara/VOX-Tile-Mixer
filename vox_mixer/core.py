"""Loss-conscious static MagicaVoxel scene reader and tile variant exporter."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import hashlib
import json
import random
import re
import struct


def read_i32(data: bytes, offset: int):
    if offset + 4 > len(data):
        raise ValueError("Truncated VOX integer")
    return struct.unpack_from("<i", data, offset)[0], offset + 4


def read_dict(data: bytes, offset: int):
    count, offset = read_i32(data, offset)
    if not 0 <= count <= 10000:
        raise ValueError("Invalid VOX dictionary")
    values = {}
    for _ in range(count):
        length, offset = read_i32(data, offset)
        if length < 0 or offset + length > len(data):
            raise ValueError("Invalid VOX string")
        key = data[offset:offset + length].decode("utf-8")
        offset += length
        length, offset = read_i32(data, offset)
        if length < 0 or offset + length > len(data):
            raise ValueError("Invalid VOX string")
        values[key] = data[offset:offset + length].decode("utf-8")
        offset += length
    return values, offset


def chunk(tag: bytes, body: bytes, children: bytes = b"") -> bytes:
    return tag + struct.pack("<II", len(body), len(children)) + body + children


def write_dict(values: dict[str, str]):
    result = struct.pack("<i", len(values))
    for key, value in values.items():
        for string in (key, value):
            encoded = string.encode("utf-8")
            result += struct.pack("<i", len(encoded)) + encoded
    return result


@dataclass(frozen=True)
class Chunk:
    tag: bytes
    body: bytes
    children: bytes
    raw: bytes


@dataclass(frozen=True)
class Model:
    size: tuple[int, int, int]
    voxels: tuple[tuple[int, int, int, int], ...]

    @property
    def bounds(self):
        if not self.voxels:
            return (0, 0, 0), (-1, -1, -1)
        return tuple(min(v[i] for v in self.voxels) for i in range(3)), tuple(max(v[i] for v in self.voxels) for i in range(3))


def geometry_key(model: Model):
    lo, _ = model.bounds
    return tuple(sorted((x - lo[0], y - lo[1], z - lo[2]) for x, y, z, _ in model.voxels))


def geometry_hash(model: Model):
    digest = hashlib.sha256()
    digest.update(struct.pack("<III", *model.size))
    for voxel in sorted(model.voxels):
        digest.update(bytes(voxel))
    return digest.hexdigest()


def rotate(model: Model, turns: int):
    turns %= 4
    sx, sy, sz = model.size
    if turns == 0:
        return model
    if turns == 1:
        return Model((sy, sx, sz), tuple((y, sx - 1 - x, z, c) for x, y, z, c in model.voxels))
    if turns == 2:
        return Model((sx, sy, sz), tuple((sx - 1 - x, sy - 1 - y, z, c) for x, y, z, c in model.voxels))
    return Model((sy, sx, sz), tuple((sy - 1 - y, x, z, c) for x, y, z, c in model.voxels))


def parse_chunks(data: bytes):
    if len(data) < 20 or data[:4] != b"VOX " or data[8:12] != b"MAIN":
        raise ValueError("Not a MagicaVoxel VOX file")
    own, children = struct.unpack_from("<II", data, 12)
    start = 20 + own
    if start + children != len(data):
        raise ValueError("Invalid MAIN chunk size")
    result = []
    offset = start
    while offset < len(data):
        if offset + 12 > len(data):
            raise ValueError("Truncated VOX chunk")
        tag = data[offset:offset + 4]
        body_size, child_size = struct.unpack_from("<II", data, offset + 4)
        body_end = offset + 12 + body_size
        end = body_end + child_size
        if end > len(data):
            raise ValueError("Invalid VOX chunk size")
        result.append(Chunk(tag, data[offset + 12:body_end], data[body_end:end], data[offset:end]))
        offset = end
    return data[20:start], result


def parse_models(chunks: list[Chunk]):
    models = []
    waiting = None
    for item in chunks:
        if item.tag == b"SIZE":
            if waiting is not None or len(item.body) != 12:
                raise ValueError("Malformed SIZE chunk")
            waiting = struct.unpack("<III", item.body)
        elif item.tag == b"XYZI":
            if waiting is None or len(item.body) < 4:
                raise ValueError("Malformed XYZI chunk")
            count = struct.unpack_from("<I", item.body)[0]
            if len(item.body) != 4 + count * 4:
                raise ValueError("Invalid voxel count")
            voxels = tuple(tuple(item.body[i:i + 4]) for i in range(4, len(item.body), 4))
            if any(any(v[axis] >= waiting[axis] for axis in range(3)) for v in voxels):
                raise ValueError("Voxel outside model canvas")
            models.append(Model(waiting, voxels))
            waiting = None
    if not models or waiting is not None:
        raise ValueError("No complete VOX models")
    return models


def material_bodies(chunks: list[Chunk]):
    return {struct.unpack_from("<i", item.body)[0]: item.body[4:]
            for item in chunks if item.tag == b"MATL" and len(item.body) >= 4}


@dataclass
class Instance:
    shape_id: int
    model_id: int
    name: str
    layer: str
    origin: tuple[int, int, int]
    axes: tuple[tuple[int, int, int], ...]
    transform_id: int | None = None
    local_axes: tuple[tuple[int, int, int], ...] = ((1, 0, 0), (0, 1, 0), (0, 0, 1))


IDENTITY = ((1, 0, 0), (0, 1, 0), (0, 0, 1))


def rotation_axes(value: str):
    b = int(value)
    first, second = b & 3, (b >> 2) & 3
    if first == second or first > 2 or second > 2:
        raise ValueError(f"Invalid VOX rotation: {value}")
    third = next(i for i in range(3) if i not in (first, second))
    axes = [[0, 0, 0] for _ in range(3)]
    axes[0][first] = -1 if b & 16 else 1
    axes[1][second] = -1 if b & 32 else 1
    axes[2][third] = -1 if b & 64 else 1
    return tuple(tuple(row) for row in axes)


def mat_mul(a, b):
    return tuple(tuple(sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3)) for i in range(3))


def mat_vec(a, v):
    return tuple(sum(a[i][k] * v[k] for k in range(3)) for i in range(3))


def parse_instances(chunks: list[Chunk], model_count: int):
    transforms = {}
    groups = {}
    shapes = {}
    layers = {}
    for item in chunks:
        b = item.body
        if item.tag == b"LAYR":
            layer_id, pos = read_i32(b, 0)
            attrs, _ = read_dict(b, pos)
            layers[layer_id] = (attrs.get("_name", str(layer_id)), attrs.get("_hidden") == "1")
        elif item.tag == b"nTRN":
            node_id, pos = read_i32(b, 0)
            attrs, pos = read_dict(b, pos)
            if pos + 16 > len(b):
                raise ValueError("Malformed nTRN")
            child, _reserved, layer, count = struct.unpack_from("<iiii", b, pos)
            pos += 16
            frames = []
            for _ in range(count):
                frame, pos = read_dict(b, pos)
                frames.append(frame)
            if not frames or pos != len(b):
                raise ValueError("Unsupported nTRN frames")
            transforms[node_id] = (child, layer, attrs.get("_name", ""), frames[0])
        elif item.tag == b"nGRP":
            node_id, pos = read_i32(b, 0)
            _, pos = read_dict(b, pos)
            count, pos = read_i32(b, pos)
            if count < 0 or pos + count * 4 != len(b):
                raise ValueError("Malformed nGRP")
            groups[node_id] = struct.unpack_from("<" + "i" * count, b, pos)
        elif item.tag == b"nSHP":
            node_id, pos = read_i32(b, 0)
            _, pos = read_dict(b, pos)
            count, pos = read_i32(b, pos)
            models = []
            for _ in range(count):
                model_id, pos = read_i32(b, pos)
                _, pos = read_dict(b, pos)
                if model_id >= model_count or model_id < 0:
                    raise ValueError("Invalid model reference")
                models.append(model_id)
            if pos != len(b):
                raise ValueError("Malformed nSHP")
            shapes[node_id] = models
    all_nodes = set(transforms) | set(groups) | set(shapes)
    children = {v[0] for v in transforms.values()}
    children.update(child for v in groups.values() for child in v)
    roots = all_nodes - children
    if not roots:
        raise ValueError("No scene graph root")
    instances = []
    def visit(node, axes, origin, name, layer, hidden, trail, leaf_id=None, leaf_axes=IDENTITY):
        if node in trail:
            raise ValueError("Cycle in VOX scene graph")
        trail = trail | {node}
        if node in transforms:
            child, layer_id, title, frame = transforms[node]
            local = rotation_axes(frame.get("_r", "4"))
            translation = tuple(int(x) for x in frame.get("_t", "0 0 0").split())
            if len(translation) != 3:
                raise ValueError("Invalid transform position")
            here = mat_vec(axes, translation)
            label, is_hidden = layers.get(layer_id, (layer, False))
            visit(child, mat_mul(axes, local), tuple(origin[i] + here[i] for i in range(3)), title or name, label if layer_id >= 0 else layer, hidden or attrs.get("_hidden") == "1" or attrs_hidden(frame) or is_hidden, trail, node if child in shapes else None, local)
        elif node in groups:
            for child in groups[node]:
                visit(child, axes, origin, name, layer, hidden, trail, None, IDENTITY)
        elif node in shapes:
            if not hidden:
                for model_id in shapes[node]:
                    instances.append(Instance(node, model_id, name, layer, origin, axes, leaf_id, leaf_axes))
        else:
            raise ValueError(f"Unrecognized scene node {node}")
    for root in sorted(roots):
        visit(root, IDENTITY, (0, 0, 0), "", "", False, set())
    return instances


def attrs_hidden(frame):
    return frame.get("_hidden") == "1"


@dataclass
class VoxScene:
    path: Path
    data: bytes
    main_body: bytes
    chunks: list[Chunk]
    models: list[Model]
    palette: bytes
    materials: dict[int, bytes]
    instances: list[Instance]

    @classmethod
    def load(cls, path):
        path = Path(path).resolve()
        data = path.read_bytes()
        main, chunks = parse_chunks(data)
        models = parse_models(chunks)
        palette = next((c.body for c in chunks if c.tag == b"RGBA"), b"")
        if len(palette) != 1024:
            raise ValueError("Scene needs an RGBA palette")
        instances = parse_instances(chunks, len(models))
        return cls(path, data, main, chunks, models, palette, material_bodies(chunks), instances)

    @property
    def sha256(self):
        return hashlib.sha256(self.data).hexdigest()


@dataclass(frozen=True)
class Tile:
    key: str
    family: str
    set_name: str
    path: Path
    model: Model
    palette: bytes
    materials: dict[int, bytes]


@dataclass(frozen=True)
class Variant:
    model: Model
    pivot_shift: tuple[int, int, int]


def tile_family(name: str):
    return re.sub(r"_damaged_[A-Za-z0-9]+$", "", name)


def load_tiles(folder: Path):
    folder = Path(folder).resolve()
    files = sorted(folder.glob("*.vox"))
    if not files:
        raise ValueError(f"No .vox tiles in {folder}")
    result = []
    for path in files:
        _, chunks = parse_chunks(path.read_bytes())
        models = parse_models(chunks)
        if len(models) != 1:
            raise ValueError(f"Tile {path.name} must have one model")
        palette = next((c.body for c in chunks if c.tag == b"RGBA"), b"")
        if len(palette) != 1024:
            raise ValueError(f"Tile {path.name} has no RGBA palette")
        result.append(Tile(f"{folder.name}/{path.stem}", tile_family(path.stem), folder.name,
                           path, models[0], palette, material_bodies(chunks)))
    return result


@dataclass
class Link:
    model_id: int
    layer: str
    family: str | None
    rotation: int = 0
    candidates: list[str] = field(default_factory=list)
    status: str = "unmatched"
    reference_key: str | None = None


def build_links(scene: VoxScene, tiles: list[Tile]):
    index = {}
    for tile in tiles:
        for turns in range(4):
            index.setdefault(geometry_key(rotate(tile.model, turns)), []).append((tile, turns))
    links = []
    for model_id, layer in sorted({(instance.model_id, instance.layer) for instance in scene.instances}):
        model = scene.models[model_id]
        matches = index.get(geometry_key(model), [])
        families = sorted(set(tile.family for tile, _ in matches))
        if len(families) == 1:
            family = families[0]
            turns = min(r for tile, r in matches if tile.family == family)
            status = "linked"
        else:
            family, turns = None, 0
            status = "ambiguous" if families else "unmatched"
        reference = next((tile.key for tile, r in matches if tile.family == family and r == turns), None)
        links.append(Link(model_id, layer, family, turns, families, status, reference))
    return links


def assign_link(scene: VoxScene, link: Link, selection: str, tiles: list[Tile]):
    model = scene.models[link.model_id]
    selected = next((tile for tile in tiles if tile.key == selection), None)
    family = selected.family if selected else selection
    matches = [(t, r) for t in tiles if (t.key == selection if selected else t.family == family) for r in range(4)
               if geometry_key(rotate(t.model, r)) == geometry_key(model)]
    if matches:
        link.rotation = min(r for _, r in matches)
        link.status = "linked"
    else:
        link.rotation = 0
        link.status = "manual"
    link.family = family
    link.reference_key = selected.key if selected else next((tile.key for tile in tiles if tile.family == family), None)


def palette_map(source: bytes, target: bytes, source_materials=None, target_materials=None):
    source_materials = source_materials or {}
    target_materials = target_materials or {}
    index = {}
    for i in range(255):
        rgba = target[i * 4:i * 4 + 4]
        index.setdefault(rgba, []).append(i + 1)
    result = {}
    for i in range(1, 256):
        candidates = index.get(source[(i - 1) * 4:i * 4], [])
        if i in source_materials:
            candidates = [candidate for candidate in candidates
                          if target_materials.get(candidate) == source_materials[i]]
        result[i] = candidates[0] if candidates else None
    return result


def fitted_variant(original: Model, tile: Tile, turns: int, target_palette: bytes, target_materials=None):
    rotated = rotate(tile.model, turns)
    old_min, _ = original.bounds
    new_min, _ = rotated.bounds
    delta = tuple(old_min[i] - new_min[i] for i in range(3))
    intended = [tuple(v[i] + delta[i] for i in range(3)) for v in rotated.voxels]
    offset = tuple(max(0, -min((p[i] for p in intended), default=0)) for i in range(3))
    size = tuple(max(original.size[i], max((p[i] + offset[i] + 1 for p in intended), default=0)) for i in range(3))
    if any(d > 256 for d in size):
        raise ValueError(f"{tile.key}: variant exceeds MagicaVoxel's 256-voxel model limit")
    pivot_shift = tuple(size[i] // 2 - original.size[i] // 2 - offset[i] for i in range(3))
    remap = palette_map(tile.palette, target_palette, tile.materials, target_materials)
    voxels = []
    for x, y, z, c in rotated.voxels:
        point = (x + delta[0] + offset[0], y + delta[1] + offset[1], z + delta[2] + offset[2])
        mapped = remap[c]
        if mapped is None:
            raise ValueError(f"{tile.key}: color/material not found in scene palette")
        voxels.append((*point, mapped))
    return Variant(Model(size, tuple(voxels)), pivot_shift)


def choose_variants(scene: VoxScene, links: list[Link], tiles: list[Tile], weights: dict[str, int], seed: int):
    by_family = {}
    for tile in tiles:
        by_family.setdefault(tile.family, []).append(tile)
    by_model = {(link.model_id, link.layer): link for link in links}
    choices = {}
    fitted = {}
    issues = []
    requested = sum(max(0, value) for name, value in weights.items() if name != "Original")
    original_weight = max(0, 100 - requested)
    for instance in scene.instances:
        link = by_model.get((instance.model_id, instance.layer))
        if link is None or link.family is None:
            continue
        options = [(None, float(original_weight))]
        available_sets = {}
        for tile in by_family.get(link.family, []):
            weight = max(0, weights.get(tile.set_name, 0))
            if not weight:
                continue
            cache_key = (instance.model_id, tile.key, link.rotation)
            if cache_key not in fitted:
                try:
                    fitted[cache_key] = fitted_variant(scene.models[instance.model_id], tile, link.rotation,
                                                       scene.palette, scene.materials)
                    if any(fitted[cache_key].pivot_shift) and instance.transform_id is None:
                        raise ValueError(f"{tile.key}: no individual transform for this scene object")
                except ValueError as exc:
                    issues.append(str(exc))
                    fitted[cache_key] = None
            if fitted[cache_key] is not None:
                available_sets.setdefault(tile.set_name, []).append(cache_key)
        for set_name, keys in available_sets.items():
            options.extend((key, float(weights[set_name]) / len(keys)) for key in keys)
        total = sum(w for _, w in options)
        if total == 0:
            continue
        # Stable per object: editing one weight doesn't reshuffle unrelated objects.
        nonce = int.from_bytes(hashlib.sha256(f"{seed}:{instance.shape_id}".encode()).digest()[:8], "little")
        draw = random.Random(nonce).random() * total
        for key, weight in options:
            draw -= weight
            if draw < 0:
                if key is not None:
                    choices[instance.shape_id] = key
                break
    return choices, fitted, sorted(set(issues))


def export_scene(scene: VoxScene, output: Path, choices: dict, fitted: dict):
    """Append models and retarget shape nodes; keep all existing chunks verbatim."""
    if not choices:
        Path(output).write_bytes(scene.data)
        return
    used_keys = sorted(set(choices.values()))
    ids = {key: len(scene.models) + i for i, key in enumerate(used_keys)}
    appended = []
    for key in used_keys:
        variant = fitted[key]
        if variant is None:
            raise ValueError(f"Unavailable replacement: {key}")
        model = variant.model
        appended.extend((chunk(b"SIZE", struct.pack("<III", *model.size)),
                         chunk(b"XYZI", struct.pack("<I", len(model.voxels)) + b"".join(bytes(v) for v in model.voxels))))
    by_shape = {instance.shape_id: instance for instance in scene.instances}
    shifts = {}
    for shape_id, key in choices.items():
        instance = by_shape[shape_id]
        shift = fitted[key].pivot_shift
        if any(shift):
            if instance.transform_id is None:
                raise ValueError(f"Shape {shape_id} needs an individual transform")
            shifts[instance.transform_id] = mat_vec(instance.local_axes, shift)
    out = []
    inserted = False
    for item in scene.chunks:
        if not inserted and item.tag == b"nTRN":
            out.extend(appended)
            inserted = True
        if item.tag == b"PACK":
            out.append(chunk(b"PACK", struct.pack("<I", len(scene.models) + len(used_keys)), item.children))
        elif item.tag == b"nSHP":
            node_id, pos = read_i32(item.body, 0)
            if node_id not in choices:
                out.append(item.raw)
                continue
            _, pos = read_dict(item.body, pos)
            count, pos = read_i32(item.body, pos)
            if count != 1:
                raise ValueError("Animated or multi-model shapes cannot be replaced")
            body = item.body[:pos] + struct.pack("<i", ids[choices[node_id]]) + item.body[pos + 4:]
            out.append(chunk(b"nSHP", body, item.children))
        elif item.tag == b"nTRN":
            node_id, pos = read_i32(item.body, 0)
            if node_id not in shifts:
                out.append(item.raw)
                continue
            attrs, pos = read_dict(item.body, pos)
            child, reserved, layer, count = struct.unpack_from("<iiii", item.body, pos)
            pos += 16
            if count != 1:
                raise ValueError("Animated transform cannot be shifted")
            frame, end = read_dict(item.body, pos)
            if end != len(item.body):
                raise ValueError("Malformed transform")
            old = tuple(int(x) for x in frame.get("_t", "0 0 0").split())
            delta = shifts[node_id]
            frame["_t"] = " ".join(str(old[i] + delta[i]) for i in range(3))
            body = (struct.pack("<i", node_id) + write_dict(attrs) +
                    struct.pack("<iiii", child, reserved, layer, count) + write_dict(frame))
            out.append(chunk(b"nTRN", body, item.children))
        else:
            out.append(item.raw)
    if not inserted:
        out.extend(appended)
    children = b"".join(out)
    header = scene.data[:8] + b"MAIN" + struct.pack("<II", len(scene.main_body), len(children))
    Path(output).write_bytes(header + scene.main_body + children)


def project_path(source: Path):
    return Path(str(source) + ".tilemix.json")


def save_project(path: Path, scene: VoxScene, original_folder: Path, variant_folders: list[Path],
                 links: list[Link], weights: dict, seed: int):
    body = {"schema_version": 2, "source_vox": str(scene.path), "source_sha256": scene.sha256,
            "original_folder": str(original_folder),
            "variant_folders": [str(p) for p in variant_folders], "weights": weights, "seed": seed,
            "links": {f"{link.model_id}|{link.layer}": {"family": link.family, "reference_key": link.reference_key,
                       "rotation": link.rotation,
                       "source_geometry_sha256": geometry_hash(scene.models[link.model_id])} for link in links}}
    Path(path).write_text(json.dumps(body, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load_project(path: Path, scene: VoxScene):
    body = json.loads(Path(path).read_text(encoding="utf-8"))
    if body.get("schema_version") not in (1, 2) or body.get("source_sha256") != scene.sha256:
        raise ValueError("Link file belongs to another version of this VOX scene")
    if body["schema_version"] == 1:
        folders = body.get("tile_folders", [])
        if not folders:
            raise ValueError("Link file contains no original tile folder")
        old_weights = body.get("weights", {})
        total = max(1, sum(max(0, int(value)) for value in old_weights.values()))
        body["original_folder"] = folders[0]
        body["variant_folders"] = folders[1:]
        body["weights"] = {Path(folder).name: round(100 * max(0, int(old_weights.get(Path(folder).name, 0))) / total)
                           for folder in folders[1:]}
    for key, saved in body.get("links", {}).items():
        i = int(key.split("|", 1)[0])
        if i >= len(scene.models) or saved.get("source_geometry_sha256") != geometry_hash(scene.models[i]):
            raise ValueError(f"Model {i} changed since linking")
    return body
