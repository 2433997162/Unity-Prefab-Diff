# Prefab Diff Tool

Unity prefab/scene diff helper for Fork and SourceGit. It converts Unity YAML into a hierarchy-aware HTML report so prefab node, component, and override changes are easier to review.

## Entry Points

- `fork_diff.cmd`: external diff entry for GUI Git clients.
- `prefab_fork_diff.py`: compare two prefab/scene files and render one HTML report.
- `prefab_commit_diff.py`: inspect prefab/scene changes in a commit or local working tree.
- `prefab_textconv.py`: convert Unity YAML into stable structured text.
- `prefab_html_renderer.py` and `prefab_diff_template.html`: render report data into HTML.

## Project Root

Pass the Unity project root explicitly when possible:

```powershell
python prefab_fork_diff.py --project-root C:\Path\To\UnityProject old.prefab new.prefab
```

The tool also reads `PREFAB_DIFF_PROJECT_ROOT`, `PREFAB_DIFF_PROJECT_ROOTS`, and `UNITY_PROJECT_ROOT`. If no explicit root is available, it tries to infer one from an `Assets` path or the current working directory. That inference is only a compatibility fallback for Git clients that pass temporary files; when multiple Unity projects are possible, pass `--project-root` instead of relying on discovery.

## SourceGit Embedded Mode

When `SOURCEGIT_CUSTOM_DIFF_TEMP` is set, the report uses embedded mode and writes the generated HTML path to stdout for SourceGit to load.
