# Playbook Litres

## Payment boundary

- Дойди до оплаты только через SberPay/СберPay/СберПэй.
- Для Litres SberPay находится за способом оплаты `Российская карта`.
- Выбери `Российская карта`, нажми `Продолжить`, дождись payment iframe с адресом вида `https://payecom.ru/pay_ru?orderId=...`.
- Извлеки `order_id` из параметра `orderId` в iframe `src`.
- Верни `payment_evidence.source="litres_payecom_iframe"` и exact iframe URL в `payment_evidence.url`.

## Stop rules

- Не продолжай оплату внутри платежного iframe.
- Если iframe PayEcom отсутствует, orderId не совпадает или выбран SBP/FPS/СБП, не возвращай `completed`.
- Если нужен пользовательский выбор формата, книги, адреса или способа оплаты, верни `needs_user_input` с одним вопросом.
