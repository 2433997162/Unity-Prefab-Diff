# HTML Data Contract

本文档说明 `prefab_diff_template.html` 收到的数据结构，以及在进入 HTML 前已经由
`prefab_textconv.py` / `prefab_fork_diff.py` 特殊处理过的字段。以后只改 HTML 样式或交互时，应优先参考这里的契约，不要假设字段仍然等同于 Unity 原始 YAML。

## 数据流

```text
Unity YAML
  -> prefab_textconv.py      解析、归一化、语义化
  -> prefab_fork_diff.py     转成节点属性表并计算差异
  -> prefab_html_renderer.py 注入 JSON 到 HTML 模板
  -> prefab_diff_template.html
```

HTML 模板拿到的是 `window.PREFAB_DIFF_DATA`，其中：

- `schemaVersion`：当前为 `1`。
- `reportMode`：`full` 或 `embed`。
- `summary`：报告摘要。
- `reports[]`：每个 prefab / scene 的报告。
- `reports[].added[]` / `removed[]`：新增、删除节点。
- `reports[].modified[]`：属性变更节点。
- `oldPaths` / `newPaths`：旧版、新版完整节点路径列表。
- `renamedPaths[]`：通过内部节点身份匹配到的路径变更，形如 `{"old": "...", "new": "..."}`。

节点和属性值最终都是字符串。HTML 可以做展示增强，但不应把它们当作原始 YAML 重新解析。

## Report Item

每个 `reports[]` 大致形态：

```json
{
  "id": "report-0",
  "filename": "Example.prefab",
  "added": [
    {
      "path": "Root/Panel/Button",
      "name": "Button",
      "status": "added",
      "props": [{"key": "TMP_Text.m_text", "value": "OK"}]
    }
  ],
  "removed": [],
  "modified": [
    {
      "path": "Root/Panel/Text",
      "name": "Text",
      "status": "modified",
      "added": [{"key": "TMP_Text.m_outlineColor", "value": "rgba(...) ..."}],
      "removed": [],
      "changed": [{"key": "RectTransform.m_SizeDelta", "old": "{x: 1, y: 2}", "new": "{x: 3, y: 2}"}]
    }
  ],
  "oldPaths": ["Root/Panel/Text"],
  "newPaths": ["Root/Panel/Text"],
  "counts": {
    "addedNodes": 1,
    "removedNodes": 0,
    "modifiedNodes": 1,
    "changedProperties": 2
  }
}
```

## Property Key 约定

属性 key 通常是：

```text
ComponentName.FieldName
```

HTML 当前用第一个 `.` 拆分组件名和字段名。例如：

- `RectTransform.m_SizeDelta`
- `TMP_Text.m_text`
- `LocalizeProperty.m_records[RectTransform.anchoredPosition].m_pairs[kr]`

以下 key 是特殊约定。

### `__id__` / `__parent_id__`

这两个字段是 textconv 生成的内部节点身份元数据，不是 Unity YAML 原始字段。

```text
__id__: go:123456
__parent_id__: go:654321
```

用途：

- `__id__` 用于在节点改名后继续识别同一个节点，避免误报为删除旧节点、新增新节点。
- `__parent_id__` 用于识别同名节点被移动到其它父节点的情况。
- 默认 HTML 会过滤这两个字段，不把它们渲染到 `Properties` 组。

当只有节点名字变化时，diff 会生成：

```text
[Name]: OldName -> NewName
```

当同名节点父级发生变化时，diff 会生成：

```text
[Parent]: OldParentPath -> NewParentPath
```

### `__flags__`

`__flags__` 是 textconv 生成的节点状态元数据，不是 Unity YAML 里的原始字段。
它仍然是报告数据的一部分，但默认 HTML 不把它当作属性行显示：

```text
__flags__: [PrefabOverride]
```

展示规则：

- 对新增 / 删除节点，`__flags__` 可能会跟随节点属性进入 `props[]`，模板层会过滤它，不渲染到 `Properties` 组。
- 对同一路径的修改节点，`__flags__` 不按普通组件字段比较；如果 old/new flags 不同，会生成下面的 `[Flags]` 行。
- 因此“不是 Unity 原始字段”表示它只能作为节点状态元数据使用，不能按组件字段或策划字段理解。

常见 flags：

- `[Prefab]`
- `[PrefabOverride]`
- `[Orphan]`
- `[Inactive]`
- `[tag:xxx]`

### `[Flags]`

当节点 flags 在 old/new 之间变化时，diff 会生成一条报告行：

```text
[Flags]: old -> new
```

HTML 应把它当作节点状态变化，而不是组件字段。样式上可以单独渲染成 badge 或状态摘要，但不要把它当作原始 Unity 字段。

### `m_records[...]`

Unity LocalizeProperty 的 `m_records` 会被语义化：

```text
LocalizeProperty.m_records[RectTransform.anchoredPosition].m_pairs[kr]
```

含义：

- `LocalizeProperty`：组件名。
- `RectTransform.anchoredPosition`：被本地化控制的组件字段。
- `kr`：语言 key；空 key 会显示为 `default`。

这类 key 可能来自两种 YAML 形态：

- 普通组件里的 `m_records` + `references.RefIds`。
- PrefabInstance override 里的 `m_records.Array.data[...]` + `managedReferences[...]`。

HTML 不需要理解 Unity 的 `rid`、`Array.data` 或 `managedReferences`，只展示语义化后的 key。

### `managedReference[rid:...]`

当 `references.RefIds` 无法归属到某个 `m_records` pair 时，会保留可追踪 fallback：

```text
SomeComponent.managedReference[rid:4202733963761156249]
```

