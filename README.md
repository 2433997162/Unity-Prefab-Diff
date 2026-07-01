# Unity Prefab Diff

通用 Unity Prefab / Scene HTML Diff 工具。它会把 Unity YAML 转成带层级结构的 HTML 报告，重点展示节点增删、节点改名、组件属性变化和 Prefab Override 变化。

这个项目不绑定某个 Unity 项目，也不绑定某个 Git 客户端。你可以直接生成 HTML，也可以把它嵌入 SourceGit、Fork 或其他工具。

## 入口脚本

- `prefab_diff.py`：核心入口，对比两个 `.prefab` / `.unity` 文件并生成 HTML。
- `prefab_diff.cmd`：Windows 外部 diff 入口，适合给 Fork、SourceGit 或其他 GUI Git 客户端调用。
- `prefab_commit_diff.py`：按 Git 提交或本地工作区批量生成 prefab / scene 变更报告。
- `prefab_textconv.py`：把 Unity YAML 转成稳定、可比较、带可读名称的结构化文本。
- `prefab_html_renderer.py`、`prefab_diff_template.html`：将 diff 数据注入 HTML 模板。
- `integrations/sourcegit/`：SourceGit 自动配置脚本，属于可选集成。

## 直接生成 HTML

最简单用法：

```powershell
python prefab_diff.py old.prefab new.prefab
```

默认会生成完整 HTML 并自动用浏览器打开。也可以指定输出路径：

```powershell
python prefab_diff.py --output C:\Temp\prefab_diff.html --no-open old.prefab new.prefab
```

如果你的 old/new 文件来自 Git 客户端临时目录，建议显式传入 Unity 项目根目录，方便工具解析脚本名、嵌套 prefab 和 fileID：

```powershell
python prefab_diff.py --project-root C:\Path\To\UnityProject old.prefab new.prefab
```

工具也会读取 `PREFAB_DIFF_PROJECT_ROOT`、`PREFAB_DIFF_PROJECT_ROOTS` 和 `UNITY_PROJECT_ROOT`。自动发现只用于兜底；同一台机器上有多个 Unity 项目时，优先显式传参。

## 嵌入到其他工具

外部宿主可以传入 old/new 文件，也可以同时传入仓库、仓库内路径和 revision。只要给了有效的 `--repo + --path + --base/--target/--commit`，工具会优先从 Git object 读取真实内容，临时文件只作为兜底。

```powershell
python prefab_diff.py `
  "$OLD" "$NEW" `
  --embed `
  --repo "$REPO" `
  --path "$PATH" `
  --base "$BASE" `
  --target "$TARGET" `
  --commit "$COMMIT" `
  --output-dir "$TEMP" `
  --print-output `
  --no-open
```

常用通用参数：

- `--full` / `--embed`：完整页面或内嵌页面模式。
- `--output <html>`：输出到指定 HTML 文件。
- `--output-dir <dir>`：输出到目录，文件名由工具生成。
- `--print-output`：把生成的 HTML 绝对路径输出到 stdout。
- `--no-open`：生成后不自动打开浏览器。
- `--repo <repo>`：Git 仓库根目录。
- `--path <path>`：当前 diff 的仓库内文件路径。
- `--base <rev>`：old/base revision。
- `--target <rev>`：new/target revision。
- `--commit <sha>`：单提交 diff 场景的目标提交；`--mode commit` 时若未传 `--base`，会自动使用 `<commit>~1`。
- `--title <text>`：diff 标题，缺文件名时用于展示。
- `--host <name>`：宿主名，仅用于行为兼容和日志定位。

通用环境变量与参数同名：

- `PREFAB_DIFF_OLD`、`PREFAB_DIFF_NEW`
- `PREFAB_DIFF_REPO`、`PREFAB_DIFF_PATH`
- `PREFAB_DIFF_BASE`、`PREFAB_DIFF_TARGET`、`PREFAB_DIFF_COMMIT`
- `PREFAB_DIFF_TITLE`、`PREFAB_DIFF_CONTEXT`、`PREFAB_DIFF_MODE`
- `PREFAB_DIFF_OUTPUT`、`PREFAB_DIFF_OUTPUT_DIR`、`PREFAB_DIFF_TEMP`
- `PREFAB_DIFF_PRINT_OUTPUT`、`PREFAB_DIFF_NO_OPEN`、`PREFAB_DIFF_HOST`

