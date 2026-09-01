"""AST-based source chunking for the RAG subsystem.

Splits a source file into function/method/class-skeleton chunks using
tree-sitter, so retrieval can pull individual symbols instead of whole
files. Falls back to a single whole-file chunk (matching work.py's
existing 10,000-char read_files() truncation) on parse failure or for
unsupported extensions — RAG never does worse than today's legacy
behavior for a given file, only better.

Node-type names below were verified against tree-sitter-dart 0.1.0 on
real EstateWiseFlutter source (2026-08-31) — see the verification note
on _DART_METHOD_TYPES. Python/JS/TS/Go node names are standard,
long-stable tree-sitter grammar conventions and were not individually
re-verified; a wrong guess there only degrades to the whole-file
fallback for that one file, never breaks anything.
"""

from __future__ import annotations

import hashlib
import importlib
import os
from dataclasses import dataclass

MAX_WHOLE_FILE_CHARS = 10_000  # matches work.py's read_files() truncation

# A single chunk this large defeats retrieval's purpose and can single-
# handedly exhaust rag_retrieve's char budget before any other candidate
# file gets a turn — observed directly during rollout validation: a
# Flutter StatelessWidget's build() method (a common pattern: the ENTIRE
# widget tree in one method) chunked to 12,800+ chars, scored highest for
# a UI-related query, and starved every other relevant file of any
# representation at all. Truncate individual chunks; the class_skeleton
# chunk for the same class still conveys the method's existence/signature.
MAX_CHUNK_CHARS = int(os.getenv("RAG_MAX_CHUNK_CHARS", "1500"))


@dataclass(frozen=True)
class Chunk:
    id: str
    file_path: str
    symbol: str
    kind: str  # "function" | "method" | "class_skeleton" | "file"
    start_line: int  # 1-based, inclusive
    end_line: int  # 1-based, inclusive
    text: str


# Dart's grammar (tree_sitter_dart 0.1.0) puts a definition's signature and
# body as ADJACENT SIBLINGS, not parent/child — a method_signature node's
# next_sibling is a separate function_body node. Confirmed by direct
# inspection: class_definition has fields (name, superclass, body); a class's
# class_body children include `method_signature` wrapping `function_signature`,
# immediately followed by a sibling `function_body`. Top-level functions are
# the same shape: `function_signature` followed by a sibling `function_body`.
_DART_METHOD_TYPES = {"method_signature"}
_DART_FUNCTION_TYPES = {"function_signature"}
_DART_CLASS_TYPES = {"class_definition", "mixin_declaration", "extension_declaration"}

LANGUAGE_CONFIG: dict[str, dict] = {
    ".dart": {
        "module": "tree_sitter_dart",
        "lang_fn": "language",
        "class_types": _DART_CLASS_TYPES,
        "method_types": _DART_METHOD_TYPES,
        "function_types": _DART_FUNCTION_TYPES,
        "sibling_body": True,  # signature/body are adjacent siblings, not nested
        "body_type": "function_body",
    },
    ".py": {
        "module": "tree_sitter_python",
        "lang_fn": "language",
        "class_types": {"class_definition"},
        "method_types": set(),  # methods are function_definition nodes inside a class body
        "function_types": {"function_definition"},
        "sibling_body": False,
        "body_type": None,
    },
    ".js": {
        "module": "tree_sitter_javascript",
        "lang_fn": "language",
        "class_types": {"class_declaration"},
        "method_types": {"method_definition"},
        "function_types": {"function_declaration"},
        "sibling_body": False,
        "body_type": None,
    },
    ".jsx": {
        "module": "tree_sitter_javascript",
        "lang_fn": "language",
        "class_types": {"class_declaration"},
        "method_types": {"method_definition"},
        "function_types": {"function_declaration"},
        "sibling_body": False,
        "body_type": None,
    },
    ".ts": {
        "module": "tree_sitter_typescript",
        "lang_fn": "language_typescript",
        "class_types": {"class_declaration", "interface_declaration"},
        "method_types": {"method_definition", "method_signature"},
        "function_types": {"function_declaration", "type_alias_declaration"},
        "sibling_body": False,
        "body_type": None,
    },
    ".tsx": {
        "module": "tree_sitter_typescript",
        "lang_fn": "language_tsx",
        "class_types": {"class_declaration", "interface_declaration"},
        "method_types": {"method_definition", "method_signature"},
        "function_types": {"function_declaration", "type_alias_declaration"},
        "sibling_body": False,
        "body_type": None,
    },
    ".go": {
        "module": "tree_sitter_go",
        "lang_fn": "language",
        "class_types": {"type_declaration"},
        "method_types": {"method_declaration"},
        "function_types": {"function_declaration"},
        "sibling_body": False,
        "body_type": None,
    },
}

