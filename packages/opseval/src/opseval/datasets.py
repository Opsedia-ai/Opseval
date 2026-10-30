# SPDX-License-Identifier: Apache-2.0
"""Datasets, placeholders and matrices: one scenario standing for many (ADR-0060, level L1).

- A **dataset** is a CSV (header row) or JSONL (one flat object per line) file under the
  catalogue root's ``datasets/`` folder. Values are scalars; anything else is a load error.
- A **placeholder** is ``{{name}}``: logic-free, no filters, defaults or expressions.
  ``\\{{`` writes a literal ``{{``. It is replaced on the *parsed* model, inside the text fields
  listed in :data:`TEXT_FIELDS`, never in the YAML text, so a value holding ``:`` or a newline
  cannot change a file's structure. A value is never scanned again: data cannot pull in data.
- A **matrix** crosses ``language``, ``dialect`` and ``persona``; the expansion is every row
  times every cell, less ``exclude``, capped per scenario (over the cap is a load error, never
  a truncation, which would drop coverage silently).

Each expansion's id is ``<id>@<8 hex>``, the start of SHA-256 over the canonical JSON of the
row's values and the cell, so reordering rows keeps every id and a changed value gets a new one.
Everything here runs at load: the runner, scorers and reports see ordinary scenarios.
"""

from __future__ import annotations

import copy
import csv
import hashlib
import io
import itertools
import json
import math
import re
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from opseval import config
from opseval.errors import ConfigError
from opseval.models.scenario import (
    DATASET_FOLDER,
    MATRIX_AXES,
    VARIABLE_NAME,
    Matrix,
    PersonaVariant,
    Scenario,
)

Value = str | int | float | bool
"""A dataset cell. CSV cells are strings; JSONL keeps numbers and booleans."""

DEFAULT_MAX_EXPANSION = config.DEFAULT_MAX_EXPANSION
DEFAULT_MAX_DATASET_BYTES = config.DEFAULT_MAX_DATASET_BYTES

PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
NAME = re.compile(VARIABLE_NAME)
ESCAPE = "\\{{"

ANY = "*"
DEEP = "**"
TEXT_FIELDS: tuple[tuple[str, ...], ...] = (
    ("name",),
    ("goal",),
    ("persona", "role"),
    ("conversation", "opening_message"),
    ("conversation", "flow"),
    ("conversation", "scripted_turns", ANY),
    ("conversation", "injections", ANY, "payload"),
    ("conversation", "checkpoint_policy", ANY, "feedback"),
    ("conversation", "checkpoint_policy", ANY, "sequence", ANY, "feedback"),
    ("expectations", "expected_outcome"),
    ("expectations", "policy", ANY),
    ("scorers", ANY, "params", DEEP),
)
"""The string fields a placeholder may sit in. ``id``, ``schema``, tags, tool and scorer ids,
checkpoint names and numbers never hold one: they decide what runs, not what is said."""


class TemplateError(ValueError):
    """A malformed placeholder: a typo, not text."""


@dataclass(frozen=True)
class Limits:
    max_expansion: int = DEFAULT_MAX_EXPANSION
    """Expanded scenarios one scenario may stand for (``OPSEVAL_MAX_EXPANSION``)."""
    max_dataset_bytes: int = DEFAULT_MAX_DATASET_BYTES
    """The largest dataset file read or pushed (``OPSEVAL_MAX_DATASET_BYTES``)."""

    @classmethod
    def from_settings(cls, settings: Any) -> Limits:
        """From a ``config.Settings`` already read (the server's, the runner's)."""
        return cls(settings.max_expansion, settings.max_dataset_bytes)

    @classmethod
    def from_env(cls) -> Limits:
        """Read now, and only these two settings (``config.CatalogueLimits``): a broken
        setting a load does not use is ignored, and a broken limit is a configuration error
        naming it, never the default."""
        try:
            found = config.CatalogueLimits()
        except ValidationError as exc:
            named = "; ".join(
                f"OPSEVAL_{'.'.join(map(str, e['loc'])).upper()}: {e['msg']}" for e in exc.errors()
            )
            raise ConfigError(f"invalid setting {named}") from exc
        return cls(found.max_expansion, found.max_dataset_bytes)


# --- placeholders ----------------------------------------------------------------------


