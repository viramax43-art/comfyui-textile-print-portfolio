from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List

OUTPUT_NODES = {"SaveImage", "PreviewImage", "MaskPreview", "SaveAnimatedWEBP", "SaveAudio"}

ROOT = Path(__file__).resolve().parent.parent

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def is_link(value: Any) -> bool:
    return (
        isinstance(value, list)
        and len(value) == 2
        and isinstance(value[0], str)
        and isinstance(value[1], int)
    )


WIDGET_TYPES = {"INT", "FLOAT", "STRING", "BOOLEAN", "COMBO", "SAMPLERS", "SCHEDULERS", "UPSCALE_METHODS", "CROP_METHODS"}


def is_widget_type(declared: str) -> bool:
    return declared.split("(")[0].upper() in WIDGET_TYPES


def widget_matches(declared: str, value: Any) -> bool:
    base = declared.split("(")[0].upper()
    if base == "INT":
        return isinstance(value, int) and not isinstance(value, bool)
    if base == "FLOAT":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if base == "BOOLEAN":
        return isinstance(value, bool)
    if base == "STRING":
        return isinstance(value, str)
    return isinstance(value, str)


class Report:
    def __init__(self) -> None:
        self.errors: List[str] = []
        self.warnings: List[str] = []

    def error(self, msg: str) -> None:
        self.errors.append(msg)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)


