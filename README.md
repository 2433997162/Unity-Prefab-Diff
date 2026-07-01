# Unity Prefab Diff

用于 Fork 和 SourceGit 的 Unity Prefab / Scene 差异查看工具。它会把 Unity YAML 转成带层级结构的 HTML 报告，方便查看节点、组件属性和 Prefab Override 的变化。

## 入口脚本

- `fork_diff.cmd`：GUI Git 客户端的外部 diff 入口。
- `prefab_fork_diff.py`：对比两个 prefab / scene 文件并生成 HTML 报告。
- `prefab_commit_diff.py`：查看某个提交或本地工作区中的 prefab / scene 变更。
- `prefab_textconv.py`：把 Unity YAML 转成稳定、可比较的结构化文本。
- `prefab_html_renderer.py` 和 `prefab_diff_template.html`：把 diff 数据渲染为 HTML。

## 文档

- [HTML 数据契约](docs/html-data-contract.md)：说明注入 HTML 的 JSON 结构，以及进入 HTML 前已经特殊处理过的字段和值。修改 `prefab_diff_template.html` 前建议先看这里。

## Unity 项目根目录

建议尽量显式传入 Unity 项目根目录：

```powershell
python prefab_fork_diff.py --project-root C:\Path\To\UnityProject old.prefab new.prefab
```

工具也会读取 `PREFAB_DIFF_PROJECT_ROOT`、`PREFAB_DIFF_PROJECT_ROOTS` 和 `UNITY_PROJECT_ROOT`。如果没有显式项目根，会尝试从 `Assets` 路径或当前工作目录推断。这个推断只是为了兼容 Git 客户端传入临时文件的场景；当本机存在多个 Unity 项目时，优先传 `--project-root`，不要依赖自动发现。

## SourceGit 内嵌模式

当存在 `SOURCEGIT_CUSTOM_DIFF_TEMP` 环境变量时，工具会使用内嵌模式，并把生成的 HTML 路径输出到 stdout，供 SourceGit 加载。

推荐直接双击 `configure_sourcegit_prefab_diff.cmd` 自动写入 SourceGit 配置。脚本会先备份 `%APPDATA%\SourceGit\preference.json`，再把 `Unity Prefab` renderer 配成：

- 可执行文件：`<fork-diff>\fork_diff.cmd`
- 命令行参数：`"$OLD" "$NEW" --repo "$REPO" --path "$PATH" --context "$CONTEXT" --mode "$MODE" --base "$BASE" --target "$TARGET" --commit "$COMMIT" --title "$TITLE"`

工具也会读取 SourceGit 注入的环境变量，例如 `SOURCEGIT_CUSTOM_DIFF_REPO`、`SOURCEGIT_CUSTOM_DIFF_PATH`、`SOURCEGIT_CUSTOM_DIFF_BASE`、`SOURCEGIT_CUSTOM_DIFF_TARGET` 和 `SOURCEGIT_CUSTOM_DIFF_COMMIT`。命令行参数优先级高于环境变量。

内容读取优先级：

1. 当存在 `$REPO + $PATH + $BASE/$TARGET` 时，工具优先用 `git cat-file blob <rev>:<path>` 从仓库读取 old/new 内容。
2. 某一侧 revision 为空、不是有效提交，或仓库中读不到该路径时，才回退到 SourceGit 导出的 `$OLD/$NEW` 临时文件。
3. `$OLD/$NEW` 只作为内容兜底，不再用于推断提交身份。

SourceGit 模式默认优先可读性，会尽量把 nested prefab override 里的 GUID/fileID 还原成具体节点名、层级和组件名：

