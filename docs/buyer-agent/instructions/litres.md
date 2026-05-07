# Инструкции Litres

## Платежная граница

- Дойди до оплаты только через SberPay/СберPay/СберПэй.
- Для Litres SberPay находится за способом оплаты `Российская карта`.
- Выбери `Российская карта`, нажми `Продолжить`, дождись payment iframe с адресом вида `https://payecom.ru/pay_ru?orderId=...`.
- Извлеки `order_id` из параметра `orderId` в iframe `src`.
- Верни `payment_evidence.source="litres_payecom_iframe"` и exact iframe URL в `payment_evidence.url`.

## Быстрый путь на `/purchase/ppd/`

- Если текущий URL уже содержит `/purchase/ppd/`, не возвращайся в корзину и не ищи товар заново.
- Сначала проверь `iframe[src*="payecom.ru/pay_ru"]`; если он уже есть, извлеки `orderId` и завершай без дополнительных кликов.
- Если на странице ошибка `Превышено время ожидания.`, нажми `button:has-text("Попробовать снова")`, затем проверь checkout.
- Для проверки заказа используй компактный `snapshot --selector '[data-testid="ppd-checkout"]'`: должны совпасть название, автор и формат книги из задачи.
- Если checkout открыт, но `Российская карта` еще не выбрана, нажми `[data-testid="payment__method--russian_card"]`.
- Если checkout уже открыт и выбран способ `Российская карта` или URL содержит `method=russian_card&system=sbercard`, сразу нажми `[data-testid="paymentLayout__payment--button"]` с `--wait-selector 'iframe[src*="payecom.ru/pay_ru"]'`.
- После нажатия проверяй только `attr --selector 'iframe[src*="payecom.ru/pay_ru"]' --name src`; не открывай и не управляй содержимым iframe.

## Stop rules

- Не продолжай оплату внутри платежного iframe.
- Если iframe PayEcom отсутствует, orderId не совпадает или выбран SBP/FPS/СБП, не возвращай `completed`.
- Если нужен пользовательский выбор формата, книги, адреса или способа оплаты, верни `needs_user_input` с одним вопросом.
