import json
import subprocess
import unittest
from pathlib import Path


class SberIdAuthIdempotencyHelperTests(unittest.TestCase):
    def test_auth_scripts_verify_existing_auth_before_entry_navigation_or_sber_clicks(self) -> None:
        buyer_root = Path(__file__).resolve().parents[1]
        cases = [
            (
                buyer_root / 'scripts' / 'sberid' / 'litres.ts',
                'const existingAuth = await verifyCurrentOrProfilePage(page, startUrl, tracePath);',
            ),
            (
                buyer_root / 'scripts' / 'sberid' / 'brandshop.ts',
                'const existingAuth = await verifyCurrentOrEntryPage(page, startUrl, tracePath);',
            ),
        ]

        for script_path, precheck_line in cases:
            with self.subTest(script=script_path.name):
                source = script_path.read_text(encoding='utf-8')
                precheck_index = source.index(precheck_line)
                first_entry_navigation_index = source.index("await tracedGoto(page, tracePath, 'auth_entry', entryUrl")
                first_sber_click_index = source.index('clickFirstVisible(page, sberIdTargets()')
                already_authenticated_index = source.index('already_authenticated: true')

                self.assertLess(precheck_index, first_entry_navigation_index)
                self.assertLess(precheck_index, first_sber_click_index)
                self.assertGreater(already_authenticated_index, precheck_index)

    def test_brandshop_auth_does_not_probe_account_page(self) -> None:
        buyer_root = Path(__file__).resolve().parents[1]
        source = (buyer_root / 'scripts' / 'sberid' / 'brandshop.ts').read_text(encoding='utf-8')

        self.assertNotIn("url.pathname = '/account/'", source)
        self.assertNotIn('auth_verify_account', source)

    def _run_tsx(self, source: str) -> dict:
        buyer_root = Path(__file__).resolve().parents[1]
        tsx = buyer_root / 'scripts' / 'node_modules' / '.bin' / 'tsx'
        if not tsx.is_file():
            self.skipTest('buyer/scripts/node_modules не установлен')

        completed = subprocess.run(
            [str(tsx), '-e', source],
            cwd=buyer_root / 'scripts',
            check=True,
            text=True,
            capture_output=True,
        )
        return json.loads(completed.stdout)

    def test_litres_auth_snapshot_requires_profile_markers(self) -> None:
        payload = self._run_tsx(
            (
                "import { verifyLitresAuthSnapshot } from './sberid/litres.ts';"
                "const authenticated = verifyLitresAuthSnapshot('https://www.litres.ru/me/profile/', 'Мои книги Профиль Бонусы');"
                "const loginForm = verifyLitresAuthSnapshot('https://www.litres.ru/auth/login/', 'Вход или регистрация Почта или логин Продолжить с почтой');"
                "const plainHome = verifyLitresAuthSnapshot('https://www.litres.ru/', 'Книги Аудиокниги Жанры Новинки');"
                "const callbackOnly = verifyLitresAuthSnapshot('https://www.litres.ru/callbacks/social-auth/?state=x', 'Мои книги Профиль');"
                "console.log(JSON.stringify({"
                "authenticatedVerified: authenticated.verified,"
                "authenticatedMarkers: authenticated.markers,"
                "loginFormVerified: loginForm.verified,"
                "loginFormSeen: loginForm.login_form_seen,"
                "plainHomeVerified: plainHome.verified,"
                "callbackOnlyVerified: callbackOnly.verified,"
                "callbackSeen: callbackOnly.callback_seen"
                "}));"
            ),
        )

        self.assertEqual(
            payload,
            {
                'authenticatedVerified': True,
                'authenticatedMarkers': ['Мои книги', 'Профиль'],
                'loginFormVerified': False,
                'loginFormSeen': True,
                'plainHomeVerified': False,
                'callbackOnlyVerified': False,
                'callbackSeen': True,
            },
        )

    def test_brandshop_auth_snapshot_requires_account_markers(self) -> None:
        payload = self._run_tsx(
            (
                "import { verifyBrandshopAuthSnapshot } from './sberid/brandshop.ts';"
                "const account = verifyBrandshopAuthSnapshot('https://brandshop.ru/account/', 'Личный кабинет Профиль Мои заказы Выйти');"
                "const logout = verifyBrandshopAuthSnapshot('https://brandshop.ru/', 'Профиль пользователя Мои заказы Logout');"
                "const sberForm = verifyBrandshopAuthSnapshot('https://brandshop.ru/', 'Войти с Сбер ID Номер телефона Получить код');"
                "const loginForm = verifyBrandshopAuthSnapshot('https://brandshop.ru/', 'Войти Регистрация Номер телефона');"
                "const accountLoginShell = verifyBrandshopAuthSnapshot('https://brandshop.ru/account/', 'Личный кабинет Профиль Войти');"
                "const plainHome = verifyBrandshopAuthSnapshot('https://brandshop.ru/', 'Новинки Бренды Мужское Женское');"
                "const callbackOnly = verifyBrandshopAuthSnapshot('https://brandshop.ru/sber/callback', 'Brandshop');"
                "console.log(JSON.stringify({"
                "accountVerified: account.verified,"
                "accountMarkers: account.markers,"
                "logoutVerified: logout.verified,"
                "logoutMarkers: logout.markers,"
                "sberFormVerified: sberForm.verified,"
                "sberFormLoginSeen: sberForm.login_form_seen,"
                "loginFormVerified: loginForm.verified,"
                "accountLoginShellVerified: accountLoginShell.verified,"
                "accountLoginShellLoginSeen: accountLoginShell.login_form_seen,"
                "plainHomeVerified: plainHome.verified,"
                "callbackOnlyVerified: callbackOnly.verified"
                "}));"
            ),
        )

        self.assertEqual(
            payload,
            {
                'accountVerified': True,
                'accountMarkers': ['Личный кабинет', 'Профиль', 'Мои заказы', 'Выйти'],
                'logoutVerified': True,
                'logoutMarkers': ['Профиль', 'Мои заказы', 'Logout'],
                'sberFormVerified': False,
                'sberFormLoginSeen': True,
                'loginFormVerified': False,
                'accountLoginShellVerified': False,
                'accountLoginShellLoginSeen': True,
                'plainHomeVerified': False,
                'callbackOnlyVerified': False,
            },
        )