def _tokens(text: str) -> Iterator[tuple[bool, str]]:
    """``(is_placeholder, text_or_name)``; raises :class:`TemplateError` on a bad ``{{``."""
    i, n, buf = 0, len(text), []
    while i < n:
        if text.startswith(ESCAPE, i):
            buf.append("{{")
            i += len(ESCAPE)
        elif text.startswith("{{", i):
            m = PLACEHOLDER.match(text, i)
            if m is None:
                snippet = text[i : i + 24]
                raise TemplateError(
                    f"malformed placeholder at {snippet!r}: write {{{{name}}}} with letters, "
                    "digits and '_' (no filters or expressions), or \\{{ for a literal {{"
                )
            if buf:
                yield False, "".join(buf)
                buf = []
            yield True, m.group(1)
            i = m.end()
        else:
            buf.append(text[i])
            i += 1
    if buf:
        yield False, "".join(buf)


def placeholders(text: str) -> list[str]:
    """The names used in ``text``, in order (repeats kept)."""
    return [value for is_var, value in _tokens(text) if is_var]


def has_template_syntax(text: str) -> bool:
    return "{{" in text


def render(text: str, values: Mapping[str, str]) -> str:
    """``text`` with each placeholder replaced and each ``\\{{`` made ``{{``. Values are
    inserted as they are: never scanned again. A missing name raises ``KeyError``."""
    return "".join(values[value] if is_var else value for is_var, value in _tokens(text))


def as_text(value: Value) -> str:
    """How a cell reads in text: strings as they are, ``true``/``false``, numbers as JSON."""
    return value if isinstance(value, str) else json.dumps(value)


def _matches(path: tuple[str, ...], pattern: tuple[str, ...]) -> bool:
    for i, part in enumerate(pattern):
        if part == DEEP:
            return len(path) > i
        if i >= len(path) or (part != ANY and part != path[i]):
            return False
    return len(path) == len(pattern)


def is_text_field(path: tuple[str, ...]) -> bool:
    return any(_matches(path, p) for p in TEXT_FIELDS)


def _walk(node: Any, path: tuple[str, ...], visit: Callable[[tuple[str, ...], str], str]) -> Any:
    if isinstance(node, dict):
        return {k: _walk(v, (*path, str(k)), visit) for k, v in node.items()}
    if isinstance(node, list):
        return [_walk(v, (*path, str(i)), visit) for i, v in enumerate(node)]
    if isinstance(node, str):
        return visit(path, node)
    return node


def _dotted(path: tuple[str, ...]) -> str:
    return ".".join(path)


# --- reading a dataset -----------------------------------------------------------------


@dataclass(frozen=True)
class Dataset:
    path: str
    """As the scenario names it: ``datasets/orders.csv``."""
    digest: str
    """SHA-256 of the file's bytes."""
    columns: tuple[str, ...]
    rows: tuple[tuple[int, dict[str, Value | None]], ...]
    """``(row, values)``; the row is the line in the file (the CSV header is row 1)."""


class DatasetError(ValueError):
    """A dataset that cannot be used, with the file named in the message."""


def _decode(name: str, data: bytes) -> str:
    try:
        text = data.decode("utf-8-sig")  # a spreadsheet's byte-order mark is not a column
    except UnicodeDecodeError as exc:
        raise DatasetError(f"{name}: not UTF-8 ({exc.reason} at byte {exc.start})") from exc
    if "\x00" in text:
        raise DatasetError(f"{name}: contains a NUL character; save it as UTF-8 text")
    return text