这不是最佳展示形态，但比静默隐藏更安全。HTML 应保持可见。

### `UnresolvedManagedReference`

当 record 指向的 rid 找不到或无法摘要时，value 会显示：

```text
UnresolvedManagedReference:4202733963761156249
```

HTML 应保持可见，不要过滤。

## Property Value 特殊格式

### Vector 分量

Unity 常见的向量字段会从独立分量合并成一个字段：

```text
m_SizeDelta.x: 168
m_SizeDelta.y: 40
```

HTML 收到：

```text
key:   RectTransform.m_SizeDelta
value: {x: 168, y: 40}
```

合并规则：

- `x/y` 同时存在时合并为 Vector2 形态。
- `x/y/z` 同时存在时合并为 Vector3 形态。
- `x/y/z/w` 同时存在时合并为 Vector4 形态。
- 只有单个分量变化时保留原字段，例如 `m_SizeDelta.x`。

### 颜色值

颜色会被格式化成以 `rgba(...)` 开头的字符串。HTML 可以通过 `rgba(...)` 识别并渲染色块。

#### 浮点 RGBA 分量

Unity 原始字段：

```text
m_Color.r: 0.78431374
m_Color.g: 0.78431374
m_Color.b: 0.78431374
m_Color.a: 0.5019608
```

HTML 收到：

```text
key:   TMP_Text.m_Color
value: rgba(200, 200, 200, 0.502) #C8C8C880 {r: 0.78431374, g: 0.78431374, b: 0.78431374, a: 0.5019608}
```

只有同一个 color/tint 字段同时出现 `r/g/b/a` 四个分量时才合并；如果只改了 `m_Color.a`，会保留原字段。

#### Inline RGBA 对象

Unity 原始字段：

```text
m_TintColor: {r: 1, g: 0, b: 0.5, a: 1}
```

HTML 收到：

```text
value: rgba(255, 0, 128, 1) #FF0080FF {r: 1, g: 0, b: 0.5, a: 1}
```

只会格式化完整、简单的 `{r, g, b, a}` 对象；如果同一个 inline 对象里还有其它字段，会保留原文。

#### TMP packed `.rgba`

TMP 常见字段：

```text
m_outlineColor.rgba: 1711276032
m_underlayColor.rgba: 3858759680
```

HTML 收到：

```text
key:   TMP_Text.m_outlineColor
value: rgba(0, 0, 0, 0.4) #00000066 {rgba: 1711276032, hex: 0x66000000, r: 0, g: 0, b: 0, a: 102}

key:   TMP_Text.m_underlayColor
value: rgba(0, 0, 0, 0.902) #000000E6 {rgba: 3858759680, hex: 0xE6000000, r: 0, g: 0, b: 0, a: 230}
```

packed `.rgba` 使用 Unity/TMP 的低位 RGBA 顺序解析：

```text
raw = 0xAABBGGRR
r = raw & 0xFF
g = (raw >> 8) & 0xFF
b = (raw >> 16) & 0xFF
a = (raw >> 24) & 0xFF
```

HTML 当前只依赖开头的 `rgba(...)` 渲染色块，后面的 `#RRGGBBAA` 和 `{...}` 是给人看的追踪信息。

### fileID 引用

本地 fileID 引用会尽量翻译：

```text
{fileID:123456 -> Root/Panel/Button/RectTransform}
```

外部 asset guid 引用只做轻量缩写：

```text
{fileID:21300000, guid:abcdef12...}
```

如果无法解析，本地 fileID 会保留：

```text
{fileID:123456}
```

HTML 不应过滤这些 fallback。

### Unicode 转义

形如 `"\u67E5\u770B"` 的字符串会在 textconv 阶段解码成可读中文。HTML 通常不需要再做 Unicode 转译。

## 节点路径特殊形态

### Nested Prefab

嵌套 prefab 节点会把源 prefab 名带在路径里：

```text
Root/List/ItemRender (ItemRender.prefab)/Text
```

### PrefabOverride

PrefabInstance override 会尽量映射到源 prefab 的真实节点路径：

```text
Root/List/ItemRender (ItemRender.prefab)/Text
```

如果目标无法唯一解析，会进入可追踪 fallback：

```text
Root/List/ItemRender (ItemRender.prefab)/PrefabOverrides/ItemRender.prefab#123456
```

### Orphan

当节点无法挂回完整层级但仍需要保留时，会出现：

```text
Orphan/SomeNode
```

HTML 应保持可见。`Orphan` 代表解析 fallback，不等于一定是业务新增或删除。

## textconv 已过滤的噪声

以下字段通常不会进入 HTML：

- Unity 基础元数据：`m_ObjectHideFlags`、`m_PrefabAsset`、`m_GameObject` 等。
- 节点名、active、layer 等已经体现在节点头或 flags 中的字段。
- `references` 原始块。
- Unity managed reference 的纯 `rid` 编号。
- PrefabInstance override 里的 `managedReferences[...]` 原始编号。

注意：过滤不是隐藏未知数据。无法语义化但可能有价值的数据，应通过 fallback key/value 保留。

## 修改 HTML 时的建议

1. 只根据 `schemaVersion` 和本文档约定处理数据。
2. 不要依赖 Unity 原始 YAML 字段一定存在。
3. 对 `rgba(...)` 这种增强值可以做 UI 特化，但必须保留完整文本。
4. 对 `managedReference[rid:...]`、`UnresolvedManagedReference`、`UnknownTarget`、`Orphan` 不要静默过滤。
5. 新增特殊格式时，同时更新本文档，并尽量提供一条真实提交或最小样例。
