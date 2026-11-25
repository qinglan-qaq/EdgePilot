PYTHON ?= python
COMPOSE ?= docker compose -f docker/compose/docker-compose.yml
COMPOSE_ENV ?= docker/compose/.env.example
MAKE ?= make

.PHONY: up app down migrate test e2e verify

up:
	$(COMPOSE) --env-file $(COMPOSE_ENV) up -d
	PYTHON="$(PYTHON)" bash scripts/wait_for_stack.sh

app:
	$(COMPOSE) --env-file $(COMPOSE_ENV) --profile app up -d --build

down:
	$(COMPOSE) --env-file $(COMPOSE_ENV) down

migrate:
	cd backend && $(PYTHON) -m alembic upgrade head

test:
	cd backend && $(PYTHON) -m pytest tests -q

# 当前尚无 @pytest.mark.e2e 用例，此目标会返回 "no tests ran"（退出码 5）。
e2e:
	cd backend && $(PYTHON) -m pytest tests -q -m e2e

verify:
	$(MAKE) up
	$(MAKE) migrate
	$(MAKE) test
	$(COMPOSE) --env-file $(COMPOSE_ENV) config -q
