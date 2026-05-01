# Site instructions: Brandshop

## Поиск товара

- Допустим быстрый путь через direct search URL: `https://brandshop.ru/search/?st=<query>`.
- В `<query>` включай только product identity из текущей задачи: бренд, модель и категорию.
- Размер, цвет и другие варианты остаются constraints для фильтрации, ранжирования и проверки, а не обязательными словами поискового URL.
- Product URL выбирай из фактических результатов поиска по соответствию задаче; нельзя hardcode SKU, product URL или `mfp`.
- UI-поиск через header search button и поле `Искать в каталоге` остается допустимым fallback и реалистичным путем, если direct search не дал надежного результата.

## Проверка варианта

- Перед добавлением в корзину проверь, что выбранный товар соответствует бренду, модели, категории и цвету из задачи.
- Если размер указан в задаче, metadata или latest user reply, выбери и подтверди его через UI-control или проверяемое состояние страницы.
- Если цветовую семью нельзя надежно отличить по text/link/alt/snapshot/screenshot, верни `needs_user_input` с одним вопросом о цвете.

## Платежная граница

- На checkout выбирай только SberPay; СБП/FPS не является заменой.
- После создания платежной сессии остановись на YooMoney URL вида `https://yoomoney.ru/checkout/payments/v2/contract?orderId=...`.
- Верни `payment_evidence.source="brandshop_yoomoney_sberpay_redirect"` и exact evidence URL.
