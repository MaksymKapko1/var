# Variational Grid Bot (testnet)

Простой грид-бот для [Variational](https://variational.io) на Python, упакованный в Docker.
Все настройки и ключи задаются в `.env`, поэтому один и тот же образ можно запускать на разных серверах с разными ключами.

## Как работает

Variational — **RFQ-биржа**. У неё нет стакана и лимитных ордеров: ты запрашиваешь котировку
у маркет-мейкеров и принимаешь лучшую. Поэтому лимитные ордера грида бот эмулирует так:

1. Диапазон `GRID_LOWER..GRID_UPPER` делится на `GRID_LEVELS` уровней.
2. Раз в `POLL_INTERVAL_SEC` бот берёт mark price (`/price/instrument`).
3. Если цена опустилась до уровня `i` и ячейка `i` пустая, бот **покупает** `ORDER_QTY`.
4. Если цена поднялась до уровня `i+1` и в ячейке `i` есть лот, бот **продаёт** `ORDER_QTY`.
5. Каждая сделка проходит через RFQ: `create_rfq`, затем лучшая котировка, `accept_quote`, при необходимости подпись permit на депозит маржи, и ожидание `success_trades_booked_into_pool`.
6. Если котировка хуже mark price больше чем на `MAX_SLIPPAGE_PCT`, сделка отменяется.

Максимальная позиция равна `(GRID_LEVELS - 1) * ORDER_QTY`. Состояние сетки хранится в `./data/state.json`, поэтому после рестарта бот не покупает повторно.

> При старте в середине диапазона бот сразу докупает лоты для всех ячеек выше цены, как обычный спотовый грид. Они продаются, когда цена растёт.

## Что нужно

1. EVM-кошелёк (MetaMask или Rabby), **отдельный, тестовый**.
2. Зайти на https://testnet.variational.io и подключить кошелёк. Взять тестовые USDC (faucet в интерфейсе) и внести их как залог.
3. Создать API-ключ в https://testnet.variational.io/app/settings, раздел API. Нужна роль **writer**: ключ с ролью reader может только читать. Secret показывается один раз.
   > В официальном SDK написано, что генерация ключей может быть закрыта и доступ выдают через hello@variational.io. Если кнопки нет, пиши им.
4. Приватный ключ этого тестового кошелька в `WALLET_PRIVATE_KEY`. Он нужен, чтобы подписывать permit на перевод маржи в settlement pool при сделке.

## Запуск

```bash
cp .env.example .env
nano .env          # ключи, диапазон, размер
```

Проверить ключи, баланс и цену, потом выйти:

```bash
docker compose run --rm gridbot check
```

Запуск (сначала с `DRY_RUN=true`: только логи, без сделок):

```bash
docker compose up -d --build
```

```bash
docker compose logs -f
```

Когда логи выглядят нормально, поставь `DRY_RUN=false` и перезапусти:

```bash
docker compose up -d
```

### На другом сервере

```bash
git clone <repo> && cd var && cp .env.example .env && nano .env && docker compose up -d --build
```

Чтобы запустить несколько ботов на одном сервере, сделай отдельную папку (свой `.env` и `data/`) на каждого.

## Переменные `.env`

| Переменная | Описание |
|---|---|
| `VAR_API_KEY`, `VAR_API_SECRET` | ключи API |
| `VAR_BASE_URL` | `https://api.testnet.variational.io/v1` (testnet) |
| `WALLET_PRIVATE_KEY` | ключ тестового кошелька для подписи депозитов |
| `UNDERLYING` | `ETH`, `BTC`, ... |
| `INSTRUMENT_TYPE` | `perpetual_future` или `spot` |
| `GRID_LOWER` / `GRID_UPPER` / `GRID_LEVELS` | сетка |
| `ORDER_QTY` | размер одной сделки (учитывай `min_order_notional` из `check`) |
| `MAX_SLIPPAGE_PCT` | защита от плохой котировки |
| `DRY_RUN` | `true` значит без реальных сделок |

## Тесты

```bash
pip install variational==0.6.0 pytest && python -m pytest -q
```