_PARSER_CACHE: dict[str, object] = {}


def _get_parser(ext: str):
    if ext not in LANGUAGE_CONFIG:
        return None
    if ext not in _PARSER_CACHE:
        from tree_sitter import Language, Parser

        cfg = LANGUAGE_CONFIG[ext]
        try:
            mod = importlib.import_module(cfg["module"])
            lang = Language(getattr(mod, cfg["lang_fn"])())
            _PARSER_CACHE[ext] = Parser(lang)
        except Exception:
            _PARSER_CACHE[ext] = None
    return _PARSER_CACHE[ext]


def _chunk_id(file_path: str, kind: str, symbol: str, disambiguator: int = 0) -> str:
    key = f"{file_path}:{kind}:{symbol}:{disambiguator}"
    return hashlib.sha1(key.encode("utf8")).hexdigest()[:16]


def _node_name(node, source: bytes, _depth: int = 0) -> str:
    """Find a definition's name, recursing into a single wrapper child if needed.

    Some grammars (confirmed for Dart's method_signature -> function_signature)
    put the `name` field on an inner node, not the outer one being chunked.
    """
    name_node = node.child_by_field_name("name")
    if name_node is None:
        for child in node.children:
            if "identifier" in child.type:
                name_node = child
                break
    if name_node is not None:
        return source[name_node.start_byte:name_node.end_byte].decode("utf8", "replace")
    if _depth < 3:
        for child in node.children:
            if child.type in ("{", "}", "(", ")", ";"):
                continue
            found = _node_name(child, source, _depth + 1)
            if found != "<anonymous>":
                return found
    return "<anonymous>"


def _span_text(source: bytes, start_byte: int, end_byte: int) -> str:
    return source[start_byte:end_byte].decode("utf8", "replace")


def _whole_file_chunk(rel_path: str, text: str) -> list[Chunk]:
    truncated = text if len(text) <= MAX_WHOLE_FILE_CHARS else text[:MAX_WHOLE_FILE_CHARS] + "\n...[truncated]"
    return [Chunk(
        id=_chunk_id(rel_path, "file", ""),
        file_path=rel_path,
        symbol="",
        kind="file",
        start_line=1,
        end_line=text.count("\n") + 1,
        text=truncated,
    )]


def _walk(node):
    yield node
    for child in node.children:
        yield from _walk(child)


def _is_chunkable_symbol(node, cfg: dict) -> bool:
    """True for a node that should become its own function/method chunk.

    Some grammars (confirmed for Dart) wrap a method's signature in an outer
    node (method_signature) whose child is the same node type used for
    top-level functions (function_signature). Walking unconditionally would
    match both the outer wrapper and the inner node for one method — skip
    the inner one when its direct parent is already a method-type wrapper.
    """
    if node.type not in cfg["method_types"] and node.type not in cfg["function_types"]:
        return False
    parent = node.parent
    if parent is not None and parent.type in cfg["method_types"] and node.type in cfg["function_types"]:
        return False
    return True


_SKELETON_SKIP_TYPES = {"{", "}", ";", ",", "function_body", "block"}


