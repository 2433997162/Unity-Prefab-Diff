#!/usr/bin/env python3
"""查看指定提交中所有 Prefab/Scene 文件的变更报告"""

import sys
import os
import subprocess
import tempfile
import webbrowser
import atexit

# Fork 启动时 stdout 管道可能不被 drain，导致 print 阻塞
# 检测非交互环境并重定向输出
if not sys.stdout.isatty():
    try:
        _log_path = os.path.join(tempfile.gettempdir(), "prefab_commit_diff.log")
        _log_file = open(_log_path, "w", encoding="utf-8", errors="replace")
        sys.stdout = _log_file
        sys.stderr = _log_file
    except Exception:
        sys.stdout = open(os.devnull, "w")
        sys.stderr = open(os.devnull, "w")
else:
    # 解决 Windows 终端编码
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from prefab_html_renderer import generate_prefab_collection_html
from prefab_fork_diff import convert_to_structured, parse_structured_text, diff_prefab


_cat_file_proc = None
_file_content_cache = {}


def _split_cli_args(argv):
    project_root = ""
    args = []
    idx = 0
    while idx < len(argv):
        arg = argv[idx]
        lower = (arg or "").lower()
        if lower.startswith("--project-root=") or lower.startswith("--root=") or lower.startswith("--unity-project="):
            project_root = arg.split("=", 1)[1]
        elif lower in {"--project-root", "--root", "--unity-project"}:
            idx += 1
            if idx < len(argv):
                project_root = argv[idx]
        else:
            args.append(arg)
        idx += 1
    return project_root, args


def _close_cat_file_proc():
    global _cat_file_proc
    try:
        if _cat_file_proc and _cat_file_proc.poll() is None:
            _cat_file_proc.stdin.close()
            _cat_file_proc.terminate()
    except Exception:
        pass
    _cat_file_proc = None


atexit.register(_close_cat_file_proc)