def load_catalog(path: Path) -> Dict[str, Dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    nodes = data.get("nodes", data)
    if not isinstance(nodes, dict):
        raise SystemExit(f"Каталог нод повреждён: {path}")
    return nodes


def validate_workflow(path: Path, catalog: Dict[str, Dict[str, Any]], report: Report) -> Dict[str, int]:
    try:
        prompt = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        report.error(f"{path.name}: невалидный JSON ({exc})")
        return {}

    if not isinstance(prompt, dict) or not prompt:
        report.error(f"{path.name}: ожидался непустой объект {{node_id: node}}")
        return {}

    links = 0
    producers: Dict[str, set] = {}

    for node_id, node in prompt.items():
        if not isinstance(node, dict):
            report.error(
                f"{path.name}: ключ верхнего уровня '{node_id}' — не нода. API-формат ComfyUI допускает "
                f"только {{node_id: {{class_type, inputs}}}}; метаданные кладутся в per-node _meta"
            )
            continue
        class_type = node.get("class_type")
        inputs = node.get("inputs")
        if not isinstance(class_type, str):
            report.error(f"{path.name}: нода '{node_id}' без class_type")
            continue
        if class_type not in catalog:
            report.error(
                f"{path.name}: нода '{node_id}' — class_type '{class_type}' отсутствует в каталоге "
                f"встроенных нод (кастомная нода или опечатка)"
            )
            continue
        if not isinstance(inputs, dict):
            report.error(f"{path.name}: нода '{node_id}' ({class_type}) без блока inputs")
            continue

        spec = catalog[class_type]
        required = spec.get("required", {})
        optional = spec.get("optional", {})
        known = set(required) | set(optional)

        for name in required:
            if name not in inputs:
                report.error(
                    f"{path.name}: нода '{node_id}' ({class_type}) — не заполнен обязательный вход '{name}'"
                )
        for name in inputs:
            if name not in known:
                report.warn(
                    f"{path.name}: нода '{node_id}' ({class_type}) — неизвестный вход '{name}'"
                )

        for name, value in inputs.items():
            if not is_link(value):
                declared = (catalog.get(class_type, {}).get("required") or {}).get(name) or (
                    catalog.get(class_type, {}).get("optional") or {}
                ).get(name)
                if declared is None:
                    continue
                if not is_widget_type(declared):
                    report.error(
                        f"{path.name}: нода '{node_id}'.{name} ожидает связь типа {declared}, "
                        f"а пришло значение {value!r}"
                    )
                elif not widget_matches(declared, value):
                    report.error(
                        f"{path.name}: нода '{node_id}'.{name} ({declared}) — значение {value!r} не того типа"
                    )
                continue
            links += 1
            src_id, slot = value
            if src_id not in prompt:
                report.error(
                    f"{path.name}: нода '{node_id}'.{name} ссылается на несуществующую ноду '{src_id}'"
                )
                continue
            src_class = prompt[src_id].get("class_type")
            src_outputs = catalog.get(src_class, {}).get("outputs")
            if isinstance(src_outputs, int) and not 0 <= slot < src_outputs:
                report.error(
                    f"{path.name}: нода '{node_id}'.{name} берёт выход {slot} у '{src_id}' ({src_class}), "
                    f"а у неё всего выходов: {src_outputs}"
                )
            producers.setdefault(node_id, set()).add(src_id)

    _check_cycles(path, producers, report)
    _check_reachability(path, prompt, producers, report)

    return {"nodes": len(prompt), "links": links}


def _check_cycles(path: Path, producers: Dict[str, set], report: Report) -> None:
    visiting: set = set()
    done: set = set()
    found = False

    def walk(node_id: str, stack: List[str]) -> bool:
        nonlocal found
        if found or node_id in done:
            return found
        if node_id in visiting:
            report.error(f"{path.name}: цикл в графе: {' -> '.join(stack + [node_id])}")
            found = True
            return True
        visiting.add(node_id)
        for parent in producers.get(node_id, ()):
            if walk(parent, stack + [node_id]):
                return True
        visiting.discard(node_id)
        done.add(node_id)
        return False

    for node_id in list(producers):
        walk(node_id, [])
        if found:
            break


def _check_reachability(path: Path, prompt: Dict[str, Any], producers: Dict[str, set], report: Report) -> None:
    outputs = [
        nid for nid, node in prompt.items()
        if isinstance(node, dict) and node.get("class_type") in OUTPUT_NODES
    ]
    if not outputs:
        report.error(f"{path.name}: нет ни одной output-ноды (SaveImage / PreviewImage / MaskPreview)")
        return

    reachable: set = set()
    queue = list(outputs)
    while queue:
        nid = queue.pop()
        if nid in reachable:
            continue
        reachable.add(nid)
        queue.extend(producers.get(nid, ()))

    orphans = sorted(set(prompt) - reachable, key=lambda x: (len(x), x))
    if orphans:
        report.warn(f"{path.name}: ноды не влияют на результат: {', '.join(orphans)}")


def validate_portfolio(path: Path, workflows_dir: Path, report: Report) -> int:
    if not path.exists():
        report.error(f"Не найден индекс портфолио: {path}")
        return 0
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        report.error(f"{path.name}: невалидный JSON ({exc})")
        return 0

    cases = data.get("cases")
    if not isinstance(cases, list) or not cases:
        report.error(f"{path.name}: нет непустого массива 'cases'")
        return 0

    for index, case in enumerate(cases):
        if not isinstance(case, dict):
            report.error(f"{path.name}: cases[{index}] — не объект")
            continue
        label = case.get("id") or f"cases[{index}]"
        for field in ("id", "title", "task", "workflow", "outputs", "tags"):
            if field not in case:
                report.error(f"{path.name}: кейс '{label}' — нет поля '{field}'")
        workflow = case.get("workflow")
        if isinstance(workflow, str) and not (ROOT / workflow).exists():
            report.error(f"{path.name}: кейс '{label}' — файл '{workflow}' не найден")

    used = {case.get("workflow") for case in cases if isinstance(case, dict)}
    on_disk = {f"workflows/{p.name}" for p in workflows_dir.glob("*.json")}
    for missing in sorted(on_disk - used):
        report.warn(f"{path.name}: workflow '{missing}' есть на диске, но не описан в портфолио")

    _check_coverage_refs(path, data, report)
    _check_files_list(path, data, report)

    return len(cases)


COVERAGE_STATUSES = {"covered", "partial", "out_of_scope_in_comfyui"}
BOUNDARY_REF = re.compile(r"^boundaries\[(\d+)\]$")


def _check_coverage_refs(path: Path, data: Dict[str, Any], report: "Report") -> None:
    coverage = data.get("coverage")
    if coverage is None:
        report.warn(f"{path.name}: нет раздела 'coverage' — не видно, что из ТЗ закрыто")
        return
    if not isinstance(coverage, list):
        report.error(f"{path.name}: 'coverage' должен быть массивом")
        return

    boundaries = data.get("boundaries") or []
    case_ids = {c.get("id") for c in data.get("cases", []) if isinstance(c, dict)}
    case_ids |= {c.get("workflow") for c in data.get("cases", []) if isinstance(c, dict)}

    for index, item in enumerate(coverage):
        if not isinstance(item, dict):
            report.error(f"{path.name}: coverage[{index}] — не объект")
            continue
        status = item.get("status")
        if status not in COVERAGE_STATUSES:
            report.error(
                f"{path.name}: coverage[{index}] — статус {status!r}, допустимо: {', '.join(sorted(COVERAGE_STATUSES))}"
            )
        refs = item.get("where")
        if not isinstance(refs, list) or not refs:
            report.error(f"{path.name}: coverage[{index}] — нет непустого 'where'")
            continue
        for ref in refs:
            if not isinstance(ref, str):
                report.error(f"{path.name}: coverage[{index}] — ссылка не строка: {ref!r}")
                continue
            boundary = BOUNDARY_REF.match(ref)
            if boundary:
                if int(boundary.group(1)) >= len(boundaries):
                    report.error(f"{path.name}: coverage[{index}] ссылается на '{ref}', а границ всего {len(boundaries)}")
                continue
            head = ref.split(" ")[0].split(" → ")[0]
            if head in case_ids:
                continue
            if (ROOT / head).exists():
                continue
            report.error(f"{path.name}: coverage[{index}] — ссылка '{ref}' никуда не ведёт")


def _check_files_list(path: Path, data: Dict[str, Any], report: "Report") -> None:
    listed = data.get("files")
    if not isinstance(listed, list) or not listed:
        report.warn(f"{path.name}: нет раздела 'files' — непонятно, что именно отдаётся заказчику")
        return
    for index, entry in enumerate(listed):
        rel = entry.get("path") if isinstance(entry, dict) else entry
        if not isinstance(rel, str) or not rel:
            report.error(f"{path.name}: files[{index}] — запись без строкового 'path'")
            continue
        if not (ROOT / rel).exists():
            report.error(f"{path.name}: files[{index}] — файл '{rel}' не найден")


def main() -> int:
    parser = argparse.ArgumentParser(description="Валидация workflow JSON портфолио (API-формат ComfyUI)")
    parser.add_argument("--workflows", default=str(ROOT / "workflows"), help="папка с workflow JSON")
    parser.add_argument("--catalog", default=str(ROOT / "tools" / "node_catalog.json"), help="снимок каталога нод")
    parser.add_argument("--portfolio", default=str(ROOT / "portfolio.json"), help="индекс портфолио")
    args = parser.parse_args()

    catalog = load_catalog(Path(args.catalog))
    workflows_dir = Path(args.workflows)
    files = sorted(workflows_dir.glob("*.json"))
    if not files:
        print(f"В папке {workflows_dir} нет *.json")
        return 1

    report = Report()
    print(f"Каталог нод ComfyUI: {len(catalog)} шт. ({Path(args.catalog).name})")
    print(f"Workflow на проверку: {len(files)}\n")

    for path in files:
        stats = validate_workflow(path, catalog, report)
        if stats:
            print(f"  [ok] {path.name:<42} нод: {stats['nodes']:>2}  связей: {stats['links']:>2}")

    cases = validate_portfolio(Path(args.portfolio), workflows_dir, report)
    print(f"  [ok] {'portfolio.json':<42} кейсов: {cases}")

    for warning in report.warnings:
        print(f"\n  [warn] {warning}")
    for error in report.errors:
        print(f"\n  [err ] {error}")

    print()
    if report.errors:
        print(f"РЕЗУЛЬТАТ: ошибок {len(report.errors)}, предупреждений {len(report.warnings)}")
        return 1
    print(f"РЕЗУЛЬТАТ: всё валидно, предупреждений {len(report.warnings)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
