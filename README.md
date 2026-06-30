# Unity Prefab Diff

用于 Fork 和 SourceGit 的 Unity Prefab / Scene 差异查看工具。它会把 Unity YAML 转成带层级结构的 HTML 报告，方便查看节点、组件属性和 Prefab Override 的变化。

## 入口脚本

- `fork_diff.cmd`：GUI Git 客户端的外部 diff 入口。
- `prefab_fork_diff.py`：对比两个 prefab / scene 文件并生成 HTML 报告。
- `prefab_commit_diff.py`：查看某个提交或本地工作区中的 prefab / scene 变更。
- `prefab_textconv.py`：把 Unity YAML 转成稳定、可比较的结构化文本。
- `prefab_html_renderer.py` 和 `prefab_diff_template.html`：把 diff 数据渲染为 HTML。

## Unity 项目根目录

建议尽量显式传入 Unity 项目根目录：

```powershell
python prefab_fork_diff.py --project-root C:\Path\To\UnityProject old.prefab new.prefab
```

工具也会读取 `PREFAB_DIFF_PROJECT_ROOT`、`PREFAB_DIFF_PROJECT_ROOTS` 和 `UNITY_PROJECT_ROOT`。如果没有显式项目根，会尝试从 `Assets` 路径或当前工作目录推断。这个推断只是为了兼容 Git 客户端传入临时文件的场景；当本机存在多个 Unity 项目时，优先传 `--project-root`，不要依赖自动发现。

## SourceGit 内嵌模式

当存在 `SOURCEGIT_CUSTOM_DIFF_TEMP` 环境变量时，工具会使用内嵌模式，并把生成的 HTML 路径输出到 stdout，供 SourceGit 加载。

推荐的 SourceGit 自定义 diff renderer 配置：

- 可执行文件：`<fork-diff>\fork_diff.cmd`
- 命令行参数：`"$OLD" "$NEW"`

SourceGit 也提供 `$LOCAL` 和 `$REMOTE`，但在 custom diff renderer 里 `$LOCAL` 表示新文件，`$REMOTE` 表示旧文件。本工具的参数顺序是旧文件在前、新文件在后，因此推荐使用 `$OLD` 和 `$NEW`。
