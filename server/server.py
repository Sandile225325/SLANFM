import socket
import threading
import os
import json
import hashlib
from pathlib import Path
import struct


class FileServer:
    def __init__(self, host, port, upload_dir):
        self.host = host
        self.port = port
        self.upload_dir = Path(upload_dir)
        self.upload_dir.mkdir(exist_ok=True)
        self.max_file_size = 2 * 1024 * 1024 * 1024
        self.chunk_size = 65536
        self.server = None

    def start(self):
        if not self.is_port_available():
            print(f"Порт {self.port} недоступен.\nНажмите enter для выхода...")
            input()
            return

        self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server.bind((self.host, self.port))
        self.server.listen(5)
        print(f"Сервер запущен на порту {self.port}")
        print(f"Директория для серверных файлов: {self.upload_dir.absolute()}")

        while True:
            client_socket, address = self.server.accept()
            client_socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

            client_thread = threading.Thread(target=self.handle_client, args=(client_socket, address))
            client_thread.daemon = True
            client_thread.start()

    def is_port_available(self):
        if self.port < 1024 or self.port > 65535:
            return False
        try:
            test_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            test_sock.bind((self.host, self.port))
            test_sock.close()
            return True
        except OSError:
            return False

    def handle_client(self, client_socket, address):
        try:
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
                    print(f"Ошибка декодирования JSON от {address}")
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
            print(f"Ошибка с клиентом {address}: {e}")
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
                    files.append({'name': f.name, 'size': f.stat().st_size, 'modified': f.stat().st_mtime})

            response = {'status': 'success', 'files': files}
            self.send_response(client_socket, response)
        except Exception as e:
            self.send_response(client_socket, {'status': 'error', 'message': str(e)})

    def send_file(self, client_socket, command):
        try:
            filename = command['filename']
            filepath = self.upload_dir / filename

            if not filepath.exists():
                self.send_response(client_socket, {'status': 'error', 'message': 'Файл не найден'})
                return

            file_size = filepath.stat().st_size

            md5_hash = hashlib.md5()
            with open(filepath, 'rb') as f:
                for chunk in iter(lambda: f.read(8192), b''):
                    md5_hash.update(chunk)

            self.send_response(client_socket, {'status': 'success', 'size': file_size, 'filename': filename, 'md5': md5_hash.hexdigest()})

            response = self.receive_response(client_socket)
            if not response or response.get('status') != 'ready':
                print("Клиент не подтвердил готовность к приему файла")
                return

            with open(filepath, 'rb') as f:
                while True:
                    chunk = f.read(self.chunk_size)
                    if not chunk:
                        break

                    try:
                        client_socket.sendall(struct.pack('>I', len(chunk)))
                        client_socket.sendall(chunk)
                    except (ConnectionError, BrokenPipeError):
                        print("Соединение разорвано при отправке файла")
                        return

            print(f"Файл {filename} отправлен клиенту ({file_size} байт)")

        except Exception as e:
            print(f"Ошибка отправки файла: {e}")
            try:
                self.send_response(client_socket, {'status': 'error', 'message': str(e)})
            except:
                pass

    def receive_file(self, client_socket, command):
        try:
            filename = command['filename']
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

                    except (ConnectionError, socket.timeout, struct.error) as e:
                        print(f"Ошибка приема чанка: {e}")
                        raise

            if received == file_size:
                md5_hash = hashlib.md5()
                with open(filepath, 'rb') as f:
                    for chunk in iter(lambda: f.read(8192), b''):
                        md5_hash.update(chunk)

                self.send_response(client_socket, {'status': 'success', 'message': 'Файл загружен', 'md5': md5_hash.hexdigest()})
                print(f"Файл {filename} успешно загружен ({file_size} байт)")
            else:
                if filepath.exists():
                    os.remove(filepath)
                self.send_response(client_socket, {'status': 'error', 'message': f'Неполная загрузка файла: получено {received} из {file_size} байт'})

        except Exception as e:
            filename = command['filename']
            filepath = self.upload_dir / filename
            if 'filepath' in locals() and filepath.exists():
                try:
                    os.remove(filepath)
                except:
                    pass
            print(f"Ошибка приема файла: {e}")
            try:
                self.send_response(client_socket, {'status': 'error', 'message': str(e)})
            except:
                pass

    def delete_file(self, client_socket, command):
        try:
            filename = command['filename']
            filepath = self.upload_dir / filename

            if filepath.exists():
                filepath.unlink()
                self.send_response(client_socket, {'status': 'success', 'message': 'Файл удален'})
                print(f"Файл {filename} удалён с сервера")
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

            info = {'status': 'success', 'info': {'upload_dir': str(self.upload_dir.absolute()), 'total_files': total_files, 'total_size': total_size}}
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
    SERVER_HOST = '0.0.0.0'
    SERVER_PORT = int(input("Введите порт для открытия сервера: "))
    UPLOAD_DIR = 'server_files'

    server = FileServer(SERVER_HOST, SERVER_PORT, UPLOAD_DIR)
    server.start()