def _check_columns(name: str, columns: list[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    for col in columns:
        if not NAME.match(col):
            raise DatasetError(
                f"{name}: column {col!r} is not a placeholder name (letters, digits and '_', "
                "not starting with a digit)"
            )
        if col in seen:
            raise DatasetError(f"{name}: column {col!r} appears twice")
        seen.add(col)
    if not columns:
        raise DatasetError(f"{name}: no columns")
    return tuple(columns)


def _parse_csv(name: str, text: str) -> tuple[tuple[str, ...], list[tuple[int, dict[str, Any]]]]:
    reader = csv.reader(io.StringIO(text, newline=""), strict=True)
    header: list[str] | None = None
    rows: list[tuple[int, dict[str, Any]]] = []
    line = 0
    try:
        for record in reader:
            start, line = line + 1, reader.line_num
            if not record:
                continue  # a blank line
            if header is None:
                header = [c.strip() for c in record]
                columns = _check_columns(name, header)
                continue
            if len(record) != len(header):
                raise DatasetError(
                    f"{name}: row {start} has {len(record)} cell(s), the header has {len(header)}"
                )
            rows.append((start, dict(zip(columns, record, strict=True))))
    except csv.Error as exc:
        raise DatasetError(f"{name}: row {reader.line_num}: invalid CSV: {exc}") from exc
    if header is None:
        raise DatasetError(f"{name}: empty; a CSV dataset starts with a header row")
    return columns, rows


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise DatasetError(f"key {key!r} appears twice")
        out[key] = value
    return out


def _no_constant(token: str) -> Any:
    raise DatasetError(f"{token} is not a value JSON allows")


def _parse_jsonl(name: str, text: str) -> tuple[tuple[str, ...], list[tuple[int, dict[str, Any]]]]:
    columns: tuple[str, ...] | None = None
    rows: list[tuple[int, dict[str, Any]]] = []
    for number, line in enumerate(text.split("\n"), start=1):
        if not line.strip():
            continue
        try:
            obj = json.loads(
                line, object_pairs_hook=_no_duplicate_keys, parse_constant=_no_constant
            )
        except (ValueError, DatasetError) as exc:
            raise DatasetError(f"{name}: row {number}: not a JSON object: {exc}") from exc
        if not isinstance(obj, dict):
            raise DatasetError(f"{name}: row {number}: a JSONL row is one object per line")
        for key, value in obj.items():
            if isinstance(value, dict | list):
                raise DatasetError(
                    f"{name}: row {number}: {key!r} is nested; dataset values are strings, "
                    "numbers or booleans"
                )
        if columns is None:
            columns = _check_columns(name, list(obj))
        elif set(obj) != set(columns):
            missing = sorted(set(columns) - set(obj))
            extra = sorted(set(obj) - set(columns))
            raise DatasetError(
                f"{name}: row {number}: keys differ from the first row's "
                f"(missing {missing}, extra {extra}); every row has the same keys"
            )
        rows.append((number, obj))
    if columns is None:
        raise DatasetError(f"{name}: empty; a JSONL dataset has one object per line")
    return columns, rows


def parse_dataset(
    name: str, data: bytes
) -> tuple[tuple[str, ...], list[tuple[int, dict[str, Any]]]]:
    """Columns and rows of a dataset file's bytes; :class:`DatasetError` if it is unusable."""
    text = _decode(name, data)
    columns, rows = (_parse_jsonl if name.endswith(".jsonl") else _parse_csv)(name, text)
    if not rows:
        raise DatasetError(f"{name}: no rows; a dataset with nothing in it would run nothing")
    return columns, rows


def read_dataset(root: Path, rel: str, *, max_bytes: int) -> Dataset:
    """Read ``rel`` (``datasets/…``) under the catalogue ``root``: never outside the
    ``datasets/`` folder (``..`` and links leaving it are refused), never over ``max_bytes``."""
    base = root.resolve()
    folder = (base / DATASET_FOLDER).resolve()
    if not folder.is_relative_to(base):
        raise DatasetError(f"{DATASET_FOLDER}/: a link to a folder outside the catalogue")
    path = (root / rel).resolve()
    if not path.is_relative_to(folder):
        raise DatasetError(f"{rel}: a link to a file outside {DATASET_FOLDER}/")
    if not path.is_file():
        raise DatasetError(f"{rel}: no such dataset under {base}")
    try:
        size = path.stat().st_size
        if size > max_bytes:
            raise DatasetError(
                f"{rel}: {size} bytes, over the {max_bytes}-byte limit (OPSEVAL_MAX_DATASET_BYTES)"
            )
        data = path.read_bytes()
    except OSError as exc:  # no permission, or removed meanwhile: named, never a traceback
        raise DatasetError(f"{rel}: cannot be read ({exc.strerror})") from exc
    columns, rows = parse_dataset(rel, data)
    return Dataset(
        path=rel,
        digest=hashlib.sha256(data).hexdigest(),
        columns=columns,
        rows=tuple((n, {k: v for k, v in r.items()}) for n, r in rows),
    )


# --- expansion -------------------------------------------------------------------------


@dataclass
class Expansion:
    """What one authored scenario expanded to: for ``validate`` and the editor."""

    base_id: str
    file: str
    ids: list[str] = field(default_factory=list)
    repeats: int = 1
    dataset: str | None = None
    unused_columns: list[str] = field(default_factory=list)
    limit: int = DEFAULT_MAX_EXPANSION

    @property
    def count(self) -> int:
        return len(self.ids)

    @property
    def attempts(self) -> int:
        return self.count * self.repeats


Problem = tuple[str, str]
"""``(field, message)``; the caller adds the file."""


MATRIX_SCAN_LIMIT = 100_000
"""The most cells a matrix with ``exclude`` may have before exclude. Excludes are applied one
cell at a time; a bigger space would take long to scan for the few cells left in it."""


def _axes(matrix: Matrix) -> list[tuple[str, list[Any]]]:
    return [(a, getattr(matrix, a)) for a in MATRIX_AXES if getattr(matrix, a)]


def _raw_cell_count(matrix: Matrix | None) -> int:
    """Cells before ``exclude``: the product of the axes' lengths, computed, not built."""
    return math.prod(len(values) for _a, values in _axes(matrix)) if matrix else 1


def _excluded(cell: Mapping[str, Any], rule: Mapping[str, str | int], matrix: Matrix) -> bool:
    for key, value in rule.items():
        if key == "persona" and isinstance(value, str):
            # A persona named in an exclude by its ``name`` (an int is its index).
            if matrix.persona[cell["persona"]].name != value:
                return False
        elif cell.get(key) != value:
            return False
    return True


def _iter_cells(matrix: Matrix | None) -> Iterator[dict[str, Any]]:
    """Every cell, in axis order, less ``exclude``, one at a time (persona is its index), so
    the cap can stop the count before a large matrix is ever built."""
    if matrix is None:
        yield {}
        return
    axes = _axes(matrix)
    for combo in itertools.product(*(range(len(values)) for _a, values in axes)):
        cell = {
            name: (i if name == "persona" else values[i])
            for (name, values), i in zip(axes, combo, strict=True)
        }
        if not any(_excluded(cell, rule, matrix) for rule in matrix.exclude):
            yield cell


def persona_label(variant: PersonaVariant) -> str:
    """How a persona variant is named in text (``{{persona}}``) and in provenance: its
    ``name``, else the first 8 hex digits of SHA-256 over its content. Never its place in
    the list, which would change when the personas are reordered."""
    if variant.name is not None:
        return variant.name
    canonical = json.dumps(
        variant.model_dump(exclude_none=True), sort_keys=True, ensure_ascii=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:8]


def _shown(cell: Mapping[str, Any], matrix: Matrix | None) -> dict[str, str]:
    """The cell as text and provenance show it: a persona by :func:`persona_label`."""
    return {
        k: persona_label(matrix.persona[v]) if k == "persona" and matrix else str(v)
        for k, v in cell.items()
    }


def _case_hash(
    row: Mapping[str, Value | None], cell: Mapping[str, Any], matrix: Matrix | None
) -> str:
    # A persona is hashed by what it says, not its place in the list, so reordering the
    # personas (like reordering rows) keeps every id.
    hashed = dict(cell)
    if "persona" in cell and matrix is not None:
        hashed["persona"] = matrix.persona[cell["persona"]].model_dump(exclude_none=True)
    canonical = json.dumps(
        {"matrix": hashed, "row": dict(row)},
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:8]


def case_label(row: int | None, dataset: str | None, cell: Mapping[str, Any]) -> str:
    parts = []
    if row is not None:
        parts.append(f"row {row} of {dataset}")
    if cell:
        parts.append("matrix " + ", ".join(f"{k}={v}" for k, v in cell.items()))
    return "; ".join(parts)


def _template_problems(
    data: dict[str, Any], variables: set[str] | None, where_vars: str
) -> tuple[list[Problem], set[str]]:
    """Every malformed or misplaced placeholder, and every name used. ``variables`` is
    ``None`` for a scenario with no dataset or matrix, where any placeholder is a typo."""
    problems: list[Problem] = []
    used: set[str] = set()

    def visit(path: tuple[str, ...], text: str) -> str:
        if not has_template_syntax(text):
            return text
        where = _dotted(path)
        if not is_text_field(path):
            problems.append((where, "this field cannot hold a placeholder (only text fields can)"))
            return text
        try:
            names = placeholders(text)
        except TemplateError as exc:
            problems.append((where, str(exc)))
            return text
        for name in names:
            used.add(name)
            if variables is None:
                problems.append(
                    (
                        where,
                        f"{{{{{name}}}}} in a scenario with no dataset: or matrix:; write "
                        "\\{{ for a literal {{",
                    )
                )
            elif name not in variables:
                problems.append((where, f"{{{{{name}}}}}: no {where_vars} named {name!r}"))
        return text

    _walk(data, (), visit)
    return problems, used


def _substitute(data: dict[str, Any], values: Mapping[str, str]) -> dict[str, Any]:
    def visit(path: tuple[str, ...], text: str) -> str:
        if not has_template_syntax(text) or not is_text_field(path):
            return text
        return render(text, values)

    out: dict[str, Any] = _walk(data, (), visit)
    return out


def _apply_cell(data: dict[str, Any], cell: Mapping[str, Any], matrix: Matrix) -> None:
    persona = data.setdefault("persona", {})
    if "language" in cell:
        persona["language"] = cell["language"]
        if "dialect" not in cell:
            # A language axis names the whole language: the template's dialect belonged to
            # the template's language, and kept here it would name a different pack.
            persona.pop("dialect", None)
    if "dialect" in cell:
        persona["dialect"] = cell["dialect"]
    if "persona" in cell:
        # The variant's name names the cell; its other fields replace the persona's own.
        variant = matrix.persona[cell["persona"]]
        persona.update(variant.model_dump(exclude_unset=True, exclude={"name"}))


def _validation_problems(exc: ValidationError, label: str) -> list[Problem]:
    out = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err["loc"])
        out.append((loc, f"{label}: {err['msg']}" if label else err["msg"]))
    return out


def expand(
    template: Scenario,
    *,
    root: Path | None,
    limits: Callable[[], Limits],
) -> tuple[list[Scenario], list[Problem], Expansion | None]:
    """The scenarios ``template`` stands for, or the problems that stop it loading.

    A scenario with no ``dataset:`` or ``matrix:`` is itself (with ``\\{{`` made ``{{``), and
    any placeholder in it is a problem. ``limits`` is called only when there is something to
    expand, so plain catalogues never read settings.
    """
    data = template.model_dump(
        mode="json", by_alias=True, exclude_unset=True, exclude={"dataset", "matrix"}
    )
    spec, matrix = template.dataset_spec, template.matrix
    if spec is None and matrix is None:
        typos, _used = _template_problems(data, None, "")
        if typos:
            return [], typos, None
        if not any(has_template_syntax(t) for t in _strings(data)):
            return [template], [], None
        try:
            return [Scenario.model_validate(_substitute(data, {}))], [], None
        except ValidationError as exc:
            return [], _validation_problems(exc, ""), None

    try:
        lim = limits()
    except ConfigError as exc:  # a broken OPSEVAL_MAX_* setting, named
        return [], [("dataset" if spec is not None else "matrix", str(exc))], None
    dataset: Dataset | None = None
    if spec is not None:
        if root is None:
            return [], [("dataset", "a dataset needs the catalogue folder it sits in")], None
        try:
            dataset = read_dataset(root, spec.path, max_bytes=lim.max_dataset_bytes)
        except DatasetError as exc:
            return [], [("dataset", str(exc))], None
    problems: list[Problem] = []
    columns = set(dataset.columns) if dataset else set()
    axes = {a for a in MATRIX_AXES if matrix is not None and getattr(matrix, a)}
    for clash in sorted(columns & axes):
        problems.append(("dataset", f"column {clash!r} is also a matrix axis; rename the column"))
    if spec is not None:
        for col in spec.allow_blank:
            if col not in columns:
                problems.append(("dataset.allow_blank", f"{spec.path} has no column {col!r}"))
    kinds = " or ".join(
        k
        for k, present in (("column", dataset is not None), ("matrix key", matrix is not None))
        if present
    )
    found, used = _template_problems(data, columns | axes, kinds)
    problems += found
    for index, variant in enumerate(matrix.persona if matrix else []):
        for name, value in variant.model_dump(exclude_none=True).items():
            if name == "role" or not isinstance(value, str) or not has_template_syntax(value):
                continue
            # A variant's name and timezone decide which scenario this is, like the
            # scenario's own non-text fields: never a placeholder.
            problems.append(
                (
                    f"matrix.persona.{index}.{name}",
                    "this field cannot hold a placeholder (only text fields can)",
                )
            )
        if variant.role is not None:
            # A variant's role is authored text, like the scenario's own: it may use the row.
            more, names = _template_problems(
                {"persona": {"role": variant.role}}, columns | axes, kinds
            )
            used |= names
            problems += [(f"matrix.persona.{index}.role", msg) for _f, msg in more]
    if problems:
        return [], problems, None

    rows: list[tuple[int | None, dict[str, Value | None]]] = (
        list(dataset.rows) if dataset else [(None, {})]
    )
    # The cap is checked before any cell is built: the product of the axes is computed,
    # and excludes are applied while counting, stopping one past the cap (ADR-0060).
    cap = lim.max_expansion
    where = "dataset" if dataset else "matrix"
    over = (
        f"over the limit of {cap} per scenario (OPSEVAL_MAX_EXPANSION); split the dataset or "
        "raise the limit"
    )
    raw = _raw_cell_count(matrix)
    if matrix is None or not matrix.exclude:
        total = len(rows) * raw
        if total > cap:
            shape = f"{len(rows)} row(s) x {raw} matrix cell(s)"
            return [], [(where, f"expands to {total} scenarios ({shape}), {over}")], None
    elif raw > MATRIX_SCAN_LIMIT:
        return (
            [],
            [
                (
                    "matrix",
                    f"has {raw} cells before exclude, more than the {MATRIX_SCAN_LIMIT} a "
                    "matrix with exclude may have; list fewer values",
                )
            ],
            None,
        )
    cells = list(itertools.islice(_iter_cells(matrix), cap // len(rows) + 1))
    if not cells:
        return [], [("matrix.exclude", "excludes every cell, so nothing would run")], None
    if len(rows) * len(cells) > cap:
        shape = f"{len(rows)} row(s) x at least {len(cells)} matrix cell(s)"
        return [], [(where, f"expands to more than {cap} scenarios ({shape}), {over}")], None

    info = Expansion(
        base_id=template.id,
        file="",
        repeats=template.execution.repeats,
        dataset=spec.path if spec else None,
        unused_columns=sorted(columns - used),
        limit=lim.max_expansion,
    )
    blank_ok = set(spec.allow_blank) if spec else set()
    out: list[Scenario] = []
    by_hash: dict[str, str] = {}
    by_content: dict[str, str] = {}
    for (row, values), cell in itertools.product(rows, cells):
        shown = _shown(cell, matrix)
        label = case_label(row, dataset.path if dataset else None, shown)
        blank = [
            k
            for k, v in values.items()
            if (v is None or (isinstance(v, str) and not v.strip())) and k not in blank_ok
        ]
        if blank:
            problems.append(
                (
                    "dataset",
                    f"{label}: blank {', '.join(map(repr, blank))}; fill it, or list the column "
                    "in dataset.allow_blank",
                )
            )
            continue
        clean = {k: ("" if v is None else v) for k, v in values.items()}
        digest = _case_hash(clean, cell, matrix)
        if digest in by_hash:
            problems.append(
                ("dataset", f"{label}: the same values as {by_hash[digest]}; remove the duplicate")
            )
            continue
        by_hash[digest] = label
        text_values = {k: as_text(v) for k, v in clean.items()}
        text_values.update(shown)
        case = copy.deepcopy(data)
        if matrix is not None:
            _apply_cell(case, cell, matrix)
        case = _substitute(case, text_values)
        content = json.dumps(case, sort_keys=True, ensure_ascii=False)
        if content in by_content:
            problems.append(
                (
                    "dataset",
                    f"{label}: expands to the same scenario as {by_content[content]} (they differ "
                    "only in columns no placeholder uses); remove one",
                )
            )
            continue
        by_content[content] = label
        case["id"] = f"{template.id}@{digest}"
        case["expanded_from"] = {
            "base_id": template.id,
            "hash": digest,
            "dataset": dataset.path if dataset else None,
            "dataset_digest": dataset.digest if dataset else None,
            "row": row,
            "matrix": shown,
        }
        try:
            out.append(Scenario.model_validate(case))
        except ValidationError as exc:
            problems += _validation_problems(exc, label)
    if problems:
        return [], problems, None
    info.ids = [s.id for s in out]
    return out, [], info


def _strings(node: Any) -> Iterator[str]:
    if isinstance(node, dict):
        for v in node.values():
            yield from _strings(v)
    elif isinstance(node, list):
        for v in node:
            yield from _strings(v)
    elif isinstance(node, str):
        yield node
