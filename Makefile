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

# P3 退出验收：一次请求产出 6 行 agent_runs / 6 个 agent span 同 trace。
e2e:
	cd backend && $(PYTHON) -m pytest tests -q -m e2e

verify:
	$(MAKE) up
	$(MAKE) migrate
	$(MAKE) test
	$(COMPOSE) --env-file $(COMPOSE_ENV) config -q
