import json
import logging
import os
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo


BASE_DIR = Path(__file__).resolve().parent
DATA_FILE = BASE_DIR / "agendamentos.json"


def load_dotenv():
    env_file = BASE_DIR / ".env"
    if not env_file.exists():
        return
    for raw_line in env_file.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


load_dotenv()
TOKEN = os.environ.get("BOT_TOKEN", "").strip()
TIMEZONE_NAME = os.environ.get("BOT_TIMEZONE", "America/Sao_Paulo").strip()
try:
    TIMEZONE = ZoneInfo(TIMEZONE_NAME)
except Exception:
    raise SystemExit(f"Fuso horário inválido em BOT_TIMEZONE: {TIMEZONE_NAME}")

try:
    ADMIN_IDS = {int(value.strip()) for value in os.environ.get("ADMIN_USER_IDS", "").split(",") if value.strip()}
except ValueError as exc:
    raise SystemExit("ADMIN_USER_IDS deve conter apenas IDs numéricos separados por vírgula") from exc

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("telegram-scheduler")
lock = threading.Lock()


class HealthHandler(BaseHTTPRequestHandler):
    """Small HTTP endpoint so Render can monitor the running web service."""

    def do_GET(self):
        if self.path not in {"/", "/health"}:
            self.send_error(404)
            return
        body = b"ok\n"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        return


def start_health_server():
    try:
        port = int(os.environ.get("PORT", "10000"))
    except ValueError as exc:
        raise SystemExit("PORT deve ser um número inteiro") from exc

    server = ThreadingHTTPServer(("0.0.0.0", port), HealthHandler)
    threading.Thread(
        target=server.serve_forever,
        name="health-server",
        daemon=True,
    ).start()
    log.info("Endpoint de saúde ouvindo em 0.0.0.0:%s", port)
    return server


def api(method, data=None):
    if not TOKEN:
        raise SystemExit("Defina BOT_TOKEN antes de iniciar o bot")
    url = f"https://api.telegram.org/bot{TOKEN}/{method}"
    encoded = urllib.parse.urlencode(data or {}).encode("utf-8")
    request = urllib.request.Request(url, data=encoded, method="POST")
    with urllib.request.urlopen(request, timeout=45) as response:
        result = json.loads(response.read().decode("utf-8"))
    if not result.get("ok"):
        raise RuntimeError(result.get("description", "Erro desconhecido na API do Telegram"))
    return result["result"]


def load_items():
    if not DATA_FILE.exists():
        return []
    with lock:
        return json.loads(DATA_FILE.read_text(encoding="utf-8"))


def save_items(items):
    with lock:
        temporary = DATA_FILE.with_suffix(".tmp")
        temporary.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(DATA_FILE)


def is_admin(user_id):
    return user_id in ADMIN_IDS


def help_text():
    return (
        "Comandos disponíveis:\n\n"
        "/id — mostra o ID deste grupo\n"
        "/agendar CHAT_ID | AAAA-MM-DD HH:MM | texto\n"
        "/agendar_foto CHAT_ID | AAAA-MM-DD HH:MM | URL_DA_FOTO | legenda\n"
        "/agendar_video CHAT_ID | AAAA-MM-DD HH:MM | URL_DO_VIDEO | legenda\n"
        "/agendar_link CHAT_ID | AAAA-MM-DD HH:MM | URL | texto opcional\n"
        "/listar — lista os agendamentos\n"
        "/cancelar ID — cancela um agendamento\n\n"
        f"Horário atual do bot: {datetime.now(TIMEZONE):%d/%m/%Y %H:%M} ({TIMEZONE_NAME})"
    )


def parse_parts(text, expected_minimum):
    parts = [part.strip() for part in text.split("|")]
    if len(parts) < expected_minimum:
        raise ValueError("Use o caractere | para separar os campos")
    chat_id = parts[0]
    when = datetime.strptime(parts[1], "%Y-%m-%d %H:%M").replace(tzinfo=TIMEZONE)
    return chat_id, when, parts[2:]


def create_item(command, payload, from_user):
    if not is_admin(from_user):
        return "Você não tem permissão para agendar mensagens."
    try:
        if command == "agendar":
            chat_id, when, fields = parse_parts(payload, 3)
            if not fields[0]:
                raise ValueError("O texto não pode ficar vazio")
            item = {"type": "text", "chat_id": chat_id, "when": when.isoformat(), "text": fields[0]}
        elif command in {"agendar_foto", "agendar_video"}:
            chat_id, when, fields = parse_parts(payload, 4)
            if not fields[0].startswith(("http://", "https://")):
                raise ValueError("A mídia precisa ser uma URL http ou https")
            item = {
                "type": "photo" if command == "agendar_foto" else "video",
                "chat_id": chat_id,
                "when": when.isoformat(),
                "media": fields[0],
                "caption": fields[1] if len(fields) > 1 else "",
            }
        else:
            chat_id, when, fields = parse_parts(payload, 3)
            if not fields[0].startswith(("http://", "https://")):
                raise ValueError("O link precisa começar com http:// ou https://")
            label = fields[1] if len(fields) > 1 and fields[1] else fields[0]
            item = {
                "type": "link",
                "chat_id": chat_id,
                "when": when.isoformat(),
                "url": fields[0],
                "text": label,
            }
    except (ValueError, IndexError) as exc:
        return f"Formato inválido: {exc}\n\nEnvie /ajuda para ver exemplos."

    items = load_items()
    item["id"] = max((entry.get("id", 0) for entry in items), default=0) + 1
    item["created_by"] = from_user
    item["sent"] = False
    items.append(item)
    save_items(items)
    return f"Agendamento #{item['id']} criado para {when:%d/%m/%Y %H:%M} ({TIMEZONE_NAME})."