def _get_cat_file_proc():
    global _cat_file_proc
    if _cat_file_proc and _cat_file_proc.poll() is None:
        return _cat_file_proc
    try:
        _cat_file_proc = subprocess.Popen(
            ["git", "--no-pager", "cat-file", "--batch"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        _cat_file_proc = None
    return _cat_file_proc


def _read_git_blob(object_spec):
    proc = _get_cat_file_proc()
    if not proc or not proc.stdin or not proc.stdout:
        return ""
    try:
        proc.stdin.write((object_spec + "\n").encode("utf-8"))
        proc.stdin.flush()
        header = proc.stdout.readline()
        if not header:
            _close_cat_file_proc()
            return ""
        if header.rstrip().endswith(b" missing"):
            return ""
        parts = header.split()
        if len(parts) < 3:
            return ""
        size = int(parts[2])
        data = proc.stdout.read(size)
        proc.stdout.read(1)
        if parts[1] != b"blob":
            return ""
        return data.decode("utf-8", errors="replace")
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
        _close_cat_file_proc()
        return ""


def run_git(args):
    result = subprocess.run(
        ["git", "--no-pager"] + args,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
        text=True, encoding="utf-8", errors="replace"
    )
    if result.returncode != 0:
        raise RuntimeError(f"git failed: {result.stderr}")
    return result.stdout


def get_prefab_files_in_commit(commit):
    """获取提交中变更的 prefab/scene 文件列表"""
    output = run_git(["diff-tree", "--no-commit-id", "-r", "--name-only", "--diff-filter=ACMR", commit])
    files = []
    for line in output.strip().splitlines():
        if line.endswith(".prefab") or line.endswith(".unity"):
            files.append(line)
    return files


def get_file_at_commit(commit, filepath):
    """获取某个 commit 时的文件内容"""
    cache_key = (commit, filepath)
    if cache_key in _file_content_cache:
        return _file_content_cache[cache_key]
    try:
        content = _read_git_blob(f"{commit}:{filepath}")
        if not content:
            content = run_git(["show", f"{commit}:{filepath}"])
        _file_content_cache[cache_key] = content
        return content
    except RuntimeError:
        _file_content_cache[cache_key] = ""
        return ""


def get_local_changed_prefab_files():
    """获取本地所有变更的 prefab/scene 文件（staged + unstaged）"""
    staged = run_git(["diff", "--cached", "--name-only", "--diff-filter=ACMR", "--", "*.prefab", "*.unity"])
    unstaged = run_git(["diff", "--name-only", "--diff-filter=ACMR", "--", "*.prefab", "*.unity"])
    all_files = set()
    for line in (staged + "\n" + unstaged).strip().splitlines():
        line = line.strip()
        if line and (line.endswith(".prefab") or line.endswith(".unity")):
            all_files.add(line)
    return sorted(all_files)


def get_file_from_head(filepath):
    """获取 HEAD 版本的文件内容"""
    return get_file_at_commit("HEAD", filepath)


def get_file_from_disk(filepath):
    """从磁盘读取文件当前内容"""
    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except FileNotFoundError:
        return ""


def main():
    project_root, args = _split_cli_args(sys.argv[1:])
    is_local_mode = len(args) < 1 or args[0].strip() == "--local"

    if is_local_mode:
        # ─── 本地模式：检测 staged + unstaged prefab 变更 ───
        print("🔍 检查本地 Prefab 变更...")
        prefab_files = get_local_changed_prefab_files()
        if not prefab_files:
            print("✅ 没有本地 Prefab/Scene 文件变更")
            sys.exit(0)

        print(f"📁 找到 {len(prefab_files)} 个 Prefab/Scene 文件变更")

        all_reports = []
        for filepath in prefab_files:
            print(f"  分析: {os.path.basename(filepath)}...")
            old_content = get_file_from_head(filepath)
            new_content = get_file_from_disk(filepath)

            old_structured = convert_to_structured(old_content, filepath, "HEAD", project_root)
            new_structured = convert_to_structured(new_content, filepath, "", project_root)

            old_nodes = parse_structured_text(old_structured)
            new_nodes = parse_structured_text(new_structured)

            diff_result = diff_prefab(old_nodes, new_nodes)

            if not diff_result["added_nodes"] and not diff_result["removed_nodes"] and not diff_result["modified_nodes"]:
                continue

            all_reports.append({
                "filename": os.path.basename(filepath),
                "diff_result": diff_result,
                "old_nodes": old_nodes,
                "new_nodes": new_nodes,
            })

        if not all_reports:
            print("✅ Prefab 文件有改动但无实质性属性变更")
            sys.exit(0)

        full_html = generate_prefab_collection_html(
            "local",
            all_reports,
            summary={
                "title": "Prefab 变更报告（本地）",
                "label": "LOCAL",
                "fileCount": len(prefab_files),
            },
        )
        output_path = os.path.join(tempfile.gettempdir(), "prefab_diff_local.html")

    else:
        # ─── Commit 模式 ───
        commit = args[0].strip()
        print(f"🔍 检查提交 {commit[:8]} 的 Prefab 变更...")

        prefab_files = get_prefab_files_in_commit(commit)
        if not prefab_files:
            print("✅ 该提交没有 Prefab/Scene 文件变更")
            sys.exit(0)

        print(f"📁 找到 {len(prefab_files)} 个 Prefab/Scene 文件")

        all_reports = []
        for filepath in prefab_files:
            print(f"  分析: {os.path.basename(filepath)}...")
            old_content = get_file_at_commit(f"{commit}~1", filepath)
            new_content = get_file_at_commit(commit, filepath)

            old_structured = convert_to_structured(old_content, filepath, f"{commit}~1", project_root)
            new_structured = convert_to_structured(new_content, filepath, commit, project_root)

            old_nodes = parse_structured_text(old_structured)
            new_nodes = parse_structured_text(new_structured)

            diff_result = diff_prefab(old_nodes, new_nodes)

            if not diff_result["added_nodes"] and not diff_result["removed_nodes"] and not diff_result["modified_nodes"]:
                continue

            all_reports.append({
                "filename": os.path.basename(filepath),
                "diff_result": diff_result,
                "old_nodes": old_nodes,
                "new_nodes": new_nodes,
            })

        if not all_reports:
            print("✅ Prefab 文件有改动但无实质性属性变更")
            sys.exit(0)

        full_html = generate_prefab_collection_html(
            commit[:8],
            all_reports,
            summary={
                "title": "Prefab 变更报告（提交级）",
                "label": commit[:8],
                "fileCount": len(prefab_files),
            },
        )
        output_path = os.path.join(tempfile.gettempdir(), f"prefab_commit_diff_{os.getpid()}.html")

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(full_html)

    try:
        os.startfile(os.path.abspath(output_path))
    except Exception:
        webbrowser.open(f"file://{os.path.abspath(output_path)}")
    print(f"✅ 已打开报告: {output_path}")


if __name__ == "__main__":
    main()
