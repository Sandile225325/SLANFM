import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog
import threading
from client import FileClient
import os
from PIL import Image, ImageTk
import time
import json
import queue
from pathlib import Path
import sys
import shutil


class FileManagerGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("SLANFM")
        self.root.geometry("850x600")
        self.root.minsize(800, 550)

        self.client = None
        self.server_files = []
        self.progress_queue = queue.Queue()
        self.user_response_queue = queue.Queue()
        self.current_operation = None
        self.operation_in_progress = False
        self.connect_operation = False
        self.connected = False
        self.ip = None
        self.port = None

        self.version = '1.8.1'

        self.config_file = "client_config.json"
        self.config = self.load_config()

        self.sort_reverse = True
        self.sort_column = 'modified'

        self.progress_var = tk.DoubleVar()
        self.progress_var.set(0)
        self.status_text = tk.StringVar()
        self.status_text.set("Не подключено")

        self.create_widgets()
        self.start_progress_monitor()

        self.download_dir = self.resolve_download_dir()
        self.download_dir.mkdir(parents=True, exist_ok=True)

        self.files_tree.bind('<<TreeviewSelect>>', self.on_file_selection_changed)

        self.total_size = 0
        self.total_number = 0

        self.change_buttons_state('normal', force=True)

        self.cleanup_stale_parts()

        self.socket_lock = threading.Lock()
        self.ping_stop = threading.Event()
        self.ping_backoff = 0.0

        self.ping_thread = threading.Thread(target=self.ping_loop, daemon=True)
        self.ping_thread.start()

        self.ping_suspended = False

        self.ping_backoff_max = 300.0

    def start_progress_monitor(self):
        self.check_progress_queue()
        self.root.after(100, self.start_progress_monitor)

    def check_progress_queue(self):
        try:
            while True:
                message = self.progress_queue.get_nowait()
                if isinstance(message, dict):
                    if 'percent' in message:
                        self.progress_var.set(message['percent'])
                    if 'status' in message:
                        self.status_text.set(message['status'])
                    if 'ask_overwrite' in message:
                        filename = message['ask_overwrite']
                        answer = messagebox.askyesno(
                            "Файл существует",
                            f'Файл "{filename}" уже существует на сервере.\nПерезаписать?'
                        )
                        self.user_response_queue.put('yes' if answer else 'no')
                    if 'ask_overwrite_local' in message:
                        filepath = message['ask_overwrite_local']
                        answer = messagebox.askyesno(
                            "Файл существует",
                            f'У вас уже существует файл "{filepath}".\nПерезаписать?'
                        )
                        self.user_response_queue.put('yes' if answer else 'no')
                elif isinstance(message, str):
                    self.status_text.set(message)
        except queue.Empty:
            pass

    def ping_loop(self):
        while not self.ping_stop.is_set():
            result = None
            try:
                if self.ping_enabled and self.connected and not self.ping_suspended:
                    if self.socket_lock.acquire(blocking=False):
                        try:
                            if self.client and self.connected:
                                result = self.client.ping()
                        except Exception:
                            result = {'error': 'Ошибка пинга'}
                        finally:
                            self.socket_lock.release()
            except Exception:
                result = {'error': 'Ошибка пинга'}

            if result is not None:
                self.handle_ping_result(result)

            sleep_time = max(self.ping_interval, self.ping_backoff)
            end_time = time.time() + sleep_time
            while time.time() < end_time:
                if self.ping_stop.is_set():
                    return
                time.sleep(min(0.1, max(0.0, end_time - time.time())))

    def handle_ping_result(self, result):
        if self.ping_suspended:
            return
        
        if result.get('error'):
            try:
                retry = float(result.get('retry_after') or 0)
            except (TypeError, ValueError):
                retry = 0.0
            if retry > 0:
                self.ping_backoff = min(max(retry, self.ping_interval), self.ping_backoff_max)
            else:
                self.ping_backoff = 0.0
            self.root.after(0, lambda: self.change_status_bar('rtt', 'ошибка'))
            return

        self.ping_backoff = 0.0
        rtt_ms = result['rtt'] * 1000.0
        label = f"{rtt_ms:.0f} мс"
        self.root.after(0, lambda v=label: self.change_status_bar('rtt', v))

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
            "connect_config": {"default_port": "6666"},
            "input_save_config": {"ip": ""},
            "paths_config": {"download_dir": "downloads"},
            "status_bar_config": {"status_bar": True, "show_connection": True, "show_rtt": True},
            "ping_config": {"ping_enabled": True, "ping_interval": 5.0}
        }

        try:
            config_path = self.config_path()
            with open(config_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except FileNotFoundError:
            messagebox.showwarning("Внимание", f"Файл {self.config_file} не найден")
            return default_values
        except json.JSONDecodeError:
            messagebox.showerror("Ошибка", "Некорректный формат JSON файла")
            return default_values
        except Exception as e:
            messagebox.showerror("Ошибка", f"Ошибка загрузки конфига: {e}")
            return default_values

    def resolve_download_dir(self):
        raw = self.config.get("paths_config", {}).get("download_dir", "downloads")
        path = Path(raw)
        if not path.is_absolute():
            if getattr(sys, 'frozen', False):
                base = Path(sys.executable).parent
            else:
                base = Path.cwd()
            path = base / path
        return path.resolve()

    def create_widgets(self):
        connect_frame = ttk.LabelFrame(self.root, text="Подключение к серверу", padding=10)
        connect_frame.pack(fill="x", padx=10, pady=5)

        ttk.Label(connect_frame, text="IP сервера:").grid(row=0, column=0, padx=5)
        self.server_ip = ttk.Entry(connect_frame, width=20)
        self.server_ip.grid(row=0, column=1, padx=5)
        input_cfg = self.config.get("input_save_config", {})
        saved_ip = input_cfg.get("ip", "")
        saved_port = input_cfg.get("port", "")
        if saved_ip:
            if saved_port not in (None, ""):
                self.server_ip.insert(0, f"{saved_ip}:{saved_port}")
            else:
                self.server_ip.insert(0, saved_ip)

        self.connect_button = ttk.Button(connect_frame, text="Подключиться",
                                         command=self.connect_server)
        self.connect_button.grid(row=0, column=2, padx=5)

        self.disconnect_button = ttk.Button(connect_frame, text="Отключиться",
                                            command=self.disconnect_server)
        self.disconnect_button.grid(row=0, column=3, padx=5)

        files_frame = ttk.LabelFrame(self.root, text="Файлы на сервере", padding=10)
        files_frame.pack(fill="both", expand=True, padx=10, pady=5)

        columns = ('name', 'size', 'modified')
        self.files_tree = ttk.Treeview(files_frame, columns=columns, show='headings')

        self.files_tree.heading('name', text='Имя файла')
        self.files_tree.heading('size', text='Размер')
        self.files_tree.heading('modified', text='Изменен')

        self.files_tree.column('name', width=300)
        self.files_tree.column('size', width=100)
        self.files_tree.column('modified', width=150)

        scrollbar = ttk.Scrollbar(files_frame, orient=tk.VERTICAL, command=self.files_tree.yview)
        self.files_tree.configure(yscrollcommand=scrollbar.set)

        self.files_tree.pack(side=tk.LEFT, fill="both", expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        button_frame = ttk.Frame(self.root)
        button_frame.pack(fill="x", padx=10, pady=5)

        self.refresh_button = ttk.Button(button_frame, text="Обновить список",
                                         command=self.refresh_files)
        self.refresh_button.pack(side=tk.LEFT, padx=5)

        self.upload_button = ttk.Button(button_frame, text="Загрузить на сервер",
                                        command=self.upload_file)
        self.upload_button.pack(side=tk.LEFT, padx=5)

        self.download_button = ttk.Button(button_frame, text="Скачать с сервера",
                                          command=self.download_file)
        self.download_button.pack(side=tk.LEFT, padx=5)

        self.delete_button = ttk.Button(button_frame, text="Удалить с сервера",
                                        command=self.delete_file)
        self.delete_button.pack(side=tk.LEFT, padx=5)

        self.about_button = ttk.Button(button_frame, text="?", width=3,
                                       command=self.show_about)
        self.about_button.pack(side=tk.RIGHT, padx=5)

        progress_frame = ttk.Frame(self.root)
        progress_frame.pack(fill="x", padx=10, pady=1)

        self.progress_bar = ttk.Progressbar(progress_frame,
                                            variable=self.progress_var,
                                            maximum=100,
                                            mode='determinate')
        self.progress_bar.pack(fill="x", pady=5)

        bar_cfg = self.config.get("status_bar_config", {})
        self.show_connection = bool(bar_cfg.get("show_connection", True))
        self.show_rtt = bool(bar_cfg.get("show_rtt", True))
        self.status_bar_enabled = (
            bool(bar_cfg.get("status_bar", True))
            and (self.show_connection or self.show_rtt)
        )

        ping_cfg = self.config.get("ping_config", {})
        self.ping_enabled = bool(ping_cfg.get("ping_enabled", True))
        try:
            self.ping_interval = max(1.0, float(ping_cfg.get("ping_interval", 5.0)))
        except (TypeError, ValueError):
            self.ping_interval = 5.0
        if not self.show_rtt:
            self.ping_enabled = False

        self.conn_status_value = "Не подключено"
        self.rtt_status_value = None

        self.status_var = tk.StringVar()
        self.status_bar = None
        if self.status_bar_enabled:
            self.status_bar = ttk.Label(self.root, textvariable=self.status_var, relief=tk.SUNKEN)
            self.status_bar.pack(side=tk.BOTTOM, fill=tk.X)

        self.render_status_bar()

        try:
            img = Image.open(self.resource_path('icon.png'))
            photo = ImageTk.PhotoImage(img)
            root.iconphoto(True, photo)
        except Exception:
            try:
                root.iconbitmap(self.resource_path('icon.ico'))
            except:
                pass

        self.server_ip.bind('<Control-c>', self.copy_to_clipboard)
        self.server_ip.bind('<Control-v>', self.paste_from_clipboard)
        self.server_ip.bind('<Control-x>', self.cut_to_clipboard)

        self.server_ip.bind('<Return>', self.connect_server_keyboard)
        self.server_ip.bind('<KP_Enter>', self.connect_server_keyboard)

    def change_status_bar(self, kind, value):
        if kind == 'connection':
            self.conn_status_value = value
        elif kind == 'rtt':
            if self.ping_suspended and value is not None:
                return
            self.rtt_status_value = value
        else:
            return
        self.render_status_bar()

    def render_status_bar(self):
        if not getattr(self, 'status_var', None):
            return
        parts = []
        if self.show_connection and self.conn_status_value:
            parts.append(str(self.conn_status_value))
        if self.show_rtt and self.rtt_status_value and not self.ping_suspended:
            parts.append(f"Пинг: {self.rtt_status_value}")
        try:
            self.status_var.set("; ".join(parts))
        except Exception:
            pass

    def sort_treeview(self, column, reverse=None):
        if reverse is None:
            self.sort_reverse = not self.sort_reverse
        else:
            self.sort_reverse = reverse

        self.sort_column = column

        items = [(self.files_tree.set(item, column), item) for item in self.files_tree.get_children('')]

        if column == 'name':
            items.sort(key=lambda x: x[0].lower(), reverse=self.sort_reverse)
        elif column == 'size':
            items.sort(key=lambda x: float(x[0].split()[0]) if x[0] and 'MB' in x[0] else 0,
                       reverse=self.sort_reverse)
        elif column == 'modified':
            def parse_date(date_str):
                try:
                    if date_str and date_str != "неизвестно":
                        parts = date_str.split()
                        if len(parts) >= 2:
                            date_part = parts[0]
                            time_part = parts[1] if len(parts) > 1 else "00:00"
                            day, month, year = date_part.split('.')
                            return time.mktime(time.strptime(f"{year}-{month}-{day} {time_part}", "%Y-%m-%d %H:%M"))
                except:
                    return 0
                return 0

            items.sort(key=lambda x: parse_date(x[0]), reverse=self.sort_reverse)

        for index, (_, item) in enumerate(items):
            self.files_tree.move(item, '', index)

        for col in ['name', 'size', 'modified']:
            heading = self.files_tree.heading(col)
            text = heading['text']
            if text.startswith('▲ ') or text.startswith('▼ '):
                text = text[2:]

            if col == column:
                arrow = '▼' if self.sort_reverse else '▲'
                heading['text'] = f'{arrow} {text}'
            else:
                heading['text'] = text

    def cleanup_stale_parts(self):
        try:
            for f in self.download_dir.glob('.*.slanfm.part'):
                if f.is_file():
                    try:
                        f.unlink()
                    except OSError:
                        pass
        except OSError:
            pass

    def authenticate(self, ip, port, explicit_port=False):
        token = simpledialog.askstring("Аутентификация", "Введите пароль:", parent=self.root)
        if token:

            def auth_thread():
                self.change_buttons_state('disabled')
                try:

                    with self.socket_lock:
                        result = self.client.authenticate(token.strip())

                    if result is True:
                        self.progress_queue.put({'status': f'Подключено к {ip}:{port}'})
                        self.root.after(0, lambda: messagebox.showinfo("Успех", f"Успешно подключено к серверу {ip}:{port}"))
                        self.change_status_bar('connection', f"Подключено к {ip}:{port}")
                        self.save_connection_info(ip, port, explicit_port)
                        self.connected = True
                        self.ip, self.port = ip, port
                        self.root.after(0, self.refresh_files(True))

                    elif isinstance(result, dict):
                        ra = int(result.get('retry_after', 0))
                        error_msg = result.get('error', 'Ошибка аутентификации')
                        self.progress_queue.put({'status': f'Заблокировано на {ra} с.'})
                        if ra == 0:
                            self.root.after(0, lambda msg=error_msg: messagebox.showerror("Ошибка", f"{msg}"))
                        else:
                            self.root.after(0, lambda msg=error_msg, r=ra: messagebox.showerror("Ошибка", f"{msg}\nПовторите через {r} с."))
                        self.disconnect_server()

                    else:
                        self.root.after(0, lambda: messagebox.showerror("Ошибка", "Неверный пароль"))
                        self.disconnect_server()

                except Exception as e:
                    error_msg = str(e)
                    self.progress_queue.put({'status': f'Ошибка: {error_msg}'})
                    self.root.after(0, lambda msg=error_msg: messagebox.showerror("Ошибка", f"Ошибка аутентификации: {msg}"))
                    self.disconnect_server()

                finally:
                    self.operation_in_progress = False
                    self.connect_operation = False
                    self.ping_suspended = False
                    self.change_buttons_state('normal')

            threading.Thread(target=auth_thread, daemon=True).start()

        else:
            with self.socket_lock:
                if self.client:
                    try:
                        self.client.disconnect()
                    except Exception:
                        pass
                self.client = None
                self.connected = False
            self.ip = None
            self.port = None
            self.clear_files_list()
            self.status_text.set("Отключено")
            self.change_status_bar('connection', "Отключено")
            self.operation_in_progress = False
            self.connect_operation = False
            self.ping_suspended = False
            self.change_buttons_state('normal', force=True)
            self.reset_progress(True)

    def connect_server(self):
        if self.operation_in_progress:
            messagebox.showwarning("Внимание", "Дождитесь завершения текущей операции")
            return

        user_input = self.server_ip.get().strip()
        if not user_input:
            messagebox.showwarning("Внимание", "Введите IP сервера")
            return

        default_port_str = self.config.get("connect_config", {}).get("default_port", "6666")
        try:
            default_port = int(default_port_str)
        except ValueError:
            messagebox.showwarning("Внимание", f"Некорректный порт в конфигурации: {default_port_str}")
            return

        self.ping_suspended = True
        self.change_status_bar('rtt', None)

        self.clear_files_list()
        self.reset_progress(True)

        self.status_text.set("Не подключено")
        self.change_status_bar('connection', "Не подключено")

        ip = user_input
        port = default_port
        explicit_port = False

        if user_input.startswith('['):
            end = user_input.find(']')
            if end == -1:
                messagebox.showwarning("Внимание", "Некорректный адрес. Формат: [IPv6]:порт")
                return

            ip = user_input[1:end]
            rest = user_input[end + 1:]

            if rest.startswith(':'):
                port_str = rest[1:].strip()
                if not port_str:
                    messagebox.showwarning("Внимание", "После ':' укажите порт")
                    return
                try:
                    port = int(port_str)
                except ValueError:
                    messagebox.showwarning("Внимание", f"Некорректный порт: {port_str}")
                    return
                explicit_port = True
            elif rest:
                messagebox.showwarning("Внимание", "Некорректный адрес. Формат: [IPv6]:порт")
                return

        elif user_input.count(':') == 1:
            host_part, port_str = user_input.rsplit(':', 1)
            if not host_part or not port_str:
                messagebox.showwarning("Внимание", "Некорректный адрес. Формат: IP:порт")
                return

            try:
                port = int(port_str)
            except ValueError:
                messagebox.showwarning("Внимание", f"Некорректный порт: {port_str}")
                return

            ip = host_part
            explicit_port = True

        if not (1 <= port <= 65535):
            messagebox.showwarning("Внимание", f"Порт должен быть в диапазоне 1..65535")
            return

        with self.socket_lock:
            if self.client:
                try:
                    self.client.disconnect()
                except Exception:
                    pass
                self.client = None
                self.connected = False
        self.ping_backoff = 0.0

        self.client = FileClient(ip, port, download_dir=self.download_dir)

        def connect_thread():
            self.operation_in_progress = True
            self.connect_operation = True
            self.change_buttons_state('disabled')
            success = False
            need_auth = False
            try:
                self.progress_queue.put({'status': f'Подключение к {ip}:{port}...'})

                connect_result = self.client.connect()

                if connect_result is True:
                    self.progress_queue.put({'status': f'Подключено к {ip}:{port}'})
                    self.root.after(0, lambda: messagebox.showinfo("Успех", f"Успешно подключено к серверу {ip}:{port}"))
                    self.change_status_bar('connection', f"Подключено к {ip}:{port}")
                    self.save_connection_info(ip, port, explicit_port)
                    success = True
                    self.connected = True
                    self.ip, self.port = ip, port

                elif connect_result == "need_auth":
                    need_auth = True
                    self.progress_queue.put({'status': 'Требуется аутентификация'})
                    self.root.after(0, lambda: self.authenticate(ip, port, explicit_port))

                elif isinstance(connect_result, dict):
                    ra = int(connect_result.get('retry_after', 0) or 0)
                    error_msg = connect_result.get('error', 'Заблокировано')
                    self.progress_queue.put({'status': f'Заблокировано на {ra} с.'})
                    self.client = None
                    if ra == 0:
                        self.root.after(0, lambda msg=error_msg: messagebox.showerror("Ошибка", f"{msg}"))
                    else:
                        self.root.after(0, lambda msg=error_msg, r=ra: messagebox.showerror("Ошибка", f"{msg}\nПовторите через {r} с." if r else msg))

                elif isinstance(connect_result, str):
                    self.progress_queue.put({'status': 'Ошибка подключения'})
                    self.client = None
                    self.root.after(0, lambda msg=connect_result: messagebox.showerror("Ошибка", msg))

                else:
                    self.progress_queue.put({'status': 'Ошибка подключения'})
                    self.client = None
                    self.root.after(0, lambda: messagebox.showerror("Ошибка", "Ошибка подключения"))

            except Exception as e:
                error_msg = str(e)
                self.progress_queue.put({'status': f'Ошибка: {error_msg}'})
                self.client = None
                self.root.after(0, lambda msg=error_msg: messagebox.showerror("Ошибка", f"Ошибка: {msg}"))

            finally:
                self.operation_in_progress = False
                self.connect_operation = False
                self.change_buttons_state('normal')
                if not need_auth:
                    self.ping_suspended = False
                if success:
                    self.root.after(0, self.refresh_files(True))

        threading.Thread(target=connect_thread, daemon=True).start()

    def disconnect_server(self):
        if self.operation_in_progress:
            messagebox.showwarning("Внимание", "Дождитесь завершения текущей операции")
            return

        if self.client:
            self.connected = False
            with self.socket_lock:
                try:
                    self.client.disconnect()
                except Exception:
                    pass
                self.client = None
            self.status_text.set("Отключено")
            self.change_status_bar('connection', "Отключено")
            self.change_status_bar('rtt', None)
            self.clear_files_list()
            self.total_size = 0
            self.total_number = 0
            self.ip = None
            self.port = None
            self.progress_var.set(0)
            self.ping_backoff = 0.0

        self.change_buttons_state('normal', force=True)
        self.ping_suspended = False

    def enough_disk_space(self, path, required_space):
        try:
            usage = shutil.disk_usage(path)
            if usage.free < required_space * 1.1:
                return False
            return True

        except Exception:
            self.root.after(0, lambda: messagebox.showerror("Ошибка", "Не удалось проверить количество свободного места"))
            return True

    def change_buttons_state(self, state, force=False):
        if not force and self.operation_in_progress and state == 'normal':
            return
        try:
            self.refresh_button.config(state=state)
            self.upload_button.config(state=state)
            self.download_button.config(state=state)
            self.delete_button.config(state=state)
            self.connect_button.config(state=state)
            self.disconnect_button.config(state=state)

        except Exception as e:
            error_msg = str(e)
            try:
                self.root.after(0, lambda msg=error_msg: messagebox.showwarning("Предупреждение", msg))
            except Exception:
                pass

    def refresh_files(self, dont_reset_progress=False):
        if not self.client:
            messagebox.showwarning("Предупреждение", "Сначала подключитесь к серверу")
            return

        if self.operation_in_progress:
            messagebox.showwarning("Внимание", "Дождитесь завершения текущей операции")
            return

        def refresh_thread():
            self.operation_in_progress = True
            self.change_buttons_state('disabled')
            try:
                self.progress_queue.put({'status': 'Получение списка файлов...'})

                with self.socket_lock:
                    self.client.send_command({'command': 'list'})
                    response = self.client.receive_response()

                if response and response.get('status') == 'success':
                    self.server_files = response.get('files', [])
                    self.root.after(0, self.update_files_list)
                    self.progress_queue.put({'status': 'Список файлов обновлен'})
                    self.total_size = 0
                    self.total_number = 0
                    if not dont_reset_progress:
                        self.reset_progress(immediate=True)
                else:
                    error_msg = response.get('message', 'Неизвестная ошибка') if response else 'Нет ответа от сервера'
                    self.progress_queue.put({'status': f'Ошибка: {error_msg}'})
                    self.root.after(0, lambda msg=error_msg: messagebox.showerror("Ошибка", f"Не удалось получить список файлов: {msg}"))

            except Exception as e:
                error_msg = str(e)
                self.progress_queue.put({'status': f'Ошибка: {error_msg}'})
                self.root.after(0, lambda msg=error_msg: messagebox.showerror("Ошибка", f"Ошибка при получении списка файлов: {msg}"))

            finally:
                self.operation_in_progress = False
                self.change_buttons_state('normal')

        threading.Thread(target=refresh_thread, daemon=True).start()

    def update_files_list(self):
        for item in self.files_tree.get_children():
            self.files_tree.delete(item)

        self.total_number = 0
        self.total_size = 0

        for file in self.server_files:
            modified_value = file.get('modified', '')
            modified_str = ''

            if isinstance(modified_value, (int, float)):
                try:
                    modified_str = time.strftime("%d.%m.%Y %H:%M",
                                                 time.localtime(float(modified_value)))
                except:
                    modified_str = str(modified_value)
            elif isinstance(modified_value, str) and modified_value:
                modified_str = modified_value
            else:
                modified_str = "неизвестно"

            size_mb = file['size'] / (1024 * 1024)
            self.files_tree.insert('', tk.END,
                                   values=(file['name'], f"{size_mb:.2f} MB", modified_str))

            self.total_number += 1
            self.total_size += size_mb

        self.sort_treeview(self.sort_column, self.sort_reverse)

    def clear_files_list(self):
        for item in self.files_tree.get_children():
            self.files_tree.delete(item)

    def upload_file(self):
        if not self.client:
            messagebox.showwarning("Предупреждение", "Сначала подключитесь к серверу")
            return

        if self.operation_in_progress:
            messagebox.showwarning("Внимание", "Дождитесь завершения текущей операции")
            return

        filepath = filedialog.askopenfilename(title="Выберите файл для загрузки")
        if not filepath:
            return

        try:
            file_size = os.path.getsize(filepath)
            if file_size > self.client.max_file_size:
                messagebox.showerror("Ошибка", f"Файл слишком большой")
                return
        except Exception as e:
            messagebox.showerror("Ошибка", f"Не удалось проверить размер файла: {e}")
            return

        def upload_thread():
            self.operation_in_progress = True
            self.change_buttons_state('disabled')
            operation_success = False
            try:
                self.progress_queue.put({'status': 'Проверка наличия файла на сервере...'})
                with self.socket_lock:
                    self.client.send_command({'command': 'list'})
                    response = self.client.receive_response()
                if response and response.get('status') == 'success':
                    server_files = response.get('files', [])
                    filename = os.path.basename(filepath)
                    file_exists = any(f['name'] == filename for f in server_files)
                    if file_exists:
                        self.progress_queue.put({'ask_overwrite': filename})
                        answer = self.user_response_queue.get()
                        if answer != 'yes':
                            self.progress_queue.put({'status': 'Загрузка отменена'})
                            return
                else:
                    error_msg = response.get('message', 'Неизвестная ошибка') if response else 'Нет ответа от сервера'
                    self.progress_queue.put({'status': f'Ошибка получения списка файлов: {error_msg}'})
                    self.root.after(0, lambda: messagebox.showerror("Ошибка", f"Не удалось проверить наличие файла: {error_msg}"))
                    return

                self.progress_queue.put({'status': f'Загрузка файла {os.path.basename(filepath)}...', 'percent': 0})

                def update_progress(percent):
                    self.progress_queue.put({'percent': percent, 'status': f'Загрузка: {percent:.1f}%'})

                with self.socket_lock:
                    result = self.client.upload_file(filepath, update_progress)

                if result is True:
                    self.progress_queue.put({'percent': 100, 'status': 'Файл успешно загружен'})
                    self.root.after(0, lambda: messagebox.showinfo("Успех", "Файл успешно загружен на сервер"))
                    operation_success = True
                else:
                    self.progress_queue.put({'status': f'Ошибка: {result}'})
                    self.root.after(0, lambda msg=result: messagebox.showerror("Ошибка", f"Не удалось загрузить файл:\n{msg}"))

            except Exception as e:
                error_msg = str(e)
                self.progress_queue.put({'status': f'Ошибка: {error_msg}'})
                self.root.after(0, lambda msg=error_msg: messagebox.showerror("Ошибка", f"Ошибка загрузки: {msg}"))

            finally:
                self.operation_in_progress = False
                self.change_buttons_state('normal')
                if operation_success:
                    self.root.after(0, self.refresh_files(True))

        threading.Thread(target=upload_thread, daemon=True).start()

    def download_file(self):
        if not self.client:
            messagebox.showwarning("Предупреждение", "Сначала подключитесь к серверу")
            return

        if self.operation_in_progress:
            messagebox.showwarning("Внимание", "Дождитесь завершения текущей операции")
            return

        selected = self.files_tree.selection()
        if not selected:
            messagebox.showwarning("Предупреждение", "Выберите файл для скачивания")
            return

        item = self.files_tree.item(selected[0])
        filename = item['values'][0]

        def download_thread():
            self.operation_in_progress = True
            self.change_buttons_state('disabled')
            operation_success = False
            try:
                filename = item['values'][0]
                save_path = self.download_dir / os.path.basename(filename)
                temp_path = save_path.with_name(f'.{save_path.name}.slanfm.part')

                if temp_path.exists():
                    try:
                        temp_path.unlink()
                    except OSError:
                        pass

                file_info = next((f for f in self.server_files if f['name'] == filename), None)
                required_space = file_info['size'] if file_info else 0

                if required_space and not self.enough_disk_space(str(self.download_dir), required_space):
                    self.progress_queue.put({'status': 'Недостаточно места на диске'})
                    self.root.after(0, lambda: messagebox.showerror("Ошибка", "Недостаточно места для сохранения файла"))
                    return

                if save_path.exists():
                    self.progress_queue.put({'ask_overwrite_local': str(save_path)})
                    answer = self.user_response_queue.get()
                    if answer != 'yes':
                        self.progress_queue.put({'status': 'Скачивание отменено'})
                        return

                self.progress_queue.put({'status': f'Скачивание файла {filename}...', 'percent': 0})

                def update_progress(percent):
                    self.progress_queue.put({'percent': percent, 'status': f'Скачивание: {percent:.1f}%'})

                with self.socket_lock:
                    result = self.client.download_file(filename, save_path, update_progress)

                if result is True:
                    self.progress_queue.put({'percent': 100, 'status': 'Файл успешно скачан'})
                    self.root.after(0, lambda f=filename, d=str(self.download_dir):
                    messagebox.showinfo("Успех", f"Файл {f} успешно скачан в папку {d}"))
                    operation_success = True
                else:
                    self.progress_queue.put({'status': f'Ошибка: {result}'})
                    self.root.after(0, lambda msg=result, f=filename: messagebox.showerror("Ошибка", f"Не удалось скачать файл {f}:\n{msg}"))

            except Exception as e:
                error_msg = str(e)
                self.progress_queue.put({'status': f'Ошибка: {error_msg}'})
                self.root.after(0, lambda msg=error_msg:
                messagebox.showerror("Ошибка", f"Ошибка скачивания: {msg}"))

            finally:
                self.operation_in_progress = False
                self.change_buttons_state('normal')
                if operation_success:
                    self.root.after(0, self.refresh_files(True))

        threading.Thread(target=download_thread, daemon=True).start()

    def delete_file(self):
        if not self.client:
            messagebox.showwarning("Предупреждение", "Сначала подключитесь к серверу")
            return

        if self.operation_in_progress:
            messagebox.showwarning("Внимание", "Дождитесь завершения текущей операции")
            return

        selected = self.files_tree.selection()
        if not selected:
            messagebox.showwarning("Предупреждение", "Выберите файл для удаления")
            return

        item = self.files_tree.item(selected[0])
        filename = item['values'][0]

        if not messagebox.askyesno("Подтверждение", f"Вы уверены, что хотите удалить файл '{filename}' с сервера?"):
            return

        def delete_thread():
            self.operation_in_progress = True
            self.change_buttons_state('disabled')
            operation_success = False
            try:
                self.progress_queue.put({'status': f'Удаление файла {filename}...'})

                with self.socket_lock:
                    delete_result = self.client.delete_file(filename)

                if isinstance(delete_result, str):
                    self.progress_queue.put({'status': f'Ошибка: {delete_result}'})
                    msg = delete_result + ". Хост сервера запретил удалять файлы" if delete_result == "Недостаточно прав" else delete_result
                    self.root.after(0, lambda: messagebox.showerror("Ошибка", f"Ошибка удаления: {msg}"))

                else:
                    self.progress_queue.put({'status': 'Файл успешно удален'})
                    self.root.after(0, lambda: messagebox.showinfo("Успех", "Файл успешно удален с сервера"))
                    operation_success = True

            except Exception as e:
                error_msg = str(e)
                self.progress_queue.put({'status': f'Ошибка: {error_msg}'})
                self.root.after(0, lambda msg=error_msg:
                messagebox.showerror("Ошибка", f"Ошибка удаления: {msg}"))
            finally:
                self.operation_in_progress = False
                self.change_buttons_state('normal')
                if operation_success:
                    self.root.after(0, self.refresh_files(True))

        threading.Thread(target=delete_thread, daemon=True).start()

    def reset_progress(self, immediate=False):
        if immediate:
            self.progress_var.set(0)
            self.status_text.set("Готово")
        else:
            self.root.after(500, self._delayed_reset_progress)

    def _delayed_reset_progress(self):
        if not self.operation_in_progress and self.progress_var.get() == 0:
            self.progress_var.set(0)
            self.status_text.set("Готово")

    def save_config_value(self, key_path, value):
        try:
            config_path = self.config_path()
            try:
                with open(config_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
            except (FileNotFoundError, json.JSONDecodeError):
                data = {}

            keys = key_path.split('.')
            current = data
            for k in keys[:-1]:
                if k not in current or not isinstance(current[k], dict):
                    current[k] = {}
                current = current[k]
            current[keys[-1]] = value

            with open(config_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)

            self.config = data

        except Exception:
            return
        
    def save_connection_info(self, ip, port, explicit_port=True):
        self.save_config_value("input_save_config.ip", str(ip))
        if explicit_port:
            self.save_config_value("input_save_config.port", str(port))
        else:
            self.save_config_value("input_save_config.port", "")

    def show_about(self):

        def format_size(size_bytes):
            if size_bytes < 1024:
                return size_bytes, 'B'
            elif size_bytes < 1024 ** 2:
                return size_bytes // 1024, 'KB'
            elif size_bytes < 1024 ** 3:
                return size_bytes // (1024 ** 2), 'MB'
            elif size_bytes < 1024 ** 4:
                return size_bytes // (1024 ** 3), 'GB'
            else:
                return size_bytes // (1024 ** 4), 'TB'

        def format_total_size(size):
            if size > 1024:
                return round(size / 1024, 2), 'GB'
            else:
                return round(size), 'MB'

        def format_range_value(value):
            if value < 1024:
                return f"{value} B"
            elif value < 1024 ** 2:
                return f"{value // 1024} KB"
            elif value < 1024 ** 3:
                return f"{value // (1024 ** 2)} MB"
            elif value < 1024 ** 4:
                return f"{value // (1024 ** 3)} GB"
            else:
                return f"{value // (1024 ** 4)} TB"

        if self.connected:
            self.update_files_list()

            total_size, ts_unit = format_total_size(self.total_size)
            max_file_size, mfs_unit = format_size(self.client.max_file_size)
            chunk_size, chs_unit = format_size(self.client.chunk_size)

            about_text = f"""
            SLANFM

            Версия: {self.version}

            Подключено к {self.ip}:{self.port}

            Количество файлов на сервере: {self.total_number}
            Общий размер файлов на сервере: {total_size} {ts_unit}

            Путь для сохранения файлов: 
            {self.download_dir}

            Максимальный размер файла: {max_file_size} {mfs_unit}
            Размер чанка: {chunk_size} {chs_unit}
            Таймаут: {self.client.timeout} с
            """

        else:
            cfg = getattr(self, 'config', {}).get('values_config', {})
            chunk_min, chunk_max = cfg.get('chunk_size_range', [1024, 10485760])
            timeout_min, timeout_max = cfg.get('timeout_range', [1, 300])

            chunk_min_formatted = format_range_value(chunk_min)
            chunk_max_formatted = format_range_value(chunk_max)

            about_text = f"""
            SLANFM

            Версия: {self.version}

            Нет подключения к серверу

            Путь для сохранения файлов: 
            • {self.download_dir}

            Разрешённый диапазон размера чанка:
            • Минимум: {chunk_min_formatted}
            • Максимум: {chunk_max_formatted}

            Разрешённый диапазон таймаута:
            • Минимум: {timeout_min} с
            • Максимум: {timeout_max} с
            """

        messagebox.showinfo("Информация", about_text)

    def copy_to_clipboard(self, event):
        try:
            selected_text = self.server_ip.selection_get()
        except tk.TclError:
            return "break"
        self.root.clipboard_clear()
        self.root.clipboard_append(selected_text)
        return "break"

    def paste_from_clipboard(self, event):
        try:
            clipboard_text = self.root.clipboard_get()
        except tk.TclError:
            return "break"
        self.server_ip.insert(tk.INSERT, clipboard_text)
        return "break"

    def cut_to_clipboard(self, event):
        try:
            selected_text = self.server_ip.selection_get()
        except tk.TclError:
            return "break"
        self.root.clipboard_clear()
        self.root.clipboard_append(selected_text)
        self.server_ip.delete(tk.SEL_FIRST, tk.SEL_LAST)
        return "break"

    def on_file_selection_changed(self, event):
        self.reset_progress(immediate=True)

    def connect_server_keyboard(self, event):
        self.connect_server()


if __name__ == "__main__":
    root = tk.Tk()
    app = FileManagerGUI(root)
    root.mainloop()