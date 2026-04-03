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

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(formatter)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)

    logger = logging.getLogger()
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    logger.addHandler(file_handler)
    logger.addHandler(stream_handler)
    logging.getLogger("urllib3.util.retry").setLevel(logging.ERROR)


@dataclass(frozen=True)
class Settings:
    telegram_token: str
    telegram_chat_id: str
    urls: List[str]
    interval_seconds: int = 60
    request_timeout_seconds: int = 10
    workers: int = 4

    @staticmethod
    def from_env() -> "Settings":
        token = os.getenv("TELEGRAM_TOKEN", "").strip()
        chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()

        raw_urls = os.getenv("MONITOR_URLS", "").strip()
        urls = [u.strip() for u in raw_urls.split(",") if u.strip()] if raw_urls else DEFAULT_URLS

        interval = _read_positive_int("CHECK_INTERVAL_SECONDS", 60)
        timeout = _read_positive_int("REQUEST_TIMEOUT_SECONDS", 10)
        workers = _read_positive_int("MAX_WORKERS", 4)

        if not token or not chat_id:
            raise ValueError(
                "Faltan variables de entorno requeridas: TELEGRAM_TOKEN y TELEGRAM_CHAT_ID."
            )
        if not urls:
            raise ValueError("No hay URLs para monitorear. Define MONITOR_URLS.")

        return Settings(
            telegram_token=token,
            telegram_chat_id=chat_id,
            urls=urls,
            interval_seconds=interval,
            request_timeout_seconds=timeout,
            workers=workers,
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
    def __init__(self, session: requests.Session, token: str, chat_id: str, timeout: int) -> None:
        self._session = session
        self._timeout = timeout
        self._chat_id = chat_id
        self._endpoint = f"https://api.telegram.org/bot{token}/sendMessage"

    def send(self, message: str) -> None:
        try:
            response = self._session.post(
                self._endpoint,
                data={
                    "chat_id": self._chat_id,
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


def build_http_session() -> requests.Session:
    retry = Retry(
        total=2,
        connect=0,
        read=0,
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
        self.heartbeat_path = Path("heartbeat.txt")
        self.session = build_http_session()
        self.notifier = TelegramNotifier(
            session=self.session,
            token=settings.telegram_token,
            chat_id=settings.telegram_chat_id,
            timeout=settings.request_timeout_seconds,
        )

    def stop(self) -> None:
        if not self.stop_event.is_set():
            logging.info("Senal de parada recibida. Cerrando monitor...")
            self.stop_event.set()

    def run(self) -> None:
        logging.info("Monitor iniciado para %s URLs.", len(self.settings.urls))
        logging.info("Lista de URLs: %s", ", ".join(self.settings.urls))

        self._write_heartbeat()
        self._send_initial_states()

        while not self.stop_event.is_set():
            cycle_start = time.monotonic()
            try:
                self._monitor_once()
            except Exception:
                logging.exception("Error no controlado en el ciclo de monitoreo.")
            finally:
                self._write_heartbeat()

            elapsed = time.monotonic() - cycle_start
            sleep_seconds = max(1, self.settings.interval_seconds - int(elapsed))
            self.stop_event.wait(timeout=sleep_seconds)

        self.session.close()

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
