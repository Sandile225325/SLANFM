import socket
import json
import os
import hashlib
from pathlib import Path
import struct
import sys
import ssl
import time


class FileClient:
    def __init__(self, server_host=None, server_port=None, download_dir=None):
        self.server_host = server_host
        self.server_port = server_port
        self.socket = None
        self.download_dir = Path(download_dir) if download_dir else None
        self.chunk_size = 65536
        self.max_file_size = 2 * 1024 * 1024 * 1024
        self.timeout = 120
        self.config_file = "client_config.json"
        self.config = self.load_config()
        self.auth_token = self.config.get('authentication_config', {}).get('token', '')
        self.tls_enabled = False
        self.ping_min_interval = 0.5
        self.last_ping_call = 0.0

    def connect(self):
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            sock.settimeout(self.config.get('connect_config', {}).get('non-tls_timeout', 2.5))
            sock.connect((self.server_host, self.server_port))

            original_socket = self.socket
            self.socket = sock
            init_response = self.receive_response()
            self.socket = original_socket

            if init_response.get('_error_kind'):
                try:
                    sock.close()
                except Exception:
                    pass
                return f"Не удалось установить соединение с сервером: {init_response.get('message', 'неизвестная ошибка')}"

            if init_response.get('type') == 'init' and not init_response.get('tls_required'):
                self.socket = sock
                self.tls_enabled = False
            else:
                sock.close()

                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                sock.settimeout(self.config.get('connect_config', {}).get('tls_timeout', 5))
                sock.connect((self.server_host, self.server_port))

                context = ssl.create_default_context()
                context.check_hostname = False
                context.verify_mode = ssl.CERT_NONE
                tls_sock = context.wrap_socket(sock, server_hostname=self.server_host)

                self.socket = tls_sock
                init_response = self.receive_response()
                if init_response.get('type') == 'init':
                    self.socket = tls_sock
                    self.tls_enabled = True
                else:
                    tls_sock.close()
                    self.socket = None
                    reason = init_response.get('message', 'неизвестная ошибка')
                    return f"Не удалось установить соединение с сервером: {reason}"

            cfg = getattr(self, 'config', {}).get('values_config', {})
            chunk_min, chunk_max = min(cfg.get('chunk_size_range', [1024, 10485760])), max(cfg.get('chunk_size_range', [1024, 10485760]))
            timeout_min, timeout_max = min(cfg.get('timeout_range', [1, 300])), max(cfg.get('timeout_range', [1, 300]))
            auth_required = init_response.get('auth_required', False)

            chunk_size = init_response['chunk_size']
            max_file_size = init_response['max_file_size']
            timeout = init_response['timeout']

            if not (chunk_min <= chunk_size <= chunk_max):
                self.socket.close()
                return "Размер чанка сервера не входит в допустимый диапазон клиента"

            if not (timeout_min <= timeout <= timeout_max):
                self.socket.close()
                return "Таймаут сервера не входит в допустимый диапазон клиента"

            self.chunk_size = chunk_size
            self.max_file_size = max_file_size
            self.timeout = timeout
            self.socket.settimeout(self.timeout)

            if auth_required:
                if self.auth_token:
                    if not self.send_command({'command': 'auth', 'token': self.auth_token}):
                        self._force_close()
                        return "Не удалось отправить данные аутентификации"
                    auth_response = self.receive_response()
                    if auth_response.get('_error_kind'):
                        self._force_close()
                        return f"Ошибка аутентификации: {auth_response.get('message', 'сетевая ошибка')}"
                    if auth_response.get('status') != 'success':
                        retry = int(auth_response.get('retry_after', 0) or 0)
                        self._force_close()
                        if retry:
                            return {
                                'error': auth_response.get('message', 'Заблокировано'),
                                'retry_after': retry,
                            }
                        return "Ошибка аутентификации: неверный токен"
                else:
                    return "need_auth"

            return True

        except Exception as e:
            return f"Ошибка подключения: {e}"
        
    def resource_path(self, relative_path):
        try:
            base_path = Path(sys._MEIPASS)
        except AttributeError:
            base_path = Path(__file__).parent
        return base_path / relative_path
    
    def config_path(self):
        if getattr(sys, 'frozen', False):
            base_path = Path(sys.executable).parent
        else:
            base_path = Path(__file__).parent
        return base_path / self.config_file

    def load_config(self):
        default_values = {
            "connect_config": {
                "non-tls_timeout": 10,
                "tls_timeout": 20
            },
            "values_config": {
                "chunk_size_range": [1024, 10485760],
                "timeout_range": [1, 300]
            },
            "authentication_config": {"token": ""}
        }

        try:
            with open(self.config_path(), 'r', encoding='utf-8') as f:
                config = json.load(f)               
                config.setdefault('authentication_config', {})
                config['authentication_config'].setdefault('token', '')
                return config
        except FileNotFoundError:
            return default_values
        except json.JSONDecodeError:
            return default_values
        except Exception:
            return default_values
        
    def authenticate(self, token):
        if not self.send_command({'command': 'auth', 'token': token}):
            return {
                'error': 'Не удалось отправить данные аутентификации',
                'retry_after': 0
            }

        response = self.receive_response()
        if response is None:
            return {
                'error': 'Соединение прервано сервером (возможно, блокировка)',
                'retry_after': 0,
            }
        if response.get('status') == 'success':
            return True
        if response.get('retry_after'):
            return {
                'error': response.get('message', 'Заблокировано'),
                'retry_after': int(response['retry_after']),
            }
        return False

    def download_file(self, filename, save_path=None, progress_callback=None):
        if not save_path:
            if self.download_dir is None:
                return "Не задана папка для скачивания"
            save_path = self.download_dir / filename

        save_path = Path(save_path)
        try:
            save_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            return "Не удалось создать папку для сохранения файла"
        temp_path = save_path.with_name(f'.{save_path.name}.slanfm.part')

        if not self.send_command({'command': 'download', 'filename': filename}):
            return "Соединение потеряно перед началом скачивания"

        response = self.receive_response()

        err = self._response_error(response)
        if err:
            return err
        if response.get('status') != 'success':
            return response.get('message', 'Неизвестная ошибка')

        file_size = response['size']
        server_md5 = response.get('md5', '')
        server_sha256 = response.get('sha256', '')

        if not self.send_command({'status': 'ready'}):
            return "Соединение потеряно перед приёмом файла"

        received = 0
        try:
            with open(temp_path, 'wb') as f:
                while received < file_size:
                    chunk_size_data = self.receive_all(4)
                    chunk_size = struct.unpack('>I', chunk_size_data)[0]
                    chunk = self.receive_all(chunk_size)
                    f.write(chunk)
                    received += len(chunk)

                    if progress_callback and file_size > 0:
                        progress_callback((received / file_size) * 100)

            if received != file_size:
                return "Неполная загрузка файла"

            if server_md5 or server_sha256:
                md5_hash = hashlib.md5()
                sha256_hash = hashlib.sha256()
                with open(temp_path, 'rb') as f:
                    for chunk in iter(lambda: f.read(8192), b''):
                        md5_hash.update(chunk)
                        sha256_hash.update(chunk)
                if server_md5 and md5_hash.hexdigest() != server_md5:
                    return "MD5 хеш скачанного файла не совпадает"
                if server_sha256 and sha256_hash.hexdigest() != server_sha256:
                    return "SHA-256 хеш скачанного файла не совпадает"

            try:
                os.replace(temp_path, save_path)
            except OSError:
                return "Не удалось сохранить файл"

            if progress_callback:
                progress_callback(100)
            return True

        except socket.timeout:
            return "Таймаут при скачивании"
        except ConnectionError as e:
            return f"Ошибка скачивания: {e}"
        except Exception:
            return "Ошибка скачивания"
        finally:
            if temp_path.exists():
                try:
                    os.remove(temp_path)
                except OSError:
                    pass

    def upload_file(self, filepath, progress_callback=None):
        path = Path(filepath)

        if not path.exists():
            return "Файл не найден"

        file_size = path.stat().st_size
        if file_size > self.max_file_size:
            return "Файл слишком большой"

        md5_hash = hashlib.md5()
        sha256_hash = hashlib.sha256()
        with open(path, 'rb') as f:
            for chunk in iter(lambda: f.read(8192), b''):
                md5_hash.update(chunk)
                sha256_hash.update(chunk)
        original_md5 = md5_hash.hexdigest()
        original_sha256 = sha256_hash.hexdigest()

        if not self.send_command({
            'command': 'upload',
            'filename': path.name,
            'size': file_size,
            'md5': original_md5,
            'sha256': original_sha256,
        }):
            return "Соединение потеряно перед началом загрузки"

        response = self.receive_response()

        err = self._response_error(response)
        if err:
            return err
        if response.get('status') != 'ready':
            return response.get('message', 'Неизвестная ошибка')

        uploaded = 0
        with open(path, 'rb') as f:
            while True:
                chunk = f.read(self.chunk_size)
                if not chunk:
                    break

                if not self.socket:
                    return "Соединение потеряно при отправке файла"

                try:
                    self.socket.sendall(struct.pack('>I', len(chunk)))
                    self.socket.sendall(chunk)
                except socket.timeout:
                    self._force_close()
                    return "Таймаут при отправке файла"
                except ConnectionError as e:
                    self._force_close()
                    return f"Соединение разорвано при отправке: {e}"
                except OSError as e:
                    self._force_close()
                    return f"Ошибка сети при отправке: {e}"

                uploaded += len(chunk)
                if progress_callback and file_size > 0:
                    progress_callback((uploaded / file_size) * 100)

        response = self.receive_response()
        err = self._response_error(response)
        if err:
            return err
        if response.get('status') == 'success':
            response_md5 = response.get('md5', '')
            response_sha256 = response.get('sha256', '')
            md5_ok = (not response_md5) or (response_md5 == original_md5)
            sha256_ok = (not response_sha256) or (response_sha256 == original_sha256)
            if md5_ok and sha256_ok:
                if progress_callback:
                    progress_callback(100)
                return True
            if not md5_ok:
                return "MD5 хеш загруженного файла не совпадает"
            return "SHA-256 хеш загруженного файла не совпадает"
        return response.get('message', 'Неизвестная ошибка')

    def disconnect(self):
        if self.socket:
            try:
                self.send_command({'command': 'disconnect'})
            except:
                pass
            try:
                self.socket.close()
            except:
                pass
            self.socket = None

    def send_command(self, command):
        if not self.socket:
            return False
        try:
            json_data = json.dumps(command).encode('utf-8')
            self.socket.sendall(len(json_data).to_bytes(4, 'big'))
            self.socket.sendall(json_data)
            return True
        except Exception:
            self._force_close()
            return False

    def receive_all(self, length):
        if not self.socket:
            raise ConnectionError("Соединение потеряно")

        data = b''
        while len(data) < length:
            try:
                chunk = self.socket.recv(min(4096, length - len(data)))
            except socket.timeout:
                self._force_close()
                raise
            except OSError as e:
                self._force_close()
                raise ConnectionError(f"Ошибка сети: {e}") from e
            if not chunk:
                self._force_close()
                raise ConnectionError("Соединение разорвано сервером")
            data += chunk
        return data

    def receive_response(self):
        try:
            timeout_val = self.socket.gettimeout() if self.socket else None
            length_data = self.receive_all(4)
            data_length = struct.unpack('>I', length_data)[0]
            json_data = self.receive_all(data_length)
            try:
                return json.loads(json_data.decode('utf-8'))
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise ValueError("Некорректный ответ сервера")
        except socket.timeout:
            msg = "Таймаут при ожидании данных от сервера"
            return {
                'status': 'error',
                'message': msg,
                '_error_kind': 'timeout'
            }
        except ConnectionError as e:
            return {
                'status': 'error',
                'message': str(e),
                '_error_kind': 'disconnected'
            }
        except ValueError as e:
            return {
                'status': 'error',
                'message': str(e),
                '_error_kind': 'protocol'
            }
        except Exception as e:
            return {
                'status': 'error',
                'message': f"Ошибка: {e}",
                '_error_kind': 'error'
            }

    def list_files(self):
        self.send_command({'command': 'list'})
        response = self.receive_response()

    def delete_file(self, filename):
        if not self.send_command({'command': 'delete', 'filename': filename}):
            return "Соединение потеряно перед удалением"

        response = self.receive_response()
        err = self._response_error(response)
        if err:
            return err
        if response.get('status') == 'success':
            return True
        return response.get('message', 'Неизвестная ошибка')

    def get_server_info(self):
        self.send_command({'command': 'info'})
        response = self.receive_response()

    def ping(self):
        if not self.socket:
            return {'error': 'Нет соединения'}

        now = time.time()
        if now - self.last_ping_call < self.ping_min_interval:
            return {
                'error': 'Слишком частый пинг',
                'retry_after': self.ping_min_interval - (now - self.last_ping_call),
            }
        self.last_ping_call = now

        try:
            old_timeout = self.socket.gettimeout()
        except Exception:
            old_timeout = None

        ping_timeout = min(2.0, float(self.timeout)) if self.timeout else 2.0

        try:
            try:
                self.socket.settimeout(ping_timeout)
            except Exception:
                pass

            t0 = time.time()
            if not self.send_command({'command': 'ping'}):
                return {'error': 'Не удалось отправить ping'}
            response = self.receive_response()
            rtt = time.time() - t0

            err = self._response_error(response)
            if err:
                return {'error': err}
            if response.get('status') != 'success':
                return {
                    'error': response.get('message', 'Ошибка ping'),
                    'retry_after': response.get('retry_after', 0),
                }
            return {'rtt': rtt}
        except Exception as e:
            return {'error': str(e)}
        finally:
            if old_timeout is not None and self.socket is not None:
                try:
                    self.socket.settimeout(old_timeout)
                except Exception:
                    pass

    def _force_close(self):
        sock = self.socket
        self.socket = None
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass

    def _response_error(self, response):
        if response is None:
            return "Нет ответа от сервера"
        if response.get('_error_kind'):
            return response.get('message', 'Ошибка сети')
        return None