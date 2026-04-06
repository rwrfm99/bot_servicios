from __future__ import annotations

import logging
import os
import signal
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from html import escape
from pathlib import Path
from typing import Dict, List, Tuple
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


DEFAULT_URLS = [
    "https://allnovu.com",
    "https://mandasaldo.com",
]


def setup_logging(log_path: str = "agente_bot.log") -> None:
    formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)

    logger = logging.getLogger()
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    logger.addHandler(stream_handler)

    log_file = Path(log_path)
    if log_file.exists() and log_file.is_dir():
        log_file = log_file / "agente_bot.log"

    try:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    except OSError as exc:
        logger.warning(
            "No se pudo abrir archivo de log '%s'. Se usara solo consola. Motivo: %s",
            log_file,
            exc,
        )

    logging.getLogger("urllib3.util.retry").setLevel(logging.ERROR)


@dataclass(frozen=True)
class Settings:
    telegram_token: str
    telegram_chat_id: str
    telegram_allowed_chat_ids: List[str]
    urls: List[str]
    interval_seconds: int = 60
    request_timeout_seconds: int = 30
    workers: int = 4
    bridge_url: str = ""
    bridge_key: str = ""
    bridge_source: str = "agente-bot-python"
    bridge_timeout_seconds: int = 5

    @staticmethod
    def from_env() -> "Settings":
        token = os.getenv("TELEGRAM_TOKEN", "").strip()
        chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
        raw_allowed_chat_ids = os.getenv("TELEGRAM_ALLOWED_CHAT_IDS", "").strip()

        raw_urls = os.getenv("MONITOR_URLS", "").strip()
        urls = [u.strip() for u in raw_urls.split(",") if u.strip()] if raw_urls else DEFAULT_URLS
        if raw_allowed_chat_ids:
            allowed_chat_ids = [c.strip() for c in raw_allowed_chat_ids.split(",") if c.strip()]
        else:
            allowed_chat_ids = [chat_id] if chat_id else []

        interval = _read_positive_int("CHECK_INTERVAL_SECONDS", 60)
        timeout = _read_positive_int("REQUEST_TIMEOUT_SECONDS", 30)
        workers = _read_positive_int("MAX_WORKERS", 4)

        bridge_url = os.getenv("BRIDGE_URL", "").strip()
        bridge_key = os.getenv("BRIDGE_KEY", "").strip()
        bridge_source = os.getenv("BRIDGE_SOURCE", "agente-bot-python").strip() or "agente-bot-python"
        bridge_timeout = _read_positive_int("BRIDGE_TIMEOUT_SECONDS", 5)

        if not token or not chat_id:
            raise ValueError(
                "Faltan variables de entorno requeridas: TELEGRAM_TOKEN y TELEGRAM_CHAT_ID."
            )
        if not urls:
            raise ValueError("No hay URLs para monitorear. Define MONITOR_URLS.")

        return Settings(
            telegram_token=token,
            telegram_chat_id=chat_id,
            telegram_allowed_chat_ids=allowed_chat_ids,
            urls=urls,
            interval_seconds=interval,
            request_timeout_seconds=timeout,
            workers=workers,
            bridge_url=bridge_url,
            bridge_key=bridge_key,
            bridge_source=bridge_source,
            bridge_timeout_seconds=bridge_timeout,
        )


def _read_positive_int(env_name: str, default: int) -> int:
    raw = os.getenv(env_name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"Valor invalido para {env_name}: {raw}") from exc
    if value <= 0:
        raise ValueError(f"{env_name} debe ser mayor que 0.")
    return value


