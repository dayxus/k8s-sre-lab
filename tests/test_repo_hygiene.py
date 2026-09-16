"""Repository hygiene: the parts of the deliverable that are prose and layout.

These tests are cheap and they catch the failure mode this kind of repository is most
likely to have: a README that describes a lab that is no longer true, a generated file
that drifted from its source, a phrase left in from a template, or a secret committed
"just for testing".
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest
from conftest import REPO_ROOT

readme_en = REPO_ROOT / "README.md"
readme_pt = REPO_ROOT / "README.pt-BR.md"

REQUIRED_SECTIONS = [
    "## What it does",
    "## Why it matters for SRE",
    "## Architecture",
    "## Quickstart",
    "## Verify it yourself",
    "## Automated maintenance",
    "## Project layout",
    "## Limitations and next steps",
]

REQUIRED_FILES = [
    "README.md",
    "README.pt-BR.md",
    "LICENSE",
    ".gitignore",
    "Makefile",
    "pyproject.toml",
    "requirements-dev.txt",
    ".kube-linter.yaml",
    ".github/workflows/ci.yml",
    ".github/workflows/maintenance.yml",
    "app/Dockerfile",
    "app/server.py",
    "chart/Chart.yaml",
    "chart/values.yaml",
    "chart/values-dev.yaml",
    "chart/values-prod.yaml",
    "docs/reliability-patterns.md",
    "docs/chaos-experiments.md",
    "docs/versions.md",
    "chaos/README.md",
    "manifests/base/kustomization.yaml",
    "manifests/overlays/dev/kustomization.yaml",
    "manifests/overlays/prod/kustomization.yaml",
    "scripts/validate.sh",
    "scripts/e2e-kind.sh",
    "scripts/install-tools.sh",
    "scripts/maintenance.sh",
    "tests/test_manifests.py",
    "tests/test_chart.py",
    "tests/test_app.py",
    "tools/versions.env",
]

# Anything that makes a commit look machine-authored, and anything left unfinished.
FORBIDDEN_PHRASES = [
    "generated with",
    "co-authored-by",
    "as an ai",
    "chatgpt",
    "copilot",
    "claude.ai",
    "\U0001f916",
    "lorem ipsum",
    "example.com",
    "TODO",
    "FIXME",
    "XXX:",
    "coming soon",
    "placeholder",
]

TEXT_SUFFIXES = (".md", ".py", ".yaml", ".yml", ".sh", ".toml", ".txt", ".tpl", ".env")
SKIP_DIRS = {
    ".git",
    ".tools",
    ".venv",
    "build",
    "artifacts",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
}

SECRET_PATTERNS = [
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"gh[pous]_[A-Za-z0-9]{20,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(
        r"(?i)\b(password|passwd|secret_key|api_key|access_token)\s*[:=]\s*['\"][^'\"]{6,}['\"]"
    ),
]


def repo_text_files():
    """Every tracked-ish text file, with the heavy directories pruned (not filtered)."""
    for root, dirnames, filenames in os.walk(REPO_ROOT):
        dirnames[:] = [name for name in dirnames if name not in SKIP_DIRS]
        for filename in sorted(filenames):
            path = Path(root) / filename
            if path.suffix in TEXT_SUFFIXES or filename in ("Makefile", "LICENSE"):
                yield path


@pytest.mark.parametrize("relative", REQUIRED_FILES)
def test_required_file_exists(relative: str):
    path = REPO_ROOT / relative
    assert path.is_file(), "%s is missing" % relative
    assert path.stat().st_size > 0, "%s is empty" % relative


def test_readme_has_the_required_sections_in_order():
    text = readme_en.read_text()
    positions = []
    for section in REQUIRED_SECTIONS:
        assert section in text, "README.md has no %r section" % section
        positions.append(text.index(section))
    assert positions == sorted(positions), "README.md sections are out of order"


def test_readme_pt_br_mirrors_the_english_structure():
    en_headings = re.findall(r"^## (.+)$", readme_en.read_text(), flags=re.M)
    pt_headings = re.findall(r"^## (.+)$", readme_pt.read_text(), flags=re.M)
    assert len(pt_headings) == len(en_headings), (
        "README.pt-BR.md has %d sections, README.md has %d" % (len(pt_headings), len(en_headings))
    )
    for translated in (
        "## O que ele faz",
        "## Por que isso importa para SRE",
        "## Arquitetura",
        "## Início rápido",
        "## Verifique você mesmo",
        "## Manutenção automatizada",
        "## Estrutura do projeto",
        "## Limitações e próximos passos",
    ):
        assert translated in readme_pt.read_text(), "README.pt-BR.md has no %r section" % translated


def test_readme_has_no_unfilled_evidence():
    """The verify section must contain real captured output, not a promise."""
    text = readme_en.read_text()
    section = text.split("## Verify it yourself", 1)[1].split("\n## ", 1)[0]
    blocks = re.findall(r"```[a-z]*\n(.*?)```", section, flags=re.S)
    assert blocks, "the verify section has no fenced code block"
    longest = max(blocks, key=lambda block: len(block.strip().splitlines()))
    assert len([line for line in longest.splitlines() if line.strip()]) >= 3, (
        "the verify section does not quote real multi-line command output"
    )


def test_readme_footer_links_the_portfolio():
    text = readme_en.read_text()
    assert "Part of the [dayxus SRE portfolio](https://github.com/dayxus)." in text
    assert "README.pt-BR.md" in text
    assert "README.md" in readme_pt.read_text()


def test_badges_point_at_this_repository():
    text = readme_en.read_text()
    for badge in ("actions/workflows/ci.yml", "img.shields.io"):
        assert badge in text, "README.md has no %s badge" % badge
    assert "dayxus/k8s-sre-lab" in text


def test_license_is_mit_for_the_right_holder():
    text = (REPO_ROOT / "LICENSE").read_text()
    assert "MIT License" in text
    assert "Copyright (c) 2026 Jeferson Melo" in text


@pytest.mark.parametrize("phrase", FORBIDDEN_PHRASES)
def test_no_forbidden_phrase_anywhere(phrase: str):
    lowered = phrase.lower()
    offenders = []
    for path in repo_text_files():
        if path.name == "test_repo_hygiene.py":
            continue
        if lowered in path.read_text(errors="replace").lower():
            offenders.append(str(path.relative_to(REPO_ROOT)))
    assert not offenders, "%r found in: %s" % (phrase, ", ".join(offenders))


def test_no_secrets_are_committed():
    for path in repo_text_files():
        text = path.read_text(errors="replace")
        for pattern in SECRET_PATTERNS:
            match = pattern.search(text)
            assert not match, "possible secret in %s: %s" % (
                path.relative_to(REPO_ROOT),
                match.group(0),
            )


def test_no_floating_image_tags_in_manifests_or_chart():
    """INV5 covers pod specs; this also covers the Dockerfile's base image."""
    offenders = []
    for path in list((REPO_ROOT / "manifests").rglob("*.yaml")):
        for line in path.read_text().splitlines():
            if re.match(r"\s*image:\s*\S+:latest\s*$", line):
                offenders.append(str(path.relative_to(REPO_ROOT)))
    dockerfile = (REPO_ROOT / "app" / "Dockerfile").read_text()
    assert re.search(r"^FROM \S+:\S+", dockerfile, flags=re.M), (
        "the base image is not pinned to a tag"
    )
    assert ":latest" not in dockerfile
    assert not offenders, offenders