命令行参数优先级高于环境变量。

## SourceGit 集成

SourceGit 只是一个可选 adapter。自动配置脚本在：

```text
integrations/sourcegit/configure_sourcegit_prefab_diff.cmd
```

双击后会备份 `%APPDATA%\SourceGit\preference.json`，再把 `Unity Prefab` renderer 配成：

- 可执行文件：`<Unity-Prefab-Diff>\prefab_diff.cmd`
- 命令行参数：

```text
"$OLD" "$NEW" --repo "$REPO" --path "$PATH" --context "$CONTEXT" --mode "$MODE" --base "$BASE" --target "$TARGET" --commit "$COMMIT" --title "$TITLE"
```

工具会兼容读取 SourceGit 注入的 `SOURCEGIT_CUSTOM_DIFF_*` 环境变量，并映射到同一套通用 `DiffContext`。SourceGit 的 `$LOCAL` 是 new，`$REMOTE` 是 old；本工具参数顺序始终是 old 在前、new 在后，所以配置里使用 `$OLD` 和 `$NEW`。

## Fork 集成

Fork 通常只需要把 `prefab_diff.cmd` 配成外部 diff 工具入口，并按 old/new 顺序传入两个文件：

```text
<Unity-Prefab-Diff>\prefab_diff.cmd "$OLD" "$NEW"
```

如果你使用的 Fork 版本或二次封装能提供 repo/path/revision 信息，建议额外传入通用参数，这样工具可以跳过不可靠的临时目录推断，直接从 Git object 读取 old/new 内容。

## HTML 模板与数据契约

- HTML 样式和交互主要改 `prefab_diff_template.html`。
- 注入 HTML 的 JSON 数据由 `prefab_html_renderer.py` 生成。
- 特殊处理过的字段和值见 [HTML 数据契约](docs/html-data-contract.md)。

以后只改页面样式时，优先改 HTML 模板；只有数据含义或解析结果需要变化时，再改 Python。

## 可读性解析参数

工具默认优先可读性，会尽量把 nested prefab override 里的 GUID/fileID 还原成具体节点名、层级和组件名。以下参数用于控制解析深度、历史搜索和性能预算：

- `PREFAB_DIFF_MAX_GIT_LOOKUPS` 默认 `2`：限制 GUID 到 prefab 路径的反查次数。
- `PREFAB_DIFF_MAX_FILEID_LOOKUPS` 默认 `32`：限制跨 prefab 的 fileID 搜索次数。
- `PREFAB_DIFF_MAX_HISTORY_ASSETS` 默认 `8`：最多回溯多少个嵌套 prefab 的历史版本。
- `PREFAB_DIFF_HISTORY_REVS` 默认 `64`：历史回溯时，每个 prefab 最多看的提交数。
- `PREFAB_DIFF_MAX_RESOLVE_DEPTH` 默认 `0`：nested prefab 解析不设层级上限；显式设置为正数时限制展开深度。
- `PREFAB_DIFF_RESOLVE_WORKERS` 默认 `1`：targeted history search 的有界并行 worker 数。
- `PREFAB_DIFF_BATCH_HISTORY` 默认 `1`：同一轮多个 nested prefab 的 history target 查询合并成一次 `git log --name-only`。
- `PREFAB_DIFF_PREFETCH_PI_TARGETS` 默认 `1`：渲染前批量收集当前 prefab 内 PrefabInstance override 的 unresolved target。
- `PREFAB_DIFF_MAX_REMAP_CANDIDATES` 默认 `32`：nested prefab 的 XOR fileID 反向映射候选数。
- `PREFAB_DIFF_REMAP_HISTORY` 默认关闭：设为 `1` 时，XOR remap 候选允许 targeted history search。
- `PREFAB_DIFF_HEAD_HISTORY_FALLBACK` 默认关闭：避免打开历史提交时拿当前 `HEAD` 做不准确且昂贵的辅助解析。
- `PREFAB_DIFF_SIMILARITY_HINTS` 默认关闭：只保留可追踪 fallback；需要排查旧资源迁移时可临时设为 `1`。
- `PREFAB_DIFF_SIMILARITY_HISTORY` 默认关闭：仅在 `PREFAB_DIFF_SIMILARITY_HINTS=1` 时有意义，通常更慢。

预算耗尽时报告会保留 `PrefabOverrides/<prefab>#<fileID>` 这类可追踪 fallback，不会静默隐藏变更。