class TelegramNotifier:
    def __init__(
        self,
        session: requests.Session,
        token: str,
        chat_id: str,
        timeout: int,
        bridge_url: str = "",
        bridge_key: str = "",
        bridge_source: str = "agente-bot-python",
        bridge_timeout: int = 5,
    ) -> None:
        self._session = session
        self._timeout = timeout
        self._chat_id = chat_id
        self._endpoint = f"https://api.telegram.org/bot{token}/sendMessage"
        self._updates_endpoint = f"https://api.telegram.org/bot{token}/getUpdates"
        self._bridge_url = bridge_url
        self._bridge_key = bridge_key
        self._bridge_source = bridge_source
        self._bridge_timeout = bridge_timeout

    def send(self, message: str) -> None:
        self.send_to_chat(chat_id=self._chat_id, message=message)
        self.send_to_bridge(message=message)

    def send_to_chat(self, chat_id: str, message: str) -> None:
        try:
            response = self._session.post(
                self._endpoint,
                data={
                    "chat_id": chat_id,
                    "text": message,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
                timeout=self._timeout,
            )
            if response.ok:
                logging.info("Notificacion enviada: %s", message)
                return
            logging.error(
                "Error enviando mensaje a Telegram. status=%s body=%s",
                response.status_code,
                response.text,
            )
        except requests.RequestException as exc:
            logging.error("Excepcion enviando mensaje a Telegram: %s", exc)

    def send_to_bridge(self, message: str) -> None:
        if not self._bridge_url or not self._bridge_key:
            return

        try:
            response = self._session.post(
                self._bridge_url,
                json={
                    "text": message,
                    "source": self._bridge_source,
                },
                headers={
                    "x-bridge-key": self._bridge_key,
                    "content-type": "application/json",
                },
                timeout=self._bridge_timeout,
            )
            if response.ok:
                logging.info("Mensaje reenviado al bridge WhatsApp.")
                return
            logging.error(
                "Error enviando al bridge. status=%s body=%s",
                response.status_code,
                response.text,
            )
        except requests.RequestException as exc:
            logging.error("Excepcion enviando al bridge: %s", exc)

    def get_updates(self, offset: int | None = None) -> List[dict]:
        params: Dict[str, int] = {"timeout": 0}
        if offset is not None:
            params["offset"] = offset
        try:
            response = self._session.get(
                self._updates_endpoint,
                params=params,
                timeout=self._timeout,
            )
            if not response.ok:
                logging.error(
                    "Error leyendo updates de Telegram. status=%s body=%s",
                    response.status_code,
                    response.text,
                )
                return []
            payload = response.json()
            if not payload.get("ok"):
                logging.error("Respuesta invalida de Telegram getUpdates: %s", payload)
                return []
            return payload.get("result", [])
        except (ValueError, requests.RequestException) as exc:
            logging.error("Excepcion leyendo updates de Telegram: %s", exc)
            return []


def build_http_session() -> requests.Session:
    retry = Retry(
        total=3,
        connect=2,
        read=2,
        status=2,
        backoff_factor=0.2,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET", "POST"}),
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=20, pool_maxsize=20)

    session = requests.Session()
    session.headers.update({"User-Agent": "agente-bot-monitor/2.0"})
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def check_website(
    session: requests.Session, url: str, timeout: int
) -> Tuple[bool, int | None, int | None, str | None]:
    start = time.monotonic()
    try:
        response = session.get(url, timeout=timeout, allow_redirects=True)
        elapsed_ms = int((time.monotonic() - start) * 1000)
        is_up = 200 <= response.status_code < 400
        return is_up, response.status_code, elapsed_ms, None
    except requests.RequestException as exc:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        return False, None, elapsed_ms, str(exc)


class WebsiteMonitor:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.stop_event = threading.Event()
        self.previous_state: Dict[str, bool] = {}
        self.last_update_id: int | None = None
        self.allowed_chat_ids = set(settings.telegram_allowed_chat_ids)
        self.heartbeat_path = Path("heartbeat.txt")
        self.session = build_http_session()
        self.notifier = TelegramNotifier(
            session=self.session,
            token=settings.telegram_token,
            chat_id=settings.telegram_chat_id,
            timeout=settings.request_timeout_seconds,
            bridge_url=settings.bridge_url,
            bridge_key=settings.bridge_key,
            bridge_source=settings.bridge_source,
            bridge_timeout=settings.bridge_timeout_seconds,
        )

    def stop(self) -> None:
        if not self.stop_event.is_set():
            logging.info("Senal de parada recibida. Cerrando monitor...")
            self.stop_event.set()

    def run(self) -> None:
        logging.info("Monitor iniciado para %s URLs.", len(self.settings.urls))
        logging.info("Lista de URLs: %s", ", ".join(self.settings.urls))
        logging.info("Chats autorizados para comandos: %s", ", ".join(sorted(self.allowed_chat_ids)))

        self._write_heartbeat()
        self._initialize_update_offset()
        self._send_initial_states()

        while not self.stop_event.is_set():
            cycle_start = time.monotonic()
            try:
                self._process_commands()
                self._monitor_once()
            except Exception:
                logging.exception("Error no controlado en el ciclo de monitoreo.")
            finally:
                self._write_heartbeat()

            elapsed = time.monotonic() - cycle_start
            sleep_seconds = max(1, self.settings.interval_seconds - int(elapsed))
            self.stop_event.wait(timeout=sleep_seconds)

        self.session.close()

    def _initialize_update_offset(self) -> None:
        updates = self.notifier.get_updates()
        if not updates:
            return
        update_ids = [u.get("update_id") for u in updates if isinstance(u.get("update_id"), int)]
        if not update_ids:
            return
        self.last_update_id = max(update_ids)
        logging.info("Offset inicial de Telegram establecido en update_id=%s", self.last_update_id)

    def _write_heartbeat(self) -> None:
        try:
            self.heartbeat_path.write_text(str(int(time.time())), encoding="utf-8")
        except Exception as exc:
            logging.warning("No se pudo actualizar heartbeat: %s", exc)

    def _send_initial_states(self) -> None:
        results = self._check_all_urls()
        for url, result in results.items():
            is_up, status_code, latency_ms, error = result
            self.previous_state[url] = is_up
            status_txt = "UP" if is_up else "DOWN"
            detail = f"(status={status_code}, latencia={latency_ms}ms)" if status_code else f"(error={error})"
            self.notifier.send(
                self._build_notification(
                    event="INICIAL",
                    url=url,
                    is_up=is_up,
                    status_code=status_code,
                    latency_ms=latency_ms,
                    error=error,
                )
            )
            logging.info("Estado inicial %s -> %s %s", url, status_txt, detail)

    def _monitor_once(self) -> None:
        results = self._check_all_urls()
        for url, result in results.items():
            current_up, status_code, latency_ms, error = result
            previous_up = self.previous_state.get(url, current_up)

            if previous_up and not current_up:
                self.notifier.send(
                    self._build_notification(
                        event="CAIDA",
                        url=url,
                        is_up=False,
                        status_code=status_code,
                        latency_ms=latency_ms,
                        error=error,
                    )
                )
                logging.warning("Caida detectada en %s", url)
            elif (not previous_up) and current_up:
                self.notifier.send(
                    self._build_notification(
                        event="RECUPERADO",
                        url=url,
                        is_up=True,
                        status_code=status_code,
                        latency_ms=latency_ms,
                        error=None,
                    )
                )
                logging.info("Recuperacion detectada en %s", url)

            self.previous_state[url] = current_up

    def _process_commands(self) -> None:
        updates = self.notifier.get_updates(
            offset=(self.last_update_id + 1) if self.last_update_id is not None else None
        )
        if not updates:
            return

        for update in updates:
            update_id = update.get("update_id")
            if isinstance(update_id, int):
                self.last_update_id = update_id

            message = update.get("message") or {}
            text = (message.get("text") or "").strip()
            chat = message.get("chat") or {}
            chat_id_raw = chat.get("id")
            chat_id = str(chat_id_raw) if chat_id_raw is not None else ""

            if not text:
                continue
            if text.split()[0].lower() == "/estado":
                logging.info("Comando /estado recibido desde chat %s", chat_id)
                self.notifier.send_to_chat(chat_id=chat_id, message=self._build_status_report())
                continue
            if chat_id not in self.allowed_chat_ids:
                logging.info("Comando ignorado desde chat no autorizado: %s", chat_id)
                continue

    def _build_status_report(self) -> str:
        results = self._check_all_urls()
        checked_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        lines = [f"📊 <b>Estado actual de servicios</b>", f"🕒 <b>Hora:</b> <code>{checked_at}</code>"]
        up_count = 0
        down_count = 0

        for url in self.settings.urls:
            is_up, status_code, latency_ms, error = results.get(url, (False, None, None, "sin resultado"))
            if is_up:
                up_count += 1
            else:
                down_count += 1
            host = urlparse(url).netloc or url
            status_visual = "🟢 <b>UP</b>" if is_up else "🔴 <b>DOWN</b>"
            http_info = str(status_code) if status_code is not None else "N/A"
            latency_info = f"{latency_ms} ms" if latency_ms is not None else "N/A"
            line = (
                f"\n\n🌐 <b>{escape(host)}</b>\n"
                f"🔗 {escape(url)}\n"
                f"📶 {status_visual}\n"
                f"🧾 HTTP: <code>{http_info}</code>\n"
                f"⏱️ Latencia: <code>{latency_info}</code>"
            )
            if error:
                line += f"\n⚠️ Error: <code>{escape(error)}</code>"
            lines.append(line)

        lines.append(
            f"\n\n📌 <b>Resumen:</b>\n"
            f"🟢 Activos: <b>{up_count}</b>\n"
            f"🔴 Caidos: <b>{down_count}</b>\n"
            f"🧮 Total: <b>{up_count + down_count}</b>"
        )

        return "".join(lines)

    def _check_all_urls(self) -> Dict[str, Tuple[bool, int | None, int | None, str | None]]:
        results: Dict[str, Tuple[bool, int | None, int | None, str | None]] = {}

        with ThreadPoolExecutor(max_workers=min(self.settings.workers, len(self.settings.urls))) as executor:
            future_to_url = {
                executor.submit(check_website, self.session, url, self.settings.request_timeout_seconds): url
                for url in self.settings.urls
            }
            for future in as_completed(future_to_url):
                url = future_to_url[future]
                try:
                    results[url] = future.result()
                except Exception as exc:
                    logging.exception("Error revisando URL %s: %s", url, exc)
                    results[url] = (False, None, None, str(exc))

        return results

    def _build_notification(
        self,
        event: str,
        url: str,
        is_up: bool,
        status_code: int | None,
        latency_ms: int | None,
        error: str | None,
    ) -> str:
        event_map = {
            "INICIAL": "🔎 <b>Estado Inicial</b>",
            "CAIDA": "🚨 <b>Servicio Caido</b>",
            "RECUPERADO": "✅ <b>Servicio Recuperado</b>",
        }
        status_visual = "🟢 <b>UP</b>" if is_up else "🔴 <b>DOWN</b>"
        host = urlparse(url).netloc or url
        checked_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        http_info = str(status_code) if status_code is not None else "N/A"
        latency_info = f"{latency_ms} ms" if latency_ms is not None else "N/A"
        error_line = f"\n⚠️ <b>Error:</b> <code>{escape(error)}</code>" if error else ""

        return (
            f"{event_map.get(event, '🔔 <b>Notificacion</b>')}\n"
            f"🌐 <b>Sitio:</b> <code>{escape(host)}</code>\n"
            f"🔗 <b>URL:</b> {escape(url)}\n"
            f"📶 <b>Estado:</b> {status_visual}\n"
            f"🧾 <b>HTTP:</b> <code>{http_info}</code>\n"
            f"⏱️ <b>Latencia:</b> <code>{latency_info}</code>\n"
            f"🕒 <b>Hora:</b> <code>{checked_at}</code>"
            f"{error_line}"
        )


def main() -> None:
    env_path = Path(__file__).resolve().parent / ".env"
    load_dotenv(dotenv_path=env_path, override=False, encoding="utf-8-sig")
    setup_logging()

    try:
        settings = Settings.from_env()
    except ValueError as exc:
        logging.error("Configuracion invalida: %s", exc)
        raise SystemExit(1) from exc

    monitor = WebsiteMonitor(settings)
    signal.signal(signal.SIGINT, lambda *_: monitor.stop())
    signal.signal(signal.SIGTERM, lambda *_: monitor.stop())

    monitor.run()


if __name__ == "__main__":
    main()
