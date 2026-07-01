import json
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path


SOURCEGIT_ARGS = (
    '"$OLD" "$NEW" '
    '--repo "$REPO" --path "$PATH" --context "$CONTEXT" --mode "$MODE" '
    '--base "$BASE" --target "$TARGET" --commit "$COMMIT" --title "$TITLE"'
)


def _preference_path() -> Path:
    appdata = os.environ.get("APPDATA") or ""
    if not appdata:
        raise RuntimeError("APPDATA is not set.")
    return Path(appdata) / "SourceGit" / "preference.json"


def _find_renderer(renderers):
    for renderer in renderers:
        if renderer.get("Name") == "Unity Prefab":
            return renderer
    for renderer in renderers:
        executable = str(renderer.get("Executable") or "").replace("\\", "/").lower()
        if executable.endswith("/fork_diff.cmd") or executable.endswith("/prefab_fork_diff.py"):
            return renderer
    return None


def configure(tool_dir: Path):
    preference_path = _preference_path()
    if not preference_path.exists():
        raise RuntimeError(f"SourceGit preference file not found: {preference_path}")

    executable = (tool_dir / "fork_diff.cmd").resolve()
    if not executable.exists():
        raise RuntimeError(f"fork_diff.cmd not found: {executable}")

    backup_path = preference_path.with_name(
        preference_path.name + f".bak-{datetime.now():%Y%m%d-%H%M%S}-prefab-diff"
    )
    shutil.copy2(preference_path, backup_path)

    data = json.loads(preference_path.read_text(encoding="utf-8-sig"))
    renderers = data.get("CustomDiffRenderers")
    if not isinstance(renderers, list):
        renderers = []
        data["CustomDiffRenderers"] = renderers

    renderer = _find_renderer(renderers)
    if renderer is None:
        renderer = {}
        renderers.append(renderer)

    renderer.update(
        {
            "IsEnabled": True,
            "Name": "Unity Prefab",
            "Patterns": "*.prefab;*.unity",
            "Executable": str(executable),
            "Arguments": SOURCEGIT_ARGS,
            "ClearPreviousContentOnLoad": True,
        }
    )

    preference_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    print(f"Updated:    {preference_path}")
    print(f"Backup:     {backup_path}")
    print(f"Executable: {executable}")
    print(f"Arguments:  {SOURCEGIT_ARGS}")


def main(argv):
    tool_dir = Path(argv[1]).resolve() if len(argv) > 1 else Path(__file__).resolve().parent
    configure(tool_dir)


if __name__ == "__main__":
    main(sys.argv)
