# NorthStar: Prefab ResolvePlan

更新时间：2026-07-01
状态：Implemented (first pass)

## 目标

实现一套新的 Prefab 解析调度层：默认可读性优先，在不隐藏 diff、不牺牲节点/组件名还原质量的前提下，减少重复 git tree 查询，让 SourceGit / Fork 中的大 prefab diff 打开更稳定、更可解释。

当前 `prefab_textconv.py` 的解析方式偏“边解析边查询”：遇到一个 GUID、fileID 或历史缺失目标时再查一次。缓存虽然存在，但查询计划不集中，容易在 nested prefab、Prefab Override、旧版本 fileID 还原时产生重复 git 查询和宽泛历史扫描。

新的目标是引入 `ResolvePlan`：

```text
先统计需要解析的目标
-> 按层批量查询
-> 将结果写入 DP 缓存
-> 发现新依赖后进入下一层
-> 渲染阶段只读缓存与 fallback
```

## 北极星原则

- 可读性优先：默认尽量还原节点名、层级路径、组件名和字段名。
- 不静默隐藏：无法唯一还原时必须显示可追踪 fallback，如 `PrefabOverrides/<prefab>#<fileID>`。
- 精确优先：当前精确命中优先于历史精确命中，历史精确命中优先于相似路径猜测。
- 不缓存 HTML 报告：只缓存解析中间结果，避免报告内容与用户正在查看的 diff 脱节。
- 分层展开：默认不设置最大层级；用 visited / memo 防循环。用户可通过参数显式限制层级。
- 批量优先，并行其次：先合并查询，再用有界并行预取；避免把一个个 resolver 调用直接丢进线程池。
- 结果确定：并行只负责填缓存，最终解析优先级必须由单一规则决定。

## 术语

- ResolvePlan：一次 prefab diff 解析过程中形成的查询计划。
- ResolveCache：解析中间结果缓存，类似 DP memo。
- Resolve Layer：从根 prefab 开始按 nested prefab / override target 依赖逐层展开的层级。
- Exact Hit：通过当前版本或历史版本 fileID 精确命中的节点、路径或组件。
- Similar Hint：无法精确命中时，根据同名节点、后缀路径和组件名做出的相似路径提示。
- Fallback：最终无法唯一还原时展示的 GUID/fileID 可追踪文本。

## 查询计划数据模型

建议新增解析调度对象，先以内部类或小模块落地，后续再拆文件：

```text
ResolvePlan
  project_root
  old_rev
  new_rev
  roots
  pending_by_layer
  visited_assets
  visited_targets
  cache

ResolveCache
  prefab_path_by_guid[(rev, guid)] -> asset_path | not_found
  prefab_blob_by_asset[(rev, asset_path)] -> yaml_text | not_found
  prefab_index_by_guid[(rev, guid)] -> PrefabIndex | partial | not_found
  target_by_fileid[(rev, guid, fileID)] -> TargetInfo | not_found | partial
  history_target_by_fileid[(rev, guid, fileID)] -> TargetInfo | not_found | partial
  fileid_assets_by_hint[(rev, hint_dir, fileIDs_batch)] -> [asset_path]

PrefabIndex
  names[fileID] -> node_name
  paths[fileID] -> hierarchy_path
  components[fileID] -> component_name
  nested_prefab_guids -> set[guid]
  override_targets -> set[(guid, fileID)]

TargetInfo
  name
  path
  component
  source_kind: current | nested | history | similar | fallback
  source_rev
  source_asset_path
```

## 分层算法

核心算法是 BFS，而不是递归临时查询。

```text
1. 解析 old/new 根 prefab YAML，收集根节点、组件、PrefabInstance、override target。
2. 将根 prefab 中直接出现的 nested prefab GUID 和 override target 加入 layer 1。
3. 对当前 layer 做批量查询：
   3.1 批量解析 GUID -> prefab path。
   3.2 批量读取 rev:asset_path 的 YAML blob。
   3.3 批量构建 PrefabIndex。
   3.4 批量解析当前层 target fileID。
   3.5 对当前层仍缺失的 fileID，按 guid 分组做 targeted history search。
   3.6 对仍缺失的 fileID，按 hint_dir 分组做跨 prefab fileID 搜索。
4. 将查询结果写入 ResolveCache。
5. 从新建的 PrefabIndex 中发现下一层 nested prefab 和 override target。
6. 如果下一层全部已 visited 或 cache 命中，则停止；否则进入下一层。
7. 渲染或结构化输出阶段按固定优先级读取 ResolveCache。
```

默认不设置 `max_depth`。循环依赖通过以下 key 去重：

