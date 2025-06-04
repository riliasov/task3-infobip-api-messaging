# Constants
DEFAULT_POOL_SIZE = 5
DEFAULT_MYSQL_PORT = 3306
DEFAULT_DELAY = 1.0
MAX_RETRY_WAIT = 60
REQUEST_TIMEOUT = 30

import mysql.connector
import requests
import json
import os
import sys
import time
import logging
from datetime import datetime
from dotenv import load_dotenv
from mysql.connector import pooling
import argparse
import phonenumbers

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler('whatsapp_sender.log')
    ]
)

def load_environment():
    load_dotenv()
    required_vars = [
        'INFOBIP_API_URL', 'INFOBIP_API_KEY', 'INFOBIP_SENDER',
        'MYSQL_HOST', 'MYSQL_PORT', 'MYSQL_USER', 
        'MYSQL_PASSWORD', 'MYSQL_DATABASE'
    ]
    missing = [v for v in required_vars if not os.getenv(v)]
    if missing:
        logging.error(f"Missing env variables: {', '.join(missing)}")
        sys.exit(1)
    return {
        'infobip': {
            'api_url': os.getenv('INFOBIP_API_URL'),
            'api_key': os.getenv('INFOBIP_API_KEY'),
            'sender': os.getenv('INFOBIP_SENDER')
        },
        'db': {
            'host': os.getenv('MYSQL_HOST'),
            'port': int(os.getenv('MYSQL_PORT', 3306)),
            'user': os.getenv('MYSQL_USER'),
            'password': os.getenv('MYSQL_PASSWORD'),
            'database': os.getenv('MYSQL_DATABASE'),
            'pool_name': 'whatsapp_pool',
            'pool_size': int(os.getenv('MYSQL_POOL_SIZE', 5))
        },
    }

def create_connection_pool(cfg):
    try:
        return pooling.MySQLConnectionPool(
        pool_name=cfg['pool_name'],
        pool_size=cfg['pool_size'],
        **{k: v for k, v in cfg.items() if k not in ['pool_name', 'pool_size']}
    )
    except mysql.connector.Error as e:
        logging.error(f"Database connection failed: {e}")
        sys.exit(1)

def create_message_table(pool):
    with pool.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
            CREATE TABLE IF NOT EXISTS message (
                id INT AUTO_INCREMENT PRIMARY KEY,
                user_id INT NOT NULL,
                user_phone VARCHAR(20) NOT NULL,
                message TEXT NOT NULL,
                status ENUM('success', 'error', 'pending', 'dry-run') DEFAULT 'pending',
                response TEXT,
                api_message_id VARCHAR(255),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES user(id) ON DELETE CASCADE
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
        """)
        conn.commit()

def validate_phone(phone):
    try:
        number = phonenumbers.parse(phone, None)
        if phonenumbers.is_valid_number(number):
            return phonenumbers.format_number(number, phonenumbers.PhoneNumberFormat.E164)
        return None
    except phonenumbers.NumberParseException:
        return None

def fetch_users(pool):
    with pool.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, phone FROM user WHERE age >= 30 AND phone IS NOT NULL AND phone != ''")
            raw_users = cur.fetchall()
    
    valid_users = []
    for user_id, phone in raw_users:
        formatted_phone = validate_phone(phone)
        if formatted_phone:
            valid_users.append((user_id, formatted_phone))
        else:
            logging.debug(f"Invalid phone format for user {user_id}: {phone}")
    
    logging.info(f"Found {len(valid_users)} valid users out of {len(raw_users)}")
    return valid_users

def log_result(pool, user_id, phone, msg, status, response, msg_id=None):
    try:
        with pool.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO message (user_id, user_phone, message, status, response, api_message_id)
                    VALUES (%s, %s, %s, %s, %s, %s)
                """, (user_id, phone, msg, status, response, msg_id))
                conn.commit()
    except Exception as e:
        logging.error(f"Log failed for {user_id}/{phone}: {e}")

