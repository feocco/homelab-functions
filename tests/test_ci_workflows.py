from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def indented_block(document: str, marker: str) -> list[str]:
    lines = document.splitlines()
    start = lines.index(marker)
    indentation = len(marker) - len(marker.lstrip())
    block: list[str] = []
    for line in lines[start + 1 :]:
        if not line.strip():
            continue
        if len(line) - len(line.lstrip()) <= indentation:
            break
        block.append(line)
    return block


def test_pull_request_workflow_is_read_only_and_runs_the_suite():
    workflow = (ROOT / ".github/workflows/tests.yml").read_text(encoding="utf-8")

    assert indented_block(workflow, "on:") == ["  pull_request:"]
    assert indented_block(workflow, "permissions:") == ["  contents: read"]
    test_job = indented_block(workflow, "  test:")
    assert "    runs-on: ubuntu-latest" in test_job
    assert "        run: ./.venv/bin/pytest -q" in test_job
    assert "    permissions:" not in test_job
    assert "if:" not in workflow
    assert "continue-on-error:" not in workflow
    assert "write" not in workflow


def test_container_registry_write_permission_is_build_job_scoped():
    workflow = (ROOT / ".github/workflows/container.yml").read_text(encoding="utf-8")
    assert indented_block(workflow, "permissions:") == ["  contents: read"]
    build_job = indented_block(workflow, "  build:")
    permissions_at = build_job.index("    permissions:")
    assert build_job[permissions_at + 1 : permissions_at + 3] == [
        "      contents: read",
        "      packages: write",
    ]
    assert "      packages: write" in build_job
    assert workflow.count("packages: write") == 1
