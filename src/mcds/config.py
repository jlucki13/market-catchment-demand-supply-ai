"""Loading vertical configs, settings, and deal inputs."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

import yaml

try:  # jsonschema >= 4.18 ships `referencing`
    from referencing.jsonschema import DRAFT202012
except ImportError:  # pragma: no cover
    DRAFT202012 = None  # type: ignore[assignment]

REPO_ROOT = Path(__file__).resolve().parents[2]
VERTICALS_DIR = REPO_ROOT / "config" / "verticals"
SCHEMAS_DIR = REPO_ROOT / "schemas"

_ENV_RE = re.compile(r"\$\{([A-Z0-9_]+)\}")


def load_vertical(vertical_id: str) -> dict:
    """Load a vertical config by id, with a listing of what exists if it does not.

    A typo'd vertical is the single most likely user error, and a bare
    FileNotFoundError does not help; naming the alternatives does.
    """
    path = VERTICALS_DIR / f"{vertical_id}.yaml"
    if not path.exists():
        available = sorted(p.stem for p in VERTICALS_DIR.glob("*.yaml"))
        raise FileNotFoundError(
            f"No vertical config '{vertical_id}'. Available: {', '.join(available)}. "
            f"Adding one is a new file in {VERTICALS_DIR}, not a code change."
        )
    return yaml.safe_load(path.read_text())


def load_settings(path: str | Path | None = None) -> dict:
    """Load settings, resolving ${ENV_VAR} references against the environment.

    Unresolved variables become None rather than the literal '${VAR}' string, so
    a missing key fails where it is used with a clear message instead of being
    sent to an API as a nonsense credential.
    """
    path = Path(path) if path else REPO_ROOT / "config" / "settings.yaml"
    if not path.exists():
        path = REPO_ROOT / "config" / "settings.example.yaml"
    return _resolve_env(yaml.safe_load(path.read_text()) or {})


def _resolve_env(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _resolve_env(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_resolve_env(v) for v in obj]
    if isinstance(obj, str):
        match = _ENV_RE.fullmatch(obj.strip())
        if match:
            return os.environ.get(match.group(1))
    return obj


def load_deal(path: str | Path) -> dict:
    """Load and validate a deal input file."""
    path = Path(path)
    data = yaml.safe_load(path.read_text()) if path.suffix in (".yaml", ".yml") else json.loads(path.read_text())
    validate(data, "deal_input")
    return data


def load_schema(name: str) -> dict:
    return json.loads((SCHEMAS_DIR / f"{name}.schema.json").read_text())


def _local_schema_store() -> dict[str, dict]:
    """Every schema in schemas/, keyed by both its $id and its filename.

    Cross-schema $refs are written as bare filenames but the schemas carry
    absolute $id URIs, so a default resolver tries to fetch them over the
    network. Preloading both keys keeps validation entirely offline, which
    matters because validation runs in tests and in --dry-run.
    """
    store: dict[str, dict] = {}
    for path in SCHEMAS_DIR.glob("*.schema.json"):
        schema = json.loads(path.read_text())
        store[path.name] = schema
        if "$id" in schema:
            store[schema["$id"]] = schema
            base = schema["$id"].rsplit("/", 1)[0]
            store[f"{base}/{path.name}"] = schema
    return store


def validate(instance: dict, schema_name: str) -> None:
    """Validate against a schema, if jsonschema is installed.

    Made optional so the scoring path has no hard dependency on a validation
    library, but wired everywhere a document crosses a boundary.
    """
    try:
        import jsonschema
    except ImportError:  # pragma: no cover
        return
    schema = load_schema(schema_name)
    store = _local_schema_store()
    try:
        from referencing import Registry, Resource

        registry = Registry().with_resources(
            [(uri, Resource.from_contents(s, default_specification=DRAFT202012))
             for uri, s in store.items()]
        )
        jsonschema.Draft202012Validator(schema, registry=registry).validate(instance)
    except ImportError:  # pragma: no cover - older jsonschema
        resolver = jsonschema.RefResolver(
            base_uri=schema.get("$id", SCHEMAS_DIR.as_uri() + "/"),
            referrer=schema,
            store=store,
        )
        jsonschema.validate(instance, schema, resolver=resolver)
