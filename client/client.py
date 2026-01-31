import socket
import json
import os
import hashlib
from pathlib import Path
import struct


class FileClient:
    def __init__(self, server_host, server_port):
        self.server_host = server_host
        self.server_port = server_port
        self.socket = None
        self.download_dir = Path('downloads')
        self.download_dir.mkdir(exist_ok=True)
        self.chunk_size = 65536
        self.timeout = 120

    def connect(self):
        try:
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self.socket.settimeout(self.timeout)
            self.socket.connect((self.server_host, self.server_port))
            return True
        except Exception:
            return False

    def download_file(self, filename, save_path=None, progress_callback=None):
        if not save_path:
            save_path = self.download_dir / filename

        self.send_command({'command': 'download', 'filename': filename})

        response = self.receive_response()

        if not response or response.get('status') != 'success':
            return False

        file_size = response['size']
        server_md5 = response.get('md5', '')

        self.send_command({'status': 'ready'})

        received = 0
        with open(save_path, 'wb') as f:
            while received < file_size:
                try:
                    chunk_size_data = self.receive_all(4)
                    if not chunk_size_data or len(chunk_size_data) != 4:
                        break

                    chunk_size = struct.unpack('>I', chunk_size_data)[0]

                    chunk = self.receive_all(chunk_size)
                    if not chunk or len(chunk) != chunk_size:
                        break

                    f.write(chunk)
                    received += len(chunk)

                    if progress_callback and file_size > 0:
                        percent = (received / file_size) * 100
                        progress_callback(percent)

                    if file_size > 0:
                        percent = (received / file_size) * 100

                except socket.timeout:
                    break
                except Exception:
                    break

        if received == file_size:
            if server_md5:
                md5_hash = hashlib.md5()
                with open(save_path, 'rb') as f:
                    for chunk in iter(lambda: f.read(8192), b''):
                        md5_hash.update(chunk)

                client_md5 = md5_hash.hexdigest()

                if client_md5 == server_md5:
                    if progress_callback:
                        progress_callback(100)
                    return True
                else:
                    if os.path.exists(save_path):
                        os.remove(save_path)
                    return False
            return True

        else:
            if os.path.exists(save_path):
                os.remove(save_path)
            return False

    def upload_file(self, filepath, progress_callback=None):
        path = Path(filepath)

        if not path.exists():
            return False

        file_size = path.stat().st_size

        md5_hash = hashlib.md5()
        with open(path, 'rb') as f:
            for chunk in iter(lambda: f.read(8192), b''):
                md5_hash.update(chunk)
        original_md5 = md5_hash.hexdigest()

        self.send_command({'command': 'upload', 'filename': path.name, 'size': file_size})

        response = self.receive_response()

        if not response:
            return False

        if response.get('status') == 'ready':

            uploaded = 0
            with open(path, 'rb') as f:
                while True:
                    chunk = f.read(self.chunk_size)
                    if not chunk:
                        break

                    chunk_size = len(chunk)
                    try:
                        self.socket.sendall(struct.pack('>I', chunk_size))
                        self.socket.sendall(chunk)
                    except (ConnectionError, BrokenPipeError):
                        return False

                    uploaded += len(chunk)

                    if progress_callback and file_size > 0:
                        percent = (uploaded / file_size) * 100
                        progress_callback(percent)

                    if file_size > 0:
                        percent = (uploaded / file_size) * 100

            response = self.receive_response()
            if response and response.get('status') == 'success':
                server_md5 = response.get('md5', '')
                if server_md5 == original_md5:
                    if progress_callback:
                        progress_callback(100)
                    return True
                else:
                    return False
            else:
                return False

        else:
            return False

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
        try:
            json_data = json.dumps(command).encode('utf-8')
            self.socket.sendall(len(json_data).to_bytes(4, 'big'))
            self.socket.sendall(json_data)
        except Exception:
            return None

    def receive_all(self, length):
        if not self.socket:
            return None

        data = b''
        while len(data) < length:
            try:
                chunk = self.socket.recv(min(4096, length - len(data)))
                if not chunk:
                    break
                data += chunk
            except socket.timeout:
                break
            except:
                break
        return data

    def receive_response(self):
        try:
            length_data = self.receive_all(4)
            if not length_data or len(length_data) != 4:
                return None

            data_length = struct.unpack('>I', length_data)[0]
            json_data = self.receive_all(data_length)

            if not json_data:
                return None

            return json.loads(json_data.decode('utf-8'))

        except Exception:
            return None

    def list_files(self):
        self.send_command({'command': 'list'})

    def delete_file(self, filename):
        self.send_command({'command': 'delete', 'filename': filename})


if __name__ == "__main__":
    SERVER_HOST = "0.0.0.0"
    SERVER_PORT = 6666

    client = FileClient(SERVER_HOST, SERVER_PORT)
