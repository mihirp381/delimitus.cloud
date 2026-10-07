"""Compare two OpenAPI documents and fail on changes that break an existing client.

    uv run python tools/openapi_breaking.py OLD.json NEW.json

Breaking, in the order checked:
  * a path or an operation (method) removed
  * a response status removed from an operation
  * a required parameter added
  * a request property removed, or made required when it was optional
  * a response property removed
  * a property's type changed (request or response); a request property's type that only
    widens, such as ``string`` to ``null|string``, passes: every old value still fits
  * an enum value removed (request or response)

Additive changes (new paths, new optional fields, new statuses, new enum values) pass. Deprecating
an operation is fine; removing it is a new major version.
"""

import json
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

METHODS = ("get", "put", "post", "delete", "patch", "head", "options", "trace")
Schema = dict[str, Any]


class Doc:
    def __init__(self, raw: dict[str, Any]) -> None:
        self.raw = raw

    def resolve(self, schema: Schema, seen: frozenset[str] = frozenset()) -> Schema:
        """Follow ``$ref`` to the referenced schema. Returns ``{}`` on a cycle."""
        ref = schema.get("$ref")
        if not isinstance(ref, str):
            return schema
        if ref in seen or not ref.startswith("#/"):
            return {}
        node: Any = self.raw
        for part in ref[2:].split("/"):
            node = node.get(part, {}) if isinstance(node, dict) else {}
        return self.resolve(node, seen | {ref}) if isinstance(node, dict) else {}

    def paths(self) -> dict[str, Any]:
        paths = self.raw.get("paths", {})
        return paths if isinstance(paths, dict) else {}


def type_signature(schema: Schema, doc: Doc) -> str:
    """One string naming the type: ``string``, ``integer|null``, ``array<string>``..."""
    schema = doc.resolve(schema)
    if "anyOf" in schema or "oneOf" in schema:
        members = schema.get("anyOf") or schema.get("oneOf") or []
        return "|".join(sorted(type_signature(m, doc) for m in members))
    kind = schema.get("type")
    if kind == "array":
        return f"array<{type_signature(schema.get('items', {}), doc)}>"
    if isinstance(kind, list):
        return "|".join(sorted(str(k) for k in kind))
    if kind is None and "enum" in schema:
        return "enum"
    return str(kind) if kind is not None else "any"


def widened(old_sig: str, new_sig: str) -> bool:
    """True when every type the old signature allowed, the new one allows too."""
    return set(old_sig.split("|")) <= set(new_sig.split("|"))


def properties(schema: Schema, doc: Doc) -> tuple[dict[str, Schema], set[str]]:
    schema = doc.resolve(schema)
    props = schema.get("properties", {})
    required = schema.get("required", [])
    return (
        {str(k): v for k, v in props.items()} if isinstance(props, dict) else {},
        {str(r) for r in required} if isinstance(required, list) else set(),
    )


def compare_schema(
    where: str, old: Schema, new: Schema, docs: tuple[Doc, Doc], *, request: bool
) -> Iterator[str]:
    """Yield breaking changes between two schemas, descending into properties and items."""
    old_doc, new_doc = docs
    old_sig, new_sig = type_signature(old, old_doc), type_signature(new, new_doc)
    if old_sig != new_sig and not (request and widened(old_sig, new_sig)):
        yield f"{where}: type changed from {old_sig} to {new_sig}"
        return
    old_r, new_r = old_doc.resolve(old), new_doc.resolve(new)
    old_enum, new_enum = old_r.get("enum"), new_r.get("enum")
    if isinstance(old_enum, list) and isinstance(new_enum, list):
        for value in old_enum:
            if value not in new_enum:
                yield f"{where}: enum value {value!r} removed"
    if old_r.get("type") == "array":
        yield from compare_schema(
            f"{where}[]",
            old_r.get("items", {}),
            new_r.get("items", {}),
            docs,
            request=request,
        )
        return
    old_props, old_req = properties(old_r, old_doc)
    new_props, new_req = properties(new_r, new_doc)
    if request:
        for name in sorted(new_req - old_req):
            yield f"{where}.{name}: request property became required"
    for name, old_prop in old_props.items():
        if name not in new_props:
            yield f"{where}.{name}: {'request' if request else 'response'} property removed"
            continue
        yield from compare_schema(
            f"{where}.{name}", old_prop, new_props[name], docs, request=request
        )