def _class_skeleton_text(cls_node, source: bytes, cfg: dict) -> str:
    """Header + field/misc declarations + one-line method signatures (no bodies)."""
    body_node = cls_node.child_by_field_name("body")
    header_end = cls_node.end_byte
    if body_node is not None:
        for child in body_node.children:
            if child.type == "{":
                header_end = child.end_byte
                break
    header = _span_text(source, cls_node.start_byte, header_end)

    lines = []
    method_node_ids = set()
    for m in _walk(cls_node):
        if m is cls_node or not _is_chunkable_symbol(m, cfg):
            continue
        method_node_ids.add(id(m))
        name = _node_name(m, source)
        sig_line = _span_text(source, m.start_byte, m.end_byte).splitlines()[0].strip()
        lines.append((m.start_byte, f"  {sig_line} {{ ... }}  // {name}"))

    if body_node is not None:
        for child in body_node.children:
            if child.type in _SKELETON_SKIP_TYPES:
                continue
            if child.type in cfg["method_types"] or child.type in cfg["function_types"]:
                continue  # already listed above via the method-chunk walk
            if cfg.get("sibling_body") and child.type == cfg.get("body_type"):
                continue  # a method's body, immediately follows its signature — not a field
            one_line = _span_text(source, child.start_byte, child.end_byte).splitlines()[0].strip()
            if one_line:
                lines.append((child.start_byte, f"  {one_line}"))

    lines.sort(key=lambda pair: pair[0])
    body_text = "\n".join(text for _, text in lines)
    return header + "\n" + body_text + "\n}"


def _line_of(source: bytes, byte_offset: int) -> int:
    return source.count(b"\n", 0, byte_offset) + 1


def chunk_file(rel_path: str, project_root: str) -> list[Chunk]:
    ext = os.path.splitext(rel_path)[1].lower()
    full_path = os.path.join(project_root, rel_path)
    try:
        text = open(full_path, encoding="utf8", errors="replace").read()
    except OSError:
        return []

    cfg = LANGUAGE_CONFIG.get(ext)
    parser = _get_parser(ext) if cfg else None
    if parser is None:
        return _whole_file_chunk(rel_path, text)

    source = text.encode("utf8")
    try:
        tree = parser.parse(source)
    except Exception:
        return _whole_file_chunk(rel_path, text)

    root = tree.root_node
    chunks: list[Chunk] = []
    seen_symbol_counts: dict[tuple[str, str], int] = {}

    def next_disambiguator(kind: str, symbol: str) -> int:
        key = (kind, symbol)
        seen_symbol_counts[key] = seen_symbol_counts.get(key, 0) + 1
        return seen_symbol_counts[key] - 1

    for node in _walk(root):
        if node.type in cfg["class_types"]:
            symbol = _node_name(node, source)
            skeleton = _class_skeleton_text(node, source, cfg)
            if len(skeleton) > MAX_CHUNK_CHARS:
                skeleton = skeleton[:MAX_CHUNK_CHARS] + "\n...[truncated]\n}"
            chunks.append(Chunk(
                id=_chunk_id(rel_path, "class_skeleton", symbol, next_disambiguator("class_skeleton", symbol)),
                file_path=rel_path, symbol=symbol, kind="class_skeleton",
                start_line=_line_of(source, node.start_byte),
                end_line=_line_of(source, node.end_byte),
                text=skeleton,
            ))
            # Methods inside this class are still walked and emitted below
            # via the same node-type checks (the walk covers all descendants).

        if _is_chunkable_symbol(node, cfg):
            kind = "method" if node.type in cfg["method_types"] else "function"
            symbol = _node_name(node, source)
            start_byte = node.start_byte
            end_byte = node.end_byte
            if cfg.get("sibling_body") and node.next_sibling is not None and \
                    node.next_sibling.type == cfg.get("body_type"):
                end_byte = node.next_sibling.end_byte
            text_span = _span_text(source, start_byte, end_byte)
            if len(text_span) > MAX_CHUNK_CHARS:
                text_span = text_span[:MAX_CHUNK_CHARS] + "\n...[truncated]"
            chunks.append(Chunk(
                id=_chunk_id(rel_path, kind, symbol, next_disambiguator(kind, symbol)),
                file_path=rel_path, symbol=symbol, kind=kind,
                start_line=_line_of(source, start_byte),
                end_line=_line_of(source, end_byte),
                text=text_span,
            ))

    if not chunks:
        return _whole_file_chunk(rel_path, text)
    return chunks
