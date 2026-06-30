#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Unity Prefab/Scene YAML structured converter.

Converts Unity YAML to a stable per-node property dump so external diff reports
can show readable hierarchy and component changes.
"""

import os
import sys
import re
import json
import time
import hashlib
import tempfile
import subprocess
import atexit
import difflib
from collections import defaultdict

# ── Type ID map (from prefab_to_md.py CLASS_NAMES — authoritative source) ────
TYPE_NAMES = {
    # Core
    1: 'GameObject', 4: 'Transform', 114: 'MonoBehaviour', 1001: 'PrefabInstance',
    # Rendering / Visual
    20: 'Camera', 23: 'MeshRenderer', 33: 'MeshFilter',
    96: 'TrailRenderer', 120: 'LineRenderer', 137: 'SkinnedMeshRenderer',
    198: 'ParticleSystem', 199: 'ParticleSystemRenderer',
    212: 'SpriteRenderer', 218: 'Terrain', 320: 'PlayableDirector', 328: 'VideoPlayer',
    # Physics / Collision
    54: 'Rigidbody2D', 61: 'BoxCollider2D', 64: 'MeshCollider',
    65: 'BoxCollider', 135: 'SphereCollider', 136: 'CapsuleCollider', 154: 'TerrainCollider',
    # Light / Audio
    81: 'AudioListener', 82: 'AudioSource', 108: 'Light',
    # Animation
    95: 'Animator', 111: 'Animation',
    # Constraints
    1183024399: 'LookAtConstraint',
    # UI (built-in classID)
    222: 'CanvasRenderer', 223: 'Canvas', 224: 'RectTransform', 225: 'CanvasGroup',
    # Legacy classID for UI components (modern Unity uses MonoBehaviour+GUID)
    258: 'HorizontalLayoutGroup', 259: 'VerticalLayoutGroup', 264: 'GridLayoutGroup',
    330: 'GraphicRaycaster', 331: 'ScrollRect',
    369: 'ContentSizeFitter', 372: 'AspectRatioFitter',
}

# Hardcoded GUID fallbacks for common UGUI components
# (safety net when Library/PackageCache scan is unavailable)
_GUID_FALLBACKS = {
    'fe87c0e1cc204ed48ad3b37840f39efc': 'Image',
    'f4688fdb7df04437aeb418b961361dc5': 'TMP_Text',
    '99081db55ede7af4399615f956b00b27': 'ColorfulImage',
    '4e29b1a8efbd4b44bb3f3716e73f07ff': 'Button',
    '1367256648004ba4a9cb869e3436c557': 'RawImage',
    '2a4db7a114972834c8e4117be1d82ba3': 'LayoutElement',
    '3312d7739989d2b4e91e6319e9a96d76': 'Mask',
    '31a19414677d06e4884707c6e22bfee8': 'RectMask2D',
    '1344c3c82d178a64d8d011048bf4b4e7': 'Toggle',
    '1aa08ab6e0800fa44ae55d278d1423e3': 'ScrollRect',
    '30649d3a9faa99c48a7b1166b86bf2a0': 'HorizontalLayoutGroup',
    '59f8146938fff824cb5fd77236b75b03': 'VerticalLayoutGroup',
    'dc42784cf5e3c4ac9b5c2e1f4476e774': 'ContentSizeFitter',
    'cfabb0440166ab443bba8876756a24be': 'GridLayoutGroup',
}

# ── Properties to suppress (pure noise) ──────────────────────────────────────
SKIP_PROPS = {
    'm_ObjectHideFlags', 'm_CorrespondingSourceObject', 'm_PrefabInstance',
    'm_PrefabAsset', 'm_EditorHideFlags', 'm_EditorClassIdentifier',
    'serializedVersion', 'm_Father', 'm_Children', 'm_Component',
    'm_GameObject', 'm_TagString', 'm_Icon', 'm_NavMeshLayer',
    'm_StaticEditorFlags', 'm_ConstrainProportionsScale',
    'm_SelectOnUp', 'm_SelectOnDown', 'm_SelectOnLeft', 'm_SelectOnRight',
    'm_NormalTrigger', 'm_HighlightedTrigger', 'm_PressedTrigger',
    'm_SelectedTrigger', 'm_DisabledTrigger', 'm_WrapAround',
    # shown on node header line, redundant in component body
    'm_Name', 'm_IsActive', 'm_Layer',
    # euler hint is redundant with quaternion rotation
    'm_LocalEulerAnglesHint',
    # sibling order already shown by tree position
    'm_RootOrder',
    # script reference already shown as component header <ClassName>
    'm_Script',
}

DOC_RE = re.compile(r'^--- !u!(\d+) &(\d+)', re.MULTILINE)

# ── GUID → 脚本类名缓存（与 analyze-prefab/prefab_to_md.py 逻辑一致）──────────
_guid_cache: dict = {}        # full_guid → class_name (from .cs.meta)
_prefab_guid_cache: dict = {} # full_guid → absolute path (from .prefab.meta)
_cache_project_root: str = ''
_asset_resolver = None
_git_asset_content_cache: dict = {}  # (git_cache_key, asset_path) -> content
_git_guid_asset_path_cache: dict = {} # (git_cache_key, guid) -> asset path
_git_prefab_label_cache: dict = {}    # (git_cache_key, guid) -> label
_git_cat_file_procs: dict = {}        # normcase(project_root) -> Popen
_git_prefab_history_cache: dict = {}  # (git_cache_key, guid) -> [(rev, sections)]
_git_fileid_asset_cache: dict = {}    # (git_cache_key, hint_dir, fid) -> [asset_path]
_git_direct_prefab_index_cache: dict = {}  # (git_cache_key, asset_path) -> (names, paths, components)
_prefab_property_target_hints: dict = {}  # (cache_key, guid, props) -> (path, component)

_CACHE_VERSION = 4
_CACHE_TTL = 6 * 3600  # 6 hours
_SKIP_DIRS = frozenset({'Library', 'Temp', 'Build', 'Logs', 'obj', '.git'})
_TOOL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TMP_EFFECT_PROPS = frozenset({
    'underlayColor', 'underlayOffsetX', 'underlayOffsetY', 'underlayDilate',
    'underlaySoftness', 'underlayVirtual', 'outlineColor', 'outlineWidth',
    'outlineSoftness', 'glowColor', 'glowOffset', 'glowInner', 'glowOuter',
    'glowPower',
})


def _close_git_cat_file_procs():
    for proc in list(_git_cat_file_procs.values()):
        try:
            if proc.poll() is None:
                proc.stdin.close()
                proc.terminate()
        except Exception:
            pass
    _git_cat_file_procs.clear()


atexit.register(_close_git_cat_file_procs)


def _git_cat_file_key(project_root: str) -> str:
    return os.path.normcase(os.path.abspath(project_root))


def _get_git_cat_file_proc(project_root: str):
    key = _git_cat_file_key(project_root)
    proc = _git_cat_file_procs.get(key)
    if proc and proc.poll() is None:
        return proc
    try:
        proc = subprocess.Popen(
            ['git', '-C', project_root, '--no-pager', 'cat-file', '--batch'],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        return None
    _git_cat_file_procs[key] = proc
    return proc


def _read_git_blob(project_root: str, object_spec: str) -> str:
    """Read one git blob through a long-lived cat-file process."""
    proc = _get_git_cat_file_proc(project_root)
    if not proc or not proc.stdin or not proc.stdout:
        return ''
    try:
        proc.stdin.write((object_spec + '\n').encode('utf-8'))
        proc.stdin.flush()
        header = proc.stdout.readline()
        if not header:
            _git_cat_file_procs.pop(_git_cat_file_key(project_root), None)
            return ''
        if header.rstrip().endswith(b' missing'):
            return ''
        parts = header.split()
        if len(parts) < 3:
            return ''
        size = int(parts[2])
        data = proc.stdout.read(size)
        proc.stdout.read(1)  # trailing newline after blob payload
        if parts[1] != b'blob':
            return ''
        return data.decode('utf-8', errors='replace')
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
        _git_cat_file_procs.pop(_git_cat_file_key(project_root), None)
        return ''


class GitTreeAssetResolver:
    def __init__(self, project_root: str, rev: str):
        self.project_root = os.path.abspath(project_root)
        self.rev = rev
        self.cache_key = f'git:{os.path.normcase(self.project_root)}:{rev}'
        self._guid_asset_path = {}
        self._guid_sections = {}
        self._guid_history_sections = {}
        self._guid_label = {}
        self.lookup_count = 0
        self.max_lookups = int(os.environ.get('PREFAB_DIFF_MAX_GIT_LOOKUPS', '2') or '2')
        self.history_lookup_count = 0
        self.max_history_assets = int(os.environ.get('PREFAB_DIFF_MAX_HISTORY_ASSETS', '8') or '8')
        self.history_revs = int(os.environ.get('PREFAB_DIFF_HISTORY_REVS', '64') or '64')
        self.fileid_lookup_count = 0
        self.max_fileid_lookups = int(os.environ.get('PREFAB_DIFF_MAX_FILEID_LOOKUPS', '32') or '32')

    def _run_git(self, args):
        result = subprocess.run(
            ['git', '-C', self.project_root, '--no-pager'] + args,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            text=True, encoding='utf-8', errors='replace'
        )
        if result.returncode != 0:
            return ''
        return result.stdout

    def find_asset_path(self, guid: str) -> str:
        if guid in self._guid_asset_path:
            return self._guid_asset_path[guid]
        cache_key = (self.cache_key, guid)
        if cache_key in _git_guid_asset_path_cache:
            asset_path = _git_guid_asset_path_cache[cache_key]
            self._guid_asset_path[guid] = asset_path
            return asset_path
        if self.lookup_count >= self.max_lookups:
            self._guid_asset_path[guid] = ''
            return ''
        self.lookup_count += 1
        output = self._run_git([
            'grep', '-I', '-l', '--fixed-strings', f'guid: {guid}',
            self.rev, '--', ':(glob)Assets/**/*.meta'
        ])
        asset_path = ''
        for line in output.splitlines():
            path = line.split(':', 1)[1] if ':' in line else line
            path = path.replace('\\', '/')
            if path.startswith('./'):
                path = path[2:]
            if path.endswith('.meta'):
                asset_path = path[:-5]
                break
        self._guid_asset_path[guid] = asset_path
        _git_guid_asset_path_cache[cache_key] = asset_path
        return asset_path

    def asset_path_from_disk_cache(self, guid: str) -> str:
        path = _prefab_guid_cache.get(guid, '')
        if not path:
            return ''
        try:
            rel = os.path.relpath(path, self.project_root).replace('\\', '/')
        except ValueError:
            return ''
        return rel if not rel.startswith('..') else ''

    def read_asset(self, asset_path: str) -> str:
        asset_path = asset_path.replace('\\', '/')
        cache_key = (self.cache_key, asset_path)
        if cache_key in _git_asset_content_cache:
            return _git_asset_content_cache[cache_key]
        content = _read_git_blob(self.project_root, f'{self.rev}:./{asset_path}')
        if not content:
            content = self._run_git(['show', f'{self.rev}:./{asset_path}'])
        _git_asset_content_cache[cache_key] = content
        return content

    def asset_path_for_guid(self, guid: str) -> str:
        return self.asset_path_from_disk_cache(guid) or self.find_asset_path(guid)

    def prefab_label(self, guid: str) -> str:
        if guid in self._guid_label:
            return self._guid_label[guid]
        cache_key = (self.cache_key, guid)
        if cache_key in _git_prefab_label_cache:
            label = _git_prefab_label_cache[cache_key]
            self._guid_label[guid] = label
            return label
        asset_path = self.find_asset_path(guid)
        label = os.path.basename(asset_path) if asset_path else ''
        self._guid_label[guid] = label
        _git_prefab_label_cache[cache_key] = label
        return label

    def prefab_sections(self, guid: str):
        if guid in self._guid_sections:
            return self._guid_sections[guid]
        asset_path = self.asset_path_for_guid(guid)
        if not asset_path or not asset_path.endswith('.prefab'):
            self._guid_sections[guid] = []
            return []
        content = self.read_asset(asset_path)
        sections = _parse_prefab_sections_from_content(content) if content else []
        self._guid_sections[guid] = sections
        return sections

    def prefab_history_sections(self, guid: str):
        if guid in self._guid_history_sections:
            return self._guid_history_sections[guid]
        cache_key = (self.cache_key, guid)
        if cache_key in _git_prefab_history_cache:
            sections = _git_prefab_history_cache[cache_key]
            self._guid_history_sections[guid] = sections
            return sections
        if self.history_revs <= 1 or self.history_lookup_count >= self.max_history_assets:
            self._guid_history_sections[guid] = []
            return []
        asset_path = self.asset_path_for_guid(guid)
        if not asset_path or not asset_path.endswith('.prefab'):
            self._guid_history_sections[guid] = []
            return []

        self.history_lookup_count += 1
        output = self._run_git([
            'rev-list', f'--max-count={self.history_revs}', self.rev,
            '--', f'./{asset_path}'
        ])
        history = []
        seen_revs = set()
        for rev in output.splitlines():
            rev = rev.strip()
            if not rev or rev in seen_revs:
                continue
            seen_revs.add(rev)
            content = _read_git_blob(self.project_root, f'{rev}:./{asset_path}')
            if not content:
                content = self._run_git(['show', f'{rev}:./{asset_path}'])
            sections = _parse_prefab_sections_from_content(content) if content else []
            if sections:
                history.append((rev, sections))

        self._guid_history_sections[guid] = history
        _git_prefab_history_cache[cache_key] = history
        return history

    def asset_paths_with_fileid(self, fid: str, hint_dir: str = ''):
        hint_dir = (hint_dir or '').replace('\\', '/').strip('/')
        cache_key = (self.cache_key, hint_dir, fid)
        if cache_key in _git_fileid_asset_cache:
            return _git_fileid_asset_cache[cache_key]
        if self.fileid_lookup_count >= self.max_fileid_lookups:
            _git_fileid_asset_cache[cache_key] = []
            return []
        self.fileid_lookup_count += 1
        pathspec = f':(glob){hint_dir}/*.prefab' if hint_dir else ':(glob)Assets/**/*.prefab'
        output = self._run_git([
            'grep', '-I', '-l', '--fixed-strings', f'&{fid}',
            self.rev, '--', pathspec
        ])
        paths = []
        for line in output.splitlines():
            path = line.split(':', 1)[1] if ':' in line else line
            path = path.replace('\\', '/')
            if path.startswith('./'):
                path = path[2:]
            if path.endswith('.prefab') and path not in paths:
                paths.append(path)
        _git_fileid_asset_cache[cache_key] = paths
        return paths

    def asset_paths_with_any_fileid(self, fids, hint_dir: str = ''):
        fids = [str(fid) for fid in fids if str(fid)]
        if not fids:
            return []
        hint_dir = (hint_dir or '').replace('\\', '/').strip('/')
        pathspec = f':(glob){hint_dir}/*.prefab' if hint_dir else ':(glob)Assets/**/*.prefab'
        args = ['grep', '-I', '-l', '--fixed-strings']
        for fid in fids:
            args.extend(['-e', f'&{fid}'])
        args.extend([self.rev, '--', pathspec])
        output = self._run_git(args)
        paths = []
        for line in output.splitlines():
            path = line.split(':', 1)[1] if ':' in line else line
            path = path.replace('\\', '/')
            if path.startswith('./'):
                path = path[2:]
            if path.endswith('.prefab') and path not in paths:
                paths.append(path)
        return paths


def set_git_tree_context(project_root: str, rev: str):
    global _asset_resolver
    _asset_resolver = GitTreeAssetResolver(project_root, rev) if project_root and rev else None


def clear_asset_context():
    global _asset_resolver
    _asset_resolver = None

def _read_guid(meta_path: str) -> str:
    """Read GUID from first 'guid:' line of a .meta file (fast, reads minimal bytes)."""
    with open(meta_path, 'rb') as f:
        # guid is always in the first ~120 bytes of a .meta file
        head = f.read(256)
    m = re.search(rb'guid:\s*([0-9a-f]+)', head)
    return m.group(1).decode() if m else ''

def _scan_all_meta(scan_dir: str):
    """Single-pass walk: collect both .cs.meta and .prefab.meta in one traversal."""
    for root, dirs, files in os.walk(scan_dir):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for fname in files:
            if fname.endswith('.cs.meta'):
                try:
                    guid = _read_guid(os.path.join(root, fname))
                    if guid:
                        _guid_cache[guid] = os.path.splitext(fname[:-5])[0]
                except Exception:
                    pass
            elif fname.endswith('.prefab.meta'):
                try:
                    guid = _read_guid(os.path.join(root, fname))
                    if guid:
                        _prefab_guid_cache[guid] = os.path.join(root, fname[:-5])
                except Exception:
                    pass

def _scan_cs_only(scan_dir: str):
    """Walk for .cs.meta only (used for Library/PackageCache)."""
    for root, dirs, files in os.walk(scan_dir):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for fname in files:
            if not fname.endswith('.cs.meta'):
                continue
            try:
                guid = _read_guid(os.path.join(root, fname))
                if guid:
                    _guid_cache[guid] = os.path.splitext(fname[:-5])[0]
            except Exception:
                pass

def _cache_path(project_root: str) -> str:
    digest = hashlib.sha1(os.path.normcase(os.path.abspath(project_root)).encode('utf-8')).hexdigest()[:12]
    return os.path.join(tempfile.gettempdir(), f'prefab-converter-cache-{digest}.json')

def _try_load_disk_cache(project_root: str) -> bool:
    """Try loading caches from disk. Returns True if successful."""
    cp = _cache_path(project_root)
    try:
        if not os.path.exists(cp):
            return False
        age = time.time() - os.path.getmtime(cp)
        if age > _CACHE_TTL:
            return False
        with open(cp, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if data.get('version') != _CACHE_VERSION:
            return False
        _guid_cache.update(_GUID_FALLBACKS)
        _guid_cache.update(data.get('guid_cache', {}))
        for guid, rel in data.get('prefab_guid_cache', {}).items():
            _prefab_guid_cache[guid] = os.path.join(project_root, rel)
        return True
    except Exception:
        return False

def _save_disk_cache(project_root: str):
    """Save caches to disk for next invocation."""
    cp = _cache_path(project_root)
    try:
        os.makedirs(os.path.dirname(cp), exist_ok=True)
        prefix = project_root + os.sep
        rel_prefab = {}
        for guid, abspath in _prefab_guid_cache.items():
            if abspath.startswith(prefix):
                rel_prefab[guid] = abspath[len(prefix):]
            else:
                rel_prefab[guid] = abspath
        guid_no_fallbacks = {k: v for k, v in _guid_cache.items() if k not in _GUID_FALLBACKS}
        data = {
            'version': _CACHE_VERSION,
            'guid_cache': guid_no_fallbacks,
            'prefab_guid_cache': rel_prefab,
        }
        with open(cp, 'w', encoding='utf-8') as f:
            json.dump(data, f, separators=(',', ':'))
    except Exception:
        pass

def build_caches(project_root: str):
    """Build both GUID caches, using disk cache when available."""
    global _cache_project_root
    project_root = os.path.abspath(project_root)
    if _guid_cache and _cache_project_root == project_root:
        return
    if _cache_project_root and _cache_project_root != project_root:
        _guid_cache.clear()
        _prefab_guid_cache.clear()
        _prefab_go_names.clear()
        _prefab_go_paths.clear()
        _prefab_component_names.clear()
        _prefab_legacy_go_names.clear()
        _prefab_legacy_go_paths.clear()
        _prefab_legacy_component_names.clear()
        _prefab_similar_target_hints.clear()
        _prefab_record_metadata.clear()
    _cache_project_root = project_root
    if _try_load_disk_cache(project_root):
        return
    _guid_cache.update(_GUID_FALLBACKS)
    assets_dir = os.path.join(project_root, 'Assets')
    if not os.path.isdir(assets_dir):
        assets_dir = project_root
    # Single-pass: collect .cs.meta + .prefab.meta in one walk
    _scan_all_meta(assets_dir)
    # Library/PackageCache: only .cs.meta (no prefabs there)
    for extra in ('Packages', os.path.join('Library', 'PackageCache')):
        d = os.path.join(project_root, extra)
        if os.path.isdir(d):
            _scan_cs_only(d)
    _save_disk_cache(project_root)

def prefab_label(guid: str) -> str:
    """返回 'SomePrefab.prefab'，找不到时退回 'guid8...'。"""
    path = _prefab_guid_cache.get(guid, '')
    if path:
        return os.path.basename(path)
    if _asset_resolver:
        label = _asset_resolver.prefab_label(guid)
        if label:
            return label
    return f'{guid[:8]}...' if guid else '?'


_prefab_go_names = {}  # (context, guid) → {fid: go_name}
_prefab_go_paths = {}  # (context, guid) → {fid: go_path}
_prefab_component_names = {}  # (context, guid) → {fid: component_name}
_prefab_legacy_go_names = {}  # (git context, guid) → {historical fid: go_name}
_prefab_legacy_go_paths = {}  # (git context, guid) → {historical fid: go_path}
_prefab_legacy_component_names = {}  # (git context, guid) → {historical fid: component_name}
_prefab_similar_target_hints = {}  # (git context, target_guid, missing_fid) → (name, path, component)
_prefab_record_metadata = {}  # (context, guid, component fid) → Localize m_records metadata

def _prefab_name_cache_key(guid: str, use_resolver: bool):
    resolver_key = _asset_resolver.cache_key if use_resolver and _asset_resolver else f'disk:{_cache_project_root}'
    return resolver_key, guid


def component_name(type_id: int, body: str) -> str:
    """Resolve Unity class or MonoBehaviour script name for a component section."""
    cname = TYPE_NAMES.get(type_id, f'Type{type_id}')
    if type_id == 114:
        sm = re.search(r'm_Script:.*?guid:\s*([0-9a-f]+)', body)
        if sm:
            cname = _guid_cache.get(sm.group(1)) or f'Script:{sm.group(1)[:8]}'
        else:
            cname = 'MonoBehaviour'
    return cname


def _parse_prefab_sections_from_content(content):
    """Parse prefab content into sections list: [(type_id, fid, body, is_stripped)]."""
    sections = []
    for m in re.finditer(r'^--- !u!(\d+) &(\d+)( stripped)?', content, re.MULTILINE):
        type_id = int(m.group(1))
        fid = m.group(2)
        is_stripped = m.group(3) is not None
        start = m.end()
        nxt = re.search(r'^--- !u!', content[start:], re.MULTILINE)
        body = content[start: start + nxt.start()] if nxt else content[start:]
        sections.append((type_id, fid, body, is_stripped))
    return sections


def _parse_prefab_sections(path):
    """Parse a prefab file into sections list: [(type_id, fid, body, is_stripped)]."""
    try:
        with open(path, 'rb') as f:
            content = f.read().decode('utf-8', errors='replace')
    except Exception:
        return []
    return _parse_prefab_sections_from_content(content)


def _prefab_sections_for_guid(guid: str, use_resolver: bool = False):
    if use_resolver and _asset_resolver:
        sections = _asset_resolver.prefab_sections(guid)
        if sections:
            return sections
    path = _prefab_guid_cache.get(guid)
    if path and os.path.exists(path):
        return _parse_prefab_sections(path)
    if _asset_resolver:
        sections = _asset_resolver.prefab_sections(guid)
        if sections:
            return sections
    return []

def _parse_transform_children(body: str):
    ch_start = body.find('m_Children:')
    if ch_start < 0:
        return []
    ch_end = body.find('m_Father:', ch_start)
    ch_section = body[ch_start:ch_end] if ch_end >= 0 else body[ch_start:]
    return [f for f in re.findall(r'\{fileID:\s*(\d+)\}', ch_section) if f != '0']


def _source_object_ref(body: str):
    src_m = re.search(
        r'm_CorrespondingSourceObject:\s*\{fileID:\s*(\d+),\s*guid:\s*([0-9a-f]+)',
        body)
    return (src_m.group(1), src_m.group(2)) if src_m else None


def _build_direct_prefab_go_index_from_sections(sections):
    """Index direct objects in one prefab version without following nested prefabs."""
    result_names = {}
    result_paths = {}
    result_component_names = {}
    go_names = {}
    tfm_go = {}
    tfm_par = {}
    tfm_chd = {}
    comp_owner = {}
    stripped_src = {}

    for type_id, fid, body, is_stripped in sections:
        if is_stripped:
            src_ref = _source_object_ref(body)
            if src_ref:
                stripped_src[fid] = src_ref
            continue
        if type_id == 1:
            nm = re.search(r'm_Name:\s*(.+)', body)
            if nm:
                name = _decode_unicode_escapes(nm.group(1).strip())
                go_names[fid] = name
                result_names[fid] = name
        elif type_id in (4, 224):
            go_ref = re.search(r'm_GameObject:\s*\{fileID:\s*(\d+)\}', body)
            par_ref = re.search(r'm_Father:\s*\{fileID:\s*(\d+)\}', body)
            gfid = go_ref.group(1) if go_ref else None
            pfid = par_ref.group(1) if par_ref else None
            result_component_names[fid] = 'RectTransform' if type_id == 224 else 'Transform'
            if gfid:
                tfm_go[fid] = gfid
            tfm_par[fid] = pfid if pfid and pfid != '0' else None
            tfm_chd[fid] = _parse_transform_children(body)
        elif type_id not in (1, 4, 224, 1001):
            go_ref = re.search(r'm_GameObject:\s*\{fileID:\s*(\d+)\}', body)
            if go_ref:
                comp_owner[fid] = go_ref.group(1)
                result_component_names[fid] = component_name(type_id, body)

    def index_node(tfm_fid, prefix):
        go_fid = tfm_go.get(tfm_fid)
        if not go_fid:
            return
        name = go_names.get(go_fid, go_fid)
        path = f'{prefix}/{name}' if prefix else name
        result_names[go_fid] = name
        result_names[tfm_fid] = name
        result_paths[go_fid] = path
        result_paths[tfm_fid] = path
        for child_fid in tfm_chd.get(tfm_fid, []):
            index_node(child_fid, path)

    for root_tfm in [fid for fid, par in tfm_par.items() if par is None]:
        index_node(root_tfm, '')

    for comp_fid, go_fid in comp_owner.items():
        if go_fid in result_names:
            result_names[comp_fid] = result_names[go_fid]
        if go_fid in result_paths:
            result_paths[comp_fid] = result_paths[go_fid]

    for fid, (src_fid, src_guid) in stripped_src.items():
        source_names = dict(get_prefab_go_names(src_guid, use_resolver=bool(_asset_resolver)))
        source_paths = dict(get_prefab_go_paths(src_guid, use_resolver=bool(_asset_resolver)))
        source_components = dict(get_prefab_component_names(src_guid, use_resolver=bool(_asset_resolver)))
        source_name = source_names.get(src_fid, '')
        source_path = source_paths.get(src_fid, '')
        source_component = source_components.get(src_fid, '')
        if source_name:
            result_names[fid] = source_name
        if source_path:
            result_paths[fid] = source_path
        if source_component:
            result_component_names[fid] = source_component

    return result_names, result_paths, result_component_names


def _legacy_prefab_index(guid: str):
    if not _asset_resolver:
        return {}, {}, {}
    cache_key = (_asset_resolver.cache_key, guid)
    if (cache_key in _prefab_legacy_go_names and
            cache_key in _prefab_legacy_go_paths and
            cache_key in _prefab_legacy_component_names):
        return (
            _prefab_legacy_go_names[cache_key],
            _prefab_legacy_go_paths[cache_key],
            _prefab_legacy_component_names[cache_key],
        )

    result_names = {}
    result_paths = {}
    result_component_names = {}
    for _rev, sections in _asset_resolver.prefab_history_sections(guid):
        names, paths, components = _build_direct_prefab_go_index_from_sections(sections)
        for fid, name in names.items():
            result_names.setdefault(fid, name)
        for fid, path in paths.items():
            result_paths.setdefault(fid, path)
        for fid, component in components.items():
            result_component_names.setdefault(fid, component)

    _prefab_legacy_go_names[cache_key] = result_names
    _prefab_legacy_go_paths[cache_key] = result_paths
    _prefab_legacy_component_names[cache_key] = result_component_names
    return result_names, result_paths, result_component_names


def _direct_prefab_index_for_asset(asset_path: str):
    if not _asset_resolver:
        return {}, {}, {}
    asset_path = (asset_path or '').replace('\\', '/')
    cache_key = (_asset_resolver.cache_key, asset_path)
    if cache_key in _git_direct_prefab_index_cache:
        return _git_direct_prefab_index_cache[cache_key]
    content = _asset_resolver.read_asset(asset_path)
    sections = _parse_prefab_sections_from_content(content) if content else []
    result = _build_direct_prefab_go_index_from_sections(sections) if sections else ({}, {}, {})
    _git_direct_prefab_index_cache[cache_key] = result
    return result


def _fileid_declared_prefab_candidates(fid: str, target_guid: str):
    if not _asset_resolver:
        return []
    target_asset_path = _asset_resolver.asset_path_for_guid(target_guid)
    hint_dir = os.path.dirname(target_asset_path).replace('\\', '/') if target_asset_path else ''
    candidates = []
    for asset_path in _asset_resolver.asset_paths_with_fileid(fid, hint_dir):
        names, paths, components = _direct_prefab_index_for_asset(asset_path)
        if fid in names or fid in paths or fid in components:
            candidates.append({
                'asset_path': asset_path,
                'name': names.get(fid, ''),
                'path': paths.get(fid, ''),
                'component': components.get(fid, ''),
            })
    return candidates


def _path_parts(path: str):
    return [part for part in (path or '').split('/') if part]


def _similar_path_score(candidate_path: str, candidate_component: str,
                        target_path: str, target_component: str) -> float:
    candidate_parts = _path_parts(candidate_path)
    target_parts = _path_parts(target_path)
    if not candidate_parts or not target_parts:
        return 0.0
    if candidate_parts[-1] != target_parts[-1]:
        return 0.0
    if candidate_component and target_component and candidate_component != target_component:
        return 0.0

    max_suffix = 0
    max_len = min(len(candidate_parts), len(target_parts))
    for size in range(1, max_len + 1):
        if candidate_parts[-size:] == target_parts[-size:]:
            max_suffix = size
        else:
            break
    component_bonus = 24.0 if candidate_component and candidate_component == target_component else 0.0
    if max_suffix >= 2:
        return 200.0 + max_suffix + component_bonus
    if len(candidate_parts) == 1 and len(target_parts) == 1:
        return 180.0 + component_bonus

    candidate_body = '/'.join(candidate_parts[1:])
    target_body = '/'.join(target_parts[1:])
    score = difflib.SequenceMatcher(None, candidate_body, target_body).ratio() * 100.0
    score += component_bonus
    return score


def _target_similarity_index(guid: str):
    target_names = dict(get_prefab_go_names(guid, use_resolver=True))
    target_paths = dict(get_prefab_go_paths(guid, use_resolver=True))
    target_components = dict(get_prefab_component_names(guid, use_resolver=True))
    legacy_names, legacy_paths, legacy_components = _legacy_prefab_index(guid)
    target_names.update(legacy_names)
    target_paths.update(legacy_paths)
    target_components.update(legacy_components)
    return target_names, target_paths, target_components


def _best_similar_target_hint(candidates, target_names, target_paths, target_components):
    best = ('', '', '')
    best_score = 0.0
    tie = False

    for candidate in candidates:
        candidate_path = candidate.get('path', '')
        candidate_component = candidate.get('component', '')
        if not candidate_path:
            continue
        for target_fid, target_path in target_paths.items():
            target_component = target_components.get(target_fid, '')
            score = _similar_path_score(candidate_path, candidate_component, target_path, target_component)
            if score <= 0:
                continue
            if score > best_score + 0.001:
                best_name = target_names.get(target_fid) or (_path_parts(target_path) or [''])[-1]
                best = (best_name, target_path, target_component)
                best_score = score
                tie = False
            elif abs(score - best_score) <= 0.001:
                tie = True

    if best_score < 55.0 or tie:
        return '', '', ''
    return best


def _prefill_similar_prefab_target_hints(guid: str, fids):
    if not _asset_resolver:
        return
    pending = []
    for fid in fids:
        fid = str(fid)
        cache_key = (_asset_resolver.cache_key, guid, fid)
        if fid and cache_key not in _prefab_similar_target_hints:
            pending.append(fid)
    if not pending:
        return

    target_asset_path = _asset_resolver.asset_path_for_guid(guid)
    hint_dir = os.path.dirname(target_asset_path).replace('\\', '/') if target_asset_path else ''
    matched_assets = _asset_resolver.asset_paths_with_any_fileid(pending, hint_dir)
    candidates_by_fid = defaultdict(list)
    pending_set = set(pending)
    for asset_path in matched_assets:
        names, paths, components = _direct_prefab_index_for_asset(asset_path)
        for fid in pending_set & (set(names) | set(paths) | set(components)):
            candidates_by_fid[fid].append({
                'asset_path': asset_path,
                'name': names.get(fid, ''),
                'path': paths.get(fid, ''),
                'component': components.get(fid, ''),
            })

    target_names, target_paths, target_components = _target_similarity_index(guid)
    for fid in pending:
        cache_key = (_asset_resolver.cache_key, guid, fid)
        _prefab_similar_target_hints[cache_key] = _best_similar_target_hint(
            candidates_by_fid.get(fid, []),
            target_names,
            target_paths,
            target_components,
        )


def _similar_prefab_target_hint(guid: str, fid: str):
    if not _asset_resolver:
        return '', '', ''
    cache_key = (_asset_resolver.cache_key, guid, fid)
    if cache_key not in _prefab_similar_target_hints:
        candidates = _fileid_declared_prefab_candidates(fid, guid)
        target_names, target_paths, target_components = _target_similarity_index(guid)
        _prefab_similar_target_hints[cache_key] = _best_similar_target_hint(
            candidates,
            target_names,
            target_paths,
            target_components,
        )
    return _prefab_similar_target_hints[cache_key]


def _build_prefab_go_index(guid, _depth=0, use_resolver=False):
    """Load fileID→name/path mappings from a source prefab file."""
    cache_key = _prefab_name_cache_key(guid, use_resolver)
    if cache_key in _prefab_go_names and cache_key in _prefab_go_paths and cache_key in _prefab_component_names:
        return _prefab_go_names[cache_key], _prefab_go_paths[cache_key]
    if _depth > 5:
        _prefab_go_names[cache_key] = {}
        _prefab_go_paths[cache_key] = {}
        _prefab_component_names[cache_key] = {}
        return {}, {}

    result_names = {}
    result_paths = {}
    result_component_names = {}
    sections = _prefab_sections_for_guid(guid, use_resolver)
    if not sections:
        _prefab_go_names[cache_key] = result_names
        _prefab_go_paths[cache_key] = result_paths
        _prefab_component_names[cache_key] = result_component_names
        return result_names, result_paths

    go_names = {}
    tfm_go = {}
    tfm_par = {}
    tfm_chd = {}
    comp_owner = {}
    stripped_src = {}

    for type_id, fid, body, is_stripped in sections:
        if is_stripped:
            src_ref = _source_object_ref(body)
            if src_ref:
                stripped_src[fid] = src_ref
        elif type_id == 1:
            nm = re.search(r'm_Name:\s*(.+)', body)
            if nm:
                name = _decode_unicode_escapes(nm.group(1).strip())
                go_names[fid] = name
                result_names[fid] = name
        elif type_id in (4, 224):
            go_ref = re.search(r'm_GameObject:\s*\{fileID:\s*(\d+)\}', body)
            par_ref = re.search(r'm_Father:\s*\{fileID:\s*(\d+)\}', body)
            gfid = go_ref.group(1) if go_ref else None
            pfid = par_ref.group(1) if par_ref else None
            result_component_names[fid] = 'RectTransform' if type_id == 224 else 'Transform'
            if gfid:
                tfm_go[fid] = gfid
            tfm_par[fid] = pfid if pfid and pfid != '0' else None
            tfm_chd[fid] = _parse_transform_children(body)
        elif type_id not in (1, 4, 224, 1001):
            go_ref = re.search(r'm_GameObject:\s*\{fileID:\s*(\d+)\}', body)
            if go_ref:
                comp_owner[fid] = go_ref.group(1)
                result_component_names[fid] = component_name(type_id, body)

    def index_node(tfm_fid, prefix):
        go_fid = tfm_go.get(tfm_fid)
        if not go_fid:
            return
        name = go_names.get(go_fid, go_fid)
        path = f'{prefix}/{name}' if prefix else name
        result_names[go_fid] = name
        result_names[tfm_fid] = name
        result_paths[go_fid] = path
        result_paths[tfm_fid] = path
        for child_fid in tfm_chd.get(tfm_fid, []):
            index_node(child_fid, path)

    for root_tfm in [fid for fid, par in tfm_par.items() if par is None]:
        index_node(root_tfm, '')

    for comp_fid, go_fid in comp_owner.items():
        if go_fid in result_names:
            result_names[comp_fid] = result_names[go_fid]
        if go_fid in result_paths:
            result_paths[comp_fid] = result_paths[go_fid]

    for fid, (src_fid, src_guid) in stripped_src.items():
        src_names, src_paths = _build_prefab_go_index(src_guid, _depth + 1, use_resolver)
        src_components = _prefab_component_names.get(_prefab_name_cache_key(src_guid, use_resolver), {})
        if src_fid in src_names:
            result_names[fid] = src_names[src_fid]
        if src_fid in src_paths:
            result_paths[fid] = src_paths[src_fid]
        if src_fid in src_components:
            result_component_names[fid] = src_components[src_fid]

    # Unity remaps fileIDs of objects inside nested prefabs as:
    #   remapped_fileID = original_fileID XOR prefab_instance_fileID
    for type_id, fid, body, _is_stripped in sections:
        if type_id != 1001:
            continue
        src_m = re.search(r'm_SourcePrefab:.*?guid:\s*([0-9a-f]+)', body)
        if not src_m:
            continue
        nested_guid = src_m.group(1)
        try:
            pi_fid = int(fid)
        except ValueError:
            continue
        nested_names, nested_paths = _build_prefab_go_index(nested_guid, _depth + 1, use_resolver)
        nested_components = _prefab_component_names.get(_prefab_name_cache_key(nested_guid, use_resolver), {})
        for nfid_str, nname in nested_names.items():
            try:
                remapped = str(int(nfid_str) ^ pi_fid)
            except (ValueError, TypeError):
                continue
            result_names.setdefault(remapped, nname)
            if nfid_str in nested_paths:
                result_paths.setdefault(remapped, nested_paths[nfid_str])
            if nfid_str in nested_components:
                result_component_names.setdefault(remapped, nested_components[nfid_str])

    _prefab_go_names[cache_key] = result_names
    _prefab_go_paths[cache_key] = result_paths
    _prefab_component_names[cache_key] = result_component_names
    return result_names, result_paths


def get_prefab_go_names(guid, _depth=0, use_resolver=False):
    """Load and cache fileID→name mapping from a source prefab file.
    Covers GO fileIDs, component fileIDs, and stripped objects from nested prefabs."""
    names, _paths = _build_prefab_go_index(guid, _depth, use_resolver)
    return names


def get_prefab_go_paths(guid, _depth=0, use_resolver=False):
    """Load and cache fileID→hierarchy path mapping from a source prefab file."""
    _names, paths = _build_prefab_go_index(guid, _depth, use_resolver)
    return paths


def get_prefab_component_names(guid, _depth=0, use_resolver=False):
    """Load and cache fileID→component class mapping from a source prefab file."""
    _build_prefab_go_index(guid, _depth, use_resolver)
    return _prefab_component_names.get(_prefab_name_cache_key(guid, use_resolver), {})


def prefab_target_label(guid: str, fid: str) -> str:
    if _asset_resolver:
        resolver_names = get_prefab_go_names(guid, use_resolver=True)
        if fid in resolver_names:
            return resolver_names[fid]
        legacy_names, _legacy_paths, _legacy_components = _legacy_prefab_index(guid)
        if fid in legacy_names:
            return legacy_names[fid]
        similar_name, _similar_path, _similar_component = _similar_prefab_target_hint(guid, fid)
        if similar_name:
            return similar_name
    names = get_prefab_go_names(guid)
    if fid in names:
        return names[fid]
    return f'UnknownTarget:{fid}@{prefab_label(guid)}'


def prefab_target_path(guid: str, fid: str) -> str:
    if _asset_resolver:
        resolver_paths = get_prefab_go_paths(guid, use_resolver=True)
        if fid in resolver_paths:
            return resolver_paths[fid]
        _legacy_names, legacy_paths, _legacy_components = _legacy_prefab_index(guid)
        if fid in legacy_paths:
            return legacy_paths[fid]
        _similar_name, similar_path, _similar_component = _similar_prefab_target_hint(guid, fid)
        if similar_path:
            return similar_path
    paths = get_prefab_go_paths(guid)
    if fid in paths:
        return paths[fid]
    return ''


def _prefab_target_path_without_similarity(guid: str, fid: str) -> str:
    if _asset_resolver:
        resolver_paths = get_prefab_go_paths(guid, use_resolver=True)
        if fid in resolver_paths:
            return resolver_paths[fid]
        _legacy_names, legacy_paths, _legacy_components = _legacy_prefab_index(guid)
        if fid in legacy_paths:
            return legacy_paths[fid]
    paths = get_prefab_go_paths(guid)
    if fid in paths:
        return paths[fid]
    return ''


def prefab_target_component(guid: str, fid: str) -> str:
    if _asset_resolver:
        resolver_components = get_prefab_component_names(guid, use_resolver=True)
        if fid in resolver_components:
            return resolver_components[fid]
        _legacy_names, _legacy_paths, legacy_components = _legacy_prefab_index(guid)
        if fid in legacy_components:
            return legacy_components[fid]
        _similar_name, _similar_path, similar_component = _similar_prefab_target_hint(guid, fid)
        if similar_component:
            return similar_component
    components = get_prefab_component_names(guid)
    if fid in components:
        return components[fid]
    return ''


def _override_prop_base(prop: str) -> str:
    return (prop or '').split('.', 1)[0]


def _source_field_candidates(prop: str):
    base = _override_prop_base(prop)
    if not base:
        return ()
    candidates = [base]
    if not base.startswith('m_'):
        candidates.append(f'm_{base}')
    return tuple(dict.fromkeys(candidates))


def _body_has_yaml_field(body: str, field_name: str) -> bool:
    return bool(re.search(rf'(?m)^\s*{re.escape(field_name)}\s*:', body))


def _looks_like_tmp_effect_override(prop_list) -> bool:
    bases = {_override_prop_base(prop) for prop, _value in prop_list}
    return bool(bases) and bases.issubset(_TMP_EFFECT_PROPS)


def _prefab_property_target_hint(guid: str, prop_list):
    """Map legacy prefab override targets by matching fields to a unique source component."""
    prop_keys = tuple(sorted(_override_prop_base(prop) for prop, _value in prop_list if prop))
    if not guid or not prop_keys:
        return '', ''

    use_resolver = bool(_asset_resolver)
    cache_key = (_prefab_name_cache_key(guid, use_resolver), guid, prop_keys)
    if cache_key in _prefab_property_target_hints:
        return _prefab_property_target_hints[cache_key]

    required_fields = [_source_field_candidates(prop) for prop in prop_keys]
    sections = _prefab_sections_for_guid(guid, use_resolver)
    paths = get_prefab_go_paths(guid, use_resolver=use_resolver)
    components = get_prefab_component_names(guid, use_resolver=use_resolver)
    matches = []
    for type_id, fid, body, is_stripped in sections:
        if is_stripped or type_id in (1, 4, 224, 1001):
            continue
        if not all(any(_body_has_yaml_field(body, field) for field in fields) for fields in required_fields):
            continue
        target_path = paths.get(fid, '')
        if target_path:
            matches.append((target_path, components.get(fid, '')))

    unique_matches = list(dict.fromkeys(matches))
    if len(unique_matches) == 1:
        target_path, component = unique_matches[0]
        if _looks_like_tmp_effect_override(prop_list):
            component = 'TmpEffect'
        _prefab_property_target_hints[cache_key] = (target_path, component)
        return target_path, component

    _prefab_property_target_hints[cache_key] = ('', '')
    return '', ''


def _normalize_project_root(path: str) -> str:
    if not path:
        return ''
    path = os.path.abspath(os.path.expandvars(os.path.expanduser(path.strip().strip('"'))))
    if os.path.isdir(os.path.join(path, 'Assets')):
        return path
    return ''


def _looks_like_unity_project(path: str) -> bool:
    return (
        os.path.isdir(os.path.join(path, 'Assets')) and
        (
            os.path.isdir(os.path.join(path, 'ProjectSettings')) or
            os.path.isdir(os.path.join(path, 'Packages'))
        )
    )


def _discover_nested_project_roots(base_dir: str, max_depth: int = 5):
    if not base_dir or not os.path.isdir(base_dir):
        return []
    base_dir = os.path.abspath(base_dir)
    roots = []
    for dirpath, dirnames, _filenames in os.walk(base_dir):
        rel = os.path.relpath(dirpath, base_dir)
        depth = 0 if rel == '.' else rel.count(os.sep) + 1
        if depth > max_depth:
            dirnames[:] = []
            continue
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS and d != 'node_modules']
        if _looks_like_unity_project(dirpath):
            roots.append(dirpath)
            dirnames[:] = [d for d in dirnames if d not in {'Assets', 'Library', 'Temp', 'Build', 'Logs'}]
    return roots


def _choose_project_root(candidates, hint_path: str = '') -> str:
    candidates = list(dict.fromkeys(_normalize_project_root(path) for path in candidates))
    candidates = [path for path in candidates if path]
    if len(candidates) == 1:
        return candidates[0]
    for root in candidates:
        if _candidate_contains_hint(root, hint_path):
            return root
    return ''


def _candidate_contains_hint(root: str, file_path: str) -> bool:
    basename = os.path.basename(file_path or '')
    if not basename:
        return False
    assets_dir = os.path.join(root, 'Assets')
    for dirpath, dirnames, filenames in os.walk(assets_dir):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        if basename in filenames:
            return True
    return False

def _iter_env_project_roots():
    env_values = [
        os.environ.get('PREFAB_DIFF_PROJECT_ROOTS') or '',
        os.environ.get('PREFAB_DIFF_PROJECT_ROOT') or '',
        os.environ.get('UNITY_PROJECT_ROOT') or '',
    ]
    for item in os.pathsep.join(value for value in env_values if value).split(os.pathsep):
        root = _normalize_project_root(item)
        if root:
            yield root


def _find_project_root(file_path, project_root: str = ''):
    """从文件路径（或 cwd）向上找包含 Assets/ 子目录的那个目录。
    临时文件无法反推项目时，可通过 project_root 参数或环境变量指定。"""
    explicit_root = _normalize_project_root(project_root)
    if explicit_root:
        return explicit_root
    # Strategy 1: walk up from file_path
    if file_path and os.path.exists(file_path):
        start = os.path.abspath(file_path)
        path = start if os.path.isdir(start) else os.path.dirname(start)
        while True:
            if os.path.isdir(os.path.join(path, 'Assets')):
                return path
            parent = os.path.dirname(path)
            if parent == path:
                break
            path = parent
    # Strategy 2: walk up from cwd
    cwd = os.getcwd()
    path = cwd
    while True:
        if os.path.isdir(os.path.join(path, 'Assets')):
            return path
        parent = os.path.dirname(path)
        if parent == path:
            break
        path = parent
    nested_roots = _discover_nested_project_roots(cwd)
    selected_root = _choose_project_root(nested_roots, file_path)
    if selected_root:
        return selected_root
    env_roots = list(dict.fromkeys(_iter_env_project_roots()))
    if len(env_roots) == 1:
        return env_roots[0]
    for root in env_roots:
        if _candidate_contains_hint(root, file_path):
            return root
    return ''


def _split_cli_args(argv):
    project_root = ''
    files = []
    idx = 0
    while idx < len(argv):
        arg = argv[idx]
        lower = (arg or '').lower()
        if lower.startswith('--project-root=') or lower.startswith('--root=') or lower.startswith('--unity-project='):
            project_root = arg.split('=', 1)[1]
        elif lower in {'--project-root', '--root', '--unity-project'}:
            idx += 1
            if idx < len(argv):
                project_root = argv[idx]
        else:
            files.append(arg)
        idx += 1
    return project_root, files


def parse_all_sections(content):
    """Split content into sections: list of (type_id, fid, body_str)."""
    markers = list(DOC_RE.finditer(content))
    sections = []
    for idx, m in enumerate(markers):
        type_id = int(m.group(1))
        fid = m.group(2)
        end = markers[idx + 1].start() if idx + 1 < len(markers) else len(content)
        body = content[m.end():end]
        sections.append((type_id, fid, body))
    return sections


def fid_from_ref(ref):
    """Extract fileID from '{fileID: 123}' string."""
    m = re.search(r'fileID:\s*(\d+)', ref)
    return m.group(1) if m else None


def guid_from_ref(ref):
    """Extract short guid from ref string."""
    m = re.search(r'guid:\s*([0-9a-f]+)', ref)
    return m.group(1)[:8] + '...' if m else None


FILEID_REF_RE = re.compile(
    r'\{fileID:\s*(-?\d+)'
    r'(?:\s*,\s*guid:\s*([0-9a-f]+))?'
    r'(?:\s*,\s*type:\s*\d+)?\s*\}'
)
UNICODE_ESCAPE_RE = re.compile(r'\\u([0-9a-fA-F]{4})')
COLOR_CHANNEL_RE = re.compile(r'^(.+)\.([rgba])$')
VECTOR_CHANNEL_RE = re.compile(r'^(.+)\.([xyzw])$')
PACKED_RGBA_RE = re.compile(r'^(.+)\.rgba$')
INLINE_COLOR_OBJECT_RE = re.compile(
    r'^\{\s*r\s*:\s*([^,{}]+)\s*,\s*g\s*:\s*([^,{}]+)\s*,'
    r'\s*b\s*:\s*([^,{}]+)\s*,\s*a\s*:\s*([^,{}]+)\s*\}$'
)


def _decode_unicode_escapes(value: str) -> str:
    if not value or '\\u' not in value:
        return value

    out = []
    pos = 0
    matches = list(UNICODE_ESCAPE_RE.finditer(value))
    idx = 0
    while idx < len(matches):
        match = matches[idx]
        out.append(value[pos:match.start()])
        code_unit = int(match.group(1), 16)
        next_match = matches[idx + 1] if idx + 1 < len(matches) else None
        if 0xD800 <= code_unit <= 0xDBFF and next_match and next_match.start() == match.end():
            low_unit = int(next_match.group(1), 16)
            if 0xDC00 <= low_unit <= 0xDFFF:
                code_point = 0x10000 + ((code_unit - 0xD800) << 10) + (low_unit - 0xDC00)
                out.append(chr(code_point))
                pos = next_match.end()
                idx += 2
                continue
        out.append(chr(code_unit))
        pos = match.end()
        idx += 1

    out.append(value[pos:])
    return ''.join(out)


def translate_fileid_refs(value, ref_resolver=None):
    """Translate local Unity fileID refs embedded anywhere in a value string."""
    value = _decode_unicode_escapes(value)
    if not value or 'fileID:' not in value:
        return value

    def repl(match):
        fid = match.group(1)
        guid = match.group(2)
        if guid:
            return f'{{fileID:{fid}, guid:{guid[:8]}...}}'
        if fid == '0':
            return 'null'
        if ref_resolver:
            label = ref_resolver(fid)
            if label:
                return f'{{fileID:{fid} -> {label}}}'
        return f'{{fileID:{fid}}}'

    return FILEID_REF_RE.sub(repl, value)


def _is_color_base(key: str) -> bool:
    lower = (key or '').lower()
    return 'color' in lower or 'tint' in lower


def _parse_float_text(value):
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _parse_int_text(value):
    try:
        return int(str(value).strip(), 0)
    except (TypeError, ValueError):
        return None


def _compact_float(value: float) -> str:
    text = f'{value:.4f}'.rstrip('0').rstrip('.')
    return text or '0'


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def _format_rgba_color(channels):
    numeric = {channel: _parse_float_text(channels.get(channel, '')) for channel in 'rgba'}
    if any(numeric[channel] is None for channel in 'rgba'):
        return ''
    rgb = [round(_clamp01(numeric[channel]) * 255) for channel in 'rgb']
    alpha_float = _clamp01(numeric['a'])
    alpha_255 = round(alpha_float * 255)
    rgba = f'rgba({rgb[0]}, {rgb[1]}, {rgb[2]}, {_compact_float(alpha_float)})'
    hex_value = f'#{rgb[0]:02X}{rgb[1]:02X}{rgb[2]:02X}{alpha_255:02X}'
    raw = ', '.join(f'{channel}: {channels[channel]}' for channel in 'rgba')
    return f'{rgba} {hex_value} {{{raw}}}'


def _format_packed_rgba_color(value):
    raw_value = _parse_int_text(value)
    if raw_value is None:
        return ''
    packed = raw_value & 0xFFFFFFFF
    if raw_value != packed and raw_value + (1 << 32) != packed:
        return ''
    r = packed & 0xFF
    g = (packed >> 8) & 0xFF
    b = (packed >> 16) & 0xFF
    a = (packed >> 24) & 0xFF
    alpha = a / 255
    rgba = f'rgba({r}, {g}, {b}, {_compact_float(alpha)})'
    hex_value = f'#{r:02X}{g:02X}{b:02X}{a:02X}'
    return f'{rgba} {hex_value} {{rgba: {value}, hex: 0x{packed:08X}, r: {r}, g: {g}, b: {b}, a: {a}}}'


def _format_inline_color_value(key, value):
    if not _is_color_base(key) or not value or '{' not in value:
        return value
    match = INLINE_COLOR_OBJECT_RE.match(str(value).strip())
    if not match:
        return value
    pairs = dict(zip('rgba', (part.strip() for part in match.groups())))
    formatted = _format_rgba_color({channel: pairs[channel].strip() for channel in 'rgba'})
    return formatted or value


def _split_packed_rgba_key(key):
    match = PACKED_RGBA_RE.match(key or '')
    if not match:
        return ''
    base = match.group(1)
    return base if _is_color_base(base) else ''


def _format_color_prop_pair(key, value):
    packed_base = _split_packed_rgba_key(key)
    if packed_base:
        formatted = _format_packed_rgba_color(value)
        if formatted:
            return packed_base, formatted
    return key, _format_inline_color_value(key, value)


def _identity_prop_pair(key, value):
    return key, value


def _split_color_channel_key(key):
    match = COLOR_CHANNEL_RE.match(key or '')
    if not match:
        return '', ''
    base, channel = match.group(1), match.group(2)
    if not _is_color_base(base):
        return '', ''
    return base, channel


def _split_vector_channel_key(key):
    match = VECTOR_CHANNEL_RE.match(key or '')
    if not match:
        return '', ''
    return match.group(1), match.group(2)


def _vector_channels_for_group(channels):
    present = set(channels)
    if all(channel in present for channel in 'xyzw'):
        return 'xyzw'
    if all(channel in present for channel in 'xyz'):
        return 'xyz'
    if all(channel in present for channel in 'xy'):
        return 'xy'
    return ''


def _format_vector_value(channels, order):
    return '{' + ', '.join(f'{channel}: {channels[channel]}' for channel in order) + '}'


def _combine_channel_prop_pairs(props, split_key, required_order, format_value, fallback_pair):
    groups = defaultdict(dict)
    first_index = {}
    for index, (key, value) in enumerate(props):
        base, channel = split_key(key)
        if not base or _parse_float_text(value) is None:
            continue
        groups[base][channel] = (value, index)
        first_index.setdefault(base, index)

    complete = {}
    for base, channels in groups.items():
        order = required_order(channels)
        if order:
            complete[base] = (order, {channel: channels[channel][0] for channel in order})
    consumed = {
        groups[base][channel][1]
        for base, (order, _values) in complete.items()
        for channel in order
    }

    result = []
    for index, (key, value) in enumerate(props):
        if index in consumed:
            base, _channel = split_key(key)
            if first_index.get(base) == index:
                order, values = complete[base]
                result.append((base, format_value(values, order)))
            continue
        result.append(fallback_pair(key, value))
    return result


def _combine_color_prop_pairs(props):
    return _combine_channel_prop_pairs(
        props,
        _split_color_channel_key,
        lambda channels: 'rgba' if all(channel in channels for channel in 'rgba') else '',
        _format_rgba_color,
        _format_color_prop_pair,
    )


def _combine_vector_prop_pairs(props):
    return _combine_channel_prop_pairs(
        props,
        _split_vector_channel_key,
        _vector_channels_for_group,
        _format_vector_value,
        _identity_prop_pair,
    )


def _combine_channel_mods(mods, split_key, required_order, format_value, fallback_pair):
    groups = defaultdict(dict)
    first_index = {}
    for index, (tfid, tguid, prop, value) in enumerate(mods):
        base, channel = split_key(prop)
        if not base or _parse_float_text(value) is None:
            continue
        group_key = (tfid, tguid, base)
        groups[group_key][channel] = (value, index)
        first_index.setdefault(group_key, index)

    complete = {}
    for group_key, channels in groups.items():
        order = required_order(channels)
        if order:
            complete[group_key] = (order, {channel: channels[channel][0] for channel in order})
    consumed = {
        groups[group_key][channel][1]
        for group_key, (order, _values) in complete.items()
        for channel in order
    }

    result = []
    for index, (tfid, tguid, prop, value) in enumerate(mods):
        if index in consumed:
            base, _channel = split_key(prop)
            group_key = (tfid, tguid, base)
            if first_index.get(group_key) == index:
                order, values = complete[group_key]
                result.append((tfid, tguid, base, format_value(values, order)))
            continue
        formatted_key, formatted_value = fallback_pair(prop, value)
        result.append((tfid, tguid, formatted_key, formatted_value))
    return result


def _combine_color_mods(mods):
    return _combine_channel_mods(
        mods,
        _split_color_channel_key,
        lambda channels: 'rgba' if all(channel in channels for channel in 'rgba') else '',
        _format_rgba_color,
        _format_color_prop_pair,
    )


def _combine_vector_mods(mods):
    return _combine_channel_mods(
        mods,
        _split_vector_channel_key,
        _vector_channels_for_group,
        _format_vector_value,
        _identity_prop_pair,
    )


def _indent_len(line):
    return len(line) - len(line.lstrip(' '))


def _skip_yaml_block(lines, idx):
    base_indent = _indent_len(lines[idx])
    idx += 1
    while idx < len(lines):
        stripped = lines[idx].strip()
        indent = _indent_len(lines[idx])
        if stripped and indent <= base_indent and not (indent == base_indent and stripped.startswith('- ')):
            break
        idx += 1
    return idx


def _managed_type_name(type_value):
    m = re.search(r'class:\s*([^,}]+)', type_value)
    if m:
        return m.group(1).strip()
    return type_value.strip()


def _summarize_managed_ref(entry_lines, ref_resolver=None):
    ref_type = ''
    values = []
    for line in entry_lines:
        stripped = line.strip()
        if not stripped or ':' not in stripped:
            continue
        key, val = stripped.split(':', 1)
        key = key.strip()
        val = val.strip()
        if key == 'type':
            ref_type = _managed_type_name(val)
            continue
        if key in ('data', 'rid') or not val:
            continue
        values.append(f'{key}: {translate_fileid_refs(val, ref_resolver)}')

    if not ref_type and not values:
        return ''
    if values:
        return f'{ref_type} {{{", ".join(values)}}}' if ref_type else ', '.join(values)
    return ref_type


def _managed_ref_summaries(lines, ref_resolver=None):
    """Return rid -> summary for Unity SerializeReference entries."""
    summaries = {}
    idx = 0
    while idx < len(lines):
        if lines[idx].strip() != 'references:':
            idx += 1
            continue

        references_indent = _indent_len(lines[idx])
        idx += 1
        while idx < len(lines):
            stripped = lines[idx].strip()
            if stripped and _indent_len(lines[idx]) <= references_indent:
                break
            if stripped != 'RefIds:':
                idx += 1
                continue

            refids_indent = _indent_len(lines[idx])
            idx += 1
            while idx < len(lines):
                stripped = lines[idx].strip()
                current_indent = _indent_len(lines[idx])
                if stripped and (current_indent < refids_indent or
                                 (current_indent == refids_indent and not stripped.startswith('- rid:'))):
                    break
                if not stripped.startswith('- rid:'):
                    idx += 1
                    continue

                rid = stripped.split(':', 1)[1].strip()
                entry_indent = _indent_len(lines[idx])
                entry_lines = []
                idx += 1
                while idx < len(lines):
                    entry_stripped = lines[idx].strip()
                    entry_indent_now = _indent_len(lines[idx])
                    if entry_stripped and entry_indent_now < refids_indent:
                        break
                    if entry_stripped.startswith('- rid:') and entry_indent_now == entry_indent:
                        break
                    entry_lines.append(lines[idx])
                    idx += 1

                summary = _summarize_managed_ref(entry_lines, ref_resolver)
                if rid:
                    summaries[rid] = summary or f'UnresolvedManagedReference:{rid}'

        # Continue scanning in case Unity ever emits multiple references blocks.
    return summaries


def _component_ref_key(value, ref_resolver=None):
    m = FILEID_REF_RE.search(value or '')
    if not m:
        return ''
    fid = m.group(1)
    label = ref_resolver(fid) if ref_resolver else ''
    if label and '/' in label:
        return label.rsplit('/', 1)[-1]
    if label:
        return label
    return f'fileID:{fid}'


def _parse_localize_record_metadata(lines, ref_resolver=None, ref_summaries=None):
    ref_summaries = ref_summaries or {}
    records = {}
    idx = 0
    while idx < len(lines):
        if lines[idx].strip() != 'm_records:':
            idx += 1
            continue

        record_index = 0
        records_indent = _indent_len(lines[idx])
        idx += 1
        while idx < len(lines):
            stripped = lines[idx].strip()
            indent = _indent_len(lines[idx])
            if stripped and (indent < records_indent or (indent == records_indent and not stripped.startswith('- '))):
                break
            if not stripped.startswith('- m_component:'):
                idx += 1
                continue

            record_indent = indent
            component_key = _component_ref_key(stripped.split(':', 1)[1].strip(), ref_resolver)
            property_name = ''
            pairs = {}
            pair_values = {}
            pair_index = 0
            idx += 1
            while idx < len(lines):
                current = lines[idx].strip()
                current_indent = _indent_len(lines[idx])
                if current and (current_indent < records_indent or
                                (current_indent == records_indent and not current.startswith('- '))):
                    break
                if current.startswith('- m_component:') and current_indent == record_indent:
                    break
                if current.startswith('m_propertyName:'):
                    property_name = current.split(':', 1)[1].strip()
                    idx += 1
                    continue
                if current.startswith('- m_key:'):
                    pair_indent = current_indent
                    locale_key = current.split(':', 1)[1].strip() or 'default'
                    rid = ''
                    idx += 1
                    while idx < len(lines):
                        pair_line = lines[idx].strip()
                        pair_line_indent = _indent_len(lines[idx])
                        if pair_line and (pair_line_indent < records_indent or
                                          (pair_line_indent == records_indent and not pair_line.startswith('- '))):
                            break
                        if pair_line.startswith('- m_component:') and pair_line_indent == record_indent:
                            break
                        if pair_line.startswith('- m_key:') and pair_line_indent == pair_indent:
                            break
                        if pair_line.startswith('rid:'):
                            rid = pair_line.split(':', 1)[1].strip()
                        idx += 1
                    pairs[pair_index] = locale_key
                    if rid and ref_summaries.get(rid):
                        pair_values[pair_index] = ref_summaries[rid]
                    pair_index += 1
                    continue
                idx += 1

            record_key = property_name or 'record'
            if component_key:
                record_key = f'{component_key}.{record_key}'
            records[record_index] = {
                'record_key': record_key,
                'pairs': pairs,
                'values': pair_values,
            }
            record_index += 1

    return records


def _source_record_metadata(guid: str, fid: str):
    if not guid or not fid:
        return {}

    use_resolver = bool(_asset_resolver)
    cache_key = (_prefab_name_cache_key(guid, use_resolver), guid, fid)
    if cache_key in _prefab_record_metadata:
        return _prefab_record_metadata[cache_key]

    def source_ref_resolver(ref_fid):
        component = prefab_target_component(guid, ref_fid)
        if component:
            return component
        path = prefab_target_path(guid, ref_fid)
        if path:
            return path
        name = prefab_target_label(guid, ref_fid)
        return '' if name.startswith('UnknownTarget:') else name

    for _type_id, section_fid, body, _is_stripped in _prefab_sections_for_guid(guid, use_resolver=use_resolver):
        if section_fid == fid:
            lines = body.splitlines()
            metadata = _parse_localize_record_metadata(
                lines,
                source_ref_resolver,
                _managed_ref_summaries(lines, source_ref_resolver),
            )
            _prefab_record_metadata[cache_key] = metadata
            return metadata

    _prefab_record_metadata[cache_key] = {}
    return {}


def _extract_localize_record_props(lines, ref_summaries, ref_resolver=None):
    props = []
    consumed_rids = set()
    skip_ranges = []
    idx = 0
    while idx < len(lines):
        if lines[idx].strip() != 'm_records:':
            idx += 1
            continue

        record_start = idx
        has_record_props = False
        records_indent = _indent_len(lines[idx])
        idx += 1
        while idx < len(lines):
            stripped = lines[idx].strip()
            indent = _indent_len(lines[idx])
            if stripped and (indent < records_indent or (indent == records_indent and not stripped.startswith('- '))):
                break
            if not stripped.startswith('- m_component:'):
                idx += 1
                continue

            record_indent = indent
            component_key = _component_ref_key(stripped.split(':', 1)[1].strip(), ref_resolver)
            property_name = ''
            pairs = []
            idx += 1
            while idx < len(lines):
                current = lines[idx].strip()
                current_indent = _indent_len(lines[idx])
                if current and (current_indent < records_indent or
                                (current_indent == records_indent and not current.startswith('- '))):
                    break
                if current.startswith('- m_component:') and current_indent == record_indent:
                    break
                if current.startswith('m_propertyName:'):
                    property_name = current.split(':', 1)[1].strip()
                    idx += 1
                    continue
                if current.startswith('- m_key:'):
                    pair_indent = current_indent
                    locale_key = current.split(':', 1)[1].strip() or 'default'
                    rid = ''
                    saw_rid = False
                    idx += 1
                    while idx < len(lines):
                        pair_line = lines[idx].strip()
                        pair_line_indent = _indent_len(lines[idx])
                        if pair_line and (pair_line_indent < records_indent or
                                          (pair_line_indent == records_indent and not pair_line.startswith('- '))):
                            break
                        if pair_line.startswith('- m_component:') and pair_line_indent == record_indent:
                            break
                        if pair_line.startswith('- m_key:') and pair_line_indent == pair_indent:
                            break
                        if pair_line.startswith('rid:'):
                            saw_rid = True
                            rid = pair_line.split(':', 1)[1].strip()
                        idx += 1
                    if rid or saw_rid:
                        pairs.append((locale_key, rid))
                    continue
                idx += 1

            record_key = property_name or 'record'
            if component_key:
                record_key = f'{component_key}.{record_key}'
            for locale_key, rid in pairs:
                prop_key = f'm_records[{record_key}].m_pairs[{locale_key}]'
                if rid:
                    summary = ref_summaries.get(rid) or f'UnresolvedManagedReference:{rid}'
                    props.append((prop_key, summary))
                    consumed_rids.add(rid)
                else:
                    props.append((f'{prop_key}.rid', 'UnresolvedManagedReference:(missing rid)'))
                has_record_props = True

        if has_record_props:
            skip_ranges.append((record_start, idx))

    return props, consumed_rids, skip_ranges


def _extract_managed_reference_props(body, ref_resolver=None):
    """Return stable summaries for Unity SerializeReference entries."""
    lines = body.splitlines()
    ref_summaries = _managed_ref_summaries(lines, ref_resolver)
    if not ref_summaries:
        return [], []

    record_props, consumed_rids, skip_ranges = _extract_localize_record_props(lines, ref_summaries, ref_resolver)
    fallback_props = [
        (f'managedReference[rid:{rid}]', ref_summaries[rid])
        for rid in sorted(ref_summaries)
        if rid not in consumed_rids
    ]
    return record_props + fallback_props, skip_ranges


def extract_managed_reference_props(body, ref_resolver=None):
    props, _skip_ranges = _extract_managed_reference_props(body, ref_resolver)
    return props


def extract_props(body, ref_resolver=None):
    """
    Extract (key, value) pairs from a YAML body.
    Handles simple key: value lines. Skips noise keys.
    Returns list of (key, val).
    """
    props, managed_record_skip_ranges = _extract_managed_reference_props(body, ref_resolver)
    managed_record_skip_starts = {start: end for start, end in managed_record_skip_ranges}
    lines = body.splitlines()
    idx = 0
    while idx < len(lines):
        line = lines[idx]
        stripped = line.strip()
        if not stripped or stripped.startswith('-') or ':' not in stripped:
            idx += 1
            continue
        colon = stripped.index(':')
        key = stripped[:colon].strip()
        val = stripped[colon+1:].strip()
        if key == 'references':
            idx = _skip_yaml_block(lines, idx)
            continue
        if key == 'm_records' and idx in managed_record_skip_starts:
            idx = managed_record_skip_starts[idx]
            continue
        if key == 'rid' and _indent_len(line) > 2:
            idx += 1
            continue
        if not val:
            items = []
            next_idx = idx + 1
            while next_idx < len(lines):
                item = lines[next_idx].strip()
                if not item.startswith('- '):
                    break
                items.append(translate_fileid_refs(item[2:].strip(), ref_resolver))
                next_idx += 1
            if items and key not in SKIP_PROPS:
                props.append((key, '[' + ', '.join(items) + ']'))
                idx = next_idx
                continue
            # Skip empty values (these are YAML type headers like "RectTransform:" or "MonoBehaviour:")
            idx += 1
            continue
        if key in SKIP_PROPS:
            idx += 1
            continue
        # Simplify fileID refs
        val = translate_fileid_refs(val, ref_resolver)
        if key and val is not None:
            props.append((key, val))
        idx += 1
    return _combine_vector_prop_pairs(_combine_color_prop_pairs(props))


def parse_prefab_instance_mods(body):
    """
    Parse PrefabInstance modification list.
    Returns list of (target_fid, target_guid, propertyPath, value).
    Grouped so caller can sort/display by target object.
    """
    mods = []
    # Each mod block: target + propertyPath + value lines
    blocks = re.split(r'(?=\n    - target:)', body)
    for block in blocks:
        target_m = re.search(r'target:\s*\{fileID:\s*(\d+)(?:,\s*guid:\s*([0-9a-f]+))?', block)
        path_m   = re.search(r'propertyPath:\s*(.+)', block)
        value_m  = re.search(r'(?m)^      value:\s*(.*)$', block)
        object_ref_m = re.search(r'(?m)^      objectReference:\s*(.*)$', block)
        if target_m and path_m:
            tfid  = target_m.group(1)
            tguid = target_m.group(2) if target_m.group(2) else ''  # 保留完整 guid
            prop  = path_m.group(1).strip()
            value = _decode_unicode_escapes(value_m.group(1).strip()) if value_m else ''
            object_ref = object_ref_m.group(1).strip() if object_ref_m else ''
            if not value and object_ref:
                value = object_ref
            # m_Name 保留用于 PrefabInstance 节点命名，其余 SKIP_PROPS 仍然跳过
            if prop and (prop == 'm_Name' or prop not in SKIP_PROPS):
                mods.append((tfid, tguid, prop, value))
    return mods


MANAGED_MOD_RE = re.compile(r'^managedReferences\[(\d+)\](?:\.(.+))?$')
M_RECORD_PAIR_VALUE_RE = re.compile(
    r'^m_records\.Array\.data\[(\d+)\]\.m_values\.m_pairs\.Array\.data\[(\d+)\]\.(.+)$'
)
M_RECORD_PAIR_ARRAY_RE = re.compile(r'^m_records\.Array\.data\[(\d+)\]\.m_values\.m_pairs\.Array\.(.+)$')
M_RECORD_FIELD_RE = re.compile(r'^m_records\.Array\.data\[(\d+)\]\.(.+)$')


def _managed_mod_type(value):
    if not value:
        return ''
    token = value.split()[-1]
    return token.rsplit('.', 1)[-1]


def _managed_ref_summary(ref):
    ref_type = ref.get('type', '')
    values = ref.get('values', {})
    ordered_keys = sorted(values, key=lambda key: (not key.startswith('m_value'), key))
    if ordered_keys:
        value_text = ', '.join(f'{key}: {values[key]}' for key in ordered_keys)
        return f'{ref_type} {{{value_text}}}' if ref_type else value_text
    return ref_type


def _collect_record_pair_keys(mods, source_guid=''):
    pair_keys = {}
    for tfid, tguid, prop, value in mods:
        pair_m = M_RECORD_PAIR_VALUE_RE.match(prop)
        if not pair_m or pair_m.group(3) != 'm_key':
            continue
        target_guid = tguid or source_guid
        record_index = int(pair_m.group(1))
        pair_index = int(pair_m.group(2))
        pair_keys[(target_guid, tfid, record_index, pair_index)] = value or 'default'
    return pair_keys


def _record_override_key(target_guid, tfid, prop, value='', pair_keys=None):
    pair_keys = pair_keys or {}
    pair_m = M_RECORD_PAIR_VALUE_RE.match(prop)
    array_m = M_RECORD_PAIR_ARRAY_RE.match(prop)
    field_m = M_RECORD_FIELD_RE.match(prop)
    if not (pair_m or array_m or field_m):
        return prop

    record_index = int((pair_m or array_m or field_m).group(1))
    record = _source_record_metadata(target_guid, tfid).get(record_index, {})
    record_key = record.get('record_key') or f'record#{record_index}'

    if pair_m:
        pair_index = int(pair_m.group(2))
        suffix = pair_m.group(3)
        pair_key = (target_guid, tfid, record_index, pair_index)
        if suffix == 'm_key':
            locale_key = value or pair_keys.get(pair_key) or 'default'
        else:
            locale_key = pair_keys.get(pair_key) or record.get('pairs', {}).get(pair_index) or ''
        locale_key = locale_key or f'pair#{pair_index}'
        if suffix in {'m_value', 'managedValue'}:
            return f'm_records[{record_key}].m_pairs[{locale_key}]'
        return f'm_records[{record_key}].m_pairs[{locale_key}].{suffix}'

    if array_m:
        return f'm_records[{record_key}].m_pairs.{array_m.group(2)}'

    return f'm_records[{record_key}].{field_m.group(2)}'


def _record_override_source_value(target_guid, tfid, prop):
    pair_m = M_RECORD_PAIR_VALUE_RE.match(prop)
    if not pair_m:
        return ''
    record = _source_record_metadata(target_guid, tfid).get(int(pair_m.group(1)), {})
    return record.get('values', {}).get(int(pair_m.group(2)), '')


def normalize_prefab_instance_mods(mods, source_guid=''):
    """Remove Unity-managed SerializeReference ids from prefab overrides."""
    managed_refs = defaultdict(dict)
    record_pair_keys = _collect_record_pair_keys(mods, source_guid)
    for tfid, tguid, prop, value in mods:
        managed_m = MANAGED_MOD_RE.match(prop)
        if not managed_m:
            continue
        target_guid = tguid or source_guid
        rid = managed_m.group(1)
        sub_path = managed_m.group(2) or ''
        ref = managed_refs[(target_guid, tfid)].setdefault(rid, {'type': '', 'values': {}})
        if sub_path:
            ref['values'][sub_path] = value
        else:
            ref['type'] = _managed_mod_type(value)

    normalized = []
    for tfid, tguid, prop, value in mods:
        target_guid = tguid or source_guid
        if MANAGED_MOD_RE.match(prop):
            continue
        if prop.startswith('m_records.') and prop.endswith('.m_value') and re.fullmatch(r'-?\d+', value or ''):
            summary = _managed_ref_summary(managed_refs.get((target_guid, tfid), {}).get(value, {}))
            if not summary:
                summary = _record_override_source_value(target_guid, tfid, prop)
            if not summary:
                summary = f'UnresolvedManagedReference:{value}'
            normalized.append((tfid, tguid, _record_override_key(target_guid, tfid, prop, value, record_pair_keys), summary))
            continue
        if prop.startswith('m_records.'):
            normalized.append((tfid, tguid, _record_override_key(target_guid, tfid, prop, value, record_pair_keys), value))
            continue
        normalized.append((tfid, tguid, prop, value))
    return _combine_vector_mods(_combine_color_mods(normalized))


def main():
    try:
        project_root_arg, files = _split_cli_args(sys.argv[1:])
        file_path = files[0] if files else None
        if file_path:
            with open(file_path, 'rb') as f:
                raw = f.read()
        else:
            raw = sys.stdin.buffer.read()
            file_path = None
        content = raw.decode('utf-8', errors='replace')
    except Exception as e:
        sys.stderr.write(f'prefab_textconv: read error: {e}\n')
        sys.exit(1)

    # 扫描 .cs.meta / .prefab.meta 建立缓存（优先从磁盘加载）
    project_root = _find_project_root(file_path, project_root_arg)
    if project_root:
        build_caches(project_root)

    try:
        out = convert(content)
    except Exception as e:
        sys.stderr.write(f'prefab_textconv: parse error: {e}\n')
        out = f'[parse error: {e}]\nFile size: {len(content)} bytes\n'

    sys.stdout.buffer.write(out.encode('utf-8'))
    sys.stdout.buffer.write(b'\n')


def convert(content):
    sections = parse_all_sections(content)
    # O(1) lookup: fid → (type_id, body)
    by_fid = {fid: (type_id, body) for type_id, fid, body in sections}

    # ── Index objects ─────────────────────────────────────────────────────
    go_name   = {}   # go_fid  → name
    go_active = {}   # go_fid  → bool
    go_layer  = {}   # go_fid  → int
    go_tag    = {}   # go_fid  → tag str
    go_source_path = {}  # stripped go_fid → source prefab path
    go_owner_pi = {}     # stripped go_fid → owning PrefabInstance fid
    tfm_go    = {}   # tfm_fid → go_fid
    go_tfm    = {}   # go_fid  → tfm_fid
    tfm_par   = {}   # tfm_fid → parent_tfm_fid | None
    tfm_chd   = {}   # tfm_fid → [child_tfm_fid]
    comp_go   = defaultdict(list)   # go_fid → [(type_id, fid, body)]
    pi_list   = []   # (pi_fid, body) — PrefabInstance objects
    pi_info   = {}   # pi_fid → (src_guid, parent_tfm_fid)
    parent_to_pis = defaultdict(list)  # parent_tfm_fid → [pi_fid]
    stripped_tfm_owner = {}  # stripped_tfm_fid → owning_pi_fid
    stripped_tfm_source_path = {}  # stripped_tfm_fid → source prefab path

    for type_id, fid, body in sections:
        if type_id == 1:
            nm = re.search(r'm_Name:\s*(.+)', body)
            src_m = re.search(
                r'm_CorrespondingSourceObject:\s*\{fileID:\s*(\d+),\s*guid:\s*([0-9a-f]+)',
                body)
            pi_ref = re.search(r'm_PrefabInstance:\s*\{fileID:\s*(\d+)\}', body)
            ac = re.search(r'm_IsActive:\s*(\d+)', body)
            ly = re.search(r'm_Layer:\s*(\d+)', body)
            tg = re.search(r'm_TagString:\s*(.+)', body)
            if nm:
                go_name[fid] = _decode_unicode_escapes(nm.group(1).strip())
            elif src_m:
                go_name[fid] = prefab_target_label(src_m.group(2), src_m.group(1))
                go_source_path[fid] = prefab_target_path(src_m.group(2), src_m.group(1))
                if pi_ref:
                    go_owner_pi[fid] = pi_ref.group(1)
            else:
                go_name[fid] = fid
            go_active[fid] = (ac.group(1) == '1') if ac else True
            go_layer[fid]  = int(ly.group(1)) if ly else 0
            go_tag[fid]    = tg.group(1).strip() if tg else ''

        elif type_id in (4, 224):  # Transform or RectTransform — both define hierarchy
            go_ref  = re.search(r'm_GameObject:\s*\{fileID:\s*(\d+)\}', body)
            par_ref = re.search(r'm_Father:\s*\{fileID:\s*(\d+)\}', body)
            # Parse children: slice from m_Children: up to m_Father: to avoid mis-capturing parent
            ch_start = body.find('m_Children:')
            if ch_start >= 0:
                chd_fids = _parse_transform_children(body)
            else:
                chd_fids = []
            gfid = go_ref.group(1) if go_ref else None
            pfid = par_ref.group(1) if par_ref else None
            if gfid:
                tfm_go[fid]  = gfid
                go_tfm[gfid] = fid
            else:
                # Stripped transform — record owning PrefabInstance
                pi_ref = re.search(r'm_PrefabInstance:\s*\{fileID:\s*(\d+)\}', body)
                if pi_ref:
                    stripped_tfm_owner[fid] = pi_ref.group(1)
                src_ref = _source_object_ref(body)
                if src_ref:
                    stripped_tfm_source_path[fid] = prefab_target_path(src_ref[1], src_ref[0])
            tfm_par[fid] = pfid if pfid and pfid != '0' else None
            tfm_chd[fid] = chd_fids

        elif type_id == 1001:
            pi_list.append((fid, body))
            src_m    = re.search(r'm_SourcePrefab:.*?guid:\s*([0-9a-f]+)', body)
            par_m    = re.search(r'm_TransformParent:\s*\{fileID:\s*(\d+)\}', body)
            if src_m:
                src_guid   = src_m.group(1)
                parent_fid = par_m.group(1) if par_m else '0'
                pi_info[fid] = (src_guid, parent_fid, body)
                parent_to_pis[parent_fid].append(fid)

        elif type_id not in (1, 4, 224):  # components (not GO, not any Transform)
            go_ref = re.search(r'm_GameObject:\s*\{fileID:\s*(\d+)\}', body)
            if go_ref:
                comp_go[go_ref.group(1)].append((type_id, fid, body))

    # ── Build parent-PI → child-PI map (for nested PrefabInstances) ──────
    pi_children = defaultdict(list)  # parent_pi_fid → [child_pi_fid]
    for parent_tfm, child_pis in parent_to_pis.items():
        if parent_tfm == '0':
            continue
        owning_pi = stripped_tfm_owner.get(parent_tfm)
        if owning_pi:
            pi_children[owning_pi].extend(child_pis)

    # ── Render (flat path format) ────────────────────────────────────────
    lines = []
    rendered_paths = set()
    rendered_go_fids = set()
    rendered_tfm_fids = set()
    pi_render_path = {}
    ref_labels = {}

    def _comp_name(type_id, cbody):
        """Resolve component display name."""
        return component_name(type_id, cbody)

    def _resolve_local_ref(fid):
        return ref_labels.get(fid, '')

    def _source_path_without_root(source_path):
        parts = [part for part in (source_path or '').split('/') if part]
        if len(parts) <= 1:
            return ''
        return '/'.join(parts[1:])

    def render_node(tfm_fid, path_prefix):
        go_fid = tfm_go.get(tfm_fid)
        if not go_fid:
            return

        name   = go_name.get(go_fid, go_fid)
        active = go_active.get(go_fid, True)
        tag    = go_tag.get(go_fid, '')

        path = f'{path_prefix}/{name}' if path_prefix else name

        flags = []
        if not active:
            flags.append('Inactive')
        if tag and tag not in ('Untagged', ''):
            flags.append(f'tag:{tag}')
        flag_str = f'  [{", ".join(flags)}]' if flags else ''
        lines.append(f'[{path}]{flag_str}')
        rendered_paths.add(path)
        rendered_go_fids.add(go_fid)
        rendered_tfm_fids.add(tfm_fid)

        # Transform / RectTransform
        if tfm_fid in by_fid:
            tfm_type_id, tbody = by_fid[tfm_fid]
            tname = 'RectTransform' if tfm_type_id == 224 else 'Transform'
            ref_labels[go_fid] = f'{path}/GameObject'
            ref_labels[tfm_fid] = f'{path}/{tname}'
            for type_id, cfid, cbody in comp_go.get(go_fid, []):
                ref_labels[cfid] = f'{path}/{_comp_name(type_id, cbody)}'
            for key, val in extract_props(tbody, _resolve_local_ref):
                lines.append(f'  {tname}.{key}: {val}')

        # Components
        for type_id, cfid, cbody in comp_go.get(go_fid, []):
            cname = _comp_name(type_id, cbody)
            ref_labels[cfid] = f'{path}/{cname}'
            for key, val in extract_props(cbody, _resolve_local_ref):
                lines.append(f'  {cname}.{key}: {val}')

        # Children
        for child_fid in tfm_chd.get(tfm_fid, []):
            if child_fid in tfm_go:
                render_node(child_fid, path)

        # Nested PrefabInstances parented here
        for pi_fid in parent_to_pis.get(tfm_fid, []):
            render_pi_node(pi_fid, path)

    def render_pi_node(pi_fid, path_prefix):
        """Render a PrefabInstance + its overrides in flat format."""
        src_guid, _parent_fid, body = pi_info[pi_fid]
        src_name = prefab_label(src_guid)

        mods = normalize_prefab_instance_mods(parse_prefab_instance_mods(body), src_guid)
        custom_name = next((v for _, _, p, v in mods if p == 'm_Name'), None)

        if custom_name:
            node_label = f'{custom_name} ({src_name})'
        else:
            node_label = src_name
        path = f'{path_prefix}/{node_label}' if path_prefix else node_label
        lines.append(f'[{path}] [Prefab]')
        rendered_paths.add(path)
        pi_render_path[pi_fid] = path

        # Overrides grouped by target hierarchy path. Unity stores PrefabInstance
        # modifications on the instance object, but reviewers expect fields on
        # stripped child nodes to appear on the actual node/component they affect.
        other_mods = [(tfid, tguid, prop, val) for tfid, tguid, prop, val in mods if prop != 'm_Name']
        if other_mods:
            by_target_path = {}
            fallback_by_target = {}
            unresolved_by_guid = defaultdict(set)
            prepared_mods = []
            for tfid, tguid, prop, val in other_mods:
                target_guid = tguid or src_guid
                source_path = _prefab_target_path_without_similarity(target_guid, tfid)
                if not source_path and _asset_resolver:
                    unresolved_by_guid[target_guid].add(tfid)
                prepared_mods.append((tfid, target_guid, prop, val, source_path))

            for target_guid, unresolved_fids in unresolved_by_guid.items():
                _prefill_similar_prefab_target_hints(target_guid, unresolved_fids)

            for tfid, target_guid, prop, val, source_path in prepared_mods:
                if not source_path:
                    source_path = prefab_target_path(target_guid, tfid)
                relative_path = _source_path_without_root(source_path)
                if relative_path:
                    target_path = f'{path}/{relative_path}'
                    comp = prefab_target_component(target_guid, tfid) or 'PrefabOverride'
                    by_target_path.setdefault(target_path, []).append((comp, prop, val))
                else:
                    fallback_by_target.setdefault(tfid, (target_guid, []))[1].append((prop, val))

            if fallback_by_target:
                remaining_fallback = {}
                for tfid, (target_guid, prop_list) in fallback_by_target.items():
                    source_path, component = _prefab_property_target_hint(target_guid, prop_list)
                    relative_path = _source_path_without_root(source_path)
                    if source_path and (relative_path or source_path):
                        target_path = f'{path}/{relative_path}' if relative_path else path
                        comp = component or 'PrefabOverride'
                        for prop, val in prop_list:
                            by_target_path.setdefault(target_path, []).append((comp, prop, val))
                    else:
                        remaining_fallback[tfid] = (target_guid, prop_list)
                fallback_by_target = remaining_fallback

            for target_path, prop_list in by_target_path.items():
                if target_path != path:
                    lines.append(f'[{target_path}] [PrefabOverride]')
                    rendered_paths.add(target_path)
                for comp, prop, val in prop_list:
                    val = translate_fileid_refs(val, _resolve_local_ref)
                    lines.append(f'  {comp}.{prop}: {val}')

            for tfid, (target_guid, prop_list) in fallback_by_target.items():
                obj_name = prefab_target_label(target_guid, tfid)
                if obj_name.startswith('UnknownTarget:'):
                    obj_name = f'{prefab_label(target_guid)}#{tfid}'
                    target_path = f'{path}/PrefabOverrides/{obj_name}'
                else:
                    target_path = f'{path}/{obj_name}'
                comp = prefab_target_component(target_guid, tfid) or 'PrefabOverride'
                lines.append(f'[{target_path}] [PrefabOverride]')
                rendered_paths.add(target_path)
                for prop, val in prop_list:
                    val = translate_fileid_refs(val, _resolve_local_ref)
                    lines.append(f'  {comp}.{prop}: {val}')

        # Recurse into child PrefabInstances (nested within this PI's stripped transforms)
        for child_pi_fid in pi_children.get(pi_fid, []):
            if child_pi_fid in pi_info:
                render_pi_node(child_pi_fid, path)

    root_tfms = [fid for fid, par in tfm_par.items() if par is None]
    for rtfm in root_tfms:
        render_node(rtfm, '')

    # Root-level PrefabInstances (m_TransformParent == 0)
    for pi_fid in parent_to_pis.get('0', []):
        render_pi_node(pi_fid, '')

    def _stripped_tfm_display_path(tfm_fid):
        owner_path = pi_render_path.get(stripped_tfm_owner.get(tfm_fid, ''))
        source_path = stripped_tfm_source_path.get(tfm_fid, '')
        if owner_path:
            relative_path = _source_path_without_root(source_path)
            return f'{owner_path}/{relative_path}' if relative_path else owner_path
        if source_path:
            return f'Orphan/{source_path}'
        return 'Orphan'

    def _orphan_display_path(go_fid):
        owner_path = pi_render_path.get(go_owner_pi.get(go_fid, ''))
        source_path = go_source_path.get(go_fid, '')
        if owner_path:
            relative_path = _source_path_without_root(source_path)
            return f'{owner_path}/{relative_path}' if relative_path else owner_path
        if source_path:
            return f'Orphan/{source_path}'
        return f'Orphan/{go_name.get(go_fid, go_fid)}'

    def _detached_root_for(tfm_fid):
        current = tfm_fid
        parent = tfm_par.get(current)
        while parent in tfm_go and tfm_go.get(parent) not in rendered_go_fids:
            current = parent
            parent = tfm_par.get(current)
        return current

    detached_roots = []
    seen_detached = set()
    for tfm_fid, go_fid in tfm_go.items():
        if go_fid in rendered_go_fids:
            continue
        root = _detached_root_for(tfm_fid)
        if root not in seen_detached:
            seen_detached.add(root)
            detached_roots.append(root)

    for tfm_fid in detached_roots:
        parent = tfm_par.get(tfm_fid)
        prefix = _stripped_tfm_display_path(parent) if parent and parent not in tfm_go else 'Orphan'
        render_node(tfm_fid, prefix)

    # Orphan GOs: keep them in a hierarchy when Unity only keeps stripped refs.
    for go_fid in go_name:
        if go_fid not in rendered_go_fids:
            orphan_path = _orphan_display_path(go_fid)
            if orphan_path and orphan_path not in rendered_paths:
                lines.append(f'[{orphan_path}] [Orphan]')
                rendered_paths.add(orphan_path)

    lines = [translate_fileid_refs(line, _resolve_local_ref) for line in lines]
    return '\n'.join(lines)


if __name__ == '__main__':
    main()