- `PREFAB_DIFF_MAX_GIT_LOOKUPS` 默认 `2`：限制 GUID 到 prefab 路径的反查次数。
- `PREFAB_DIFF_MAX_FILEID_LOOKUPS` 默认 `32`：限制跨 prefab 的 fileID 搜索次数。
- `PREFAB_DIFF_MAX_HISTORY_ASSETS` 默认 `8`：最多回溯多少个嵌套 prefab 的历史版本。
- `PREFAB_DIFF_HISTORY_REVS` 默认 `64`：允许历史回溯时，每个 prefab 最多看的提交数。
- `PREFAB_DIFF_MAX_RESOLVE_DEPTH` 默认 `0`：nested prefab 解析不设层级上限；显式设置为正数时限制展开深度。
- `PREFAB_DIFF_RESOLVE_WORKERS` 默认 `1`：targeted history search 的有界并行 worker 数。`PREFAB_DIFF_BATCH_HISTORY=1` 时优先走批量 history；关闭 batch history 后，设置为 `4` 可并行处理同层不同 GUID。并行 worker 不共享 `git cat-file --batch` 进程。
- `PREFAB_DIFF_MAX_REMAP_CANDIDATES` 默认 `32`：nested prefab 的 XOR fileID 反向映射候选数。默认只用当前 prefab index 做便宜命中，不回溯历史；设为 `0` 可完全关闭该层依赖发现。
- `PREFAB_DIFF_REMAP_HISTORY` 默认关闭：设为 `1` 时，XOR remap 候选也允许 targeted history search。该路径可能显著增加大 prefab 的查询量，只建议临时排查旧节点还原时打开。
- `PREFAB_DIFF_HEAD_HISTORY_FALLBACK` 默认关闭：SourceGit 的临时目录通常不是 Git revision，默认不会拿 `HEAD` 强行做 history search，避免打开历史提交时多跑一轮不准确且昂贵的解析。确实需要用当前 `HEAD` 辅助还原旧节点名时，可临时设为 `1`。
- `PREFAB_DIFF_BATCH_HISTORY` 默认 `1`：同一轮多个 nested prefab 的 history target 查询会合并成一次 `git log --name-only`，再按路径拆回 DP 缓存。设为 `0` 可退回逐个 prefab 查询。
- `PREFAB_DIFF_PREFETCH_PI_TARGETS` 默认 `1`：渲染前批量收集当前 prefab 内 PrefabInstance override 的 unresolved target，配合 batch history 降低重复 git 查询。设为 `0` 可退回逐实例懒查询。
- `PREFAB_DIFF_SIMILARITY_HINTS` 默认关闭：相似路径猜测会做额外 `git grep`，且不是精确还原。默认只保留可追踪 fallback；需要排查旧资源迁移时可临时设为 `1`。
- `PREFAB_DIFF_SIMILARITY_HISTORY` 默认关闭：仅在 `PREFAB_DIFF_SIMILARITY_HINTS=1` 时有意义，允许相似路径猜测额外扫描 source prefab 历史索引，通常更慢。

这些限制只影响“把缺失 fileID 猜回具体节点名/路径”的深度解析。预算耗尽时报告仍会保留 `PrefabOverrides/<prefab>#<fileID>` 这类可追踪 fallback，不会静默隐藏变更。需要更快打开但可以接受较少旧节点名还原时，可以临时调低这些环境变量；大量 unresolved fileID 会自动分批搜索，避免 Windows 参数过长导致整页解析失败。

解析策略默认保持懒加载：当前 prefab 精确命中失败时，先按 `(guid, fileID)` 做 targeted history search，找到就直接复用 DP 缓存，找不到再进入相似路径提示或 fallback。`PREFAB_DIFF_PREFETCH_ROOT_TARGETS=1` 可以打开根文件目标预取实验开关，但真实大 prefab 中可能提前查询不少最终不影响当前 diff 可读性的目标，因此默认关闭。

SourceGit 也提供 `$LOCAL` 和 `$REMOTE`，但在 custom diff renderer 里 `$LOCAL` 表示新文件，`$REMOTE` 表示旧文件。本工具的参数顺序是旧文件在前、新文件在后，因此推荐使用 `$OLD` 和 `$NEW`。
