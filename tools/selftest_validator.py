from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VALIDATOR = ROOT / "tools" / "validate_workflows.py"

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def build_broken_inputs(tmp: Path) -> None:
    prompt = json.loads((ROOT / "workflows" / "01_brief_to_label_artwork.json").read_text(encoding="utf-8"))
    prompt["5"]["inputs"]["cfg"] = "6.5"
    prompt["5"]["inputs"]["steps"] = 20.5
    prompt["4"]["inputs"]["width"] = 1024.5
    prompt["11"]["inputs"]["image"] = "sdcard.png"
    prompt["6"]["inputs"]["vae"] = ["1", 7]
    prompt["metadata"] = "case 01"
    prompt["7"]["inputs"]["image"] = ["9", 0]

    portfolio = json.loads((ROOT / "portfolio.json").read_text(encoding="utf-8"))
    portfolio["coverage"][0]["where"] = ["case-99-nowhere"]
    portfolio["coverage"][8]["status"] = "covered_maybe"
    portfolio["files"].append({"path": "workflows/99_missing.json", "role": "нет такого файла"})

    (tmp / "wf").mkdir()
    (tmp / "wf" / "broken.json").write_text(json.dumps(prompt, ensure_ascii=False), encoding="utf-8")
    (tmp / "portfolio.json").write_text(json.dumps(portfolio, ensure_ascii=False), encoding="utf-8")


EXPECTED = [
    "значение '6.5' не того типа",
    "значение 20.5 не того типа",
    "значение 1024.5 не того типа",
    "ожидает связь типа IMAGE",
    "а у неё всего выходов",
    "ключ верхнего уровня 'metadata'",
    "цикл в графе",
    "ссылка 'case-99-nowhere' никуда не ведёт",
    "статус 'covered_maybe'",
    "файл 'workflows/99_missing.json' не найден",
]


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="comfy_selftest_"))
    build_broken_inputs(tmp)

    proc = subprocess.run(
        [
            sys.executable,
            str(VALIDATOR),
            "--workflows", str(tmp / "wf"),
            "--portfolio", str(tmp / "portfolio.json"),
            "--catalog", str(ROOT / "tools" / "node_catalog.json"),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    missed = [fragment for fragment in EXPECTED if fragment not in proc.stdout]
    print(f"сработавших проверок: {len(EXPECTED) - len(missed)} из {len(EXPECTED)}")
    for fragment in missed:
        print(f"  [НЕ СРАБОТАЛО] {fragment}")
    if proc.returncode != 1:
        print(f"  [ПРОБЛЕМА] при ошибках exit code должен быть 1, а был {proc.returncode}")

    if missed or proc.returncode != 1:
        print(proc.stdout)
        return 1

    print("OK: валидатор ловит все подброшенные дефекты")
    return 0


if __name__ == "__main__":
    sys.exit(main())
