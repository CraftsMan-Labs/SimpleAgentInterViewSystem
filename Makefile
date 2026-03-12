.PHONY: up down logs ps restart

up:
	docker compose up -d --build

down:
	docker compose down --remove-orphans

logs:
	docker compose logs -f simpleagent-interview-system

ps:
	docker compose ps

restart: down up
