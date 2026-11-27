import socket
import threading
import os
import json
import hashlib
from pathlib import Path
import logging
import struct
import sys
import ssl
import shutil
import hmac
import time
import tempfile
import math

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')


class FileServer:
    def __init__(self, host='0.0.0.0', port=6666, upload_dir='server_files', config_path='server_config.json'):
        self.host = host
        self.port = port
        self.upload_dir = Path(upload_dir)
        self.upload_dir.mkdir(exist_ok=True)
        self.clients = {}
        self.lock = threading.Lock()
        self.server = None
        self.max_file_size = 2 * 1024 * 1024 * 1024
        self.chunk_size = 65536
        self.timeout = 120
        self.can_clients_delete_files = True
        self.auth_token = None
        self.tls_enabled = False
        self.cert_file = None
        self.key_file = None
        self.ssl_context = None
        self.max_json_size = 1024 * 1024
        self.auth_rate_limit_enabled = True
        self.auth_max_attempts = 5
        self.auth_window = 300
        self.auth_lockout = 900
        self.auth_failure_delay = 0.3
        self.auth_global_max_attempts = 50
        self.auth_global_window = 300
        self.auth_state = {}
        self.auth_global_failures = []
        self.auth_global_locked_until = 0.0
        self.auth_lock = threading.Lock()
        self.auth_last_cleanup = 0.0
        self.temp_dir = self.upload_dir / '.tmp'
        self.temp_dir.mkdir(exist_ok=True)
        self.ping_rate_limit_enabled = True
        self.ping_rate_limit_max = 20
        self.ping_rate_limit_window = 10.0
        self.ping_min_interval = 0.1
        self.ping_state = {}
        self.ping_lock = threading.Lock()
        self.ping_last_cleanup = 0.0
        self.progress_log_value = 10
        self.progress_log_unit = 'mb'
        self.clients_limit = 100
        self.clients_limit_enabled = False
        self.client_count = 0
        self.clients_lock = threading.Lock()

        if config_path:
            self.load_config(config_path)
            if self.tls_enabled:
                self.setup_tls()

    def resource_path(self, relative_path):
        try:
            base_path = Path(sys._MEIPASS)
        except AttributeError:
            base_path = Path(__file__).parent
        return base_path / relative_path

    def load_config(self, config_path):
        try:
            config_file = Path(config_path)
            if not config_file.is_file():
                config_file = self.resource_path(config_path)
                if not config_file.is_file():
                    logging.warning(f"Файл конфигурации {config_path} не найден. Используются значения по умолчанию.")
                    return

            with open(config_file, 'r', encoding='utf-8') as f:
                config = json.load(f)

            if 'host' in config:
                new_host = config['host']
                if isinstance(new_host, str) and new_host.strip() != "":
                    self.host = new_host
                else:
                    logging.warning(f"Некорректный хост в конфиге: {new_host}. Используется значение {self.host}")

            if 'port' in config:
                new_port = config['port']
                if isinstance(new_port, int) and 1 <= new_port <= 65535:
                    self.port = new_port
                else:
                    logging.warning(f"Некорректный порт в конфиге: {new_port}. Используется значение {self.port}")

            if 'upload_dir' in config:
                base_dir = Path.cwd()
                try:
                    configured_path = Path(config['upload_dir'])
                    if not configured_path.is_absolute():
                        configured_path = base_dir / configured_path
                    resolved_path = configured_path.resolve()
                    if base_dir.resolve() in resolved_path.parents or resolved_path == base_dir.resolve():
                        self.upload_dir = resolved_path
                        self.upload_dir.mkdir(exist_ok=True)
                    else:
                        logging.error(f"Ошибка при настройке загрузочной папки. Используется {self.upload_dir}")
                except Exception:
                    logging.error(f"Ошибка при настройке загрузочной папки. Используется {self.upload_dir}")

            if 'max_file_size' in config:
                new_max_size = config['max_file_size']
                if isinstance(new_max_size, int) and new_max_size > 0:
                    self.max_file_size = new_max_size
                else:
                    logging.warning(f"Некорректный max_file_size в конфиге: {new_max_size}. Используется значение {self.max_file_size}")

            if 'chunk_size' in config:
                new_chunk = config['chunk_size']
                if isinstance(new_chunk, int) and new_chunk > 0:
                    self.chunk_size = new_chunk
                else:
                    logging.warning(f"Некорректный chunk_size в конфиге: {new_chunk}. Используется значение {self.chunk_size}")

            if 'timeout' in config:
                new_timeout = config['timeout']
                if isinstance(new_timeout, int) and new_timeout > 0:
                    self.timeout = new_timeout
                else:
                    logging.warning(f"Некорректный timeout в конфиге: {new_timeout}. Используется значение {self.timeout}")

            if 'can_clients_delete_files' in config:
                can_clients_delete_files = config['can_clients_delete_files']
                if isinstance(can_clients_delete_files, bool):
                    self.can_clients_delete_files = can_clients_delete_files
                else:
                    logging.warning(f"Некорректный can_clients_delete_files в конфиге: {can_clients_delete_files}. Используется значение {self.can_clients_delete_files}")

            if 'auth_token' in config:
                auth_token = config['auth_token']
                if isinstance(auth_token, str):
                    stripped = auth_token.strip()
                    self.auth_token = stripped if stripped else None
                else:
                    logging.warning(f"Некорректный auth_token в конфиге: {auth_token}. Аутентификация для клиентов не требуется")

            if 'tls_enabled' in config:
                tls_enabled = config['tls_enabled']
                if isinstance(tls_enabled, bool):
                    self.tls_enabled = tls_enabled
                else:
                    logging.warning(f"Некорректный tls_enabled в конфиге: {tls_enabled}. Используется значение {self.tls_enabled}")

            if 'cert_file' in config:
                if self.tls_enabled:
                    cert_file = config['cert_file']
                    if isinstance(cert_file, str):
                        self.cert_file = cert_file
                    else:
                        logging.warning(f"Некорректный cert_file в конфиге: {cert_file}. Используется значение {self.cert_file}")

            if 'key_file' in config:
                if self.tls_enabled:
                    key_file = config['key_file']
                    if isinstance(key_file, str):
                        self.key_file = key_file
                    else:
                        logging.warning(f"Некорректный key_file в конфиге: {key_file}. Используется значение {self.key_file}")

            if 'max_json_size' in config:
                max_json_size = config['max_json_size']
                if isinstance(max_json_size, int) and 1024 <= max_json_size <= 64 * 1024 * 1024:
                    self.max_json_size = max_json_size
                else:
                    logging.warning(f"Некорректный max_json_size в конфиге: {max_json_size}. Используется значение {self.max_json_size}")

            if 'auth_rate_limit_enabled' in config:
                auth_rate_limit_enabled = config['auth_rate_limit_enabled']
                if isinstance(auth_rate_limit_enabled, bool):
                    self.auth_rate_limit_enabled = auth_rate_limit_enabled
                else:
                    logging.warning(f"Некорректный auth_rate_limit_enabled в конфиге: {auth_rate_limit_enabled}. Используется значение {self.auth_rate_limit_enabled}")

            if 'ping_rate_limit_enabled' in config:
                ping_rate_limit_enabled = config['ping_rate_limit_enabled']
                if isinstance(ping_rate_limit_enabled, bool):
                    self.ping_rate_limit_enabled = ping_rate_limit_enabled
                else:
                    logging.warning(f"Некорректный ping_rate_limit_enabled в конфиге: {ping_rate_limit_enabled}. Используется значение {self.ping_rate_limit_enabled}")

            if 'progress_log_unit' in config:
                progress_log_unit = config['progress_log_unit']
                if isinstance(progress_log_unit, str) and progress_log_unit.strip().lower() in ('mb', 'мб', 'percent', '%'):
                    normalized = progress_log_unit.strip().lower()
                    self.progress_log_unit = 'percent' if normalized in ('percent', '%') else 'mb'
                else:
                    logging.warning(f"Некорректный progress_log_unit в конфиге: {progress_log_unit}. Используется значение {self.progress_log_unit}")

            if 'progress_log_value' in config:
                progress_log_value = config['progress_log_value']
                if isinstance(progress_log_value, (int, float)) and progress_log_value > 0:
                    if self.progress_log_unit == 'percent' and progress_log_value > 100:
                        self.progress_log_value = 20
                        logging.warning(f"Некорректный progress_log_value в конфиге: {progress_log_value}. Используется значение 20")
                    else:
                        self.progress_log_value = progress_log_value
                else:
                    if self.progress_log_unit == 'percent':
                        self.progress_log_value = 20
                        logging.warning(f"Некорректный progress_log_value в конфиге: {progress_log_value}. Используется значение 20")
                    else:
                        logging.warning(f"Некорректный progress_log_value в конфиге: {progress_log_value}. Используется значение {self.progress_log_value}")
                if self.progress_log_unit == 'mb' and self.progress_log_value < 1:
                    self.progress_log_value = 1
                    logging.warning(f"Некорректный progress_log_value в конфиге: {progress_log_value}. Используется значение 1")
                if self.progress_log_unit == 'percent' and self.progress_log_value < 0.0001:
                    self.progress_log_value = 0.0001
                    logging.warning(f"Некорректный progress_log_value в конфиге: {progress_log_value}. Используется значение 0.0001")

            if 'clients_limit_enabled' in config:
                clients_limit_enabled = config['clients_limit_enabled']
                if isinstance(clients_limit_enabled, bool):
                    self.clients_limit_enabled = clients_limit_enabled
                else:
                    logging.warning(f"Некорректный clients_limit_enabled в конфиге: {clients_limit_enabled}. Используется значение {self.clients_limit_enabled}")

            if 'clients_limit' in config:
                clients_limit = config['clients_limit']
                if isinstance(clients_limit, int) and clients_limit > 0:
                    self.clients_limit = clients_limit
                else:
                    logging.warning(f"Некорректный clients_limit в конфиге: {clients_limit}. Используется значение {self.clients_limit}")

            def _pos_int(key, default):
                v = config.get(key)
                if isinstance(v, int) and v > 0:
                    return v
                if key in config:
                    logging.warning(f"Некорректный {key}: {v}. Используется значение {default}")
                return default

            self.auth_max_attempts = _pos_int('auth_rate_limit_max_attempts', self.auth_max_attempts)
            self.auth_window = _pos_int('auth_rate_limit_window', self.auth_window)
            self.auth_lockout = _pos_int('auth_rate_limit_lockout', self.auth_lockout)
            self.auth_global_max_attempts = _pos_int('auth_rate_limit_global_max_attempts', self.auth_global_max_attempts)
            self.auth_global_window = _pos_int('auth_rate_limit_global_window', self.auth_global_window)

            auth_rate_limit_failure_delay = config.get('auth_rate_limit_failure_delay')
            if isinstance(auth_rate_limit_failure_delay, (int, float)) and 0 <= auth_rate_limit_failure_delay <= 10:
                self.auth_failure_delay = float(auth_rate_limit_failure_delay)
            elif 'auth_rate_limit_failure_delay' in config:
                logging.warning(f"Некорректный auth_rate_limit_failure_delay в конфиге: {auth_rate_limit_failure_delay}. Используется значение {self.auth_failure_delay}")

            self.ping_rate_limit_max = _pos_int('ping_rate_limit_max', self.ping_rate_limit_max)
            self.ping_rate_limit_window = float(_pos_int('ping_rate_limit_window', int(self.ping_rate_limit_window)))

            ping_min_interval = config.get('ping_min_interval')
            if isinstance(ping_min_interval, (int, float)) and ping_min_interval >= 0.01:
                self.ping_min_interval = float(ping_min_interval)
            elif 'ping_min_interval' in config:
                logging.warning(f"Некорректный ping_min_interval в конфиге: {ping_min_interval}. Используется значение {self.ping_min_interval}")

            self.temp_dir = self.upload_dir / '.tmp'
            self.temp_dir.mkdir(exist_ok=True)

            logging.info(f"Конфигурация загружена из {config_path}")

        except json.JSONDecodeError as e:
            logging.error(f"Ошибка парсинга JSON в конфигурации {config_path}: {e}")
        except Exception as e:
            logging.error(f"Ошибка загрузки конфигурации: {e}")

    def setup_tls(self):
        try:
            cert_file = Path(self.cert_file)
            key_file = Path(self.key_file)
            if not cert_file.is_file() or not key_file.is_file():
                cert_file = self.resource_path(self.cert_file)
                key_file = self.resource_path(self.key_file)

            self.ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            self.ssl_context.load_cert_chain(cert_file, key_file)

        except Exception as e:
            logging.error(f"Ошибка загрузки TLS сертификата: {e}")
            self.tls_enabled = False
            raise

    def enable_keepalive(self, sock, idle=60, interval=10, count=3):
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        except OSError:
            return

        if sys.platform.startswith('win'):
            try:
                sock.ioctl(socket.SIO_KEEPALIVE_VALS, (1, idle * 1000, interval * 1000))
                return
            except (AttributeError, OSError):
                return

        keepidle = getattr(socket, 'TCP_KEEPIDLE', None)
        if keepidle is None:
            keepidle = getattr(socket, 'TCP_KEEPALIVE', None)

        keepintvl = getattr(socket, 'TCP_KEEPINTVL', None)
        keepcnt = getattr(socket, 'TCP_KEEPCNT', None)

        for opt, value in ((keepidle, idle), (keepintvl, interval), (keepcnt, count)):
            if opt is None:
                continue
            try:
                sock.setsockopt(socket.IPPROTO_TCP, opt, value)
            except OSError:
                pass

    def auth_retry_after(self, ip):
        if not self.auth_rate_limit_enabled:
            return 0
        now = time.time()
        with self.auth_lock:
            e = self.auth_state.get(ip)
            if e and e['locked_until'] > now:
                return int(e['locked_until'] - now) + 1
            if self.auth_global_locked_until > now:
                return int(self.auth_global_locked_until - now) + 1
            return 0

    def auth_attempts_left(self, ip):
        if not self.auth_rate_limit_enabled:
            return 0
        now = time.time()
        with self.auth_lock:
            e = self.auth_state.get(ip)
            if not e:
                return self.auth_max_attempts
            failures = [t for t in e['failures'] if now - t <= self.auth_window]
            return max(0, self.auth_max_attempts - len(failures))

    def auth_record_failure(self, ip):
        if not self.auth_rate_limit_enabled:
            return 0
        now = time.time()
        with self.auth_lock:
            e = self.auth_state.get(ip)
            if e is None:
                e = {'failures': [], 'locked_until': 0.0, 'last_seen': now}
                self.auth_state[ip] = e
            e['last_seen'] = now

            e['failures'] = [t for t in e['failures'] if now - t <= self.auth_window]
            e['failures'].append(now)

            if len(e['failures']) >= self.auth_max_attempts:
                e['locked_until'] = now + self.auth_lockout
                e['failures'].clear()
                logging.warning(f"IP {ip} заблокирован на {self.auth_lockout}с после {self.auth_max_attempts} неудачных попыток аутентификации")
                self.auth_cleanup(now)
                return self.auth_lockout

            self.auth_global_failures = [t for t in self.auth_global_failures if now - t <= self.auth_global_window]
            self.auth_global_failures.append(now)
            if len(self.auth_global_failures) >= self.auth_global_max_attempts:
                self.auth_global_locked_until = now + self.auth_lockout
                logging.warning(f"Глобальная блокировка аутентификации на {self.auth_lockout}с ({self.auth_global_max_attempts} неудач за {self.auth_global_window}с)")
                self.auth_global_failures.clear()
                self.auth_cleanup(now)
                return self.auth_lockout

            self.auth_cleanup(now)
            return 0

    def auth_record_success(self, ip):
        if not self.auth_rate_limit_enabled:
            return
        with self.auth_lock:
            self.auth_state.pop(ip, None)

    def auth_cleanup(self, now):
        if now - self.auth_last_cleanup < 60:
            return
        self.auth_last_cleanup = now
        to_remove = []
        for ip, e in self.auth_state.items():
            if e['locked_until'] > now:
                continue
            if e['failures'] and now - e['failures'][-1] <= self.auth_window:
                continue
            to_remove.append(ip)
        for ip in to_remove:
            del self.auth_state[ip]

    def start(self):
        if not self.is_port_available():
            logging.error(f"Порт {self.port} уже занят!\nНажмите enter для выхода...")
            input()
            return

        self.cleanup_temp_dir()

        self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server.bind((self.host, self.port))
        self.server.listen(5)

        auth_required = 'включена' if self.auth_token else 'не требуется'
        tls_enabled = 'включён, сертификат и ключ загружены' if self.tls_enabled else 'выключен'
        token_rate_limit_enabled = 'включена' if self.auth_token and self.auth_rate_limit_enabled else 'выключена'
        ping_rate_limit_enabled = 'включена' if self.ping_rate_limit_enabled else 'выключена'
        progress_unit_label = '%' if self.progress_log_unit == 'percent' else 'МБ'
        clients_limit_enabled = f'включено (максимум: {self.clients_limit})' if self.clients_limit_enabled else 'выключено'
        logging.info(f"Сервер запущен на {self.host}:{self.port}")
        logging.info(f"Директория для серверных файлов: {self.upload_dir.absolute()}")
        logging.info(f"Аутентификация для пользователей {auth_required}")
        logging.info(f"TLS {tls_enabled}")
        logging.info(f"Защита от брутфорса {token_rate_limit_enabled}")
        logging.info(f"Ping-защита {ping_rate_limit_enabled}")
        logging.info(f"Логирование прогресса: каждые {self.progress_log_value} {progress_unit_label}")
        logging.info(f"Ограничение количества клиентов {clients_limit_enabled}")

        try:
            while True:
                try:
                    client_socket, address = self.server.accept()
                except OSError as e:
                    logging.error(f"Ошибка accept: {e}")
                    continue

                self.enable_keepalive(client_socket)

                try:
                    client_socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                except OSError:
                    try:
                        client_socket.close()
                    except Exception:
                        pass
                    continue

                is_tls = False
                peer = f"{address[0]}:{address[1]}"

                if self.tls_enabled and self.ssl_context:
                    try:
                        client_socket.settimeout(1.0)
                        first_byte = client_socket.recv(1, socket.MSG_PEEK)
                    except socket.timeout:
                        first_byte = b''
                        logging.warning(f"Клиент {peer}: таймаут ожидания TLS ClientHello")
                    except (ConnectionResetError, OSError) as e:
                        logging.warning(f"Клиент {peer}: ошибка при проверке TLS: {e}")
                        try:
                            client_socket.close()
                        except Exception:
                            pass
                        continue

                    if first_byte and first_byte[0] == 0x16:
                        try:
                            client_socket.settimeout(self.timeout)
                            client_socket = self.ssl_context.wrap_socket(
                                client_socket,
                                server_side=True
                            )
                            is_tls = True
                        except (ssl.SSLError, ConnectionResetError, OSError) as e:
                            logging.warning(f"TLS-рукопожатие с {peer} не удалось: {e}")
                            try:
                                client_socket.close()
                            except Exception:
                                pass
                            continue
                    else:
                        client_socket.settimeout(self.timeout)
                else:
                    client_socket.settimeout(self.timeout)

                try:
                    client_thread = threading.Thread(
                        target=self.handle_client,
                        args=(client_socket, address, is_tls)
                    )
                    client_thread.daemon = True
                    client_thread.start()
                except Exception as e:
                    logging.error(f"Не удалось запустить обработчик для {peer}: {e}")
                    try:
                        client_socket.close()
                    except Exception:
                        pass

        except KeyboardInterrupt:
            logging.info("Остановка сервера...")
        finally:
            if self.server:
                self.server.close()

    def is_port_available(self):
        try:
            test_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            test_sock.bind((self.host, self.port))
            test_sock.close()
            return True
        except OSError:
            logging.warning(f"Порт {self.port} недоступен.")
            return False

    def is_safe_path(self, filename):
        try:
            user_path = Path(filename)
            if user_path.is_absolute():
                return False
            requested_path = (self.upload_dir / user_path).resolve()
            base_path = self.upload_dir.resolve()
            temp_dir = self.temp_dir.resolve()
            if requested_path == temp_dir or temp_dir in requested_path.parents:
                return False
            return base_path == requested_path or base_path in requested_path.parents
        except Exception:
            return False

    def cleanup_temp_dir(self):
        try:
            for f in self.temp_dir.iterdir():
                if f.is_file():
                    try:
                        f.unlink()
                        logging.info(f"Удалён устаревший временный файл {f.name}")
                    except Exception as e:
                        logging.warning(f"Не удалось удалить {f.name}: {e}")
        except Exception:
            pass

    def handle_client(self, client_socket, address, is_tls=False):
        peer = f"{address[0]}:{address[1]}"
        connected = False
        counted = False

        try:
            if self.tls_enabled and not is_tls:
                self.send_response(client_socket, {
                    'type': 'init',
                    'chunk_size': self.chunk_size,
                    'max_file_size': self.max_file_size,
                    'timeout': self.timeout,
                    'auth_required': bool(self.auth_token),
                    'tls_required': True,
                })
                logging.warning(f"Клиент {peer} отклонён: требуется TLS-соединение")
                return

            with self.clients_lock:
                if self.clients_limit_enabled and self.client_count >= self.clients_limit:
                    limit_reached = True
                else:
                    self.client_count += 1
                    counted = True
                    limit_reached = False

            if limit_reached:
                logging.warning(f"Клиент {peer} отклонён: достигнут лимит ({self.client_count}/{self.clients_limit})")
                try:
                    self.send_response(client_socket, {
                        'type': 'init',
                        'status': 'error',
                        'message': 'Сервер перегружен: достигнут лимит клиентов',
                        'max_clients': self.clients_limit,
                    })
                except Exception:
                    pass
                return

            self.send_response(client_socket, {
                'type': 'init',
                'chunk_size': self.chunk_size,
                'max_file_size': self.max_file_size,
                'timeout': self.timeout,
                'auth_required': bool(self.auth_token),
                'tls_required': False,
            })

            if self.auth_token:
                client_ip = address[0]

                command_data = self.receive_all(client_socket, 4)
                data_length = struct.unpack('>I', command_data)[0]

                if data_length > self.max_json_size:
                    try:
                        self.send_response(client_socket, {'status': 'error', 'message': 'Слишком большое сообщение'})
                    except Exception:
                        pass
                    self.auth_record_failure(client_ip)
                    return

                json_data = self.receive_all(client_socket, data_length)

                try:
                    command = json.loads(json_data.decode('utf-8'))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    try:
                        self.send_response(client_socket, {'status': 'error', 'message': 'Ошибка декодирования'})
                    except Exception:
                        pass
                    self.auth_record_failure(client_ip)
                    return

                retry_after = self.auth_retry_after(client_ip)
                if retry_after > 0:
                    self.send_response(client_socket, {
                        'status': 'error',
                        'message': f'Слишком много попыток аутентификации.',
                        'retry_after': retry_after,
                    })
                    logging.warning(f"Аутентификация с {client_ip} отклонена: блокировка, осталось {retry_after}с")
                    return

                if not isinstance(command, dict) or command.get('command') != 'auth':
                    try:
                        self.send_response(client_socket, {'status': 'error', 'message': 'Требуется аутентификация'})
                    except Exception:
                        pass
                    self.auth_record_failure(client_ip)
                    return

                token = command.get('token', '')
                if not isinstance(token, str):
                    try:
                        self.send_response(client_socket, {'status': 'error', 'message': 'Неверный формат пароля'})
                    except Exception:
                        pass
                    self.auth_record_failure(client_ip)
                    return

                token_ok = hmac.compare_digest(
                    token.encode('utf-8'),
                    self.auth_token.encode('utf-8'),
                )

                if not token_ok:
                    lockout = self.auth_record_failure(client_ip)

                    if self.auth_failure_delay > 0:
                        time.sleep(self.auth_failure_delay)

                    try:
                        if lockout:
                            self.send_response(client_socket, {
                                'status': 'error',
                                'message': f'Слишком много неверных попыток. Блокировка на {lockout} с.',
                                'retry_after': lockout,
                            })
                        else:
                            left = self.auth_attempts_left(client_ip)
                            msg = 'Неверный пароль'
                            if self.auth_rate_limit_enabled and left > 0:
                                msg += f'. Осталось попыток: {left}'
                            self.send_response(client_socket, {'status': 'error', 'message': msg})
                    except Exception:
                        pass

                    logging.warning(f"Неудачная аутентификация с {client_ip}")
                    return

                self.auth_record_success(client_ip)
                self.send_response(client_socket, {'status': 'success', 'message': 'Аутентификация успешна'})

                logging.info(f"Клиент {peer} успешно подключён и аутентифицирован")
                connected = True
            else:
                logging.info(f"Клиент {peer} подключён")
                connected = True

            client_socket.settimeout(None)

            while True:
                try:
                    command_data = self.receive_all(client_socket, 4)
                    data_length = struct.unpack('>I', command_data)[0]
                    json_data = self.receive_all(client_socket, data_length)

                    try:
                        command = json.loads(json_data.decode('utf-8'))
                    except UnicodeDecodeError:
                        logging.error(f"Ошибка декодирования JSON от {peer}")
                        break

                    cmd = command.get('command')

                    if cmd == 'list':
                        client_socket.settimeout(self.timeout)
                        self.send_file_list(client_socket)
                        client_socket.settimeout(None)
                    elif cmd == 'upload':
                        client_socket.settimeout(self.timeout)
                        self.receive_file(client_socket, command)
                        client_socket.settimeout(None)
                    elif cmd == 'download':
                        client_socket.settimeout(self.timeout)
                        self.send_file(client_socket, command)
                        client_socket.settimeout(None)
                    elif cmd == 'delete':
                        client_socket.settimeout(self.timeout)
                        self.delete_file(client_socket, command)
                        client_socket.settimeout(None)
                    elif cmd == 'info':
                        client_socket.settimeout(self.timeout)
                        self.send_server_info(client_socket)
                        client_socket.settimeout(None)
                    elif cmd == 'ping':
                        client_socket.settimeout(self.timeout)
                        self.send_ping_response(client_socket, address[0])
                        client_socket.settimeout(None)
                    elif cmd == 'disconnect':
                        break
                    else:
                        self.send_response(client_socket, {'status': 'error', 'message': 'Неизвестная команда'})
                except socket.timeout:
                    logging.warning(f"Таймаут при работе с {peer}")
                    break
                except ConnectionError:
                    break
                except Exception as e:
                    logging.error(f"Ошибка с клиентом {peer}: {e}", exc_info=True)
                    break

        except socket.timeout:
            logging.warning(f"Таймаут при работе с {peer}")
        except ConnectionError:
            pass
        except Exception as e:
            logging.error(f"Ошибка с клиентом {peer}: {e}", exc_info=True)
        finally:
            try:
                client_socket.close()
            except Exception:
                pass
            if counted:
                with self.clients_lock:
                    self.client_count -= 1
            if connected:
                logging.info(f"Клиент {peer} отключён")

    def send_file_list(self, client_socket):
        try:
            files = []
            for f in self.upload_dir.iterdir():
                if f.is_file():
                    files.append({
                        'name': f.name,
                        'size': f.stat().st_size,
                        'modified': f.stat().st_mtime
                    })
            response = {'status': 'success', 'files': files}
        except Exception as e:
            response = {'status': 'error', 'message': str(e)}
        self.send_response(client_socket, response)

    def send_file(self, client_socket, command):
        try:
            filename = command['filename']
            if not self.is_safe_path(filename):
                err_response = {'status': 'error', 'message': 'Некорректное имя файла'}
            else:
                filepath = self.upload_dir / filename
                if not filepath.exists():
                    err_response = {'status': 'error', 'message': 'Файл не найден'}
                else:
                    file_size = filepath.stat().st_size
                    md5_hash = hashlib.md5()
                    sha256_hash = hashlib.sha256()
                    with open(filepath, 'rb') as f:
                        for chunk in iter(lambda: f.read(8192), b''):
                            md5_hash.update(chunk)
                            sha256_hash.update(chunk)
                    file_md5 = md5_hash.hexdigest()
                    file_sha256 = sha256_hash.hexdigest()
                    err_response = None
        except Exception as e:
            logging.error(f"Ошибка подготовки отправки файла: {e}", exc_info=True)
            err_response = {'status': 'error', 'message': str(e)}

        if err_response is not None:
            self.send_response(client_socket, err_response)
            return

        self.send_response(client_socket, {
            'status': 'success',
            'size': file_size,
            'filename': filename,
            'md5': file_md5,
            'sha256': file_sha256,
        })

        response = self.receive_response(client_socket)
        if not response or response.get('status') != 'ready':
            logging.error("Клиент не подтвердил готовность к приёму файла")
            raise ConnectionError("Клиент не готов к приёму файла")

        send_chunk = self.progress_read_size(file_size)
        progress_state = self.progress_log_state(file_size)
        sent_total = 0
        with open(filepath, 'rb') as f:
            while True:
                chunk = f.read(send_chunk)
                if not chunk:
                    break

                client_socket.sendall(struct.pack('>I', len(chunk)))
                client_socket.sendall(chunk)

                sent_total += len(chunk)

                for threshold in self.progress_crossed(progress_state, sent_total, file_size):
                    self._log_progress("Отправка", filename, progress_state, threshold, sent_total, file_size)

        logging.info(f"Файл {filename} отправлен клиенту ({file_size} байт)")

    def receive_file(self, client_socket, command):
        try:
            filename = command.get('filename')
            if not filename or not self.is_safe_path(filename):
                err_response = {'status': 'error', 'message': 'Некорректное имя файла'}
            else:
                try:
                    file_size = int(command['size'])
                except (KeyError, TypeError, ValueError):
                    err_response = {'status': 'error', 'message': 'Некорректный размер файла'}
                else:
                    if file_size > self.max_file_size:
                        err_response = {'status': 'error', 'message': 'Файл слишком большой'}
                    else:
                        try:
                            usage = shutil.disk_usage(self.upload_dir)
                            if usage.free < file_size * 1.1:
                                err_response = {'status': 'error', 'message': 'На сервере недостаточно свободного места'}
                            else:
                                err_response = None
                        except OSError:
                            err_response = None
                    expected_md5 = command.get('md5')
                    expected_sha256 = command.get('sha256')
        except Exception as e:
            logging.error(f"Ошибка валидации загрузки: {e}", exc_info=True)
            err_response = {'status': 'error', 'message': str(e)}

        if err_response is not None:
            self.send_response(client_socket, err_response)
            return

        filepath = self.upload_dir / filename
        tmp_fd, tmp_path_str = tempfile.mkstemp(prefix=f'{filename}.', suffix='.slanfm.part', dir=self.temp_dir)
        os.close(tmp_fd)
        tmp_path = Path(tmp_path_str)

        try:
            self.send_response(client_socket, {'status': 'ready'})

            received = 0
            progress_state = self.progress_log_state(file_size)

            with open(tmp_path, 'wb') as f:
                while received < file_size:
                    chunk_size_data = self.receive_all(client_socket, 4)
                    chunk_size = struct.unpack('>I', chunk_size_data)[0]

                    if chunk_size > self.chunk_size:
                        raise ConnectionError(f"Размер чанка {chunk_size} превышает допустимый {self.chunk_size}")

                    remaining = file_size - received
                    if chunk_size > remaining:
                        raise ConnectionError(f"Размер чанка {chunk_size} превышает оставшийся {remaining}")

                    sub = self.progress_read_size(file_size)
                    left = chunk_size
                    while left > 0:
                        piece_size = min(sub, left)
                        piece = self.receive_all(client_socket, piece_size)
                        f.write(piece)
                        received += len(piece)
                        left -= len(piece)

                        for threshold in self.progress_crossed(progress_state, received, file_size):
                            self._log_progress("Приём", filename, progress_state, threshold, received, file_size)

            md5_hash = hashlib.md5()
            sha256_hash = hashlib.sha256()
            with open(tmp_path, 'rb') as f:
                for chunk in iter(lambda: f.read(8192), b''):
                    md5_hash.update(chunk)
                    sha256_hash.update(chunk)
            computed_md5 = md5_hash.hexdigest()
            computed_sha256 = sha256_hash.hexdigest()

            if expected_md5 and computed_md5 != expected_md5:
                self.send_response(client_socket, {
                    'status': 'error',
                    'message': 'MD5 хеш полученного файла не совпадает с заявленным'
                })
                logging.error(f"MD5 не совпадает для {filename}")
                return

            if expected_sha256 and computed_sha256 != expected_sha256:
                self.send_response(client_socket, {
                    'status': 'error',
                    'message': 'SHA-256 хеш полученного файла не совпадает с заявленным'
                })
                logging.error(f"SHA-256 не совпадает для {filename}")
                return

            try:
                os.replace(tmp_path, filepath)
            except OSError as e:
                self.send_response(client_socket, {'status': 'error', 'message': 'Не удалось сохранить файл'})
                logging.error(f"Не удалось сохранить {filename}: {e}")
                return

            self.send_response(client_socket, {
                'status': 'success',
                'message': 'Файл загружен',
                'md5': computed_md5,
                'sha256': computed_sha256,
            })
            logging.info(f"Файл {filename} успешно загружен на сервер ({file_size} байт)")

        except (socket.timeout, ConnectionError, ssl.SSLError):
            logging.error(f"Сетевая ошибка/ошибка протокола при приёме {filename}")
            raise
        finally:
            if tmp_path.exists():
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass

    def delete_file(self, client_socket, command):
        if not self.can_clients_delete_files:
            self.send_response(client_socket, {'status': 'error', 'message': 'Недостаточно прав'})
            return

        try:
            filename = command['filename']
            if not self.is_safe_path(filename):
                response = {'status': 'error', 'message': 'Некорректное имя файла'}
            else:
                filepath = self.upload_dir / filename
                if filepath.exists():
                    filepath.unlink()
                    logging.info(f"Файл {filename} удалён с сервера")
                    response = {'status': 'success', 'message': 'Файл удален'}
                else:
                    response = {'status': 'error', 'message': 'Файл не найден'}
        except Exception as e:
            response = {'status': 'error', 'message': str(e)}

        self.send_response(client_socket, response)

    def send_server_info(self, client_socket):
        try:
            total_size = 0
            total_files = 0
            for f in self.upload_dir.iterdir():
                if f.is_file():
                    total_files += 1
                    total_size += f.stat().st_size

            response = {
                'status': 'success',
                'info': {
                    'total_files': total_files,
                    'total_size': total_size
                }
            }
        except Exception as e:
            response = {'status': 'error', 'message': str(e)}
        self.send_response(client_socket, response)

    def send_ping_response(self, client_socket, client_ip=None):
        now = time.time()
        response = {'status': 'success', 'timestamp': now}

        if self.ping_rate_limit_enabled and client_ip:
            with self.ping_lock:
                e = self.ping_state.get(client_ip)
                if e is None:
                    e = {'times': [], 'last': 0.0}
                    self.ping_state[client_ip] = e

                if self.ping_min_interval > 0 and (now - e['last']) < self.ping_min_interval:
                    wait = self.ping_min_interval - (now - e['last'])
                    response = {
                        'status': 'error',
                        'message': 'Слишком частые пинги',
                        'retry_after': max(1, int(wait) + 1),
                    }
                else:
                    e['times'] = [t for t in e['times'] if now - t <= self.ping_rate_limit_window]
                    if len(e['times']) >= self.ping_rate_limit_max:
                        retry = max(1, int(self.ping_rate_limit_window - (now - e['times'][0])) + 1)
                        response = {
                            'status': 'error',
                            'message': 'Превышен лимит пингов',
                            'retry_after': retry,
                        }
                    else:
                        e['times'].append(now)
                        e['last'] = now
                        self._ping_cleanup(now)

        self.send_response(client_socket, response)

    def _ping_cleanup(self, now):
        if now - self.ping_last_cleanup < 60:
            return
        self.ping_last_cleanup = now
        stale = []
        for ip, e in self.ping_state.items():
            last_activity = e['times'][-1] if e['times'] else e['last']
            if now - last_activity > max(60.0, self.ping_rate_limit_window * 3):
                stale.append(ip)
        for ip in stale:
            del self.ping_state[ip]

    def progress_log_state(self, total_size):
        if self.progress_log_unit == 'percent':
            step = float(max(0.0001, self.progress_log_value))
            return {'unit': 'percent', 'step': step, 'next': step}
        step_bytes = int(max(1, self.progress_log_value) * 1024 * 1024)
        return {'unit': 'mb', 'step': step_bytes, 'next': step_bytes}

    def progress_crossed(self, state, done, total):
        if total <= 0:
            return []
        crossed = []
        if state['unit'] == 'percent':
            percent = (done / total) * 100.0
            while percent + 1e-9 >= state['next']:
                crossed.append(state['next'])
                state['next'] += state['step']
        else:
            while done >= state['next']:
                crossed.append(state['next'])
                state['next'] += state['step']
        return crossed
    
    def progress_read_size(self, total_size):
        if total_size <= 0:
            return self.chunk_size
        if self.progress_log_unit == 'percent':
            step = min(100.0, max(0.0001, float(self.progress_log_value)))
            num_steps = max(1, int(100.0 / step))
            target = math.ceil(total_size / num_steps)
            return max(1, min(self.chunk_size, target))
        target = int(max(1, self.progress_log_value) * 1024 * 1024)
        return min(self.chunk_size, target)
    
    def _format_threshold(self, value):
        s = f"{value:.6f}".rstrip('0').rstrip('.')
        return s or "0"
    
    def _log_progress(self, action, filename, state, threshold, done, total):
        if state['unit'] == 'percent':
            logging.info(
                f"{action} {filename}: порог {self._format_threshold(threshold)}% "
                f"({done}/{total} байт)"
            )
        else:
            mb = threshold / (1024 * 1024)
            logging.info(
                f"{action} {filename}: {self._format_threshold(mb)} МБ "
                f"({done}/{total} байт)"
            )

    def receive_all(self, sock, length):
        data = b''
        while len(data) < length:
            chunk = sock.recv(min(4096, length - len(data)))
            if not chunk:
                raise ConnectionError("Соединение разорвано")
            data += chunk
        return data

    def receive_response(self, sock):
        length_data = self.receive_all(sock, 4)
        data_length = struct.unpack('>I', length_data)[0]
        json_data = self.receive_all(sock, data_length)
        return json.loads(json_data.decode('utf-8'))

    def send_response(self, sock, data):
        json_data = json.dumps(data).encode('utf-8')
        sock.sendall(len(json_data).to_bytes(4, 'big'))
        sock.sendall(json_data)


if __name__ == "__main__":
    server = FileServer()
    server.start()
