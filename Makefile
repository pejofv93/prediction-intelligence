PROJECT=prediction-intelligence
REGION=europe-west1

# OBLIGATORIO: copiar shared/ en cada servicio antes de deployar.
# gcloud run deploy --source usa el directorio como build context.
# Sin este paso el build falla porque shared/ no esta dentro del servicio.
#
# ENV_VARS: exportar antes de deployar, ej: export TELEGRAM_TOKEN=xxx
# O crear un archivo .env y ejecutar: export $(cat .env | xargs) antes del make

deploy-sports:
	cp -r shared/ services/sports-agent/shared/
	gcloud run deploy sports-agent \
		--source services/sports-agent \
		--project $(PROJECT) --region $(REGION) \
		--allow-unauthenticated \
		--timeout=900 \
		--min-instances=0 \
		--memory=512Mi --cpu=1 \
		--set-env-vars="GOOGLE_CLOUD_PROJECT=$(PROJECT),FOOTBALL_API_KEY=$(FOOTBALL_API_KEY),FOOTBALL_RAPID_API_KEY=$(FOOTBALL_RAPID_API_KEY),CLOUD_RUN_TOKEN=$(CLOUD_RUN_TOKEN),TELEGRAM_BOT_URL=$(TELEGRAM_BOT_URL),FIRESTORE_COLLECTION_PREFIX=$(FIRESTORE_COLLECTION_PREFIX)"
	rm -rf services/sports-agent/shared/

deploy-poly:
	cp -r shared/ services/polymarket-agent/shared/
	gcloud run deploy polymarket-agent \
		--source services/polymarket-agent \
		--project $(PROJECT) --region $(REGION) \
		--allow-unauthenticated \
		--timeout=300 \
		--min-instances=0 \
		--memory=256Mi --cpu=1 \
		--set-env-vars="GOOGLE_CLOUD_PROJECT=$(PROJECT),GROQ_API_KEY=$(GROQ_API_KEY),TAVILY_API_KEY=$(TAVILY_API_KEY),COINGECKO_API_KEY=$(COINGECKO_API_KEY),CLOUD_RUN_TOKEN=$(CLOUD_RUN_TOKEN),TELEGRAM_BOT_URL=$(TELEGRAM_BOT_URL),FIRESTORE_COLLECTION_PREFIX=$(FIRESTORE_COLLECTION_PREFIX)"
	rm -rf services/polymarket-agent/shared/

deploy-bot:
	cp -r shared/ services/telegram-bot/shared/
	gcloud run deploy telegram-bot \
		--source services/telegram-bot \
		--project $(PROJECT) --region $(REGION) \
		--allow-unauthenticated \
		--timeout=60 \
		--min-instances=0 \
		--memory=256Mi --cpu=1 \
		--set-env-vars="GOOGLE_CLOUD_PROJECT=$(PROJECT),TELEGRAM_TOKEN=$(TELEGRAM_TOKEN),TELEGRAM_CHAT_ID=$(TELEGRAM_CHAT_ID),GROQ_API_KEY=$(GROQ_API_KEY),CLOUD_RUN_TOKEN=$(CLOUD_RUN_TOKEN),FIRESTORE_COLLECTION_PREFIX=$(FIRESTORE_COLLECTION_PREFIX)"
	rm -rf services/telegram-bot/shared/

deploy-dashboard:
	cp -r shared/ services/dashboard/shared/
	gcloud run deploy dashboard \
		--source services/dashboard \
		--project $(PROJECT) --region $(REGION) \
		--allow-unauthenticated \
		--timeout=60 \
		--min-instances=0 \
		--memory=512Mi --cpu=1 \
		--set-env-vars="GOOGLE_CLOUD_PROJECT=$(PROJECT),GROQ_API_KEY=$(GROQ_API_KEY),TAVILY_API_KEY=$(TAVILY_API_KEY),DASHBOARD_USER=$(DASHBOARD_USER),DASHBOARD_PASS=$(DASHBOARD_PASS),CLOUD_RUN_TOKEN=$(CLOUD_RUN_TOKEN),FIRESTORE_COLLECTION_PREFIX=$(FIRESTORE_COLLECTION_PREFIX)"
	rm -rf services/dashboard/shared/

# Tras deploy-bot, configurar webhook de Telegram:
set-webhook:
	curl -X POST "https://api.telegram.org/bot$(TELEGRAM_TOKEN)/setWebhook" \
		-d "url=$(shell gcloud run services describe telegram-bot --project=$(PROJECT) --region=$(REGION) --format='value(status.url)')/webhook"

# PRIMER DEPLOY — orden obligatorio para evitar chicken-and-egg con TELEGRAM_BOT_URL:
# 1. make deploy-bot
# 2. Obtener URL: gcloud run services describe telegram-bot --format='value(status.url)' --region=europe-west1
# 3. Exportar: export TELEGRAM_BOT_URL=<url-del-paso-2>
# 4. Anadir TELEGRAM_BOT_URL como GitHub Secret
# 5. make deploy-sports deploy-poly deploy-dashboard
# 6. make set-webhook
#
# Redeploys posteriores ya funcionan porque TELEGRAM_BOT_URL esta en los secrets.
deploy-all: deploy-bot deploy-sports deploy-poly deploy-dashboard

build-frontend:
	cd services/dashboard/frontend && npm install && npm run build
