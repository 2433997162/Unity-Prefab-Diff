#!/usr/bin/env python3
"""
Prefab/Scene External Diff Tool - 变更可视化

将两个 .prefab/.unity 文件的差异以 HTML 形式展示：
- 新增/删除的节点
- 属性变更（按节点分组）
- PrefabInstance Override 变更

可直接生成 HTML，也可通过 prefab_diff.cmd 嵌入 SourceGit、Fork 或其他 Git 客户端。
"""

import sys
import os
import tempfile
import webbrowser
import subprocess
import re
from collections import OrderedDict
from dataclasses import dataclass

TOOL_DIR = os.path.dirname(os.path.abspath(__file__))
from prefab_html_renderer import (
    REPORT_MODE_EMBED,
    REPORT_MODE_FULL,
    generate_prefab_html,
    _normalize_report_mode,
)

_PROJECT_SEARCH_SKIP = frozenset({"Library", "Temp", "Build", "Builds", "Logs", "obj", ".git", "node_modules"})
_GIT_REV_VALIDATION_CACHE = {}


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    return value.strip().lower() not in {"0", "false", "no", "off"}


def _argv_requests_stdout(argv) -> bool:
    for arg in argv:
        lower = (arg or "").lower()
        if lower == "--print-output":
            return True
    return False


_IS_SOURCEGIT_CUSTOM_DIFF = bool(os.environ.get("SOURCEGIT_CUSTOM_DIFF_TEMP"))
_IS_GENERIC_EMBEDDED_DIFF = bool(
    os.environ.get("PREFAB_DIFF_TEMP")
    or os.environ.get("PREFAB_DIFF_OUTPUT_DIR")
    or _env_bool("PREFAB_DIFF_PRINT_OUTPUT")
)
_IS_EMBEDDED_DIFF = _IS_SOURCEGIT_CUSTOM_DIFF or _IS_GENERIC_EMBEDDED_DIFF
_WANTS_STDOUT = _IS_EMBEDDED_DIFF or _argv_requests_stdout(sys.argv[1:])


@dataclass
class DiffContext:
    old_path: str = ""
    new_path: str = ""
    repo: str = ""
    path: str = ""
    title: str = ""
    context: str = ""
    mode: str = ""
    base: str = ""
    target: str = ""
    commit: str = ""
    output: str = ""
    output_dir: str = ""
    print_output: bool = False
    no_open: bool = False
    host: str = ""

    @classmethod
    def from_env(cls):
        generic_output_dir = os.environ.get("PREFAB_DIFF_OUTPUT_DIR") or os.environ.get("PREFAB_DIFF_TEMP") or ""
        sourcegit_output_dir = os.environ.get("SOURCEGIT_CUSTOM_DIFF_TEMP") or ""
        host = os.environ.get("PREFAB_DIFF_HOST") or ("sourcegit" if _IS_SOURCEGIT_CUSTOM_DIFF else "")
        is_embedded = bool(host or generic_output_dir or sourcegit_output_dir)
        return cls(
            old_path=os.environ.get("PREFAB_DIFF_OLD") or os.environ.get("SOURCEGIT_CUSTOM_DIFF_OLD") or "",
            new_path=os.environ.get("PREFAB_DIFF_NEW") or os.environ.get("SOURCEGIT_CUSTOM_DIFF_NEW") or "",
            repo=os.environ.get("PREFAB_DIFF_REPO") or os.environ.get("SOURCEGIT_CUSTOM_DIFF_REPO") or "",
            path=os.environ.get("PREFAB_DIFF_PATH") or os.environ.get("SOURCEGIT_CUSTOM_DIFF_PATH") or "",
            title=os.environ.get("PREFAB_DIFF_TITLE") or os.environ.get("SOURCEGIT_CUSTOM_DIFF_TITLE") or "",
            context=os.environ.get("PREFAB_DIFF_CONTEXT") or os.environ.get("SOURCEGIT_CUSTOM_DIFF_CONTEXT") or "",
            mode=os.environ.get("PREFAB_DIFF_MODE") or os.environ.get("SOURCEGIT_CUSTOM_DIFF_MODE") or "",
            base=os.environ.get("PREFAB_DIFF_BASE") or os.environ.get("SOURCEGIT_CUSTOM_DIFF_BASE") or "",
            target=os.environ.get("PREFAB_DIFF_TARGET") or os.environ.get("SOURCEGIT_CUSTOM_DIFF_TARGET") or "",
            commit=os.environ.get("PREFAB_DIFF_COMMIT") or os.environ.get("SOURCEGIT_CUSTOM_DIFF_COMMIT") or "",
            output=os.environ.get("PREFAB_DIFF_OUTPUT") or "",
            output_dir=generic_output_dir or sourcegit_output_dir,
            print_output=_env_bool("PREFAB_DIFF_PRINT_OUTPUT", is_embedded),
            no_open=_env_bool("PREFAB_DIFF_NO_OPEN", is_embedded),
            host=host,
        )

    def apply_cli(self, values: dict):
        for key, value in values.items():
            if value is not None and hasattr(self, key):
                setattr(self, key, value)

    def repo_file_path(self) -> str:
        if not self.repo or not self.path:
            return ""
        return os.path.abspath(os.path.join(self.repo, self.path.replace("/", os.sep)))

    def revisions(self):
        old_rev = (self.base or "").strip()
        new_rev = (self.target or "").strip()
        commit = (self.commit or "").strip()
        if commit:
            if not new_rev:
                new_rev = commit
            if not old_rev and (self.mode or "").lower() == "commit":
                old_rev = f"{commit}~1"
        return old_rev, new_rev


