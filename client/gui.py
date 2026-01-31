import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import threading
from client import FileClient
import os
from PIL import Image, ImageTk
import time
import json
import queue
from pathlib import Path


class FileManagerGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("SLANFM")
        self.root.geometry("850x600")

        self.root.minsize(800, 550)

        self.client = None
        self.server_files = []
        self.progress_queue = queue.Queue()
        self.current_operation = None
        self.operation_in_progress = False

        self.config_file = "config.json"
        self.config = self.load_config(self.config_file)

        self.sort_reverse = True
        self.sort_column = 'modified'

        self.max_file_size = 2 * 1024 * 1024 * 1024

        self.progress_var = tk.DoubleVar()
        self.progress_var.set(0)
        self.status_text = tk.StringVar()
        self.status_text.set("Не подключено")

        self.create_widgets()
        self.start_progress_monitor()

        self.download_dir = Path('downloads')
        self.download_dir.mkdir(exist_ok=True)

        self.files_tree.bind('<<TreeviewSelect>>', self.on_file_selection_changed)

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
                elif isinstance(message, str):
                    self.status_text.set(message)
        except queue.Empty:
            pass

    def load_config(self, file):
        try:
            with open(file, 'r', encoding='utf-8') as f:
                return json.load(f)
        except FileNotFoundError:
            messagebox.showwarning("Внимание", f"Файл {self.config_file} не найден")
            return {"server_config": {"PORT": "6666"}}
        except json.JSONDecodeError:
            messagebox.showerror("Ошибка", "Некорректный формат JSON файла")
            return {"server_config": {"PORT": "6666"}}

    def create_widgets(self):
        connect_frame = ttk.LabelFrame(self.root, text="Подключение к серверу", padding=10)
        connect_frame.pack(fill="x", padx=10, pady=5)

        ttk.Label(connect_frame, text="IP сервера:").grid(row=0, column=0, padx=5)
        self.server_ip = ttk.Entry(connect_frame, width=20)
        self.server_ip.insert(0, "")
        self.server_ip.grid(row=0, column=1, padx=5)

        ttk.Button(connect_frame, text="Подключиться", command=self.connect_server).grid(row=0, column=2, padx=5)
        ttk.Button(connect_frame, text="Отключиться", command=self.disconnect_server).grid(row=0, column=3, padx=5)

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

        ttk.Button(button_frame, text="Обновить список", command=self.refresh_files).pack(side=tk.LEFT, padx=5)
        ttk.Button(button_frame, text="Загрузить на сервер", command=self.upload_file).pack(side=tk.LEFT, padx=5)
        ttk.Button(button_frame, text="Скачать с сервера", command=self.download_file).pack(side=tk.LEFT, padx=5)
        ttk.Button(button_frame, text="Удалить с сервера", command=self.delete_file).pack(side=tk.LEFT, padx=5)

        progress_frame = ttk.Frame(self.root)
        progress_frame.pack(fill="x", padx=10, pady=1)

        self.progress_bar = ttk.Progressbar(progress_frame, variable=self.progress_var, maximum=100, mode='determinate')
        self.progress_bar.pack(fill="x", pady=5)

        self.status_var = tk.StringVar()
        self.status_var.set("Не подключено")
        status_bar = ttk.Label(self.root, textvariable=self.status_var, relief=tk.SUNKEN)
        status_bar.pack(side=tk.BOTTOM, fill=tk.X)

        try:
            img = Image.open('icon.png')
            photo = ImageTk.PhotoImage(img)
            root.iconphoto(False, photo)
        except:
            try:
                root.iconbitmap('icon.ico')
            except:
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

    def connect_server(self):
        self.status_var.set("Не подключено")
        if self.operation_in_progress:
            messagebox.showwarning("Внимание", "Дождитесь завершения текущей операции")
            return

        user_input = self.server_ip.get().strip()

        ip = user_input
        if not ip:
            return

        server_config = self.config.get("server_config", {})
        port_str = server_config.get("PORT", "6666")
        try:
            port = int(port_str)
        except ValueError:
            port = 6666

        self.client = FileClient(ip, port)

        def connect_thread():
            self.operation_in_progress = True
            try:
                self.progress_queue.put({'status': f'Подключение к {ip}:{port}...'})

                if self.client.connect():
                    self.progress_queue.put({'status': f'Подключено к {ip}:{port}'})

                    self.refresh_files()

                    self.root.after(0, lambda: messagebox.showinfo("Успех", f"Успешно подключено к серверу {ip}:{port}"))
                    self.status_var.set(f"Подключено к {ip}:{port}")
                else:
                    self.progress_queue.put({'status': 'Ошибка подключения'})
                    self.client = None
            except Exception as e:
                error_msg = str(e)
                self.progress_queue.put({'status': f'Ошибка: {error_msg}'})
                self.client = None
            finally:
                self.operation_in_progress = False

        threading.Thread(target=connect_thread, daemon=True).start()

    def disconnect_server(self):
        if self.client:
            self.client.disconnect()
            self.client = None
            self.status_text.set("Отключено")
            self.status_var.set("Отключено")
            self.clear_files_list()

    def refresh_files(self):
        if not self.client:
            messagebox.showwarning("Предупреждение", "Сначала подключитесь к серверу")
            return

        def refresh_thread():
            try:
                self.progress_queue.put({'status': 'Получение списка файлов...'})

                self.client.send_command({'command': 'list'})
                response = self.client.receive_response()

                if response and response.get('status') == 'success':
                    self.server_files = response.get('files', [])
                    self.root.after(0, self.update_files_list)
                    self.progress_queue.put({'status': 'Список файлов обновлен'})
                else:
                    error_msg = response.get('message', 'Неизвестная ошибка') if response else 'Нет ответа от сервера'
                    self.progress_queue.put({'status': f'Ошибка: {error_msg}'})
                    self.root.after(0, lambda msg=error_msg: messagebox.showerror("Ошибка", f"Не удалось получить список файлов: {msg}"))
            except Exception as e:
                error_msg = str(e)
                self.progress_queue.put({'status': f'Ошибка: {error_msg}'})
                self.root.after(0, lambda msg=error_msg: messagebox.showerror("Ошибка", f"Ошибка при получении списка файлов: {msg}"))

        threading.Thread(target=refresh_thread, daemon=True).start()

    def update_files_list(self):
        for item in self.files_tree.get_children():
            self.files_tree.delete(item)

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
            if file_size > self.max_file_size:
                messagebox.showerror("Ошибка", f"Файл слишком большой (максимум {self.max_file_size / (1024 * 1024 * 1024):.1f} ГБ)")
                return
        except Exception as e:
            error_msg = str(e)
            messagebox.showerror("Ошибка", f"Не удалось проверить размер файла: {error_msg}")
            return

        def upload_thread():
            self.operation_in_progress = True
            try:
                self.progress_queue.put({'status': f'Загрузка файла {os.path.basename(filepath)}...'})
                self.progress_queue.put({'percent': 0})

                def update_progress(percent):
                    self.progress_queue.put({'percent': percent, 'status': f'Загрузка: {percent:.1f}%'})

                success = self.client.upload_file(filepath, update_progress)

                if success:
                    self.progress_queue.put({'percent': 100})
                    self.progress_queue.put({'status': 'Файл успешно загружен'})

                    self.root.after(0, lambda: self.refresh_files())

                    self.root.after(0, lambda: messagebox.showinfo("Успех", "Файл успешно загружен на сервер"))
                else:
                    self.progress_queue.put({'status': 'Ошибка загрузки файла'})
                    self.root.after(0, lambda: messagebox.showerror("Ошибка", "Не удалось загрузить файл на сервер"))

            except Exception as e:
                error_msg = str(e)
                self.progress_queue.put({'status': f'Ошибка: {error_msg}'})
                self.root.after(0, lambda msg=error_msg: messagebox.showerror("Ошибка", f"Ошибка загрузки: {msg}"))
            finally:
                self.operation_in_progress = False

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

        save_path = self.download_dir / filename

        def download_thread():
            self.operation_in_progress = True
            try:
                self.progress_queue.put({'status': f'Скачивание файла {filename}...'})
                self.progress_queue.put({'percent': 0})

                def update_progress(percent):
                    self.progress_queue.put({'percent': percent, 'status': f'Скачивание: {percent:.1f}%'})

                success = self.client.download_file(filename, save_path, update_progress)

                if success:
                    self.progress_queue.put({'percent': 100})
                    self.progress_queue.put({'status': 'Файл успешно скачан'})

                    self.root.after(0, lambda f=filename, d=str(self.download_dir): messagebox.showinfo("Успех", f"Файл {f} успешно скачан в папку {d}"))
                else:
                    self.progress_queue.put({'status': 'Ошибка скачивания файла'})
                    self.root.after(0, lambda f=filename: messagebox.showerror("Ошибка", f"Не удалось скачать файл {f}"))

            except Exception as e:
                error_msg = str(e)
                self.progress_queue.put({'status': f'Ошибка: {error_msg}'})
                self.root.after(0, lambda msg=error_msg: messagebox.showerror("Ошибка", f"Ошибка скачивания: {msg}"))

            finally:
                self.operation_in_progress = False

        threading.Thread(target=download_thread, daemon=True).start()

    def delete_file(self):
        if not self.client:
            messagebox.showwarning("Предупреждение", "Сначала подключитесь к серверу")
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
            try:
                self.progress_queue.put({'status': f'Удаление файла {filename}...'})

                self.client.delete_file(filename)

                self.progress_queue.put({'status': 'Файл успешно удален'})

                self.root.after(0, lambda: self.refresh_files())

                self.root.after(0, lambda: messagebox.showinfo("Успех", "Файл успешно удален с сервера"))
            except Exception as e:
                error_msg = str(e)
                self.progress_queue.put({'status': f'Ошибка: {error_msg}'})
                self.root.after(0, lambda msg=error_msg: messagebox.showerror("Ошибка", f"Ошибка удаления: {msg}"))

        threading.Thread(target=delete_thread, daemon=True).start()

    def reset_progress(self, immediate=False):
        if immediate:
            self.progress_var.set(0)
        else:
            self.root.after(500, self._delayed_reset_progress)

    def _delayed_reset_progress(self):
        if not self.operation_in_progress and self.progress_var.get() == 0:
            self.progress_var.set(0)

    def on_file_selection_changed(self, event):
        self.reset_progress(immediate=True)


if __name__ == "__main__":
    root = tk.Tk()
    app = FileManagerGUI(root)
    root.mainloop()
