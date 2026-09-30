import tempfile
import unittest
from pathlib import Path
import struct
import json

from vox_mixer.core import (
    Model, VoxScene, assign_link, build_links, choose_variants, chunk,
    export_scene, fitted_variant, load_project, load_tiles, project_path,
    save_project, write_dict,
)


def tiny_vox(path: Path, models, shape_models, extra=b""):
    palette = bytes((0, 0, 0, 255)) * 256
    palette = bytes((0, 0, 0, 255)) + bytes((180, 150, 120, 255)) + palette[8:]
    children = chunk(b"PACK", struct.pack("<I", len(models)))
    for model in models:
        children += chunk(b"SIZE", struct.pack("<III", *model.size))
        children += chunk(b"XYZI", struct.pack("<I", len(model.voxels)) + b"".join(bytes(v) for v in model.voxels))
    children += chunk(b"META", extra)
    # Root group -> two independently positioned shapes that initially share model 0.
    children += chunk(b"nTRN", struct.pack("<i", 0) + write_dict({}) +
                      struct.pack("<iiii", 1, -1, -1, 1) + write_dict({"_r": "4"}))
    children += chunk(b"nGRP", struct.pack("<i", 1) + write_dict({}) +
                      struct.pack("<i", len(shape_models)) +
                      b"".join(struct.pack("<i", 2 + i * 2) for i in range(len(shape_models))))
    for i, model_id in enumerate(shape_models):
        node = 2 + i * 2
        children += chunk(b"nTRN", struct.pack("<i", node) + write_dict({"_name": f"Tile_{i}"}) +
                          struct.pack("<iiii", node + 1, -1, 0, 1) +
                          write_dict({"_r": "4", "_t": f"{i * 10} 0 0"}))
        children += chunk(b"nSHP", struct.pack("<i", node + 1) + write_dict({}) +
                          struct.pack("<ii", 1, model_id) + write_dict({}))
    children += chunk(b"LAYR", struct.pack("<i", 0) + write_dict({"_name": "Structure_Walls"}) + struct.pack("<i", -1))
    children += chunk(b"RGBA", palette)
    path.write_bytes(b"VOX " + struct.pack("<I", 150) + chunk(b"MAIN", b"", children))


class MixerCoreTests(unittest.TestCase):
    def test_shared_instances_can_receive_different_variants_without_losing_chunks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = Model((2, 1, 2), ((0, 0, 0, 2), (0, 0, 1, 2)))
            damage = Model((2, 3, 2), ((0, 0, 0, 2), (0, 1, 1, 2), (0, 2, 0, 2)))
            source = root / "scene.vox"
            tiny_vox(source, [original], [0, 0], b"preserve this metadata")
            folder = root / "tiles_damaged_A"
            folder.mkdir()
            tiny_vox(folder / "wall_damaged_A.vox", [damage], [0])
            scene = VoxScene.load(source)
            tiles = load_tiles(folder)
            links = build_links(scene, tiles)
            assign_link(scene, links[0], "wall", tiles)
            variant = fitted_variant(original, tiles[0], 0, scene.palette)
            self.assertEqual(variant.model.size, (2, 3, 2))
            self.assertNotEqual(variant.pivot_shift, (0, 0, 0))
            choices = {scene.instances[1].shape_id: (0, tiles[0].key, 0)}
            fitted = {(0, tiles[0].key, 0): variant}
            output = root / "scene_mixed.vox"
            export_scene(scene, output, choices, fitted)
            result = VoxScene.load(output)
            self.assertEqual(len(result.instances), 2)
            self.assertEqual(result.instances[0].model_id, 0)
            self.assertEqual(result.models[0], original)
            self.assertNotEqual(result.instances[1].model_id, 0)
            self.assertIn(b"preserve this metadata", output.read_bytes())
            self.assertEqual(result.instances[1].origin[1] - scene.instances[1].origin[1],
                             variant.pivot_shift[1])
            weights = {folder.name: 100}
            save_project(project_path(output), result, folder, [], build_links(result, tiles), weights, 42)
            self.assertEqual(load_project(project_path(output), result)["weights"], weights)

    def test_linking_flags_identical_shapes_as_ambiguous(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model = Model((1, 1, 1), ((0, 0, 0, 2),))
            source = root / "scene.vox"
            tiny_vox(source, [model], [0])
            folder = root / "tiles"
            folder.mkdir()
            tiny_vox(folder / "slab.vox", [model], [0])
            tiny_vox(folder / "ground.vox", [model], [0])
            scene = VoxScene.load(source)
            tiles = load_tiles(folder)
            links = build_links(scene, tiles)
            self.assertEqual(links[0].status, "ambiguous")
            assign_link(scene, links[0], "ground", tiles)
            self.assertEqual(links[0].family, "ground")
            choices, _, issues = choose_variants(scene, links, tiles, {"Original": 0, "tiles": 100}, 9)
            self.assertEqual(len(choices), 1)
            self.assertEqual(issues, [])

    def test_zero_and_full_mix_percentages(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "scene.vox"
            model = Model((1, 1, 1), ((0, 0, 0, 2),))
            tiny_vox(source, [model], [0, 0, 0])
            folder = root / "damage"
            folder.mkdir()
            tiny_vox(folder / "slab_damaged_A.vox", [model], [0])
            scene = VoxScene.load(source)
            tiles = load_tiles(folder)
            links = build_links(scene, tiles)
            none, _, _ = choose_variants(scene, links, tiles, {"damage": 0}, 42)
            all_changed, _, _ = choose_variants(scene, links, tiles, {"damage": 100}, 42)
            self.assertEqual(len(none), 0)
            self.assertEqual(len(all_changed), len(scene.instances))

    def test_legacy_link_file_separates_original_and_mix_in_folders(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "scene.vox"
            model = Model((1, 1, 1), ((0, 0, 0, 2),))
            tiny_vox(source, [model], [0])
            scene = VoxScene.load(source)
            sidecar = project_path(source)
            sidecar.write_text(json.dumps({"schema_version": 1, "source_sha256": scene.sha256,
                                           "tile_folders": [str(root / "tiles"), str(root / "tiles_damaged_A")],
                                           "weights": {"Original": 70, "tiles": 10, "tiles_damaged_A": 20}}),
                               encoding="utf-8")
            profile = load_project(sidecar, scene)
            self.assertEqual(profile["original_folder"], str(root / "tiles"))
            self.assertEqual(profile["variant_folders"], [str(root / "tiles_damaged_A")])
            self.assertEqual(profile["weights"], {"tiles_damaged_A": 20})


if __name__ == "__main__":
    unittest.main()