# Fork 启动时 stdout 管道可能不被 drain，导致 print 阻塞。
# 嵌入式 renderer 会读取 stdout，因此保留管道并输出 HTML 路径。
if not sys.stdout.isatty() and not _WANTS_STDOUT:
    try:
        _log_path = os.path.join(tempfile.gettempdir(), "prefab_diff.log")
        _log_file = open(_log_path, "w", encoding="utf-8", errors="replace")
        sys.stdout = _log_file
        sys.stderr = _log_file
    except Exception:
        sys.stdout = open(os.devnull, "w")
        sys.stderr = open(os.devnull, "w")
else:
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def read_file(filepath: str) -> str:
    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except Exception:
        return ""


def _git_object_path(repo_path: str) -> str:
    return (repo_path or "").replace("\\", "/").lstrip("/")


def _read_git_file(repo: str, rev: str, repo_path: str) -> str | None:
    repo = os.path.abspath(repo) if repo else ""
    object_path = _git_object_path(repo_path)
    if not repo or not rev or not object_path or not _is_valid_git_rev(repo, rev):
        return None
    try:
        result = subprocess.run(
            ["git", "-C", repo, "cat-file", "blob", f"{rev}:{object_path}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    return result.stdout.decode("utf-8", errors="replace")


def is_prefab_or_scene(filepath: str) -> bool:
    """检测文件是否为 Unity Prefab/Scene YAML"""
    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            head = f.read(512)
        return "%YAML" in head and "!u!" in head
    except Exception:
        return False


_textconv_mod = None

def _get_textconv_mod(hint_path: str = "", project_root: str = ""):
    """懒加载 prefab_textconv 模块，并按当前项目根确保缓存可用"""
    global _textconv_mod
    if _textconv_mod is None:
        import prefab_textconv
        _textconv_mod = prefab_textconv

    resolved_root = _find_project_root(hint_path, project_root)
    if resolved_root:
        _textconv_mod.build_caches(resolved_root)
    return _textconv_mod


def _is_valid_git_rev(project_root: str, rev: str) -> bool:
    if not project_root or not rev:
        return False
    cache_key = (os.path.normcase(os.path.abspath(project_root)), rev)
    if cache_key in _GIT_REV_VALIDATION_CACHE:
        return _GIT_REV_VALIDATION_CACHE[cache_key]
    try:
        result = subprocess.run(
            ["git", "-C", project_root, "rev-parse", "--verify", f"{rev}^{{commit}}"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            text=True,
        )
        is_valid = result.returncode == 0
    except Exception:
        is_valid = False
    _GIT_REV_VALIDATION_CACHE[cache_key] = is_valid
    return is_valid


def _should_use_head_history_fallback(hint_path: str) -> bool:
    if os.environ.get("PREFAB_DIFF_HEAD_HISTORY_FALLBACK") != "1":
        return False
    normalized = (hint_path or "").replace("\\", "/").lower()
    if "/assets/" in normalized or normalized.endswith("/assets"):
        return False
    return _IS_EMBEDDED_DIFF or "sourcegit-custom-diff" in normalized


def convert_to_structured(content: str, hint_path: str = "", git_rev: str = "", project_root: str = "") -> str:
    """用 prefab_textconv 的 convert 函数将 prefab YAML 转为结构化文本"""
    try:
        mod = _get_textconv_mod(hint_path, project_root)
        resolved_root = _find_project_root(hint_path, project_root)
        if git_rev and resolved_root and _is_valid_git_rev(resolved_root, git_rev):
            mod.set_git_tree_context(resolved_root, git_rev)
        elif resolved_root and _should_use_head_history_fallback(hint_path) and _is_valid_git_rev(resolved_root, "HEAD"):
            mod.set_git_tree_context(resolved_root, "HEAD")
        else:
            mod.clear_asset_context()
        return mod.convert(content)
    except Exception as e:
        return f"[parse error: {e}]"
    finally:
        try:
            _get_textconv_mod(hint_path, project_root).clear_asset_context()
        except Exception:
            pass


def _normalize_project_root(path: str) -> str:
    if not path:
        return ""
    path = os.path.abspath(os.path.expandvars(os.path.expanduser(path.strip().strip('"'))))
    if os.path.isdir(os.path.join(path, "Assets")):
        return path
    return ""


def _project_root_from_repo_path(repo: str, repo_path: str) -> str:
    if not repo or not repo_path:
        return ""
    abs_path = os.path.abspath(os.path.join(repo, repo_path.replace("/", os.sep)))
    parts = abs_path.split(os.sep)
    for idx, part in enumerate(parts):
        if part.lower() != "assets":
            continue
        root = os.sep.join(parts[:idx])
        if root and os.path.isdir(os.path.join(root, "Assets")):
            return root
    return _normalize_project_root(repo)


def _looks_like_unity_project(path: str) -> bool:
    return (
        os.path.isdir(os.path.join(path, "Assets")) and
        (
            os.path.isdir(os.path.join(path, "ProjectSettings")) or
            os.path.isdir(os.path.join(path, "Packages"))
        )
    )


def _discover_nested_project_roots(base_dir: str, max_depth: int = 5):
    if not base_dir or not os.path.isdir(base_dir):
        return []
    base_dir = os.path.abspath(base_dir)
    roots = []
    for dirpath, dirnames, _filenames in os.walk(base_dir):
        rel = os.path.relpath(dirpath, base_dir)
        depth = 0 if rel == "." else rel.count(os.sep) + 1
        if depth > max_depth:
            dirnames[:] = []
            continue
        dirnames[:] = [d for d in dirnames if d not in _PROJECT_SEARCH_SKIP]
        if _looks_like_unity_project(dirpath):
            roots.append(dirpath)
            dirnames[:] = [d for d in dirnames if d not in {"Assets", "Library", "Temp", "Build", "Logs"}]
    return roots


def _choose_project_root(candidates, hint_path: str = "") -> str:
    candidates = list(OrderedDict.fromkeys(_normalize_project_root(path) for path in candidates))
    candidates = [path for path in candidates if path]
    if len(candidates) == 1:
        return candidates[0]
    for root in candidates:
        if _candidate_contains_hint(root, hint_path):
            return root
    return ""


def _iter_env_project_roots():
    env_values = [
        os.environ.get("PREFAB_DIFF_PROJECT_ROOTS") or "",
        os.environ.get("PREFAB_DIFF_PROJECT_ROOT") or "",
        os.environ.get("UNITY_PROJECT_ROOT") or "",
    ]
    for item in os.pathsep.join(value for value in env_values if value).split(os.pathsep):
        root = _normalize_project_root(item)
        if root:
            yield root


def _candidate_contains_hint(root: str, hint_path: str) -> bool:
    basename = os.path.basename(hint_path or "")
    if not basename:
        return False
    assets_dir = os.path.join(root, "Assets")
    for dirpath, dirnames, filenames in os.walk(assets_dir):
        dirnames[:] = [d for d in dirnames if d not in {"Library", "Temp", "Build", "Logs", "obj", ".git"}]
        if basename in filenames:
            return True
    return False


def _find_project_root(hint_path: str = "", project_root: str = "") -> str:
    """查找 Unity 项目根目录（含 Assets/ 的目录）

    查找策略（按优先级）：
    1. 使用 --project-root / 环境变量传入的显式项目根
    2. 从 hint_path（prefab 文件路径）中截取 Assets 之前的部分
    3. 从 cwd 向上查找
    4. 从环境变量读取候选项目
    """
    explicit_root = _normalize_project_root(project_root)
    if explicit_root:
        return explicit_root

    # 策略 1: 从 hint_path 反推
    if hint_path:
        normalized = hint_path.replace("/", os.sep).replace("\\", os.sep)
        # 查找 "Assets" 目录段
        parts = normalized.split(os.sep)
        for i, part in enumerate(parts):
            if part == "Assets":
                candidate = os.sep.join(parts[:i])
                if candidate and os.path.isdir(os.path.join(candidate, "Assets")):
                    return candidate

    # 策略 2: 从 cwd 向上查找
    cwd = os.getcwd()
    path = cwd
    while True:
        if os.path.isdir(os.path.join(path, "Assets")):
            return path
        parent = os.path.dirname(path)
        if parent == path:
            break
        path = parent

    nested_roots = _discover_nested_project_roots(cwd)
    selected_root = _choose_project_root(nested_roots, hint_path)
    if selected_root:
        return selected_root

    env_roots = list(OrderedDict.fromkeys(_iter_env_project_roots()))
    if len(env_roots) == 1:
        return env_roots[0]
    for root in env_roots:
        if _candidate_contains_hint(root, hint_path):
            return root

    return ""


def parse_structured_text(text: str) -> OrderedDict:
    """
    解析 prefab_textconv 输出为结构化数据:
    {node_path: OrderedDict of {prop_key: prop_value}}

    格式:
    [path/to/node]  [flags]
      Component.property: value

    注意：同一路径可能出现多次（如多个同名嵌套 prefab 实例），
    通过添加 #N 后缀区分。
    """
    nodes = OrderedDict()
    current_node = None
    path_count = {}  # 记录每个路径出现的次数

    for line in text.splitlines():
        if not line:
            continue
        if line.startswith("[") and not line.startswith("  "):
            # 节点头行: [path/name] [optional flags]
            bracket_end = line.index("]") if "]" in line else len(line)
            node_path = line[1:bracket_end]
            flags = line[bracket_end + 1:].strip() if bracket_end + 1 < len(line) else ""

            # 处理重复路径：第 2 次及以后出现的加 #N 后缀
            if node_path in path_count:
                path_count[node_path] += 1
                current_node = f"{node_path}#{path_count[node_path]}"
            else:
                path_count[node_path] = 1
                current_node = node_path

            nodes[current_node] = OrderedDict()
            if flags:
                nodes[current_node]["__flags__"] = flags
        elif line.startswith("  ") and current_node:
            # 属性行:   Component.property: value
            stripped = line.strip()
            colon_idx = stripped.find(": ")
            if colon_idx > 0:
                key = stripped[:colon_idx]
                val = stripped[colon_idx + 2:]
                nodes[current_node][key] = val
            elif stripped.endswith(":"):
                key = stripped[:-1]
                nodes[current_node][key] = ""

    return nodes


INTERNAL_PROPS = {"__flags__", "__id__", "__parent_id__"}
FILEID_LABEL_RE = re.compile(r"\{fileID:([^}]+?)\s+->\s+[^}]+\}")
INTERNAL_DUPLICATE_SUFFIX_RE = re.compile(r"#\d+$")


def _node_identity(props):
    return props.get("__id__", "") if props else ""


def _identities_compatible(old_props, new_props):
    old_id = _node_identity(old_props)
    new_id = _node_identity(new_props)
    return not old_id or not new_id or old_id == new_id


def _display_path(path):
    return INTERNAL_DUPLICATE_SUFFIX_RE.sub("", str(path or ""))


def _basename(path):
    return _display_path(path).split("/")[-1]


def _parent_path(path):
    text = _display_path(path)
    if "/" not in text:
        return "(root)"
    return text.rsplit("/", 1)[0] or "(root)"


def _stable_compare_value(value):
    return FILEID_LABEL_RE.sub(r"{fileID:\1}", str(value or ""))


def _unique_identity_paths(nodes):
    by_id = {}
    duplicated = set()
    for path, props in nodes.items():
        node_id = _node_identity(props)
        if not node_id:
            continue
        if node_id in by_id:
            duplicated.add(node_id)
            continue
        by_id[node_id] = path
    for node_id in duplicated:
        by_id.pop(node_id, None)
    return by_id


def _match_node_paths(old_nodes, new_nodes):
    old_paths = set(old_nodes.keys())
    matches = OrderedDict()
    used_old = set()

    for path in new_nodes:
        if path in old_paths and _identities_compatible(old_nodes[path], new_nodes[path]):
            matches[path] = path
            used_old.add(path)

    old_by_id = _unique_identity_paths(old_nodes)
    new_by_id = _unique_identity_paths(new_nodes)
    for node_id, new_path in new_by_id.items():
        old_path = old_by_id.get(node_id)
        if not old_path or new_path in matches or old_path in used_old:
            continue
        matches[new_path] = old_path
        used_old.add(old_path)

    return matches


def diff_prefab(old_nodes: OrderedDict, new_nodes: OrderedDict) -> dict:
    """
    对比两个版本的 prefab 结构化数据。
    返回:
    {
        "added_nodes": {path: props_dict},
        "removed_nodes": {path: props_dict},
        "modified_nodes": {path: {"added": {}, "removed": {}, "changed": {key: (old, new)}}},
    }
    """
    matches = _match_node_paths(old_nodes, new_nodes)
    matched_new_paths = set(matches.keys())
    matched_old_paths = set(matches.values())
    renamed_paths = OrderedDict(
        (old_path, new_path)
        for new_path, old_path in matches.items()
        if old_path != new_path
    )

    added_nodes = OrderedDict()
    for p in new_nodes:
        if p not in matched_new_paths:
            added_nodes[p] = new_nodes[p]

    removed_nodes = OrderedDict()
    for p in old_nodes:
        if p not in matched_old_paths:
            removed_nodes[p] = old_nodes[p]

    modified_nodes = OrderedDict()
    for p, old_path in matches.items():
        if p not in new_nodes or old_path not in old_nodes:
            continue
        old_props = old_nodes[old_path]
        new_props = new_nodes[p]

        # __flags__ 是生成的节点状态元数据；普通字段比较里跳过，下面转成 [Flags] 行展示。
        old_keys = set(k for k in old_props if k not in INTERNAL_PROPS)
        new_keys = set(k for k in new_props if k not in INTERNAL_PROPS)

        added_props = {k: new_props[k] for k in (new_keys - old_keys)}
        removed_props = {k: old_props[k] for k in (old_keys - new_keys)}
        changed_props = {}
        for k in old_keys & new_keys:
            if (
                old_props[k] != new_props[k]
                and _stable_compare_value(old_props[k]) != _stable_compare_value(new_props[k])
            ):
                changed_props[k] = (old_props[k], new_props[k])

        # flags 变更（如 active 状态切换）
        old_flags = old_props.get("__flags__", "")
        new_flags = new_props.get("__flags__", "")
        if old_flags != new_flags:
            changed_props["[Flags]"] = (old_flags or "(none)", new_flags or "(none)")

        if old_path != p:
            old_name = _basename(old_path)
            new_name = _basename(p)
            if old_name != new_name:
                changed_props["[Name]"] = (old_name, new_name)
            if old_props.get("__parent_id__", "") != new_props.get("__parent_id__", ""):
                changed_props["[Parent]"] = (_parent_path(old_path), _parent_path(p))

        if added_props or removed_props or changed_props:
            modified_nodes[p] = {
                "added": added_props,
                "removed": removed_props,
                "changed": changed_props,
            }

    return {
        "added_nodes": added_nodes,
        "removed_nodes": removed_nodes,
        "modified_nodes": modified_nodes,
        "renamed_paths": renamed_paths,
    }



# ─── CLI 参数 ─────────────────────────────────────────────────────────────


def _split_cli_args(argv):
    report_mode = REPORT_MODE_EMBED if _IS_EMBEDDED_DIFF else REPORT_MODE_FULL
    project_root = os.environ.get("PREFAB_DIFF_PROJECT_ROOT") or os.environ.get("UNITY_PROJECT_ROOT") or ""
    diff_context = DiffContext.from_env()
    files = []
    idx = 0

    def read_value(arg_name: str, current_idx: int):
        if "=" in arg_name:
            return arg_name.split("=", 1)[1], current_idx
        next_idx = current_idx + 1
        return (argv[next_idx] if next_idx < len(argv) else ""), next_idx

    while idx < len(argv):
        arg = argv[idx]
        lower = (arg or "").lower()
        if lower in {"--full", "--mode=full"}:
            report_mode = REPORT_MODE_FULL
        elif lower in {"--embed", "--embedded", "--mode=embed", "--mode=embedded"}:
            report_mode = REPORT_MODE_EMBED
        elif lower.startswith("--mode="):
            mode_value = arg.split("=", 1)[1]
            if mode_value.lower() in {"full", "embed", "embedded"}:
                report_mode = _normalize_report_mode(mode_value)
            else:
                diff_context.mode = mode_value
        elif lower.startswith("--project-root=") or lower.startswith("--root=") or lower.startswith("--unity-project="):
            project_root = arg.split("=", 1)[1]
        elif lower in {"--project-root", "--root", "--unity-project"}:
            idx += 1
            if idx < len(argv):
                project_root = argv[idx]
        elif lower.startswith("--repo="):
            diff_context.repo = arg.split("=", 1)[1]
        elif lower == "--repo":
            diff_context.repo, idx = read_value(arg, idx)
        elif lower.startswith("--path="):
            diff_context.path = arg.split("=", 1)[1]
        elif lower == "--path":
            diff_context.path, idx = read_value(arg, idx)
        elif lower.startswith("--title="):
            diff_context.title = arg.split("=", 1)[1]
        elif lower == "--title":
            diff_context.title, idx = read_value(arg, idx)
        elif lower.startswith("--context="):
            diff_context.context = arg.split("=", 1)[1]
        elif lower == "--context":
            diff_context.context, idx = read_value(arg, idx)
        elif lower == "--mode":
            diff_context.mode, idx = read_value(arg, idx)
        elif lower.startswith("--base="):
            diff_context.base = arg.split("=", 1)[1]
        elif lower == "--base":
            diff_context.base, idx = read_value(arg, idx)
        elif lower.startswith("--target="):
            diff_context.target = arg.split("=", 1)[1]
        elif lower == "--target":
            diff_context.target, idx = read_value(arg, idx)
        elif lower.startswith("--commit="):
            diff_context.commit = arg.split("=", 1)[1]
        elif lower == "--commit":
            diff_context.commit, idx = read_value(arg, idx)
        elif lower.startswith("--output="):
            diff_context.output = arg.split("=", 1)[1]
        elif lower == "--output":
            diff_context.output, idx = read_value(arg, idx)
        elif lower.startswith("--output-dir="):
            diff_context.output_dir = arg.split("=", 1)[1]
        elif lower == "--output-dir":
            diff_context.output_dir, idx = read_value(arg, idx)
        elif lower == "--print-output":
            diff_context.print_output = True
        elif lower == "--no-open":
            diff_context.no_open = True
        elif lower == "--open":
            diff_context.no_open = False
        elif lower.startswith("--host="):
            diff_context.host = arg.split("=", 1)[1]
        elif lower == "--host":
            diff_context.host, idx = read_value(arg, idx)
        else:
            files.append(arg)
        idx += 1

    return report_mode, project_root, diff_context, files


def guess_filename(left_path: str, right_path: str) -> str:
    """猜测原始文件名"""
    for p in [right_path, left_path]:
        basename = os.path.basename(p)
        if basename.endswith(".prefab") or basename.endswith(".unity"):
            return basename
    return os.path.basename(right_path) or "unknown.prefab"


def _infer_git_rev_from_temp_path(path: str) -> str:
    folder = os.path.basename(os.path.dirname(path or ""))
    if not folder:
        return ""
    lower = folder.lower()
    if lower == "head":
        return "HEAD"
    if lower in {"staged", "unstaged", "working", "worktree"}:
        return ""
    if folder.endswith("~") and len(folder) > 1:
        return f"{folder[:-1]}~1"
    if all(ch in "0123456789abcdefABCDEF" for ch in folder) and len(folder) >= 6:
        return folder
    return ""


def _old_new_temp_slot(path: str) -> str:
    name = os.path.basename(path or "").lower()
    stem, _ext = os.path.splitext(name)
    if stem in {"old", "new"}:
        return stem
    return ""


def _normalize_context_old_new_paths(left_path: str, right_path: str, context: DiffContext):
    if context.old_path or context.new_path:
        return context.old_path or left_path, context.new_path or right_path

    if not (_IS_SOURCEGIT_CUSTOM_DIFF or (context.host or "").lower() == "sourcegit"):
        return left_path, right_path

    left_slot = _old_new_temp_slot(left_path)
    right_slot = _old_new_temp_slot(right_path)
    if left_slot == "new" and right_slot == "old":
        return right_path, left_path
    return left_path, right_path


def run_prefab_diff(left_path: str, right_path: str, report_mode: str = REPORT_MODE_FULL,
                    project_root: str = "", diff_context: DiffContext | None = None):
    """主入口：对比两个 prefab 文件并生成 HTML"""
    report_mode = _normalize_report_mode(report_mode)
    diff_context = diff_context or DiffContext.from_env()
    project_root = _normalize_project_root(project_root) or _project_root_from_repo_path(
        diff_context.repo, diff_context.path
    )
    import logging
    log_path = os.path.join(tempfile.gettempdir(), "prefab_diff_debug.log")
    logging.basicConfig(filename=log_path, level=logging.DEBUG, force=True,
                        format='%(asctime)s %(message)s')
    logging.debug(f"run_prefab_diff called: left={left_path}, right={right_path}, report_mode={report_mode}, project_root={project_root}")
    logging.debug(
        "diff context: "
        f"host={diff_context.host}, repo={diff_context.repo}, path={diff_context.path}, "
        f"mode={diff_context.mode}, base={diff_context.base}, "
        f"target={diff_context.target}, commit={diff_context.commit}, "
        f"output={diff_context.output}, output_dir={diff_context.output_dir}"
    )
    logging.debug(f"cwd={os.getcwd()}")

    original_left_path, original_right_path = left_path, right_path
    left_path, right_path = _normalize_context_old_new_paths(left_path, right_path, diff_context)
    if (left_path, right_path) != (original_left_path, original_right_path):
        logging.debug(f"normalized old/new paths: old={left_path}, new={right_path}")
    is_embedded_context = bool(
        _IS_EMBEDDED_DIFF
        or diff_context.output_dir
        or diff_context.print_output
        or (diff_context.host or "").strip()
    )

    # 优先使用真实 Assets 路径；Fork 双临时文件对比时退回配置根目录。
    hint_path = diff_context.repo_file_path()
    if not hint_path:
        hint_path = next((p for p in [right_path, left_path] if "Assets" in p.replace("\\", "/")), "")
    if not hint_path:
        hint_path = right_path or left_path
    logging.debug(f"hint_path={hint_path}")

    context_old_rev, context_new_rev = diff_context.revisions()
    old_rev = context_old_rev or ("" if is_embedded_context else _infer_git_rev_from_temp_path(left_path))
    new_rev = context_new_rev or ("" if is_embedded_context else _infer_git_rev_from_temp_path(right_path))
    logging.debug(f"old_rev={old_rev}, new_rev={new_rev}")

    old_content = _read_git_file(diff_context.repo, old_rev, diff_context.path)
    old_source = "git" if old_content is not None else "temp"
    if old_content is None:
        old_content = read_file(left_path)

    new_content = _read_git_file(diff_context.repo, new_rev, diff_context.path)
    new_source = "git" if new_content is not None else "temp"
    if new_content is None:
        new_content = read_file(right_path)
    logging.debug(f"content source: old={old_source}, new={new_source}")

    logging.debug(f"old_content length={len(old_content)}, new_content length={len(new_content)}")

    # 转换为结构化文本（传入 hint_path 用于缓存初始化）
    old_text = convert_to_structured(old_content, hint_path, old_rev, project_root)
    new_text = convert_to_structured(new_content, hint_path, new_rev, project_root)

    logging.debug(f"old_text lines={len(old_text.splitlines())}, new_text lines={len(new_text.splitlines())}")

    # 检查 textconv 缓存状态
    mod = _get_textconv_mod(hint_path, project_root)
    logging.debug(f"_prefab_guid_cache entries={len(mod._prefab_guid_cache)}")
    logging.debug(f"project_root used: {_find_project_root(hint_path, project_root)}")

    # 解析为节点字典
    old_nodes = parse_structured_text(old_text)
    new_nodes = parse_structured_text(new_text)

    logging.debug(f"old_nodes={len(old_nodes)}, new_nodes={len(new_nodes)}")
    for p in list(new_nodes.keys())[:5]:
        logging.debug(f"  node: {p}")

    # 对比
    diff_result = diff_prefab(old_nodes, new_nodes)

    logging.debug(f"added={len(diff_result['added_nodes'])}, removed={len(diff_result['removed_nodes'])}, modified={len(diff_result['modified_nodes'])}")

    # 生成 HTML（传入完整节点数据用于 Hierarchy 树构建）
    filename = guess_filename(diff_context.path or diff_context.title or left_path, right_path)
    html_content = generate_prefab_html(filename, diff_result, old_nodes, new_nodes, report_mode)

    if diff_context.output:
        output_path = diff_context.output
        output_parent = os.path.dirname(os.path.abspath(output_path))
        if output_parent:
            os.makedirs(output_parent, exist_ok=True)
    else:
        output_dir = diff_context.output_dir or tempfile.gettempdir()
        os.makedirs(output_dir, exist_ok=True)
        output_path = os.path.join(output_dir, f"prefab_diff_{report_mode}_{os.getpid()}.html")
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html_content)

    logging.debug(f"Report written to: {output_path}")
    output_path = os.path.abspath(output_path)

    if diff_context.no_open:
        return output_path

    try:
        os.startfile(output_path)
    except Exception:
        webbrowser.open(f"file://{output_path}")

    return output_path


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    report_mode, project_root, diff_context, files = _split_cli_args(argv)
    if len(files) < 2:
        print("用法: prefab_diff.py [--full|--embed] [--project-root <UnityProject>] [--repo <Repo>] [--path <RepoPath>] [--base <Rev>] [--target <Rev>] [--output <Html>] [--output-dir <Dir>] [--print-output] [--no-open] <old_file> <new_file>")
        return 1
    output_path = run_prefab_diff(files[0], files[1], report_mode, project_root, diff_context)
    if diff_context.print_output and output_path:
        print(output_path, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