def body_schema(operation: dict[str, Any], doc: Doc) -> Schema | None:
    body = operation.get("requestBody")
    if not isinstance(body, dict):
        return None
    body = doc.resolve(body)
    content = body.get("content", {})
    for media in content.values():
        if isinstance(media, dict) and "schema" in media:
            return media["schema"]
    return None


def response_schemas(operation: dict[str, Any], doc: Doc) -> dict[str, Schema]:
    """``status -> schema`` of the first media type of each response."""
    out: dict[str, Schema] = {}
    for status, response in operation.get("responses", {}).items():
        if not isinstance(response, dict):
            continue
        content = doc.resolve(response).get("content", {})
        for media in content.values():
            if isinstance(media, dict) and "schema" in media:
                out[str(status)] = media["schema"]
                break
    return out


def required_parameters(operation: dict[str, Any], doc: Doc) -> set[tuple[str, str]]:
    out: set[tuple[str, str]] = set()
    for raw in operation.get("parameters", []):
        if not isinstance(raw, dict):
            continue
        p = doc.resolve(raw)
        if p.get("required"):
            out.add((str(p.get("in")), str(p.get("name"))))
    return out


def compare_operation(
    where: str, old: dict[str, Any], new: dict[str, Any], old_doc: Doc, new_doc: Doc
) -> Iterator[str]:
    old_responses, new_responses = response_schemas(old, old_doc), response_schemas(new, new_doc)
    for status in old.get("responses", {}):
        if str(status) not in {str(s) for s in new.get("responses", {})}:
            yield f"{where}: response {status} removed"
    for location, name in sorted(
        required_parameters(new, new_doc) - required_parameters(old, old_doc)
    ):
        yield f"{where}: required {location} parameter {name!r} added"
    old_body, new_body = body_schema(old, old_doc), body_schema(new, new_doc)
    if old_body is not None and new_body is not None:
        yield from compare_schema(
            f"{where} request", old_body, new_body, (old_doc, new_doc), request=True
        )
    for status, old_schema in old_responses.items():
        if status in new_responses:
            yield from compare_schema(
                f"{where} response {status}",
                old_schema,
                new_responses[status],
                (old_doc, new_doc),
                request=False,
            )


def breaking_changes(old_raw: dict[str, Any], new_raw: dict[str, Any]) -> list[str]:
    old_doc, new_doc = Doc(old_raw), Doc(new_raw)
    out: list[str] = []
    new_paths = new_doc.paths()
    for path, old_item in sorted(old_doc.paths().items()):
        if path not in new_paths:
            out.append(f"{path}: path removed")
            continue
        new_item = new_paths[path]
        for method in METHODS:
            if method not in old_item:
                continue
            where = f"{method.upper()} {path}"
            if method not in new_item:
                out.append(f"{where}: operation removed")
                continue
            out.extend(
                compare_operation(where, old_item[method], new_item[method], old_doc, new_doc)
            )
    return out


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)  # noqa: T201
        return 2
    old = json.loads(Path(argv[0]).read_text())
    new = json.loads(Path(argv[1]).read_text())
    changes = breaking_changes(old, new)
    for change in changes:
        print(f"BREAKING {change}")  # noqa: T201
    if changes:
        summary = f"{len(changes)} breaking change(s); bump the API version or keep the old shape"
        print(summary)  # noqa: T201
        return 1
    print("no breaking changes")  # noqa: T201
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
