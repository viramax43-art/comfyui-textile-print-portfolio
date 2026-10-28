from __future__ import annotations

import argparse
import ast
import json
import os
from pathlib import Path
from typing import Dict, Tuple


def const_str(node: ast.AST):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def type_of(value_node: ast.AST) -> str:
    if isinstance(value_node, ast.Tuple) and value_node.elts:
        first = value_node.elts[0]
        literal = const_str(first)
        if literal:
            return literal
        if isinstance(first, (ast.List, ast.Tuple)):
            return "COMBO"
        if isinstance(first, ast.Call):
            func = first.func
            return "COMBO(%s)" % getattr(func, "attr", getattr(func, "id", ""))
        if isinstance(first, ast.Attribute):
            return first.attr
    return "?"


def parse_input_types(cls: ast.ClassDef) -> Tuple[Dict[str, str], Dict[str, str]]:
    required: Dict[str, str] = {}
    optional: Dict[str, str] = {}
    for item in cls.body:
        if not isinstance(item, ast.FunctionDef) or item.name != "INPUT_TYPES":
            continue
        for node in ast.walk(item):
            if not isinstance(node, ast.Return) or not isinstance(node.value, ast.Dict):
                continue
            for key, value in zip(node.value.keys, node.value.values):
                section = const_str(key)
                if section not in ("required", "optional") or not isinstance(value, ast.Dict):
                    continue
                target = required if section == "required" else optional
                for input_key, input_value in zip(value.keys, value.values):
                    name = const_str(input_key)
                    if name:
                        target[name] = type_of(input_value)
    return required, optional


def parse_io_schema(cls: ast.ClassDef) -> Tuple[Dict[str, str], Dict[str, str]]:
    required: Dict[str, str] = {}
    optional: Dict[str, str] = {}
    for node in ast.walk(cls):
        if not isinstance(node, ast.Call) or getattr(node.func, "attr", "") != "Input" or not node.args:
            continue
        name = const_str(node.args[0])
        if not name:
            continue
        type_name = getattr(getattr(node.func, "value", None), "attr", "?")
        is_optional = any(
            kw.arg == "optional" and isinstance(kw.value, ast.Constant) and kw.value.value is True
            for kw in node.keywords
        )
        (optional if is_optional else required)[name] = type_name
    return required, optional


def node_id_of(cls: ast.ClassDef):
    for node in ast.walk(cls):
        if isinstance(node, ast.Call):
            for kw in node.keywords:
                if kw.arg == "node_id":
                    return const_str(kw.value)
    return None


def count_outputs(cls: ast.ClassDef):
    for item in cls.body:
        if isinstance(item, (ast.Assign, ast.AnnAssign)) and item.value is not None:
            targets = item.targets if isinstance(item, ast.Assign) else [item.target]
            for target in targets:
                if isinstance(target, ast.Name) and target.id == "RETURN_TYPES":
                    if isinstance(item.value, ast.Tuple):
                        return len(item.value.elts)
                    return 1
    for node in ast.walk(cls):
        if isinstance(node, ast.keyword) and node.arg == "outputs" and isinstance(node.value, ast.List):
            return len([e for e in node.value.elts if not (isinstance(e, ast.Constant) and e.value is None)])
    return None


def read_version(comfyui_root: Path) -> str:
    version_file = comfyui_root / "comfyui_version.py"
    if version_file.exists():
        for line in version_file.read_text(encoding="utf-8").splitlines():
            if line.startswith("__version__"):
                return line.split("=", 1)[1].strip().strip('"')
    return "unknown"


def build_catalog(comfyui_root: Path) -> Dict[str, dict]:
    catalog: Dict[str, dict] = {}
    files = [comfyui_root / "nodes.py"] + sorted((comfyui_root / "comfy_extras").glob("*.py"))
    for path in files:
        if not path.exists():
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for cls in [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]:
            required, optional = parse_input_types(cls)
            if not required:
                required, optional = parse_io_schema(cls)
            if not required:
                continue
            key = node_id_of(cls) or cls.name
            catalog[key] = {
                "required": required,
                "optional": optional,
                "outputs": count_outputs(cls),
                "file": os.path.relpath(path, comfyui_root).replace("\\", "/"),
            }
    return catalog


def main() -> int:
    parser = argparse.ArgumentParser(description="Снимок каталога встроенных нод ComfyUI")
    parser.add_argument("--comfyui", required=True, help="путь к папке ComfyUI (где лежит nodes.py)")
    parser.add_argument(
        "--out",
        default=str(Path(__file__).resolve().parent / "node_catalog.json"),
        help="куда сохранить снимок",
    )
    args = parser.parse_args()

    root = Path(args.comfyui).expanduser().resolve()
    if not (root / "nodes.py").exists():
        raise SystemExit(f"Не найден {root / 'nodes.py'} — это точно папка ComfyUI?")

    catalog = build_catalog(root)
    version = read_version(root)
    payload = {
        "_meta": {
            "description": "Снимок каталога встроенных нод ComfyUI: имена входов, типы и число выходов. "
                           "Нужен validate_workflows.py, чтобы проверять workflow JSON без запуска ComfyUI.",
            "comfyui_version": version,
            "source": "https://github.com/comfyanonymous/ComfyUI",
            "generated_by": "tools/snapshot_node_catalog.py",
        },
        "nodes": catalog,
    }

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"ComfyUI {version}: нод {len(catalog)} -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