def send_message(user_id, phone, cfg, pool, msg=None, dry_run=False, max_retries=3, base_delay=1):
    msg = msg or os.getenv('DEFAULT_MESSAGE', 'Fallback message')
    if dry_run:
        logging.info(f"[DRY RUN] Would send to {phone} (user {user_id})")
        log_result(pool, user_id, phone, msg, 'dry-run', 'Simulated dry run response', 'dry-run-id')
        return True, "Dry run"

    headers = {
        'Authorization': f"App {cfg['api_key']}",
        'Content-Type': 'application/json'
    }
    payload = {
        "from": cfg['sender'],
        "to": phone,
        "content": {"text": msg}
    }
    
    for attempt in range(max_retries):
        try:
            r = requests.post(f"{cfg['api_url']}/whatsapp/1/message/text", 
                            headers=headers, json=payload, timeout=30)
            
            if r.status_code == 200:
                data = r.json()
                info = data.get('messages', [{}])[0]
                status = info.get('status', {})
                msg_id = info.get('messageId')
                if status.get('groupId') == 1:
                    log_result(pool, user_id, phone, msg, 'success', r.text, msg_id)
                    return True, "Sent"
                else:
                    desc = status.get('description', 'Rejected')
                    log_result(pool, user_id, phone, msg, 'error', r.text, msg_id)
                    return False, f"Rejected: {desc}"
            
            elif r.status_code == 429:
                if 'Retry-After' in r.headers:
                    wait = min(int(r.headers['Retry-After']), 60)
                    if attempt < max_retries - 1:  # Не ждем на последней попытке
                        logging.warning(f"Rate limited. Waiting {wait}s... (attempt {attempt+1}/{max_retries})")
                        time.sleep(wait)
                        continue
                # Если последняя попытка или нет Retry-After
                log_result(pool, user_id, phone, msg, 'error', r.text)
                return False, "Rate limit exceeded"
            
            else:
                reason_map = {
                    400: "Bad Request - ошибка в формате запроса",
                    401: "Unauthorized - неверный API ключ",
                    403: "Forbidden - недостаточно прав",
                    404: "Not Found - неверный endpoint",
                    500: "Internal Server Error",
                    503: "Service Unavailable"
                }
                reason = reason_map.get(r.status_code, f"HTTP {r.status_code}")
                log_result(pool, user_id, phone, msg, 'error', r.text)
                return False, reason
                
        except requests.exceptions.RequestException as e:
            logging.error(f"Attempt {attempt + 1}/{max_retries} failed: {e}")
            if attempt < max_retries - 1:
                delay = base_delay * (2 ** attempt)
                time.sleep(delay)
            else:
                log_result(pool, user_id, phone, msg, 'error', str(e))
                return False, str(e)
    
    return False, "Max retries exceeded"

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--dry-run', action='store_true', help='Simulate sending messages')
    parser.add_argument('--delay', type=float, default=1.0, help='Delay between messages in seconds')
    args = parser.parse_args()

    cfg = load_environment()
    pool = create_connection_pool(cfg['db'])
    create_message_table(pool)
    users = fetch_users(pool)

    if not users:
        logging.info("No valid users found.")
        return

    success, fail = 0, 0
    for i, (uid, phone) in enumerate(users, 1):
        logging.info(f"[{i}/{len(users)}] Sending to {phone} (user {uid})")
        ok, msg = send_message(uid, phone, cfg['infobip'], pool, dry_run=args.dry_run)
        if ok:
            logging.info(f"✓ Success: {phone}")
            success += 1
        else:
            logging.error(f"✗ Failed: {phone} - {msg}")
            fail += 1
        time.sleep(args.delay)

    logging.info("=" * 50)
    logging.info(f"Done: {success} success, {fail} fail out of {len(users)}")

if __name__ == "__main__":
    main()