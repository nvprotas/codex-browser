# Инструкции Litres

## Платежная граница

- Дойди до оплаты только через SberPay/СберPay/СберПэй.
- Для Litres SberPay находится за способом оплаты `Российская карта`.
- Если браузер уже на странице `Покупка`/checkout или на `payment-error` с кнопкой `Попробовать снова`, не начинай поиск заново: восстанови checkout через эту кнопку и сразу проверь товар/способ оплаты.
- На checkout используй точные Litres-селекторы: `snapshot --selector '[data-testid="ppd-checkout"]' --limit 60`, затем `click --selector '[data-testid="payment__method--russian_card"]'`.
- Нажми `Продолжить` через `click --selector '[data-testid="paymentLayout__payment--button"]' --wait-selector 'iframe[src^="https://payecom.ru/pay_ru"][src*="orderId="]'`.
- Для evidence не читай полный HTML: одной структурной проверкой получи iframe `src` через `attr --selector 'iframe[src^="https://payecom.ru/pay_ru"][src*="orderId="]' --name src`: на Litres бывают сторонние iframe, которые не являются платежной границей.
- Извлеки `order_id` из параметра `orderId` в PayEcom iframe `src`.
- Верни `payment_evidence.source="litres_payecom_iframe"` и exact iframe URL в `payment_evidence.url`.

## Короткий путь на `/purchase/ppd/`

- Если текущий URL уже содержит `/purchase/ppd/`, не возвращайся в корзину и не ищи товар заново.
- Сначала проверь `exists --selector 'iframe[src*="payecom.ru/pay_ru"]'`; если iframe уже есть, извлеки `orderId` через `attr --name src` и завершай без дополнительных кликов.
- Если текущий URL содержит `/purchase/ppd/`, `method=russian_card&system=sbercard` и параметры `offer-page`/`book-slug`/`author` уже указывают на целевую книгу, не делай `title`, `links`, `snapshot body` и не возвращайся на карточку товара.
- На таком подтвержденном checkout перед кликом не трать 15 секунд на клик по еще не появившейся кнопке: проверь `exists --selector '[data-testid="paymentLayout__payment--button"]'`; если кнопка есть, нажми ее с ожиданием PayEcom iframe.
- Если кнопки нет, прочитай только короткий `text --selector body --max-chars 500`; при тексте `Превышено время ожидания` сразу нажми `button:has-text("Попробовать снова")` с ожиданием PayEcom iframe, а если ошибки нет, один раз дождись `wait-selector --selector '[data-testid="paymentLayout__payment--button"]' --timeout-ms 5000` и затем нажми кнопку.
- Если на странице ошибка `Превышено время ожидания`, нажми `button:has-text("Попробовать снова")`: это повторная загрузка провайдера, а не финальная оплата. После клика жди только PayEcom iframe или checkout/error milestone.
- Для проверки заказа используй компактный `snapshot --selector '[data-testid="ppd-checkout"]' --limit 60` только если URL checkout не подтверждает целевую книгу или способ оплаты еще не выбран: должны совпасть название, автор и формат книги из задачи.
- Если checkout открыт, но `Российская карта` еще не выбрана, нажми `[data-testid="payment__method--russian_card"]`.
- Если checkout открыт и выбран способ `Российская карта` или URL содержит `method=russian_card&system=sbercard`, сразу нажми `[data-testid="paymentLayout__payment--button"]` с `--wait-selector 'iframe[src*="payecom.ru/pay_ru"]'`.
- После нажатия проверяй только `attr --selector 'iframe[src*="payecom.ru/pay_ru"]' --name src`; не открывай и не управляй содержимым iframe.
- Не сохраняй полный HTML checkout и не ищи по нему, пока доступны `snapshot`, `exists`, `attr` и `text`.

## Stop rules

- Не продолжай оплату внутри платежного iframe.
- Если iframe PayEcom отсутствует, orderId не совпадает или выбран SBP/FPS/СБП, не возвращай `completed`.
- Если нужен пользовательский выбор формата, книги, адреса или способа оплаты, верни `needs_user_input` с одним вопросом.
