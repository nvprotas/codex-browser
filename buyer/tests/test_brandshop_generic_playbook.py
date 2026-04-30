from __future__ import annotations

import argparse
import unittest

from buyer.app.prompt_builder import build_agent_prompt
from buyer.tools.cdp_tool import _collect_snapshot, parser


def _brandshop_prompt() -> str:
    return build_agent_prompt(
        task='Купи светлые кроссовки Jordan Air High 45 EU',
        start_url='https://brandshop.ru/',
        browser_cdp_endpoint='http://browser:9223',
        cdp_preflight_summary='OK',
        metadata={'brand': 'Jordan', 'model': 'Air High', 'size': '45 EU', 'color': 'светлые'},
        auth_payload=None,
        auth_context={'profile': 'brandshop_sberid'},
        user_profile_text=None,
        user_profile_truncated=False,
        memory=[],
        latest_user_reply=None,
    )


class BrandshopGenericPlaybookPromptTests(unittest.TestCase):
    def test_prompt_contains_brandshop_generic_playbook_requirements(self) -> None:
        prompt = _brandshop_prompt()

        expected_fragments = [
            'https://brandshop.ru/',
            'aria-label="search"',
            'Искать в каталоге',
            'press Enter',
            'Jordan Air High',
            'размер и цвет являются ограничениями',
            '45 EU',
            'фильтр',
            'mfp',
            'светлые',
            'light/beige/white',
            'needs_user_input',
            'Перед `Добавить в корзину`',
            'бренд, модель, категорию, цвет и размер',
            'ровно один товар',
            'quantity `1`',
            'адрес доставки',
            'SberPay',
            'SBP/FPS/СБП',
            'Подтвердить заказ',
            'внешнюю платежную сессию',
            'https://yoomoney.ru/checkout/payments/v2/contract?orderId=',
            'brandshop_yoomoney_sberpay_redirect',
            'не продолжай оплату на YooMoney',
        ]
        for fragment in expected_fragments:
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, prompt)

    def test_prompt_does_not_hardcode_brandshop_example_as_runtime_defaults(self) -> None:
        prompt = build_agent_prompt(
            task='Купи темную худи Stussy размера M',
            start_url='https://brandshop.ru/',
            browser_cdp_endpoint='http://browser:9223',
            cdp_preflight_summary='OK',
            metadata={'brand': 'Stussy', 'category': 'худи', 'size': 'M', 'color': 'темная'},
            auth_payload=None,
            auth_context={'profile': 'brandshop_sberid'},
            user_profile_text=None,
            user_profile_truncated=False,
            memory=[],
            latest_user_reply=None,
        )

        self.assertNotIn('Jordan Air High', prompt)
        self.assertNotIn('45 EU', prompt)
        self.assertNotIn('светлые', prompt)
        self.assertIn('product identity', prompt)
        self.assertIn('размер из текущей задачи', prompt)
        self.assertIn('цветовое предпочтение из текущей задачи', prompt)


class _SnapshotElement:
    def __init__(self, *, tag: str, text: str, attrs: dict[str, str] | None = None) -> None:
        self.tag = tag
        self.text = text
        self.attrs = attrs or {}


class _SnapshotLocator:
    def __init__(self, elements: list[_SnapshotElement]) -> None:
        self.elements = elements

    async def evaluate(self, script: str, options: dict[str, object]) -> list[dict[str, object]]:
        self.script = script
        limit = int(options['limit'])
        text_limit = int(options['textLimit'])
        option_tags = set(options.get('optionTags', []))
        option_class_hints = [str(item) for item in options.get('optionClassHints', [])]
        option_data_attributes = [str(item) for item in options.get('optionDataAttributes', [])]
        option_state_attributes = [str(item) for item in options.get('optionStateAttributes', [])]

        items: list[dict[str, object]] = []
        for element in self.elements:
            if len(items) >= limit:
                break
            attrs = element.attrs
            class_name = attrs.get('class', '')
            is_base = element.tag in {'a', 'button', 'input', 'textarea', 'select', 'h1', 'h2', 'h3', 'label', 'p'}
            has_base_marker = any(name in attrs for name in ('role', 'data-testid'))
            has_hint = element.tag in option_tags and (
                any(hint in class_name.lower() for hint in option_class_hints)
                or any(name in attrs for name in option_data_attributes)
                or any(name in attrs for name in option_state_attributes)
            )
            if not is_base and not has_base_marker and not has_hint:
                continue

            data = {
                name: attrs[name][:text_limit]
                for name in option_data_attributes
                if name in attrs
            }
            item: dict[str, object] = {
                'tag': element.tag,
                'role': attrs.get('role'),
                'testid': attrs.get('data-testid'),
                'text': element.text[:text_limit],
                'href': attrs.get('href'),
                'aria_label': attrs.get('aria-label'),
                'name': attrs.get('name'),
                'type': attrs.get('type'),
                'placeholder': attrs.get('placeholder'),
                'visible': True,
            }
            if has_hint:
                item['class'] = class_name
                if data:
                    item['data'] = data
            items.append(item)
        return items


class _SnapshotPage:
    def __init__(self, elements: list[_SnapshotElement]) -> None:
        self.url = 'https://brandshop.ru/search/?st=Jordan+Air+High'
        self._locator = _SnapshotLocator(elements)

    def locator(self, selector: str) -> _SnapshotLocator:
        self.selector = selector
        return self._locator


class BrandshopSnapshotHintTests(unittest.IsolatedAsyncioTestCase):
    async def test_snapshot_surfaces_brandshop_controls_from_class_hints(self) -> None:
        page = _SnapshotPage(
            [
                _SnapshotElement(tag='div', text='layout only', attrs={'class': 'page-grid'}),
                _SnapshotElement(tag='span', text='Поиск', attrs={'class': 'header-search-trigger'}),
                _SnapshotElement(tag='div', text='45 EU', attrs={'class': 'catalog-filter__value'}),
                _SnapshotElement(tag='div', text='Jordan Air High светлые', attrs={'class': 'product-card'}),
                _SnapshotElement(tag='div', text='Jordan Air High 45 EU quantity 1', attrs={'class': 'cart-item'}),
                _SnapshotElement(tag='div', text='Москва, улица Пушкина', attrs={'class': 'checkout-address'}),
                _SnapshotElement(tag='li', text='SberPay', attrs={'class': 'radio-list__item'}),
            ]
        )
        args: argparse.Namespace = parser().parse_args(['snapshot', '--selector', 'main', '--limit', '20'])

        result = await _collect_snapshot(page, args)

        texts = [str(item['text']) for item in result['items']]
        self.assertNotIn('layout only', texts)
        for expected in (
            'Поиск',
            '45 EU',
            'Jordan Air High светлые',
            'Jordan Air High 45 EU quantity 1',
            'Москва, улица Пушкина',
            'SberPay',
        ):
            with self.subTest(expected=expected):
                self.assertIn(expected, texts)
