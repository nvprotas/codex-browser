# Playbook Brandshop

## Generic flow

- Brandshop auth script возвращает браузер на `https://brandshop.ru/`; сначала проверь текущую страницу и работай от главной страницы, а не от hardcoded SKU.
- Для поиска допустим fast path через direct search URL `/search/?st=<query>`, где `<query>` строится из product identity текущей задачи.
- UI-поиск через header search button с `aria-label="search"`, catalog search input с placeholder `Искать в каталоге` и press Enter остается допустимым fallback и реалистичным путем.
- Строй поисковый запрос только из product identity текущей задачи: бренд, модель и категория из task/metadata/latest_user_reply идут в запрос.
- Размер и цвет являются ограничениями для фильтрации, ранжирования и проверки, а не дефолтными словами поиска.
- Product URL выбирай из фактических результатов поиска; не hardcode SKU или product URL.
- Размер из текущей задачи, metadata или latest_user_reply является обязательным constraint, если он указан: выбирай его через UI-фильтр или подтвержденный control на странице товара.
- URL с `mfp=...` допустим только если он достигнут через UI или подтвержден через page links/state; не hardcode `mfp` как единственный путь.
- Цветовое предпочтение из текущей задачи, metadata или latest_user_reply используй как ranking/verification constraint.
- Если запрошенную цветовую семью нельзя надежно отличить по text/link/alt/snapshot/screenshot, например light/beige/white от black, верни `status=needs_user_input` с одним вопросом о цвете.

## Product and cart verification

- Перед `Добавить в корзину` проверь бренд, модель, категорию, цвет и размер выбранного товара относительно текущей задачи, metadata, профиля и свежего ответа пользователя.
- Если в task, metadata или latest_user_reply указан размер, цвет или вариант, перед `Добавить в корзину` найди, выбери и проверь точный вариант через `snapshot`, `text`, `exists` или `attr`.
- Если кнопка `Добавить в корзину` показывает другой выбранный размер, цвет или вариант, клик запрещен до выбора нужного варианта.
- После добавления открой корзину и проверь ровно один товар: matching product, указанный размер при наличии, quantity `1`.
- Если товаров больше одного, товар не совпадает, размер не соответствует текущей задаче или количество не `1`, исправь обратимым действием или верни `needs_user_input`.

## Checkout and SberPay evidence

- На checkout проверь адрес доставки. Если адрес отсутствует, неоднозначен или требует выбора/редактирования, верни `status=needs_user_input`.
- Выбирай только radio/payment method `SberPay`; SBP/FPS/СБП и Система быстрых платежей не являются заменой.
- На Brandshop `Подтвердить заказ` можно нажать только после явного выбора SberPay и только чтобы создать внешнюю платежную сессию; не продолжай оплату на YooMoney.
- Остановись сразу на `https://yoomoney.ru/checkout/payments/v2/contract?orderId=...`.
- Извлеки matching `order_id` из `orderId` и верни `payment_evidence.source="brandshop_yoomoney_sberpay_redirect"` с exact evidence URL.

## Example

Example task shape: `купи светлые кроссовки Jordan Air High 45 EU`.

Используй `Jordan Air High` как product identity для поиска, например через `/search/?st=Jordan%20Air%20High`, а `45 EU` и `светлые` как constraints. Не hardcode SKU, product URL или `mfp` как единственный путь.
