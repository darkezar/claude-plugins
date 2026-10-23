"""Проверяет полноту и переносимость устанавливаемых пакетов Codex."""

import json
import re
import unittest
from pathlib import Path
from urllib.parse import unquote, urlsplit


ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins/review-kit"
CONTEXT_PLUGIN = ROOT / "plugins/context-kit"


class CodexPackageTest(unittest.TestCase):
    def test_every_claude_entrypoint_is_available_in_codex(self):
        # Независимый список исходных возможностей: новый Claude skill/command тоже
        # потребует адаптации. Регрессия, из-за которой установленный пакет не имел ship.
        expected = {p.parent.name for p in (PLUGIN / "skills").glob("*/SKILL.md")}
        expected.update(p.stem for p in (PLUGIN / "commands").glob("*.md"))
        manifest = json.loads((PLUGIN / ".codex-plugin/plugin.json").read_text())
        entrypoints = list((PLUGIN / manifest["skills"]).glob("*/SKILL.md"))
        actual = set()
        for path in entrypoints:
            with self.subTest(skill=path.parent.name):
                name = re.search(r"^name: ([a-z0-9-]+)$", path.read_text(), re.MULTILINE)
                self.assertIsNotNone(name)
                self.assertEqual(path.parent.name, name[1])
                self.assertNotIn(name[1], actual)
                actual.add(name[1])
        self.assertEqual(expected, actual)

    def test_context_kit_exposes_audit_and_refresh_as_codex_skills(self):
        manifest = json.loads((CONTEXT_PLUGIN / ".codex-plugin/plugin.json").read_text())
        skills = (CONTEXT_PLUGIN / manifest["skills"]).resolve()
        self.assertTrue(skills.is_relative_to(CONTEXT_PLUGIN))
        entrypoints = list(skills.glob("*/SKILL.md"))
        names = []
        for path in entrypoints:
            with self.subTest(skill=path.parent.name):
                name = re.search(r"^name: ([a-z0-9-]+)$", path.read_text(), re.MULTILINE)
                self.assertIsNotNone(name)
                self.assertEqual(path.parent.name, name[1])
                names.append(name[1])
        self.assertCountEqual(["context-audit", "context-refresh"], names)

    def test_context_kit_keeps_claude_entrypoints(self):
        for relative in (
            "commands/context-init.md",
            "commands/context-refresh.md",
            "skills/context-audit/SKILL.md",
        ):
            with self.subTest(entrypoint=relative):
                self.assertTrue((CONTEXT_PLUGIN / relative).is_file())

    def test_codex_resources_are_contained_and_resolve_inside_the_package(self):
        # Устанавливается только plugin directory, без checkout и абсолютных путей автора.
        for plugin in (PLUGIN, CONTEXT_PLUGIN):
            self.check_resources(plugin)

    def check_resources(self, plugin):
        manifest = json.loads((plugin / ".codex-plugin/plugin.json").read_text())
        pending = list((plugin / manifest["skills"]).rglob("*.md"))
        visited = set()
        while pending:
            path = pending.pop().resolve()
            if path in visited:
                continue
            visited.add(path)
            for target in re.findall(r"\]\(([^)\s]+)\)", path.read_text()):
                url = urlsplit(target)
                if url.scheme or url.netloc or not url.path:
                    continue
                resource = (path.parent / unquote(url.path)).resolve()
                with self.subTest(
                    plugin=plugin.name, source=str(path.relative_to(plugin)), target=target
                ):
                    self.assertTrue(resource.is_relative_to(plugin))
                    self.assertTrue(resource.is_file(), str(resource))
                if resource.is_file() and resource.suffix == ".md":
                    pending.append(resource)

    def test_clients_share_one_release_and_marketplace_source(self):
        for plugin in (PLUGIN, CONTEXT_PLUGIN):
            with self.subTest(plugin=plugin.name):
                codex = json.loads((plugin / ".codex-plugin/plugin.json").read_text())
                claude = json.loads((plugin / ".claude-plugin/plugin.json").read_text())
                self.assertEqual(claude["name"], codex["name"])
                self.assertEqual(claude["version"], codex["version"])
                marketplace = json.loads((ROOT / ".agents/plugins/marketplace.json").read_text())
                entry = next(p for p in marketplace["plugins"] if p["name"] == codex["name"])
                self.assertEqual(plugin, (ROOT / entry["source"]["path"]).resolve())


if __name__ == "__main__":
    unittest.main()
