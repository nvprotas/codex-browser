from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from eval_service.app.models import EvalCase, ExpectedOutcome


TEMPLATE_VARIABLE_RE = re.compile(r'{{\s*([A-Za-z_][A-Za-z0-9_]*)\s*}}')


class CaseRegistryError(ValueError):
    pass


class CaseRegistry:
    def __init__(self, cases_dir: Path) -> None:
        self.cases_dir = cases_dir

    def load_cases(self) -> list[EvalCase]:
        cases: list[EvalCase] = []
        seen_case_ids: set[str] = set()
        for path in sorted(self.cases_dir.glob('*.yaml')):
            file_cases = self._load_file(path)
            for case in file_cases:
                if case.eval_case_id in seen_case_ids:
                    raise CaseRegistryError(f'{path}: дублирующийся eval_case_id {case.eval_case_id}')
                seen_case_ids.add(case.eval_case_id)
            cases.extend(file_cases)
        return cases

    def _load_file(self, path: Path) -> list[EvalCase]:
        template = _load_yaml_template(path)
        if template.get('enabled') is False:
            return []
        variants = template.get('variants')
        if not isinstance(variants, list) or not variants:
            raise CaseRegistryError(f'{path}: variants должен быть непустым списком')

        return [_build_case(template, variant, path) for variant in variants]


def _load_yaml_template(path: Path) -> dict[str, Any]:
    with path.open(encoding='utf-8') as file:
        documents = [document for document in yaml.safe_load_all(file) if document is not None]

    if len(documents) != 1:
        raise CaseRegistryError(f'{path}: файл должен содержать ровно один YAML template')
    document = documents[0]
    if not isinstance(document, dict):
        raise CaseRegistryError(f'{path}: YAML template должен быть объектом')
    return document


def _build_case(template: dict[str, Any], variant: Any, path: Path) -> EvalCase:
    if not isinstance(variant, dict):
        raise CaseRegistryError(f'{path}: variant должен быть объектом')

    variables = variant.get('variables')
    if not isinstance(variables, dict):
        raise CaseRegistryError(f'{path}: variant.variables должен быть объектом')

    default_metadata = template.get('default_metadata') or {}
    variant_metadata = variant.get('metadata') or {}
    if not isinstance(default_metadata, dict):
        raise CaseRegistryError(f'{path}: default_metadata должен быть объектом')
    if not isinstance(variant_metadata, dict):
        raise CaseRegistryError(f'{path}: variant.metadata должен быть объектом')

    expected_outcome = template.get('expected_outcome') or {}

    return EvalCase(
        eval_case_id=_required_string(variant, 'eval_case_id', path),
        case_version=_required_string(variant, 'case_version', path),
        variant_id=_required_string(variant, 'variant_id', path),
        title=template.get('title'),
        host=template.get('host'),
        task=_render_required(template.get('task_template'), variables, path, 'task_template'),
        start_url=_render_required(template.get('start_url_template'), variables, path, 'start_url_template'),
        metadata={**default_metadata, **variant_metadata},
        auth_profile=template.get('auth_profile'),
        expected_outcome=ExpectedOutcome.model_validate(_render_value(expected_outcome, variables, path)),
        forbidden_actions=_render_value(template.get('forbidden_actions') or [], variables, path),
        rubric=_render_value(template.get('rubric') or {}, variables, path),
    )


def _required_string(data: dict[str, Any], field_name: str, path: Path) -> str:
    value = data.get(field_name)
    if not isinstance(value, str) or not value:
        raise CaseRegistryError(f'{path}: {field_name} должен быть непустой строкой')
    return value


def _render_required(value: Any, variables: dict[str, Any], path: Path, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise CaseRegistryError(f'{path}: {field_name} должен быть непустой строкой')
    return _render_text(value, variables, path)


def _render_value(value: Any, variables: dict[str, Any], path: Path) -> Any:
    if isinstance(value, str):
        return _render_text(value, variables, path)
    if isinstance(value, list):
        return [_render_value(item, variables, path) for item in value]
    if isinstance(value, dict):
        return {key: _render_value(item, variables, path) for key, item in value.items()}
    return value


def _render_text(template: str, variables: dict[str, Any], path: Path) -> str:
    def replace(match: re.Match[str]) -> str:
        variable_name = match.group(1)
        if variable_name not in variables:
            raise CaseRegistryError(f'{path}: не задана переменная шаблона {variable_name}')
        return str(variables[variable_name])

    return TEMPLATE_VARIABLE_RE.sub(replace, template)
