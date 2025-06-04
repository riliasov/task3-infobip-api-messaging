# WhatsApp Bulk Sender через Infobip

Скрипт для отправки массовых WhatsApp-сообщений через Infobip API на основе данных из MySQL.

## Требования

- Python 3.8+
- Аккаунт Infobip с активным WhatsApp каналом
- Настроенная база данных MySQL с таблицей `user` (поля `id`, `phone`, `age`)

## 🔐 Настройка .env
INFOBIP_API_URL=https://your-infobip-url.com
INFOBIP_API_KEY=your_api_key
INFOBIP_SENDER=your_whatsapp_number

MYSQL_HOST=localhost
MYSQL_PORT=3306
MYSQL_USER=your_user
MYSQL_PASSWORD=your_password
MYSQL_DATABASE=your_database

DEFAULT_MESSAGE=Привет! Это ваше маркетинговое сообщение.

## 📊 Логирование

Все результаты записываются в таблицу message
Подробный лог сохраняется в whatsapp_sender.log

##⚠️ Возможности:

Проверка номеров с помощью библиотеки phonenumbers
Повторные попытки при сетевых сбоях с экспоненциальной задержкой
Резервная защита от лимитов API (429)
