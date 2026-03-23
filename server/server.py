import socket
import threading
import os
import json
import hashlib
from pathlib import Path
import logging
import struct

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

        if config_path:
            self.load_config(config_path)

    def load_config(self, config_path):
        try:
            config_file = Path(config_path)
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

            logging.info(f"Конфигурация загружена из {config_path}")

        except json.JSONDecodeError as e:
            logging.error(f"Ошибка парсинга JSON в конфигурации {config_path}: {e}")
        except Exception as e:
            logging.error(f"Ошибка загрузки конфигурации: {e}")

    def start(self):
        if not self.is_port_available():
            logging.error(f"Порт {self.port} уже занят!\nНажмите enter для выхода...")
            input()
            return

        self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server.bind((self.host, self.port))
        self.server.listen(5)
        logging.info(f"Сервер запущен на {self.host}:{self.port}")
        logging.info(f"Директория для серверных файлов: {self.upload_dir.absolute()}")

        try:
            while True:
                client_socket, address = self.server.accept()
                client_socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

                client_thread = threading.Thread(
                    target=self.handle_client,
                    args=(client_socket, address)
                )
                client_thread.daemon = True
                client_thread.start()
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
        except OSError as e:
            logging.warning(f"Порт {self.port} недоступен.")
            return False
        
    def is_safe_path(self, filename):
        try:
            requested_path = (self.upload_dir / filename).resolve()
            return self.upload_dir.resolve() in requested_path.parents or requested_path == self.upload_dir.resolve()
        except Exception:
            return False

    def handle_client(self, client_socket, address):
        try:
            self.send_response(client_socket, {
                'type': 'init',
                'chunk_size': self.chunk_size,
                'max_file_size': self.max_file_size,
                'timeout': self.timeout
            })

            while True:
                command_data = self.receive_all(client_socket, 4)
                if not command_data or len(command_data) != 4:
                    break

                data_length = struct.unpack('>I', command_data)[0]
                json_data = self.receive_all(client_socket, data_length)

                if not json_data:
                    break

                try:
                    command = json.loads(json_data.decode('utf-8'))
                except UnicodeDecodeError:
                    logging.error(f"Ошибка декодирования JSON от {address}")
                    break

                cmd = command.get('command')

                if cmd == 'list':
                    self.send_file_list(client_socket)
                elif cmd == 'upload':
                    self.receive_file(client_socket, command)
                elif cmd == 'download':
                    self.send_file(client_socket, command)
                elif cmd == 'delete':
                    self.delete_file(client_socket, command)
                elif cmd == 'info':
                    self.send_server_info(client_socket)
                elif cmd == 'disconnect':
                    break
                else:
                    self.send_response(client_socket, {'status': 'error', 'message': 'Неизвестная команда'})

        except Exception as e:
            logging.error(f"Ошибка с клиентом {address}: {e}", exc_info=True)
        finally:
            try:
                client_socket.close()
            except:
                pass

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

            response = {
                'status': 'success',
                'files': files
            }
            self.send_response(client_socket, response)
        except Exception as e:
            self.send_response(client_socket, {'status': 'error', 'message': str(e)})

    def send_file(self, client_socket, command):
        try:
            filename = command['filename']
            if not self.is_safe_path(filename):
                self.send_response(client_socket, {'status': 'error', 'message': 'Некорректное имя файла'})
                return
            filepath = self.upload_dir / filename

            if not filepath.exists():
                self.send_response(client_socket, {'status': 'error', 'message': 'Файл не найден'})
                return

            file_size = filepath.stat().st_size

            md5_hash = hashlib.md5()
            with open(filepath, 'rb') as f:
                for chunk in iter(lambda: f.read(8192), b''):
                    md5_hash.update(chunk)

            self.send_response(client_socket, {
                'status': 'success',
                'size': file_size,
                'filename': filename,
                'md5': md5_hash.hexdigest()
            })

            response = self.receive_response(client_socket)
            if not response or response.get('status') != 'ready':
                logging.error("Клиент не подтвердил готовность к приему файла")
                return

            sent_total = 0
            with open(filepath, 'rb') as f:
                while True:
                    chunk = f.read(self.chunk_size)
                    if not chunk:
                        break

                    try:
                        client_socket.sendall(struct.pack('>I', len(chunk)))
                        client_socket.sendall(chunk)
                    except (ConnectionError, BrokenPipeError):
                        logging.error("Соединение разорвано при отправке файла")
                        return

                    sent_total += len(chunk)

                    if sent_total % (10 * 1024 * 1024) < self.chunk_size:
                        percent = (sent_total / file_size) * 100
                        logging.info(f"Отправка {filename}: {percent:.1f}% ({sent_total}/{file_size} байт)")

            logging.info(f"Файл {filename} отправлен клиенту ({file_size} байт)")

        except Exception as e:
            logging.error(f"Ошибка отправки файла: {e}", exc_info=True)
            try:
                self.send_response(client_socket, {'status': 'error', 'message': str(e)})
            except:
                pass

    def receive_file(self, client_socket, command):
        try:
            filename = command['filename']
            if not self.is_safe_path(filename):
                self.send_response(client_socket, {'status': 'error', 'message': 'Некорректное имя файла'})
                return
            file_size = int(command['size'])

            if file_size > self.max_file_size:
                self.send_response(client_socket, {'status': 'error', 'message': 'Файл слишком большой'})
                return

            filepath = self.upload_dir / filename

            self.send_response(client_socket, {'status': 'ready'})

            received = 0
            with open(filepath, 'wb') as f:
                while received < file_size:
                    try:
                        chunk_size_data = self.receive_all(client_socket, 4)
                        if not chunk_size_data or len(chunk_size_data) != 4:
                            raise ConnectionError("Не удалось получить размер чанка")

                        chunk_size = struct.unpack('>I', chunk_size_data)[0]

                        chunk = self.receive_all(client_socket, chunk_size)
                        if not chunk or len(chunk) != chunk_size:
                            raise ConnectionError(f"Не удалось получить чанк: ожидалось {chunk_size}, получено {len(chunk) if chunk else 0}")

                        f.write(chunk)
                        received += len(chunk)

                        if received % (10 * 1024 * 1024) < self.chunk_size:
                            percent = (received / file_size) * 100
                            logging.info(f"Прием {filename}: {percent:.1f}% ({received}/{file_size} байт)")

                    except (ConnectionError, socket.timeout, struct.error) as e:
                        logging.error(f"Ошибка приема чанка: {e}")
                        raise

            if received == file_size:
                md5_hash = hashlib.md5()
                with open(filepath, 'rb') as f:
                    for chunk in iter(lambda: f.read(8192), b''):
                        md5_hash.update(chunk)

                self.send_response(client_socket, {
                    'status': 'success',
                    'message': 'Файл загружен',
                    'md5': md5_hash.hexdigest()
                })
                logging.info(f"Файл {filename} успешно загружен на сервер ({file_size} байт)")
            else:
                if filepath.exists():
                    os.remove(filepath)
                self.send_response(client_socket, {
                    'status': 'error',
                    'message': f'Неполная загрузка файла: получено {received} из {file_size} байт'
                })

        except Exception as e:
            filename = command['filename']
            filepath = self.upload_dir / filename
            if 'filepath' in locals() and filepath.exists():
                try:
                    os.remove(filepath)
                except:
                    pass
            logging.error(f"Ошибка приема файла: {e}", exc_info=True)
            try:
                self.send_response(client_socket, {'status': 'error', 'message': str(e)})
            except:
                pass

    def delete_file(self, client_socket, command):
        if not self.can_clients_delete_files:
            self.send_response(client_socket, {'status': 'error', 'message': 'Недостаточно прав'})
            return

        try:
            filename = command['filename']
            if not self.is_safe_path(filename):
                self.send_response(client_socket, {'status': 'error', 'message': 'Некорректное имя файла'})
                return
            filepath = self.upload_dir / filename

            if filepath.exists():
                filepath.unlink()
                self.send_response(client_socket, {'status': 'success', 'message': 'Файл удален'})
                logging.info(f"Файл {filename} удалён с сервера")
            else:
                self.send_response(client_socket, {'status': 'error', 'message': 'Файл не найден'})
        except Exception as e:
            self.send_response(client_socket, {'status': 'error', 'message': str(e)})

    def send_server_info(self, client_socket):
        try:
            total_size = 0
            total_files = 0
            for f in self.upload_dir.iterdir():
                if f.is_file():
                    total_files += 1
                    total_size += f.stat().st_size

            info = {
                'status': 'success',
                'info': {
                    'upload_dir': str(self.upload_dir.absolute()),
                    'total_files': total_files,
                    'total_size': total_size
                }
            }
            self.send_response(client_socket, info)
        except Exception as e:
            self.send_response(client_socket, {'status': 'error', 'message': str(e)})

    def receive_all(self, sock, length):
        data = b''
        while len(data) < length:
            chunk = sock.recv(min(4096, length - len(data)))
            if not chunk:
                break
            data += chunk
        return data

    def receive_response(self, sock):
        try:
            length_data = self.receive_all(sock, 4)
            if not length_data or len(length_data) != 4:
                return None

            data_length = struct.unpack('>I', length_data)[0]
            json_data = self.receive_all(sock, data_length)

            if not json_data:
                return None

            return json.loads(json_data.decode('utf-8'))
        except:
            return None

    def send_response(self, sock, data):
        try:
            json_data = json.dumps(data).encode('utf-8')
            sock.sendall(len(json_data).to_bytes(4, 'big'))
            sock.sendall(json_data)
        except:
            pass


if __name__ == "__main__":
    server = FileServer()
    server.start()