```text
visited_assets: (rev, guid)
visited_targets: (rev, guid, fileID)
visited_history_targets: (rev, guid, fileID)
```

如果用户显式传参：

```text
PREFAB_DIFF_MAX_RESOLVE_DEPTH=2
```

则只解析根 prefab 后向外展开两层依赖。空值或 `0` 表示不限层级。

## Targeted History Search

历史搜索必须按缺失目标驱动，而不是为了构建完整历史缓存而扫满所有版本。

输入：

```text
(rev, guid, missing_fileIDs)
```

流程：

```text
1. 找到 guid 对应 prefab asset_path。
2. rev-list --max-count=N rev -- asset_path。
3. 逐个历史 rev 读取 prefab blob。
4. 每读一个 rev 只检查 missing_fileIDs 是否命中。
5. 命中的写入 history_target_by_fileid。
6. missing_fileIDs 全部找齐后立即停止。
7. 扫完仍未命中的写入 negative cache 或 partial cache。
```

缓存规则：

- 精确找不到：写入 `not_found`，避免反复查。
- 因预算、显式 depth、Git 异常或用户参数停止：写入 `partial`，不能当成真实不存在。
- 下次预算更高或参数不同，`partial` 允许重新查询。

## 批量与并行策略

批量维度：

- `guid -> prefab path` 按 rev 分组。
- `rev:asset_path` blob 读取按 rev 分组。
- `fileID grep` 按 `(rev, hint_dir)` 分组。
- history search 按 `(rev, guid)` 分组，批量处理该 guid 下缺失的多个 fileID。

并行边界：

- 可以并行：不同 `(rev, guid)` 的 prefab path 查找、blob 读取、PrefabIndex 构建、history search。
- 必须有界：默认 worker 建议为 4，可通过参数调整。
- 不共享同一个 `git cat-file --batch` 进程跨线程读写。
- 不并行执行 `convert(old)` 与 `convert(new)` 共享全局 `_asset_resolver` 的旧模式；应先把 resolver context 局部化或让 ResolvePlan 接管。

建议参数：

```text
PREFAB_DIFF_RESOLVE_WORKERS=4
PREFAB_DIFF_MAX_RESOLVE_DEPTH=0
PREFAB_DIFF_MAX_GIT_LOOKUPS=2
PREFAB_DIFF_MAX_FILEID_LOOKUPS=32
PREFAB_DIFF_MAX_HISTORY_ASSETS=8
PREFAB_DIFF_HISTORY_REVS=64
PREFAB_DIFF_FILEID_GREP_BATCH_SIZE=64
```

默认策略：

- `PREFAB_DIFF_MAX_RESOLVE_DEPTH=0`：不限层级。
- 其他预算默认继续可读性优先。
- 用户如果需要更快打开，可以显式降低预算。

## 解析优先级

所有并行和缓存结果最终必须按固定顺序决策：

```text
1. 当前 prefab 精确命中
2. 当前层 nested prefab 精确命中
3. 历史版本精确命中
4. 相似路径猜测
5. Fallback
```

相似路径猜测不得覆盖任何 exact hit。

## 与现有代码的落点

预计改动文件：

- `prefab_textconv.py`
  - 新增 ResolvePlan / ResolveCache / PrefabIndex / TargetInfo。
  - 将 `GitTreeAssetResolver` 从“即时查询器”逐步改为“计划执行器”或被 ResolvePlan 包装。
  - 将 `_legacy_prefab_index`、`_prefill_similar_prefab_target_hints`、`prefab_target_label/path/component` 接到 ResolveCache。
  - 避免继续扩大全局 `_asset_resolver` 的职责。
- `prefab_fork_diff.py`
  - 如需要，传递 old/new rev 与 project_root 给 ResolvePlan。
  - 不改变 SourceGit old/new 顺序约定。
- `README.md`
  - 补充 ResolvePlan、参数和可读性优先策略。
- `docs/html-data-contract.md`
  - 如输出数据新增 source_kind 或 fallback 标记，再同步说明。

## 实施路线

### Phase 1：抽取数据结构与缓存

- 新增 ResolveCache、PrefabIndex、TargetInfo。
- 不改变外部输出。
- 将现有缓存 key 统一为 `(rev, guid)`、`(rev, guid, fileID)`。
- 验证：现有真实 prefab diff 输出不出现 parse error。

### Phase 2：Inventory Pass

- 在 convert 主流程中先收集 root prefab 的 nested guid 和 override target。
- 不立即深查，只生成第一层 ResolvePlan。
- 验证：统计出的 GUID/fileID 数量与当前即时解析链路一致或更多。