def test_versions_doc_is_generated_from_the_pinned_versions():
    import sys

    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    from update_versions import VERSIONS_DOC, parse_env, render_versions_md

    values = parse_env((REPO_ROOT / "tools" / "versions.env").read_text())
    expected = render_versions_md(values)
    actual = VERSIONS_DOC.read_text()
    assert expected == actual, "docs/versions.md is stale; run scripts/update_versions.py --write"


def test_shell_scripts_are_executable_and_have_a_shebang():
    scripts = sorted((REPO_ROOT / "scripts").glob("*.sh"))
    assert scripts, "no shell scripts found"
    for script in scripts:
        assert script.stat().st_mode & 0o111, "%s is not executable" % script.name
        assert script.read_text().startswith("#!/usr/bin/env bash"), (
            "%s has no bash shebang" % script.name
        )


def test_python_entry_points_have_a_shebang_and_no_interactive_only_constructs():
    for name in ("render.py", "invariants_report.py", "audit.py", "update_versions.py"):
        text = (REPO_ROOT / "scripts" / name).read_text()
        assert text.startswith("#!/usr/bin/env python3"), "%s has no python3 shebang" % name
        assert "while True: input(" not in text


def test_workflows_declare_the_conventions_permissions():
    ci = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text()
    maintenance = (REPO_ROOT / ".github" / "workflows" / "maintenance.yml").read_text()
    assert "on:" in ci and "workflow_dispatch:" in ci and "pull_request:" in ci
    assert "actions/checkout@v4" in ci
    assert "actions/setup-python@v5" in ci
    assert "schedule:" in maintenance and "workflow_dispatch:" in maintenance
    assert "contents: write" in maintenance
    assert "issues: write" in maintenance


def test_maintenance_commits_only_on_a_real_diff():
    text = (REPO_ROOT / "scripts" / "maintenance.sh").read_text()
    assert "git diff --quiet" in text
    assert "gh issue create" in text
