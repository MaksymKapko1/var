# Управление ботом на сервере. Всё через docker compose в текущей папке.
DC := docker compose

.PHONY: help up down restart logs check status cancel close resume stop update test ps rot-up rot-logs rot-check rot-status rot-stop rot-close rot-resume br-up br-logs br-check br-status br-stop br-close br-resume

help:
	@echo "make up       — собрать и запустить (фоном, авто-рестарт)"
	@echo "make logs     — логи в реальном времени"
	@echo "make check    — проверить ключ/баланс/инструмент (бот не торгует)"
	@echo "make status   — состояние бота, позиция, открытые ордера"
	@echo "make ps       — статус контейнера и healthcheck"
	@echo "make stop     — остановить бота (ордера остаются, если CANCEL_ON_EXIT=false)"
	@echo "make cancel   — остановить и снять все ордера на SYMBOL"
	@echo "make close    — остановить, снять ордера и ЗАКРЫТЬ позицию по рынку"
	@echo "make resume   — снять режим СТОП и запустить заново"
	@echo "make update   — git pull + пересборка + перезапуск"
	@echo "make test     — тесты в контейнере"
	@echo ""
	@echo "Ротация (рыночные позиции BTC/ETH/HYPE/SOL, конфиг .env.rotation):"
	@echo "make rot-check / rot-up / rot-logs / rot-status / rot-stop / rot-close / rot-resume"
	@echo ""
	@echo "Bracket (US500 + золото, TP/SL, конфиг .env.bracket):"
	@echo "make br-check / br-up / br-logs / br-status / br-stop / br-close / br-resume"

up:
	$(DC) up -d --build

down stop:
	$(DC) stop

restart:
	$(DC) restart

logs:
	$(DC) logs -f --tail=200

ps:
	$(DC) ps

check:
	$(DC) run --rm --build gridbot check

status:
	$(DC) run --rm gridbot status

cancel:
	$(DC) stop
	$(DC) run --rm gridbot cancel

close:
	$(DC) stop
	$(DC) run --rm gridbot close

resume:
	$(DC) stop
	$(DC) run --rm gridbot resume
	$(DC) up -d

update:
	git pull --ff-only
	$(DC) up -d --build

test:
	docker run --rm -v "$(CURDIR)":/src -w /src python:3.12-slim sh -c "pip -q install -r requirements.txt -r requirements-dev.txt && python -m pytest -q"

# ---------------- ротация (сервис rotation, конфиг .env.rotation) ----------------
DCR := docker compose --profile rotation

rot-up:
	$(DCR) up -d --build rotation

rot-logs:
	$(DCR) logs -f --tail=200 rotation

rot-check:
	$(DCR) run --rm --build rotation check

rot-status:
	$(DCR) run --rm rotation status

rot-stop:
	$(DCR) stop rotation

rot-close:
	$(DCR) stop rotation
	$(DCR) run --rm rotation close

rot-resume:
	$(DCR) stop rotation
	$(DCR) run --rm rotation resume
	$(DCR) up -d rotation

# ---------------- bracket (сервис bracket, конфиг .env.bracket) ----------------
DCB := docker compose --profile bracket

br-up:
	$(DCB) up -d --build bracket

br-logs:
	$(DCB) logs -f --tail=200 bracket

br-check:
	$(DCB) run --rm --build bracket check

br-status:
	$(DCB) run --rm bracket status

br-stop:
	$(DCB) stop bracket

br-close:
	$(DCB) stop bracket
	$(DCB) run --rm bracket close

br-resume:
	$(DCB) stop bracket
	$(DCB) run --rm bracket resume
	$(DCB) up -d bracket
