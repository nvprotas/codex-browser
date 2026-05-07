# Инструкции Litres

## Платежная граница

- Дойди до оплаты только через SberPay/СберPay/СберПэй.
- Для Litres SberPay находится за способом оплаты `Российская карта`.
- Если браузер уже на странице `Покупка`/checkout или на `payment-error` с кнопкой `Попробовать снова`, не начинай поиск заново: восстанови checkout через эту кнопку и сразу проверь товар/способ оплаты.
- На checkout используй точные Litres-селекторы: `snapshot --selector '[data-testid="ppd-checkout"]' --limit 60`, затем `click --selector '[data-testid="payment__method--russian_card"]'`.
- Нажми `Продолжить` через `click --selector '[data-testid="paymentLayout__payment--button"]' --wait-selector 'iframe[src*="payecom.ru/pay_ru"]'`.
- Не читай `html` и не проверяй голый `iframe`, если достаточно `attr --selector 'iframe[src*="payecom.ru/pay_ru"]' --name src`: на Litres бывают сторонние iframe, которые не являются платежной границей.
- Извлеки `order_id` из параметра `orderId` в PayEcom iframe `src`.
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