def send_item(item):
    kind = item["type"]
    common = {"chat_id": item["chat_id"]}
    if kind == "text":
        api("sendMessage", {**common, "text": item["text"]})
    elif kind == "photo":
        api("sendPhoto", {**common, "photo": item["media"], "caption": item.get("caption", "")})
    elif kind == "video":
        api("sendVideo", {**common, "video": item["media"], "caption": item.get("caption", "")})
    elif kind == "link":
        api("sendMessage", {**common, "text": f"{item['text']}\n{item['url']}"})


def scheduler_loop():
    while True:
        now = datetime.now(TIMEZONE)
        items = load_items()
        changed = False
        for item in items:
            if item.get("sent"):
                continue
            due = datetime.fromisoformat(item["when"])
            if due > now:
                continue
            try:
                send_item(item)
                item["sent"] = True
                item["sent_at"] = now.isoformat()
                changed = True
                log.info("Agendamento #%s enviado", item["id"])
            except Exception as exc:
                item["error"] = str(exc)
                item["attempts"] = item.get("attempts", 0) + 1
                item["when"] = (now.timestamp() + 300)
                item["when"] = datetime.fromtimestamp(item["when"], TIMEZONE).isoformat()
                changed = True
                log.exception("Falha no agendamento #%s; nova tentativa em 5 minutos", item["id"])
        if changed:
            save_items(items)
        time.sleep(10)


def process_update(update):
    message = update.get("message") or {}
    text = message.get("text", "")
    user = message.get("from") or {}
    user_id = user.get("id")
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    if not text or user_id is None or chat_id is None:
        return
    command, _, payload = text.partition(" ")
    command = command.split("@", 1)[0].lower()
    if command in {"/start", "/ajuda", "/help"}:
        api("sendMessage", {"chat_id": chat_id, "text": help_text()})
    elif command == "/id":
        api("sendMessage", {"chat_id": chat_id, "text": f"ID deste chat: {chat_id}"})
    elif command in {"/agendar", "/agendar_foto", "/agendar_video", "/agendar_link"}:
        reply = create_item(command[1:], payload, user_id)
        api("sendMessage", {"chat_id": chat_id, "text": reply})
    elif command == "/listar":
        if not is_admin(user_id):
            return
        pending = [item for item in load_items() if not item.get("sent")]
        if not pending:
            reply = "Não há agendamentos pendentes."
        else:
            pending.sort(key=lambda item: item["when"])
            reply = "\n".join(
                f"#{item['id']} — {item['type']} — {datetime.fromisoformat(item['when']):%d/%m/%Y %H:%M} — chat {item['chat_id']}"
                for item in pending
            )
        api("sendMessage", {"chat_id": chat_id, "text": reply})
    elif command == "/cancelar":
        if not is_admin(user_id):
            return
        try:
            item_id = int(payload.strip())
        except ValueError:
            api("sendMessage", {"chat_id": chat_id, "text": "Use: /cancelar ID"})
            return
        items = load_items()
        found = next((item for item in items if item.get("id") == item_id and not item.get("sent")), None)
        if not found:
            reply = f"Agendamento #{item_id} não encontrado ou já enviado."
        else:
            found["cancelled"] = True
            found["sent"] = True
            save_items(items)
            reply = f"Agendamento #{item_id} cancelado."
        api("sendMessage", {"chat_id": chat_id, "text": reply})


def main():
    if not TOKEN:
        raise SystemExit("Defina BOT_TOKEN no ambiente. Consulte .env.example.")
    if not ADMIN_IDS:
        raise SystemExit("Defina ADMIN_USER_IDS com seu ID numérico do Telegram.")
    start_health_server()
    me = api("getMe")
    log.info("Bot @%s iniciado", me.get("username"))
    threading.Thread(target=scheduler_loop, daemon=True).start()
    offset = None
    while True:
        try:
            params = {"timeout": 50, "allowed_updates": json.dumps(["message"])}
            if offset is not None:
                params["offset"] = offset
            for update in api("getUpdates", params):
                offset = update["update_id"] + 1
                try:
                    process_update(update)
                except Exception:
                    log.exception("Erro ao processar atualização")
        except Exception:
            log.exception("Falha na conexão; tentando novamente em 5 segundos")
            time.sleep(5)


if __name__ == "__main__":
    main()