### Phase 3：Layered Prefetch

- 实现按层 BFS 展开。
- 每层批量构建 PrefabIndex。
- 发现新依赖后继续下一层。
- 默认不限层级，用 visited 防循环。
- 验证：构造 A->B->A 循环依赖不重复查询、不死循环。

### Phase 4：Targeted History Search

- 将历史解析改为按 missing_fileIDs 早停。
- 命中后写入 history target cache。
- 区分 `not_found` 与 `partial`。
- 验证：历史精确命中优先于相似猜测。

### Phase 5：有界并行

- 对同层不同 `(rev, guid)` 查询加有界线程池。
- 禁止共享同一个 batch cat-file 进程跨线程读写。
- 并行只填缓存，最终渲染顺序保持确定。
- 验证：多次运行同一 diff 结果稳定。

### Phase 6：真实项目回归

使用 `F:\nslgF` 的真实提交验证：

- `cb0e1e9fc64c9b130075ef851cb7b3c72f9e2f1b`
- `48703c355092`
- `f2ccc6d37b8d2b88578ecc9ba0a1ad05861944d6`

验证项：

- 无 `parse error`。
- 无 `_format_rgba_color` 报错。
- nested prefab override 尽量显示节点名、层级和组件名。
- fallback 可见且可追踪。
- `MainUI.prefab` 耗时低于当前深解析基线。

## 验收标准

- 默认配置可读性优先，不因默认 depth 限制丢失旧节点名还原。
- 解析过程中同一 `(rev, guid)` 只构建一次 PrefabIndex。
- 同一 `(rev, guid, fileID)` 只做一次精确 target 解析。
- history search 对缺失 fileID 找齐即停。
- Git 查询按组批量执行，fileID grep 自动分批。
- 有界并行不会改变 diff 结果。
- 无法还原时显示 fallback，不静默隐藏。
- SourceGit old/new 顺序保持正确。

## 不做范围

- 不缓存最终 HTML 报告。
- 不改变 HTML 主体交互和视觉。
- 不改变 SourceGit / Fork 的调用协议。
- 不为了性能默认降低可读性预算。

## 当前基线

最近一次真实 SourceGit 形态验证：

- `MapPreviewPathUI.prefab`：约 6.7 秒。
- `MainUI.prefab`：约 66.5 秒。

该基线来自可读性优先深解析默认值。后续优化目标是在保持可读性优先的前提下降低 `MainUI.prefab` 这类大 prefab 的耗时。

## 当前实现进度

2026-07-01 已完成第一轮实现：

- 新增 `TargetInfo`、`PrefabIndex`、`ResolveCache` 基础结构。
- 新增 history target DP 缓存：`(rev, guid, fileID)` 精确命中后复用。
- 新增 targeted history search：只针对当前缺失的 fileID 扫历史，找齐即停。
- Prefab Override 当前版本无法命中时，先批量预取历史精确命中，再进入相似路径猜测。
- fileID grep 保持分批执行，避免 Windows 参数过长导致整页 parse error。
- `prefab_target_label/path/component` 不再主动触发完整 legacy history 扫描；先走 targeted history，旧 legacy 结果只在已经缓存时兜底。
- 旧的 nested prefab 递归硬编码 5 层上限已移除，默认无限层级，用 visited 防循环；`PREFAB_DIFF_MAX_RESOLVE_DEPTH` 仍可显式限制。
- 保留根文件 target inventory / prefetch 入口，但默认关闭。真实回归显示提前预取所有根 target 会让部分 prefab 变慢，应等旧渲染查询链彻底替换后再作为分层调度默认路径。
- 新增 `ResolvePlan` 调度对象：集中管理 seed targets、按层 PrefabIndex 构建、缺失 target history search、可选相似提示预取。
- Prefab Override 渲染中的 unresolved targets 在单个 PrefabInstance 内提交给 `ResolvePlan`，按 GUID 分组执行，避免同一节点内逐个查。
- targeted history search 支持 `PREFAB_DIFF_RESOLVE_WORKERS` 有界并行；worker 不共享 `git cat-file --batch`，并通过父 resolver 共享预算计数。
- `prefab_index()` 改为保留 nested/remap 后的完整漂亮名索引，同时额外记录依赖集合，避免直接索引覆盖完整可读性缓存。
- 默认 Prefab Override 渲染前会批量收集当前 prefab 内实际出现的 PrefabInstance override target，并交给 `ResolvePlan` 统一处理。它只覆盖当前 diff 输入中已经出现的 override，不递归追踪源 prefab 内部所有 override target；`PREFAB_DIFF_PREFETCH_PI_TARGETS=0` 可退回逐实例懒查询。
- 新增 nested prefab XOR remap 相关性追踪：仅对实际未解析 target 计算 `original = remapped ^ prefabInstanceFileID` 的下一层候选，并可通过 `PREFAB_DIFF_MAX_REMAP_CANDIDATES` 控制预算。默认只做 current index 命中，不跑 history；`PREFAB_DIFF_REMAP_HISTORY=1` 才允许 remap 候选进入 targeted history search。
- SourceGit 接入优先使用 renderer 传入的 `$REPO/$PATH/$BASE/$TARGET/$COMMIT` 或对应环境变量。有 repo/path/revision 时直接从 git 读取内容；缺 revision、读不到对象或本地/暂存/stash 等无法用提交表达的场景，再回退到 `$OLD/$NEW` 临时文件。禁止把随机临时目录名当成 revision；如确实需要当前 `HEAD` 辅助还原，可显式设置 `PREFAB_DIFF_HEAD_HISTORY_FALLBACK=1`。
- 同一轮 PrefabInstance override unresolved target 默认先批量收集，再按 source prefab 分组做 history target 查询。多个 source prefab 的历史 revision 通过一次 `git log --name-only` 批量拿回，再拆回 `(rev, asset_path)` DP 缓存，避免逐 prefab `rev-list`。
- 相似路径猜测默认关闭。它只产生 guessed hint，不是精确还原，且在 `MainUI.prefab` 这类大文件上会触发昂贵 `git grep`。默认保留 `PrefabOverrides/<prefab>#<fileID>` fallback；需要临时排查旧资源迁移时设置 `PREFAB_DIFF_SIMILARITY_HINTS=1`。

