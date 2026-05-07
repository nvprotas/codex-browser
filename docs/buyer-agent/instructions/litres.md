# Инструкции Litres

## Платежная граница

- Дойди до оплаты только через SberPay/СберPay/СберПэй.
- Для Litres SberPay находится за способом оплаты `Российская карта`.
- Выбери `Российская карта`, нажми `Продолжить`, дождись payment iframe с адресом вида `https://payecom.ru/pay_ru?orderId=...`.
- Извлеки `order_id` из параметра `orderId` в iframe `src`.
- Верни `payment_evidence.source="litres_payecom_iframe"` и exact iframe URL в `payment_evidence.url`.

## Короткий путь через checkout

- Если браузер уже на странице `Покупка`/checkout Litres и виден PayEcom iframe, не делай `snapshot`/`html`: сразу проверь `exists --selector 'iframe[src*="payecom.ru/pay_ru"]'`, затем прочитай `src` через `attr --selector 'iframe[src*="payecom.ru/pay_ru"]' --name src`.
- Если на платежном шаге видна ошибка `Превышено время ожидания` и кнопка `Попробовать снова`, можно один раз нажать ее: это повторная загрузка провайдера, а не финальная оплата. После клика жди только PayEcom iframe или checkout/error milestone.
- Если после retry открыт checkout с `data-testid="payment__method--russian_card"` и `data-testid="paymentLayout__payment--button"`, выбери/оставь `Российская карта` и нажми `Продолжить`; затем проверяй только PayEcom iframe через `exists` + `attr --name src`.
- Не сохраняй полный HTML checkout и не ищи по нему, пока доступны `snapshot`, `exists`, `attr` и `text`: это обычно лишний шаг.

## Stop rules

- Не продолжай оплату внутри платежного iframe.
- Если iframe PayEcom отсутствует, orderId не совпадает или выбран SBP/FPS/СБП, не возвращай `completed`.
- Если нужен пользовательский выбор формата, книги, адреса или способа оплаты, верни `needs_user_input` с одним вопросом.
