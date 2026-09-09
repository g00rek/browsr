import base64
import hashlib
import importlib.util
import json
import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("bridge_repository", ROOT / "bridge.py")
bridge = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bridge)


class RepositoryTests(unittest.TestCase):
    def test_extension_key_matches_native_host_origin(self):
        manifest = json.loads((ROOT / "extension/manifest.json").read_text())
        public_key = base64.b64decode(manifest["key"])
        digest = hashlib.sha256(public_key).digest()[:16]
        extension_id = "".join(chr(ord("a") + nibble) for byte in digest for nibble in (byte >> 4, byte & 15))
        self.assertEqual(extension_id, bridge.EXTENSION_ID)

    def test_versions_match(self):
        import tomllib

        plugin = tomllib.loads((ROOT / "herdr-plugin.toml").read_text())
        extension = json.loads((ROOT / "extension/manifest.json").read_text())
        self.assertEqual(plugin["version"], extension["version"])
        self.assertEqual(plugin["name"], extension["name"])

    def test_executables_have_shebangs(self):
        for filename in ("bridge.py", "native_host.py", "browsr-chrome-mcp.py"):
            self.assertTrue((ROOT / filename).read_text().startswith("#!/usr/bin/python3\n"))


if __name__ == "__main__":
    unittest.main()
