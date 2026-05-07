import json
import os
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ENGINE_CONFIG = Path(os.environ["APPDATA"]) / "org.encroissant.app" / "engines" / "engines.json"


def upsert_engine(engines: list[dict], entry: dict) -> None:
    for index, engine in enumerate(engines):
        if engine.get("id") == entry["id"] or engine.get("name") == entry["name"]:
            merged = dict(engine)
            merged.update(entry)
            engines[index] = merged
            return
    engines.append(entry)


def main() -> None:
    if ENGINE_CONFIG.exists():
        engines = json.loads(ENGINE_CONFIG.read_text(encoding="utf-8"))
    else:
        ENGINE_CONFIG.parent.mkdir(parents=True, exist_ok=True)
        engines = []

    backup = ENGINE_CONFIG.with_suffix(f".json.bak-{time.strftime('%Y%m%d-%H%M%S')}")
    if ENGINE_CONFIG.exists():
        backup.write_text(json.dumps(engines, indent=4), encoding="utf-8")

    upsert_engine(
        engines,
        {
            "type": "local",
            "id": "codex-chess-app-server",
            "name": "Codex-chess",
            "version": "0.1.0",
            "path": str(ROOT / "engines" / "codex-chess" / "codex-chess.cmd"),
            "image": None,
            "elo": 1800,
            "downloadSize": None,
            "downloadLink": None,
            "loaded": True,
            "go": {"t": "Time", "c": 1000},
            "enabled": True,
            "settings": [{"name": "UCI_Chess960", "value": False}],
        },
    )
    upsert_engine(
        engines,
        {
            "type": "local",
            "id": "llm-chess-engine-openrouter",
            "name": "llm-chess-engine",
            "version": "0.1.0",
            "path": str(ROOT / "engines" / "llm-chess-engine" / "llm-chess-engine.cmd"),
            "image": None,
            "elo": 1700,
            "downloadSize": None,
            "downloadLink": None,
            "loaded": True,
            "go": {"t": "Time", "c": 30000},
            "enabled": True,
            "settings": [
                {"name": "Model", "value": os.environ.get("OPENROUTER_MODEL", "moonshotai/kimi-k2.6")},
                {"name": "Temperature", "value": 20},
                {"name": "MaxRetries", "value": 1},
            ],
        },
    )

    ENGINE_CONFIG.write_text(json.dumps(engines, indent=4), encoding="utf-8")
    print(f"Updated {ENGINE_CONFIG}")
    if backup.exists():
        print(f"Backup {backup}")


if __name__ == "__main__":
    main()
