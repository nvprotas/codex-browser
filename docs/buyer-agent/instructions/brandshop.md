# Инструкции Brandshop

## Поиск товара

- Brandshop auth script возвращает браузер на обычную страницу Brandshop; сначала проверь текущую страницу и работай от нее, а не от hardcoded SKU.
- Допустим быстрый путь через direct search URL: `https://brandshop.ru/search/?st=<query>`.
- В `<query>` включай только product identity из текущей задачи.
- Размер и цвет являются ограничениями для фильтрации, ранжирования и проверки, а не обязательными словами поискового URL.
- UI-поиск через header search button с `aria-label="search"`, catalog search input с placeholder `Искать в каталоге` и press Enter остается допустимым fallback.
- Product URL выбирай из фактических результатов поиска по соответствию задаче; нельзя hardcode SKU или product URL.
- URL с `mfp=...` допустим только если он достигнут через UI или подтвержден через page links/state; не hardcode `mfp` как единственный путь.
- Если цветовую семью нельзя надежно отличить по text/link/alt/snapshot/screenshot, например light/beige/white от black, верни `status=needs_user_input` с одним вопросом о цвете.

## Проверка варианта

- Перед `Добавить в корзину` проверь product identity и явно заданные constraints выбранного товара относительно текущей задачи, metadata, профиля и свежего ответа пользователя.
- Размер из текущей задачи, metadata или latest_user_reply является обязательным constraint, если он указан: выбирай его через UI-control или подтвержденное состояние страницы.
- Цветовое предпочтение из текущей задачи, metadata или latest_user_reply используй как ranking/verification constraint.
- Если в task, metadata или latest_user_reply указан размер, цвет или вариант, перед `Добавить в корзину` найди, выбери и проверь точный вариант через `snapshot`, `text`, `exists` или `attr`.
- Если кнопка `Добавить в корзину` показывает другой выбранный размер, цвет или вариант, клик запрещен до выбора нужного варианта.
- Если после выбора размера `.product-order` уже показывает `1 в корзине` и доступна кнопка `Оформить заказ`, не пытайся повторно нажимать `Добавить в корзину`.

## Корзина и checkout

- Не переходи на `/cart/`: для Brandshop это не надежная страница корзины и может вернуть 404.
- Для проверки корзины используй header cart popup, `.product-order` или checkout summary.
- Проверь ровно один товар: matching product, указанный размер при наличии, quantity `1`.
- Если товаров больше одного, товар не совпадает, размер не соответствует текущей задаче или количество не `1`, исправь обратимым действием или верни `needs_user_input`.
- Если доступна `.product-order__checkout-btn` или кнопка `Оформить заказ`, можно переходить к checkout через нее с `--wait-url-contains checkout`.
- На checkout проверь адрес доставки. Если адрес отсутствует, неоднозначен или требует выбора/редактирования, верни `status=needs_user_input`.

## Платежная граница

- Выбирай только radio/payment method `SberPay`; SBP/FPS/СБП и Система быстрых платежей не являются заменой.
- На Brandshop `Подтвердить заказ` можно нажать только после явного выбора SberPay и только чтобы создать внешнюю платежную сессию; не продолжай оплату на YooMoney.
- Остановись сразу на `https://yoomoney.ru/checkout/payments/v2/contract?orderId=...`.
- Извлеки matching `order_id` из `orderId` и верни `payment_evidence.source="brandshop_yoomoney_sberpay_redirect"` с exact evidence URL.

## Пример

Example task shape: `Купи на brandshop.ru Air Jordan Retro High OG`.

Используй `Air Jordan Retro High OG` как product identity для поиска, например через `/search/?st=Air%20Jordan%20Retro%20High%20OG`, а `45 EU` как constraint. Не hardcode SKU, product URL или `mfp`.