当前验证结果：

- 默认 worker=1：
  - `MapPreviewPathUI.prefab`：约 3.8 秒，无 `parse error` / `_format_rgba_color` 报错 / `UnknownTarget` / `未解析`。
  - `MainUI.prefab`：约 21.0 秒，无 `parse error` / `_format_rgba_color` 报错 / `UnknownTarget` / `未解析`。
  - `Activity_SpringFestival25_SignInUI.prefab`：约 0.7 秒，无 `parse error` / `_format_rgba_color` 报错 / `UnknownTarget` / `未解析`。
- `PREFAB_DIFF_RESOLVE_WORKERS=4`：
  - `MapPreviewPathUI.prefab`：约 3.6 秒，无 `parse error` / `_format_rgba_color` 报错 / `UnknownTarget` / `未解析`。
- `PREFAB_DIFF_PREFETCH_ROOT_TARGETS=1` + workers=4：
  - `MapPreviewPathUI.prefab`：约 14.5 秒，无 `parse error` / `_format_rgba_color` 报错 / `UnknownTarget`。
- SourceGit 随机临时目录：
  - `MainUI.prefab`：从约 32.5 秒降到约 10.5 秒，无 `parse error` / `UnknownTarget` / `未解析`。
  - `AgainCityUI.prefab` 本地变更：约 0.4 秒，无 `parse error` / `UnknownTarget` / `未解析`。
- SourceGit 完整参数：
  - commit diff 会使用 `$BASE/$TARGET` 作为 old/new revision，`$REPO/$PATH` 作为项目根和真实资源路径线索。
  - working copy diff 在 `$BASE/$TARGET` 为空时不做 git tree history search，避免把临时目录误识别为提交。
  - 当 `$BASE/$TARGET` 都是有效提交时，即使 `$OLD/$NEW` 是临时文件，也优先从 git 对象库读取 prefab 内容。
- `48703c355092109f74eb2208ac4c216ddb1ce843` / `MainUI.prefab`：
  - 修复前 SourceGit 完整参数形态约 35-36 秒，主要耗时为逐 source prefab 的 `git rev-list` 和 similarity `git grep`。
  - 批量 history + 默认关闭 similarity guess 后约 10.9 秒。
  - 验证无 `parse error` / `UnknownTarget` / `未解析`，且坏 `$OLD/$NEW` temp 内容未进入 HTML，内容来源为 `old=git, new=git`。

暂未默认启用完整 root BFS prefetch。直接在 `convert()` 开头展开所有 nested prefab 或根文件所有 target，会提前查询过多不影响当前 diff 的依赖，曾导致 `MapPreviewPathUI.prefab` 上升到 14-20 秒；在 Prefab Override 渲染路径追踪源 prefab 内部所有 override target，也会让 `MainUI.prefab` 上升到约 36 秒。默认路径已启用“实际缺失 target -> current-only XOR remap -> grouped targeted history”的相关依赖分层；更昂贵的 remap history 作为显式参数保留。
