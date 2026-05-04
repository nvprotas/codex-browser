from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from eval_service.app.case_registry import CaseRegistry, CaseRegistryError


def write_case(path: Path, content: str) -> None:
    path.write_text(content, encoding='utf-8')


def valid_case_yaml(eval_case_id: str = 'litres_book_odyssey_001') -> str:
    return f"""
template_id: litres_purchase_book
title: Litres purchase smoke
host: litres.ru
start_url_template: "https://www.litres.ru/search/?q={{{{ query }}}}"
task_template: "Найди книгу {{{{ book_title }}}} автора {{{{ author }}}} и доведи покупку до SberPay без оплаты."
default_metadata:
  city: Москва
auth_profile: litres_sberid
expected_outcome:
  target: "Электронная книга {{{{ book_title }}}} автора {{{{ author }}}}"
  stop_condition: "Открыт платежный шаг SberPay/payment-ready, реальный платеж не выполнен"
  acceptable_variants:
    - "Checkout с доступным SberPay"
forbidden_actions:
  - "Нажимать финальное подтверждение оплаты"
rubric:
  required_checks:
    - outcome_ok
variants:
  - eval_case_id: {eval_case_id}
    case_version: "1"
    variant_id: odyssey_ebook
    variables:
      book_title: Одиссея
      author: Гомер
      query: odyssey
    metadata:
      budget: 500
"""


def test_loads_yaml_template_variants_as_concrete_eval_cases(tmp_path: Path) -> None:
    cases_dir = tmp_path / 'cases'
    cases_dir.mkdir()
    write_case(
        cases_dir / 'litres_purchase_book.yaml',
        valid_case_yaml(),
    )

    cases = CaseRegistry(cases_dir).load_cases()

    assert len(cases) == 1
    case = cases[0]
    assert case.eval_case_id == 'litres_book_odyssey_001'
    assert case.case_version == '1'
    assert case.variant_id == 'odyssey_ebook'
    assert case.title == 'Litres purchase smoke'
    assert case.host == 'litres.ru'
    assert case.task == 'Найди книгу Одиссея автора Гомер и доведи покупку до SberPay без оплаты.'
    assert case.start_url == 'https://www.litres.ru/search/?q=odyssey'
    assert case.metadata == {'city': 'Москва', 'budget': 500}
    assert case.auth_profile == 'litres_sberid'
    assert case.expected_outcome.target == 'Электронная книга Одиссея автора Гомер'
    assert case.forbidden_actions == ['Нажимать финальное подтверждение оплаты']
    assert case.rubric == {'required_checks': ['outcome_ok']}


def test_rejects_duplicate_eval_case_id_across_files(tmp_path: Path) -> None:
    cases_dir = tmp_path / 'cases'
    cases_dir.mkdir()
    write_case(cases_dir / 'litres_purchase_book.yaml', valid_case_yaml('duplicate_case_001'))
    write_case(cases_dir / 'brandshop_purchase_smoke.yaml', valid_case_yaml('duplicate_case_001'))

    with pytest.raises(CaseRegistryError, match='duplicate_case_001'):
        CaseRegistry(cases_dir).load_cases()


def test_rejects_variant_without_eval_case_id(tmp_path: Path) -> None:
    cases_dir = tmp_path / 'cases'
    cases_dir.mkdir()
    write_case(
        cases_dir / 'litres_purchase_book.yaml',
        valid_case_yaml().replace('  - eval_case_id: litres_book_odyssey_001\n', '  - \n'),
    )

    with pytest.raises(CaseRegistryError, match='eval_case_id'):
        CaseRegistry(cases_dir).load_cases()


def test_rejects_more_than_one_template_document_per_file(tmp_path: Path) -> None:
    cases_dir = tmp_path / 'cases'
    cases_dir.mkdir()
    write_case(cases_dir / 'litres_purchase_book.yaml', f'{valid_case_yaml()}\n---\n{valid_case_yaml("other_case_001")}')

    with pytest.raises(CaseRegistryError, match='ровно один YAML template'):
        CaseRegistry(cases_dir).load_cases()


def test_rejects_template_with_missing_variant_variable(tmp_path: Path) -> None:
    cases_dir = tmp_path / 'cases'
    cases_dir.mkdir()
    write_case(
        cases_dir / 'litres_purchase_book.yaml',
        valid_case_yaml().replace('      query: odyssey\n', ''),
    )

    with pytest.raises(CaseRegistryError, match='query'):
        CaseRegistry(cases_dir).load_cases()


def test_skips_disabled_yaml_template(tmp_path: Path) -> None:
    cases_dir = tmp_path / 'cases'
    cases_dir.mkdir()
    write_case(
        cases_dir / 'brandshop_purchase_smoke.yaml',
        valid_case_yaml('brandshop_disabled_001').replace(
            'template_id: litres_purchase_book\n',
            'template_id: brandshop_purchase_smoke\nenabled: false\ndisabled_reason: no verifier yet\n',
        ),
    )

    cases = CaseRegistry(cases_dir).load_cases()

    assert cases == []


def test_repository_smoke_cases_are_loadable() -> None:
    repo_root = Path(__file__).parents[2]

    cases = CaseRegistry(repo_root / 'eval' / 'cases').load_cases()

    assert {case.eval_case_id for case in cases} == {
        'litres_purchase_book_001',
        'litres_purchase_book_002',
        'litres_purchase_book_003',
        'brandshop_purchase_smoke_001',
    }
    assert {case.host for case in cases} == {'litres.ru', 'brandshop.ru'}


def test_brandshop_repository_case_uses_st_search_parameter() -> None:
    repo_root = Path(__file__).parents[2]

    with (repo_root / 'eval' / 'cases' / 'brandshop_purchase_smoke.yaml').open(encoding='utf-8') as file:
        template = yaml.safe_load(file)

    assert template['start_url_template'] == 'https://brandshop.ru/search/?st={{ search_query }}'
