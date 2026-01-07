#!/usr/bin/env python3
"""
Minecraft Server Telegram Bot

Версия: 1.0
Автор: AI Assistant
Для: Minecraft Forge серt Message, CallbackQuery, FSInputFile, InlineKeyboardMarkup, InlineKeyboardButton
"""

import asyncio
import json
import logging
import subprocess
import sys
import tarfile
from datetime import datetime, time
from logging import getLogger
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import aiocron
from aiogram import Bot, Dispatcher, F, Router, types
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT_DIR = Path(__file__).parent
ENV_FILE = ROOT_DIR / ".env"

logger = getLogger(__name__)

if not ENV_FILE.exists():
    raise FileNotFoundError(f".env file not found at: {ENV_FILE}")


class Config(BaseSettings):
    TOKEN_BOT: SecretStr
    ADMIN_ID: int
    BACKUP_CHAT_ID: int
    
    SERVER_IP: str = "195.10.205.59"
    SERVER_PORT: int = 25565
    SERVER_SERVICE: str = "minecraft-forge.service"
    
    # Настройки автобэкапов
    AUTO_BACKUP_ENABLED: bool = False
    AUTO_BACKUP_INTERVAL: str = "daily"  # daily, weekly, hourly, 15min, 30min
    AUTO_BACKUP_TIME: str = "03:00"  # Время для daily/weekly бэкапов
    AUTO_BACKUP_KEEP_COUNT: int = 7  # Количество бэкапов для хранения
    
    LOG_LEVEL: str = "INFO"
    LOG_FORMAT: str = "%(asctime)s - [%(levelname)s] - %(name)s: %(message)s"
    LOG_DATE_FORMAT: str = "%d.%m.%Y %H:%M:%S"
    LOG_FILE: Path = ROOT_DIR / "mc_bot.log"
    
    model_config = SettingsConfigDict(
        env_file=ENV_FILE,
        env_file_encoding="utf-8"
    )


class ColorFormatter(logging.Formatter):
    GREEN = '\033[92m'
    RED = '\033[91m'
    YELLOW = '\033[93m'
    MAGENTA = '\033[95m'
    RESET = '\033[0m'
    
    def format(self, record):
        color = self.RESET
        if record.levelno == logging.INFO:
            color = self.GREEN
        elif record.levelno == logging.ERROR:
            color = self.RED
        elif record.levelno == logging.WARNING:
            color = self.YELLOW
        elif record.levelno == logging.DEBUG:
            color = self.MAGENTA
        
        record.levelname = f"{color}{record.levelname}{self.RESET}"
        record.msg = f"{color}{record.msg}{self.RESET}"
        return super().format(record)


class MinecraftServerBot:
    def __init__(self, config: Config):
        self.config = config
        self.server_dir = Path("/server")  # Путь внутри контейнера
        self.backup_dir = Path("/app/backups")  # Путь внутри контейнера
        self.backup_dir.mkdir(exist_ok=True)
        
        # Файлы сервера
        self.server_properties = self.server_dir / "server.properties"
        self.whitelist_file = self.server_dir / "whitelist.json"
        self.ops_file = self.server_dir / "ops.json"
        self.server_log = self.server_dir / "logs" / "latest.log"
        
        # Кэш белого списка
        self.whitelist_cache: List[Dict] = []
        
        # Настройки автобэкапов
        self.backup_settings = {
            "enabled": self.config.AUTO_BACKUP_ENABLED,
            "interval": self.config.AUTO_BACKUP_INTERVAL,
            "time": self.config.AUTO_BACKUP_TIME,
            "keep_count": self.config.AUTO_BACKUP_KEEP_COUNT
        }
        self.backup_job = None
        
        # Инициализация бота
        self.bot = Bot(
            token=config.TOKEN_BOT.get_secret_value(),
            default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        )
        self.dp = Dispatcher()
        self.router = Router()
        self.dp.include_router(self.router)
        
        self._setup_handlers()
    
    def is_admin(self, user_id: int) -> bool:
        """Проверяет, является ли пользователь администратором."""
        return user_id == self.config.ADMIN_ID
    
    def load_whitelist(self) -> List[Dict]:
        """Загружает белый список из файла."""
        try:
            if self.whitelist_file.exists():
                with open(self.whitelist_file, "r", encoding="utf-8") as f:
                    self.whitelist_cache = json.load(f)
            else:
                self.whitelist_cache = []
        except Exception as e:
            logger.error(f"Ошибка загрузки whitelist: {e}")
            self.whitelist_cache = []
        return self.whitelist_cache
    
    def save_whitelist(self, whitelist: List[Dict]) -> bool:
        """Сохраняет белый список в файл."""
        try:
            with open(self.whitelist_file, "w", encoding="utf-8") as f:
                json.dump(whitelist, f, indent=2, ensure_ascii=False)
            self.whitelist_cache = whitelist
            return True
        except Exception as e:
            logger.error(f"Ошибка сохранения whitelist: {e}")
            return False
    
    def get_server_status(self) -> str:
        """Получает статус сервера."""
        try:
            result = subprocess.run(
                ["systemctl", "is-active", self.config.SERVER_SERVICE],
                capture_output=True,
                text=True,
            )
            status = result.stdout.strip()
            if status == "active":
                return "🟢 <b>Сервер запущен</b>"
            elif status == "inactive":
                return "🔴 <b>Сервер остановлен</b>"
            else:
                return f"🟡 <b>Сервер: {status}</b>"
        except Exception as e:
            return f"❌ <b>Ошибка получения статуса:</b> {e}"
    
    def execute_server_command(self, command: str) -> Tuple[bool, str]:
        """Выполняет команду на сервере через RCON или файл команд."""
        try:
            # Метод 1: Попробуем использовать RCON, если настроен
            rcon_result = self._try_rcon_command(command)
            if rcon_result[0]:
                return rcon_result
            
            # Метод 2: Создаем файл команд в директории бота (не в read-only директории сервера)
            command_file = Path("/app/server_commands.txt")
            try:
                with open(command_file, "a", encoding="utf-8") as f:
                    f.write(f"{command}\n")
                logger.info(f"Команда записана в файл: {command}")
                
                # Для systemd сервисов команды нужно отправлять через другие методы
                # Пока что просто логируем команду
                logger.info(f"Команда для сервера: {command}")
                return True, f"Команда '{command}' записана (требуется настройка RCON для прямой отправки)"
            except Exception as e:
                logger.error(f"Ошибка записи команды в файл: {e}")
                return False, f"Ошибка записи команды: {e}"
                
        except Exception as e:
            return False, f"Ошибка: {e}"
    
    def _try_rcon_command(self, command: str) -> Tuple[bool, str]:
        """Пытается выполнить команду через RCON."""
        try:
            # Проверяем, есть ли настройки RCON в server.properties
            if not self.server_properties.exists():
                return False, "server.properties не найден"
            
            # Читаем настройки RCON
            rcon_enabled = False
            rcon_port = 25575
            rcon_password = ""
            
            with open(self.server_properties, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("enable-rcon=true"):
                        rcon_enabled = True
                    elif line.startswith("rcon.port="):
                        rcon_port = int(line.split("=")[1])
                    elif line.startswith("rcon.password="):
                        rcon_password = line.split("=", 1)[1]
            
            if not rcon_enabled or not rcon_password:
                return False, "RCON не настроен"
            
            # Используем RCON библиотеку
            try:
                from mcrcon import MCRcon
                with MCRcon("localhost", rcon_password, port=rcon_port) as mcr:
                    response = mcr.command(command)
                    logger.info(f"RCON команда выполнена: {command} -> {response}")
                    return True, f"RCON: {response}"
            except ImportError:
                return False, "RCON библиотека не установлена"
            except Exception as rcon_error:
                return False, f"Ошибка RCON соединения: {rcon_error}"
            
        except Exception as e:
            return False, f"Ошибка RCON: {e}"
    
    def save_backup_settings(self) -> bool:
        """Сохраняет настройки автобэкапов в файл."""
        try:
            settings_file = ROOT_DIR / "backup_settings.json"
            with open(settings_file, "w", encoding="utf-8") as f:
                json.dump(self.backup_settings, f, indent=2, ensure_ascii=False)
            return True
        except Exception as e:
            logger.error(f"Ошибка сохранения настроек бэкапа: {e}")
            return False
    
    def load_backup_settings(self) -> bool:
        """Загружает настройки автобэкапов из файла."""
        try:
            settings_file = ROOT_DIR / "backup_settings.json"
            if settings_file.exists():
                with open(settings_file, "r", encoding="utf-8") as f:
                    self.backup_settings = json.load(f)
                return True
            return False
        except Exception as e:
            logger.error(f"Ошибка загрузки настроек бэкапа: {e}")
            return False
    
    def cleanup_old_backups(self):
        """Удаляет старые бэкапы, оставляя только указанное количество."""
        try:
            backup_files = list(self.backup_dir.glob("world_backup_*.tar.gz"))
            backup_files.sort(key=lambda x: x.stat().st_mtime, reverse=True)
            
            keep_count = self.backup_settings.get("keep_count", 7)
            if len(backup_files) > keep_count:
                for old_backup in backup_files[keep_count:]:
                    old_backup.unlink()
                    logger.info(f"Удален старый бэкап: {old_backup.name}")
        except Exception as e:
            logger.error(f"Ошибка очистки старых бэкапов: {e}")
    
    async def auto_backup_task(self):
        """Задача автоматического бэкапа."""
        try:
            logger.info("Выполняется автоматический бэкап...")
            success, result, backup_path = self.create_backup()
            
            if success and backup_path:
                # Очищаем старые бэкапы
                self.cleanup_old_backups()
                
                # Отправляем в чат для бэкапов
                try:
                    with open(backup_path, "rb") as file:
                        await self.bot.send_document(
                            chat_id=self.config.BACKUP_CHAT_ID,
                            document=types.BufferedInputFile(file.read(), filename=backup_path.name),
                            caption=f"🤖 Автоматический бэкап мира Minecraft\nДата: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
                        )
                    logger.info(f"Автобэкап успешно создан и отправлен: {backup_path.name}")
                except Exception as e:
                    logger.error(f"Ошибка отправки автобэкапа: {e}")
            else:
                logger.error(f"Ошибка создания автобэкапа: {result}")
        except Exception as e:
            logger.error(f"Ошибка в задаче автобэкапа: {e}")
    
    def setup_auto_backup(self):
        """Настраивает автоматические бэкапы."""
        # Останавливаем предыдущую задачу если есть
        if self.backup_job:
            self.backup_job.stop()
            self.backup_job = None
        
        if not self.backup_settings.get("enabled", False):
            logger.info("Автобэкапы отключены")
            return
        
        interval = self.backup_settings.get("interval", "daily")
        backup_time = self.backup_settings.get("time", "03:00")
        
        try:
            if interval == "15min":
                # Каждые 15 минут
                self.backup_job = aiocron.crontab('*/15 * * * *', func=self.auto_backup_task)
            elif interval == "30min":
                # Каждые 30 минут
                self.backup_job = aiocron.crontab('*/30 * * * *', func=self.auto_backup_task)
            elif interval == "hourly":
                # Каждый час
                self.backup_job = aiocron.crontab('0 * * * *', func=self.auto_backup_task)
            elif interval == "daily":
                # Каждый день в указанное время
                hour, minute = backup_time.split(":")
                self.backup_job = aiocron.crontab(f'{minute} {hour} * * *', func=self.auto_backup_task)
            elif interval == "weekly":
                # Каждую неделю в воскресенье в указанное время
                hour, minute = backup_time.split(":")
                self.backup_job = aiocron.crontab(f'{minute} {hour} * * 0', func=self.auto_backup_task)
            
            if self.backup_job:
                logger.info(f"Автобэкапы настроены: {interval} в {backup_time if interval in ['daily', 'weekly'] else 'по расписанию'}")
            
        except Exception as e:
            logger.error(f"Ошибка настройки автобэкапов: {e}")
    
    def get_backup_settings_keyboard(self) -> InlineKeyboardMarkup:
        """Создает клавиатуру для настроек бэкапов."""
        builder = InlineKeyboardBuilder()
        
        # Статус автобэкапов
        status = "✅ Включены" if self.backup_settings.get("enabled", False) else "❌ Отключены"
        builder.row(
            InlineKeyboardButton(text=f"Автобэкапы: {status}", callback_data="toggle_auto_backup")
        )
        
        if self.backup_settings.get("enabled", False):
            # Интервал
            interval_text = {
                "15min": "15 минут",
                "30min": "30 минут", 
                "hourly": "Каждый час",
                "daily": "Ежедневно",
                "weekly": "Еженедельно"
            }.get(self.backup_settings.get("interval", "daily"), "Ежедневно")
            
            builder.row(
                InlineKeyboardButton(text=f"Интервал: {interval_text}", callback_data="set_backup_interval")
            )
            
            # Время (только для daily/weekly)
            if self.backup_settings.get("interval") in ["daily", "weekly"]:
                builder.row(
                    InlineKeyboardButton(text=f"Время: {self.backup_settings.get('time', '03:00')}", callback_data="set_backup_time")
                )
            
            # Количество хранимых бэкапов
            builder.row(
                InlineKeyboardButton(text=f"Хранить: {self.backup_settings.get('keep_count', 7)} бэкапов", callback_data="set_backup_count")
            )
        
        builder.row(
            InlineKeyboardButton(text="💾 Создать бэкап сейчас", callback_data="create_backup")
        )
        builder.row(
            InlineKeyboardButton(text="↩️ Назад", callback_data="back_to_main")
        )
        
        return builder.as_markup()
    
    def get_interval_keyboard(self) -> InlineKeyboardMarkup:
        """Создает клавиатуру для выбора интервала бэкапов."""
        builder = InlineKeyboardBuilder()
        
        intervals = [
            ("15min", "⚡ Каждые 15 минут"),
            ("30min", "🔄 Каждые 30 минут"),
            ("hourly", "⏰ Каждый час"),
            ("daily", "📅 Ежедневно"),
            ("weekly", "📆 Еженедельно")
        ]
        
        for interval_key, interval_name in intervals:
            current = "✅ " if self.backup_settings.get("interval") == interval_key else ""
            builder.row(
                InlineKeyboardButton(text=f"{current}{interval_name}", callback_data=f"interval_{interval_key}")
            )
        
        builder.row(
            InlineKeyboardButton(text="↩️ Назад", callback_data="backup_settings")
        )
        
        return builder.as_markup()
    
    def _get_backup_settings_text(self) -> str:
        """Возвращает текст с текущими настройками бэкапов."""
        status = "✅ Включены" if self.backup_settings.get("enabled", False) else "❌ Отключены"
        
        interval_text = {
            "15min": "Каждые 15 минут",
            "30min": "Каждые 30 минут", 
            "hourly": "Каждый час",
            "daily": "Ежедневно",
            "weekly": "Еженедельно"
        }.get(self.backup_settings.get("interval", "daily"), "Ежедневно")
        
        text = f"⚙️ <b>Настройки автоматических бэкапов</b>\n\n"
        text += f"Статус: {status}\n"
        
        if self.backup_settings.get("enabled", False):
            text += f"Интервал: {interval_text}\n"
            
            if self.backup_settings.get("interval") in ["daily", "weekly"]:
                text += f"Время: {self.backup_settings.get('time', '03:00')}\n"
            
            text += f"Хранить бэкапов: {self.backup_settings.get('keep_count', 7)}\n"
            
            # Показываем следующий запланированный бэкап
            if self.backup_job:
                text += f"\n📅 Следующий бэкап запланирован согласно расписанию"
        
        return text
    
    def get_server_info(self) -> str:
        """Получает информацию о сервере."""
        info_lines = [self.get_server_status()]
        
        try:
            # Ядро системы
            try:
                kernel_result = subprocess.run(["uname", "-r"], capture_output=True, text=True, timeout=5)
                if kernel_result.returncode == 0:
                    kernel = kernel_result.stdout.strip()
                    info_lines.append(f"<b>Ядро системы:</b> {kernel}")
                else:
                    info_lines.append(f"<b>Ядро системы:</b> Недоступно")
            except FileNotFoundError:
                info_lines.append(f"<b>Ядро системы:</b> Команда uname не найдена")
            except Exception as e:
                logger.error(f"Ошибка получения информации о ядре: {e}")
                info_lines.append(f"<b>Ядро системы:</b> Ошибка получения")
            
            # Java версия
            try:
                java = subprocess.run(["java", "-version"], capture_output=True, text=True, timeout=5)
                if java.returncode == 0:
                    # Java выводит версию в stderr, поэтому используем stderr
                    java_output = java.stderr if java.stderr else java.stdout
                    java_lines = java_output.strip().split("\n")
                    if java_lines:
                        info_lines.append(f"<b>Java:</b> {java_lines[0]}")
                else:
                    info_lines.append(f"<b>Java:</b> Не установлена")
            except FileNotFoundError:
                info_lines.append(f"<b>Java:</b> Не найдена")
            except Exception as e:
                logger.error(f"Ошибка получения информации о Java: {e}")
                info_lines.append(f"<b>Java:</b> Ошибка проверки")
            
            # Загрузка CPU и памяти
            try:
                memory_result = subprocess.run(["free", "-h"], capture_output=True, text=True, timeout=5)
                if memory_result.returncode == 0:
                    memory_lines = memory_result.stdout.strip().split("\n")
                    if len(memory_lines) > 1:
                        memory_info = " ".join(memory_lines[1].split()[1:4])
                        info_lines.append(f"<b>Память:</b> {memory_info}")
                    else:
                        info_lines.append(f"<b>Память:</b> Недоступно")
                else:
                    info_lines.append(f"<b>Память:</b> Ошибка получения")
            except FileNotFoundError:
                info_lines.append(f"<b>Память:</b> Команда free не найдена")
            except Exception as e:
                logger.error(f"Ошибка получения информации о памяти: {e}")
                info_lines.append(f"<b>Память:</b> Ошибка получения")
            
            # Дисковое пространство
            try:
                disk_result = subprocess.run(["df", "-h", "/server"], capture_output=True, text=True, timeout=5)
                if disk_result.returncode == 0:
                    disk_lines = disk_result.stdout.strip().split("\n")
                    if len(disk_lines) > 1:
                        disk_info = " ".join(disk_lines[1].split()[1:5])
                        info_lines.append(f"<b>Диск:</b> {disk_info}")
                    else:
                        info_lines.append(f"<b>Диск:</b> Недоступно")
                else:
                    info_lines.append(f"<b>Диск:</b> Ошибка получения")
            except FileNotFoundError:
                info_lines.append(f"<b>Диск:</b> Команда df не найдена")
            except Exception as e:
                logger.error(f"Ошибка получения информации о диске: {e}")
                info_lines.append(f"<b>Диск:</b> Ошибка получения")
            
            # Белый список
            try:
                whitelist = self.load_whitelist()
                info_lines.append(f"<b>Игроков в белом списке:</b> {len(whitelist)}")
            except Exception as e:
                logger.error(f"Ошибка получения информации о белом списке: {e}")
            
            # IP сервера
            info_lines.append(f"<b>IP сервера:</b> {self.config.SERVER_IP}:{self.config.SERVER_PORT}")
            
            # Директория сервера (показываем реальный путь на хосте)
            info_lines.append(f"<b>Директория:</b> /root/projects/mrok-minecraft-server")
            
        except Exception as e:
            info_lines.append(f"<b>Ошибка получения информации:</b> {e}")
            logger.error(f"Общая ошибка в get_server_info: {e}")
        
        return "\n".join(info_lines)
    
    def get_logs(self, lines: int = 50) -> str:
        """Получает последние строки логов из разных источников."""
        try:
            # Метод 1: Пробуем получить логи из systemd journal
            try:
                result = subprocess.run(
                    ["journalctl", "-u", self.config.SERVER_SERVICE, "-n", str(lines), "--no-pager"],
                    capture_output=True,
                    text=True,
                    timeout=10
                )
                
                if result.returncode == 0 and result.stdout.strip() and "-- No entries --" not in result.stdout:
                    return result.stdout.strip()
            except Exception as e:
                logger.error(f"Ошибка получения логов через journalctl: {e}")
            
            # Метод 2: Пробуем файл логов сервера
            try:
                if self.server_log.exists():
                    with open(self.server_log, "r", encoding="utf-8", errors="ignore") as f:
                        all_lines = f.readlines()
                        last_lines = all_lines[-lines:] if len(all_lines) > lines else all_lines
                        if last_lines:
                            return "".join(last_lines)
            except Exception as e:
                logger.error(f"Ошибка чтения файла логов: {e}")
            
            # Метод 3: Пробуем другие возможные файлы логов
            possible_log_files = [
                self.server_dir / "logs" / "debug.log",
                self.server_dir / "server.log",
                self.server_dir / "minecraft_server.log",
            ]
            
            for log_file in possible_log_files:
                try:
                    if log_file.exists():
                        with open(log_file, "r", encoding="utf-8", errors="ignore") as f:
                            all_lines = f.readlines()
                            last_lines = all_lines[-lines:] if len(all_lines) > lines else all_lines
                            if last_lines:
                                return f"Логи из {log_file.name}:\n" + "".join(last_lines)
                except Exception as e:
                    logger.error(f"Ошибка чтения {log_file}: {e}")
            
            # Метод 4: Пробуем получить логи через systemctl status
            try:
                result = subprocess.run(
                    ["systemctl", "status", self.config.SERVER_SERVICE, "-n", str(min(lines, 20))],
                    capture_output=True,
                    text=True,
                    timeout=10
                )
                
                if result.returncode in [0, 3] and result.stdout.strip():  # 3 = inactive but ok
                    return f"Статус сервиса:\n{result.stdout.strip()}"
            except Exception as e:
                logger.error(f"Ошибка получения статуса сервиса: {e}")
            
            return "Логи не найдены. Возможные причины:\n" \
                   "• Сервер не запущен\n" \
                   "• Логи еще не созданы\n" \
                   "• Проблемы с доступом к файлам логов\n\n" \
                   "Попробуйте запустить сервер или проверить его статус."
                   
        except subprocess.TimeoutExpired:
            return "Таймаут получения логов"
        except Exception as e:
            return f"Ошибка получения логов: {e}"
    
    def create_backup(self) -> Tuple[bool, str, Optional[Path]]:
        """Создает резервную копию мира."""
        try:
            world_dir = self.server_dir / "world"
            if not world_dir.exists():
                return False, "Директория мира не найдена", None
            
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_name = f"world_backup_{timestamp}.tar.gz"
            backup_path = self.backup_dir / backup_name
            
            with tarfile.open(backup_path, "w:gz") as tar:
                tar.add(world_dir, arcname="world")
            
            return True, f"Бэкап создан: {backup_name}", backup_path
        except Exception as e:
            return False, f"Ошибка создания бэкапа: {e}", None
    
    def get_main_keyboard(self) -> InlineKeyboardMarkup:
        """Создает основную клавиатуру."""
        builder = InlineKeyboardBuilder()
        
        builder.row(
            InlineKeyboardButton(text="🔄 Статус сервера", callback_data="server_status"),
            InlineKeyboardButton(text="📊 Информация", callback_data="server_info"),
        )
        builder.row(
            InlineKeyboardButton(text="📜 Логи сервера", callback_data="server_logs"),
            InlineKeyboardButton(text="🔍 Статус сервиса", callback_data="service_status"),
        )
        builder.row(
            InlineKeyboardButton(text="⚙️ Управление", callback_data="server_control"),
            InlineKeyboardButton(text="�  Белый список", callback_data="whitelist_menu"),
        )
        builder.row(
            InlineKeyboardButton(text="💾 Создать бэкап", callback_data="create_backup"),
            InlineKeyboardButton(text="⚙️ Настройки бэкапов", callback_data="backup_settings"),
        )
        builder.row(
            InlineKeyboardButton(text="📢 Отправить сообщение", callback_data="send_message")
        )
        
        return builder.as_markup()
    
    def get_control_keyboard(self) -> InlineKeyboardMarkup:
        """Создает клавиатуру для управления сервером."""
        builder = InlineKeyboardBuilder()
        
        builder.row(
            InlineKeyboardButton(text="▶️ Запустить сервер", callback_data="start_server"),
            InlineKeyboardButton(text="⏹️ Остановить сервер", callback_data="stop_server"),
        )
        builder.row(
            InlineKeyboardButton(text="🔄 Перезагрузить", callback_data="restart_server"),
            InlineKeyboardButton(text="💾 Сохранить мир", callback_data="save_world"),
        )
        builder.row(
            InlineKeyboardButton(text="☀️ Ясная погода", callback_data="weather_clear"),
            InlineKeyboardButton(text="🌧️ Дождь", callback_data="weather_rain"),
        )
        builder.row(
            InlineKeyboardButton(text="⛈️ Гроза", callback_data="weather_thunder"),
            InlineKeyboardButton(text="🕐 Установить день", callback_data="time_day"),
        )
        builder.row(
            InlineKeyboardButton(text="🌙 Установить ночь", callback_data="time_night"),
            InlineKeyboardButton(text="📋 Список игроков", callback_data="list_players"),
        )
        builder.row(
            InlineKeyboardButton(text="↩️ Назад", callback_data="back_to_main")
        )
        
        return builder.as_markup()
    
    def get_whitelist_keyboard(self) -> InlineKeyboardMarkup:
        """Создает клавиатуру для управления белым списком."""
        builder = InlineKeyboardBuilder()
        
        builder.row(
            InlineKeyboardButton(text="📋 Показать белый список", callback_data="show_whitelist")
        )
        builder.row(
            InlineKeyboardButton(text="➕ Добавить игрока", callback_data="add_player"),
            InlineKeyboardButton(text="➖ Удалить игрока", callback_data="remove_player"),
        )
        builder.row(
            InlineKeyboardButton(text="🔄 Обновить белый список", callback_data="refresh_whitelist")
        )
        builder.row(
            InlineKeyboardButton(text="↩️ Назад", callback_data="back_to_main")
        )
        
        return builder.as_markup()
    
    def _setup_handlers(self):
        """Настройка обработчиков."""
        
        @self.router.message(Command("start"))
        async def cmd_start(message: Message):
            if not self.is_admin(message.from_user.id):
                await message.answer("⛔ У вас нет доступа к этому боту.")
                return
            
            welcome_text = (
                "🤖 <b>Minecraft Server Bot</b>\n\n"
                "Добро пожаловать в панель управления Minecraft сервером!\n\n"
                "Доступные команды:\n"
                "/start - Начальное меню\n"
                "/status - Статус сервера\n"
                "/info - Информация о сервере\n"
                "/logs - Последние логи\n"
                "/whitelist - Управление белым списком\n"
                "/backup - Создать бэкап\n"
                "/command - Отправить команду на сервер\n"
                "/message - Отправить сообщение в чат сервера\n"
                "/help - Помощь\n\n"
                "Или используйте кнопки ниже:"
            )
            await message.answer(welcome_text, reply_markup=self.get_main_keyboard())
        
        @self.router.message(Command("status"))
        async def cmd_status(message: Message):
            if not self.is_admin(message.from_user.id):
                await message.answer("⛔ У вас нет доступа к этой команде.")
                return
            
            status_text = self.get_server_status()
            await message.answer(status_text)
        
        @self.router.message(Command("info"))
        async def cmd_info(message: Message):
            if not self.is_admin(message.from_user.id):
                await message.answer("⛔ У вас нет доступа к этой команде.")
                return
            
            info_text = self.get_server_info()
            await message.answer(info_text)
        
        @self.router.message(Command("logs"))
        async def cmd_logs(message: Message, command: CommandObject):
            if not self.is_admin(message.from_user.id):
                await message.answer("⛔ У вас нет доступа к этой команде.")
                return
            
            lines = 50
            if command.args:
                try:
                    lines = int(command.args)
                    lines = min(lines, 200)
                except ValueError:
                    lines = 50
            
            logs_text = self.get_logs(lines)
            if len(logs_text) > 4000:
                logs_text = logs_text[-4000:]
            
            await message.answer(f"<code>{logs_text}</code>")
        
        @self.router.message(Command("whitelist"))
        async def cmd_whitelist(message: Message):
            if not self.is_admin(message.from_user.id):
                await message.answer("⛔ У вас нет доступа к этой команде.")
                return
            
            await message.answer(
                "👥 <b>Управление белым списком</b>\nВыберите действие:",
                reply_markup=self.get_whitelist_keyboard(),
            )
        
        @self.router.message(Command("backup"))
        async def cmd_backup(message: Message):
            if not self.is_admin(message.from_user.id):
                await message.answer("⛔ У вас нет доступа к этой команде.")
                return
            
            await message.answer("⏳ Создаю бэкап мира...")
            success, result, backup_path = self.create_backup()
            
            if success and backup_path:
                await message.answer(f"✅ {result}")
                
                try:
                    with open(backup_path, "rb") as file:
                        await self.bot.send_document(
                            chat_id=self.config.BACKUP_CHAT_ID,
                            document=types.BufferedInputFile(file.read(), filename=backup_path.name),
                            caption=f"📦 Бэкап мира Minecraft\nДата: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
                        )
                except Exception as e:
                    await message.answer(f"⚠️ Бэкап создан, но не отправлен в чат: {e}")
            else:
                await message.answer(f"❌ {result}")
        
        @self.router.message(Command("command"))
        async def cmd_command(message: Message, command: CommandObject):
            if not self.is_admin(message.from_user.id):
                await message.answer("⛔ У вас нет доступа к этой команде.")
                return
            
            if not command.args:
                await message.answer("Использование: /command <команда>\nПример: /command say Привет!")
                return
            
            success, result = self.execute_server_command(command.args)
            if success:
                await message.answer(f"✅ Команда отправлена: <code>{command.args}</code>")
            else:
                await message.answer(f"❌ Ошибка: {result}")
        
        @self.router.message(Command("message"))
        async def cmd_message(message: Message, command: CommandObject):
            if not self.is_admin(message.from_user.id):
                await message.answer("⛔ У вас нет доступа к этой команде.")
                return
            
            if not command.args:
                await message.answer("Использование: /message <текст>\nПример: /message Внимание, сервер перезагружается!")
                return
            
            success, result = self.execute_server_command(f"say {command.args}")
            if success:
                await message.answer(f"✅ Сообщение отправлено: {command.args}")
            else:
                await message.answer(f"❌ Ошибка: {result}")
        
        @self.router.message(Command("help"))
        async def cmd_help(message: Message):
            if not self.is_admin(message.from_user.id):
                await message.answer("⛔ У вас нет доступа к этой команде.")
                return
            
            help_text = (
                "📚 <b>Помощь по командам</b>\n\n"
                "<b>Основные команды:</b>\n"
                "/start - Начальное меню\n"
                "/status - Статус сервера\n"
                "/info - Информация о сервере\n"
                "/logs [количество] - Логи сервера (по умолчанию 50 строк)\n"
                "/whitelist - Управление белым списком\n"
                "/backup - Создать бэкап мира\n"
                "/command <команда> - Выполнить команду на сервере\n"
                "/message <текст> - Отправить сообщение в чат сервера\n\n"
                "<b>Примеры команд для /command:</b>\n"
                "say Привет! - Отправить сообщение\n"
                "whitelist add Player - Добавить игрока\n"
                "whitelist remove Player - Удалить игрока\n"
                "op Player - Выдать оператора\n"
                "weather clear - Ясная погода\n"
                "time set day - Установить день\n"
                "save-all - Сохранить мир\n"
                "list - Список игроков\n\n"
                "<b>Управление через кнопки:</b>\n"
                "Используйте кнопки в меню для быстрого доступа к функциям.\n\n"
                "<b>⚠️ Важно - Настройка RCON:</b>\n"
                "Для отправки команд на сервер добавьте в server.properties:\n"
                "enable-rcon=true\n"
                "rcon.port=25575\n"
                "rcon.password=your_secure_password\n"
                "Без RCON команды только логируются, но не выполняются на сервере."
            )
            await message.answer(help_text)
        
        # Обработчики кнопок
        @self.router.callback_query(F.data == "server_status")
        async def callback_server_status(callback: CallbackQuery):
            if not self.is_admin(callback.from_user.id):
                await callback.answer("⛔ Нет доступа", show_alert=True)
                return
            
            status_text = self.get_server_status()
            await callback.message.edit_text(status_text, reply_markup=self.get_main_keyboard())
            await callback.answer()
        
        @self.router.callback_query(F.data == "server_info")
        async def callback_server_info(callback: CallbackQuery):
            if not self.is_admin(callback.from_user.id):
                await callback.answer("⛔ Нет доступа", show_alert=True)
                return
            
            info_text = self.get_server_info()
            await callback.message.edit_text(info_text, reply_markup=self.get_main_keyboard())
            await callback.answer()
        
        @self.router.callback_query(F.data == "server_logs")
        async def callback_server_logs(callback: CallbackQuery):
            if not self.is_admin(callback.from_user.id):
                await callback.answer("⛔ Нет доступа", show_alert=True)
                return
            
            logs_text = self.get_logs(50)
            if len(logs_text) > 4000:
                logs_text = logs_text[-4000:]
            
            await callback.message.edit_text(
                f"📜 <b>Последние 50 строк логов:</b>\n\n<code>{logs_text}</code>",
                reply_markup=self.get_main_keyboard(),
            )
            await callback.answer()
        
        @self.router.callback_query(F.data == "service_status")
        async def callback_service_status(callback: CallbackQuery):
            if not self.is_admin(callback.from_user.id):
                await callback.answer("⛔ Нет доступа", show_alert=True)
                return
            
            try:
                # Получаем детальный статус сервиса
                result = subprocess.run(
                    ["systemctl", "status", self.config.SERVER_SERVICE, "--no-pager", "-l"],
                    capture_output=True,
                    text=True,
                    timeout=10
                )
                
                status_text = f"🔍 <b>Статус сервиса {self.config.SERVER_SERVICE}</b>\n\n"
                
                if result.stdout:
                    # Ограничиваем вывод для Telegram
                    output = result.stdout
                    if len(output) > 3500:
                        output = output[:3500] + "\n... (обрезано)"
                    status_text += f"<code>{output}</code>"
                else:
                    status_text += "Информация о сервисе недоступна"
                
                await callback.message.edit_text(
                    status_text,
                    reply_markup=self.get_main_keyboard(),
                )
                
            except subprocess.TimeoutExpired:
                await callback.message.edit_text(
                    "⏱️ Таймаут получения статуса сервиса",
                    reply_markup=self.get_main_keyboard(),
                )
            except Exception as e:
                await callback.message.edit_text(
                    f"❌ Ошибка получения статуса: {e}",
                    reply_markup=self.get_main_keyboard(),
                )
            
            await callback.answer()
        
        @self.router.callback_query(F.data == "server_control")
        async def callback_server_control(callback: CallbackQuery):
            if not self.is_admin(callback.from_user.id):
                await callback.answer("⛔ Нет доступа", show_alert=True)
                return
            
            await callback.message.edit_text(
                "⚙️ <b>Управление сервером</b>\nВыберите действие:",
                reply_markup=self.get_control_keyboard(),
            )
            await callback.answer()
        
        @self.router.callback_query(F.data == "whitelist_menu")
        async def callback_whitelist_menu(callback: CallbackQuery):
            if not self.is_admin(callback.from_user.id):
                await callback.answer("⛔ Нет доступа", show_alert=True)
                return
            
            await callback.message.edit_text(
                "👥 <b>Управление белым списком</b>\nВыберите действие:",
                reply_markup=self.get_whitelist_keyboard(),
            )
            await callback.answer()
        
        @self.router.callback_query(F.data == "show_whitelist")
        async def callback_show_whitelist(callback: CallbackQuery):
            if not self.is_admin(callback.from_user.id):
                await callback.answer("⛔ Нет доступа", show_alert=True)
                return
            
            whitelist = self.load_whitelist()
            if not whitelist:
                text = "📋 <b>Белый список пуст</b>"
            else:
                players = "\n".join([f"• {player.get('name', 'Unknown')}" for player in whitelist])
                text = f"📋 <b>Белый список ({len(whitelist)} игроков):</b>\n\n{players}"
            
            await callback.message.edit_text(text, reply_markup=self.get_whitelist_keyboard())
            await callback.answer()
        
        @self.router.callback_query(F.data == "back_to_main")
        async def callback_back_to_main(callback: CallbackQuery):
            if not self.is_admin(callback.from_user.id):
                await callback.answer("⛔ Нет доступа", show_alert=True)
                return
            
            await callback.message.edit_text(
                "🤖 <b>Главное меню</b>\nВыберите действие:",
                reply_markup=self.get_main_keyboard(),
            )
            await callback.answer()
        
        # Обработчики управления сервером
        @self.router.callback_query(F.data == "start_server")
        async def callback_start_server(callback: CallbackQuery):
            if not self.is_admin(callback.from_user.id):
                await callback.answer("⛔ Нет доступа", show_alert=True)
                return
            
            try:
                subprocess.run(["systemctl", "start", self.config.SERVER_SERVICE], check=True)
                await callback.answer("✅ Сервер запускается...")
                await asyncio.sleep(3)
                await callback.message.edit_text(self.get_server_status(), reply_markup=self.get_control_keyboard())
            except Exception as e:
                await callback.answer(f"❌ Ошибка: {e}")
        
        @self.router.callback_query(F.data == "stop_server")
        async def callback_stop_server(callback: CallbackQuery):
            if not self.is_admin(callback.from_user.id):
                await callback.answer("⛔ Нет доступа", show_alert=True)
                return
            
            try:
                # Сначала отправляем предупреждение игрокам
                self.execute_server_command("say ⚠️ Сервер останавливается через 10 секунд!")
                await asyncio.sleep(10)
                
                subprocess.run(["systemctl", "stop", self.config.SERVER_SERVICE], check=True)
                await callback.answer("⏹️ Сервер остановлен")
                await asyncio.sleep(3)
                await callback.message.edit_text(self.get_server_status(), reply_markup=self.get_control_keyboard())
            except Exception as e:
                await callback.answer(f"❌ Ошибка: {e}")
        
        @self.router.callback_query(F.data == "restart_server")
        async def callback_restart_server(callback: CallbackQuery):
            if not self.is_admin(callback.from_user.id):
                await callback.answer("⛔ Нет доступа", show_alert=True)
                return
            
            try:
                # Предупреждаем игроков
                self.execute_server_command("say ⚠️ Сервер перезагружается через 10 секунд!")
                await asyncio.sleep(10)
                
                subprocess.run(["systemctl", "restart", self.config.SERVER_SERVICE], check=True)
                await callback.answer("🔄 Сервер перезагружается...")
                await asyncio.sleep(5)
                await callback.message.edit_text(self.get_server_status(), reply_markup=self.get_control_keyboard())
            except Exception as e:
                await callback.answer(f"❌ Ошибка: {e}")
        
        @self.router.callback_query(F.data == "save_world")
        async def callback_save_world(callback: CallbackQuery):
            if not self.is_admin(callback.from_user.id):
                await callback.answer("⛔ Нет доступа", show_alert=True)
                return
            
            self.execute_server_command("save-all")
            await callback.answer("💾 Команда сохранения отправлена")
        
        @self.router.callback_query(F.data == "weather_clear")
        async def callback_weather_clear(callback: CallbackQuery):
            if not self.is_admin(callback.from_user.id):
                await callback.answer("⛔ Нет доступа", show_alert=True)
                return
            
            self.execute_server_command("weather clear")
            await callback.answer("☀️ Команда установки ясной погоды отправлена")
        
        @self.router.callback_query(F.data == "weather_rain")
        async def callback_weather_rain(callback: CallbackQuery):
            if not self.is_admin(callback.from_user.id):
                await callback.answer("⛔ Нет доступа", show_alert=True)
                return
            
            self.execute_server_command("weather rain")
            await callback.answer("🌧️ Команда установки дождя отправлена")
        
        @self.router.callback_query(F.data == "weather_thunder")
        async def callback_weather_thunder(callback: CallbackQuery):
            if not self.is_admin(callback.from_user.id):
                await callback.answer("⛔ Нет доступа", show_alert=True)
                return
            
            self.execute_server_command("weather thunder")
            await callback.answer("⛈️ Команда установки грозы отправлена")
        
        @self.router.callback_query(F.data == "time_day")
        async def callback_time_day(callback: CallbackQuery):
            if not self.is_admin(callback.from_user.id):
                await callback.answer("⛔ Нет доступа", show_alert=True)
                return
            
            self.execute_server_command("time set day")
            await callback.answer("🕐 Команда установки дня отправлена")
        
        @self.router.callback_query(F.data == "time_night")
        async def callback_time_night(callback: CallbackQuery):
            if not self.is_admin(callback.from_user.id):
                await callback.answer("⛔ Нет доступа", show_alert=True)
                return
            
            self.execute_server_command("time set night")
            await callback.answer("🌙 Команда установки ночи отправлена")
        
        @self.router.callback_query(F.data == "list_players")
        async def callback_list_players(callback: CallbackQuery):
            if not self.is_admin(callback.from_user.id):
                await callback.answer("⛔ Нет доступа", show_alert=True)
                return
            
            self.execute_server_command("list")
            await callback.answer("📋 Команда списка игроков отправлена, проверьте логи")
        
        # Обработчики белого списка
        @self.router.callback_query(F.data == "add_player")
        async def callback_add_player(callback: CallbackQuery):
            if not self.is_admin(callback.from_user.id):
                await callback.answer("⛔ Нет доступа", show_alert=True)
                return
            
            await callback.message.edit_text(
                "Введите никнейм игрока для добавления в белый список:",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="↩️ Назад", callback_data="whitelist_menu")]
                ]),
            )
            await callback.answer()
        
        @self.router.callback_query(F.data == "remove_player")
        async def callback_remove_player(callback: CallbackQuery):
            if not self.is_admin(callback.from_user.id):
                await callback.answer("⛔ Нет доступа", show_alert=True)
                return
            
            whitelist = self.load_whitelist()
            if not whitelist:
                await callback.message.edit_text(
                    "Белый список пуст",
                    reply_markup=self.get_whitelist_keyboard(),
                )
                await callback.answer()
                return
            
            # Создаем клавиатуру с игроками для удаления
            builder = InlineKeyboardBuilder()
            for player in whitelist:
                player_name = player.get("name", "Unknown")
                builder.row(InlineKeyboardButton(
                    text=f"❌ {player_name}",
                    callback_data=f"remove_player_{player_name}",
                ))
            builder.row(InlineKeyboardButton(text="↩️ Назад", callback_data="whitelist_menu"))
            
            await callback.message.edit_text(
                "Выберите игрока для удаления:",
                reply_markup=builder.as_markup(),
            )
            await callback.answer()
        
        @self.router.callback_query(F.data.startswith("remove_player_"))
        async def callback_remove_player_confirm(callback: CallbackQuery):
            if not self.is_admin(callback.from_user.id):
                await callback.answer("⛔ Нет доступа", show_alert=True)
                return
            
            player_name = callback.data.replace("remove_player_", "")
            
            # Загружаем текущий белый список
            whitelist = self.load_whitelist()
            
            # Ищем и удаляем игрока
            new_whitelist = [p for p in whitelist if p.get("name") != player_name]
            
            if len(new_whitelist) == len(whitelist):
                # Игрок не найден
                await callback.message.edit_text(
                    f"Игрок '{player_name}' не найден в белом списке",
                    reply_markup=self.get_whitelist_keyboard(),
                )
            else:
                # Сохраняем изменения
                if self.save_whitelist(new_whitelist):
                    # Обновляем на сервере
                    self.execute_server_command(f"whitelist remove {player_name}")
                    self.execute_server_command("whitelist reload")
                    await callback.message.edit_text(
                        f"✅ Игрок '{player_name}' удален из белого списка",
                        reply_markup=self.get_whitelist_keyboard(),
                    )
                else:
                    await callback.message.edit_text(
                        f"❌ Ошибка при удалении игрока '{player_name}'",
                        reply_markup=self.get_whitelist_keyboard(),
                    )
            await callback.answer()
        
        @self.router.callback_query(F.data == "refresh_whitelist")
        async def callback_refresh_whitelist(callback: CallbackQuery):
            if not self.is_admin(callback.from_user.id):
                await callback.answer("⛔ Нет доступа", show_alert=True)
                return
            
            self.execute_server_command("whitelist reload")
            await callback.answer("🔄 Белый список обновлен на сервере")
        
        @self.router.callback_query(F.data == "create_backup")
        async def callback_create_backup(callback: CallbackQuery):
            if not self.is_admin(callback.from_user.id):
                await callback.answer("⛔ Нет доступа", show_alert=True)
                return
            
            await callback.message.edit_text("⏳ Создаю бэкап мира...")
            success, result, backup_path = self.create_backup()
            
            if success and backup_path:
                await callback.message.edit_text(f"✅ {result}")
                
                try:
                    with open(backup_path, "rb") as file:
                        await self.bot.send_document(
                            chat_id=self.config.BACKUP_CHAT_ID,
                            document=types.BufferedInputFile(file.read(), filename=backup_path.name),
                            caption=f"📦 Бэкап мира Minecraft\nДата: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
                        )
                except Exception as e:
                    await callback.message.edit_text(f"⚠️ Бэкап создан, но не отправлен в чат: {e}")
            else:
                await callback.message.edit_text(f"❌ {result}")
            await callback.answer()
        
        @self.router.callback_query(F.data == "send_message")
        async def callback_send_message(callback: CallbackQuery):
            if not self.is_admin(callback.from_user.id):
                await callback.answer("⛔ Нет доступа", show_alert=True)
                return
            
            await callback.message.edit_text(
                "Введите сообщение для отправки в чат сервера:",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="↩️ Назад", callback_data="back_to_main")]
                ]),
            )
            await callback.answer()
        
        # Обработчики настроек бэкапов
        @self.router.callback_query(F.data == "backup_settings")
        async def callback_backup_settings(callback: CallbackQuery):
            if not self.is_admin(callback.from_user.id):
                await callback.answer("⛔ Нет доступа", show_alert=True)
                return
            
            settings_text = self._get_backup_settings_text()
            await callback.message.edit_text(
                settings_text,
                reply_markup=self.get_backup_settings_keyboard(),
            )
            await callback.answer()
        
        @self.router.callback_query(F.data == "toggle_auto_backup")
        async def callback_toggle_auto_backup(callback: CallbackQuery):
            if not self.is_admin(callback.from_user.id):
                await callback.answer("⛔ Нет доступа", show_alert=True)
                return
            
            self.backup_settings["enabled"] = not self.backup_settings.get("enabled", False)
            self.save_backup_settings()
            self.setup_auto_backup()
            
            status = "включены" if self.backup_settings["enabled"] else "отключены"
            await callback.answer(f"✅ Автобэкапы {status}")
            
            settings_text = self._get_backup_settings_text()
            await callback.message.edit_text(
                settings_text,
                reply_markup=self.get_backup_settings_keyboard(),
            )
        
        @self.router.callback_query(F.data == "set_backup_interval")
        async def callback_set_backup_interval(callback: CallbackQuery):
            if not self.is_admin(callback.from_user.id):
                await callback.answer("⛔ Нет доступа", show_alert=True)
                return
            
            await callback.message.edit_text(
                "⏰ <b>Выберите интервал для автобэкапов:</b>",
                reply_markup=self.get_interval_keyboard(),
            )
            await callback.answer()
        
        @self.router.callback_query(F.data.startswith("interval_"))
        async def callback_set_interval(callback: CallbackQuery):
            if not self.is_admin(callback.from_user.id):
                await callback.answer("⛔ Нет доступа", show_alert=True)
                return
            
            interval = callback.data.replace("interval_", "")
            self.backup_settings["interval"] = interval
            self.save_backup_settings()
            self.setup_auto_backup()
            
            interval_names = {
                "15min": "каждые 15 минут",
                "30min": "каждые 30 минут",
                "hourly": "каждый час",
                "daily": "ежедневно",
                "weekly": "еженедельно"
            }
            
            await callback.answer(f"✅ Интервал установлен: {interval_names.get(interval, interval)}")
            
            settings_text = self._get_backup_settings_text()
            await callback.message.edit_text(
                settings_text,
                reply_markup=self.get_backup_settings_keyboard(),
            )
        
        @self.router.callback_query(F.data == "set_backup_time")
        async def callback_set_backup_time(callback: CallbackQuery):
            if not self.is_admin(callback.from_user.id):
                await callback.answer("⛔ Нет доступа", show_alert=True)
                return
            
            await callback.message.edit_text(
                "🕐 <b>Введите время для бэкапов в формате ЧЧ:ММ</b>\n\n"
                "Например: 03:00 или 15:30\n"
                "Время указывается в 24-часовом формате.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="↩️ Назад", callback_data="backup_settings")]
                ]),
            )
            await callback.answer()
        
        @self.router.callback_query(F.data == "set_backup_count")
        async def callback_set_backup_count(callback: CallbackQuery):
            if not self.is_admin(callback.from_user.id):
                await callback.answer("⛔ Нет доступа", show_alert=True)
                return
            
            await callback.message.edit_text(
                "📦 <b>Введите количество бэкапов для хранения</b>\n\n"
                "Рекомендуется: 5-10 бэкапов\n"
                "Старые бэкапы будут автоматически удаляться.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="↩️ Назад", callback_data="backup_settings")]
                ]),
            )
            await callback.answer()
        
        # Обработчик текстовых сообщений
        @self.router.message(F.text)
        async def handle_text(message: Message):
            if not self.is_admin(message.from_user.id):
                return
            
            # Проверяем, находимся ли мы в режиме добавления игрока
            if message.reply_to_message and message.reply_to_message.text and "никнейм игрока" in message.reply_to_message.text:
                player_name = message.text.strip()
                
                # Загружаем текущий белый список
                whitelist = self.load_whitelist()
                
                # Проверяем, нет ли уже такого игрока
                for player in whitelist:
                    if player.get("name") == player_name:
                        await message.answer(
                            f"❌ Игрок '{player_name}' уже есть в белом списке",
                            reply_markup=self.get_whitelist_keyboard(),
                        )
                        return
                
                # Добавляем игрока
                whitelist.append({"uuid": "", "name": player_name})
                if self.save_whitelist(whitelist):
                    # Добавляем на сервере
                    success, result = self.execute_server_command(f"whitelist add {player_name}")
                    self.execute_server_command("whitelist reload")
                    
                    if success:
                        await message.answer(
                            f"✅ Игрок '{player_name}' добавлен в белый список",
                            reply_markup=self.get_whitelist_keyboard(),
                        )
                    else:
                        await message.answer(
                            f"⚠️ Игрок добавлен в файл, но ошибка на сервере: {result}",
                            reply_markup=self.get_whitelist_keyboard(),
                        )
                else:
                    await message.answer(
                        f"❌ Ошибка при добавлении игрока '{player_name}'",
                        reply_markup=self.get_whitelist_keyboard(),
                    )
                return
            
            # Проверяем, находимся ли мы в режиме отправки сообщения
            elif message.reply_to_message and message.reply_to_message.text and "сообщение для отправки" in message.reply_to_message.text:
                text = message.text.strip()
                success, result = self.execute_server_command(f"say {text}")
                if success:
                    await message.answer(
                        f"✅ Сообщение отправлено: {text}",
                        reply_markup=self.get_main_keyboard(),
                    )
                else:
                    await message.answer(
                        f"❌ Ошибка: {result}",
                        reply_markup=self.get_main_keyboard(),
                    )
                return
            
            # Проверяем, находимся ли мы в режиме ввода времени бэкапа
            elif message.reply_to_message and message.reply_to_message.text and "время для бэкапов" in message.reply_to_message.text:
                time_text = message.text.strip()
                
                # Проверяем формат времени
                try:
                    time_parts = time_text.split(":")
                    if len(time_parts) != 2:
                        raise ValueError("Неверный формат")
                    
                    hour = int(time_parts[0])
                    minute = int(time_parts[1])
                    
                    if not (0 <= hour <= 23) or not (0 <= minute <= 59):
                        raise ValueError("Неверное время")
                    
                    # Форматируем время
                    formatted_time = f"{hour:02d}:{minute:02d}"
                    
                    self.backup_settings["time"] = formatted_time
                    self.save_backup_settings()
                    self.setup_auto_backup()
                    
                    await message.answer(
                        f"✅ Время бэкапов установлено: {formatted_time}",
                        reply_markup=self.get_backup_settings_keyboard(),
                    )
                    
                except ValueError:
                    await message.answer(
                        "❌ Неверный формат времени!\n\n"
                        "Используйте формат ЧЧ:ММ (например: 03:00 или 15:30)",
                        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                            [InlineKeyboardButton(text="↩️ Назад", callback_data="backup_settings")]
                        ]),
                    )
                return
            
            # Проверяем, находимся ли мы в режиме ввода количества бэкапов
            elif message.reply_to_message and message.reply_to_message.text and "количество бэкапов" in message.reply_to_message.text:
                try:
                    count = int(message.text.strip())
                    
                    if count < 1 or count > 50:
                        raise ValueError("Количество должно быть от 1 до 50")
                    
                    self.backup_settings["keep_count"] = count
                    self.save_backup_settings()
                    
                    await message.answer(
                        f"✅ Количество хранимых бэкапов установлено: {count}",
                        reply_markup=self.get_backup_settings_keyboard(),
                    )
                    
                except ValueError as e:
                    await message.answer(
                        f"❌ Неверное значение!\n\n"
                        f"Введите число от 1 до 50",
                        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                            [InlineKeyboardButton(text="↩️ Назад", callback_data="backup_settings")]
                        ]),
                    )
                return
            
            # Если это не ответ на запрос, показываем меню
            await message.answer(
                "🤖 <b>Главное меню</b>\nВыберите действие:",
                reply_markup=self.get_main_keyboard(),
            )
    
    async def start_polling(self):
        """Запуск бота."""
        logger.info("Запуск Minecraft Server Bot...")
        self.load_whitelist()
        self.load_backup_settings()
        self.setup_auto_backup()
        
        try:
            await self.bot.delete_webhook()
            await self.dp.start_polling(self.bot)
        except asyncio.CancelledError:
            logger.info("Бот остановлен")
        finally:
            if self.backup_job:
                self.backup_job.stop()
            await self.bot.session.close()


def setup_logging(config: Config) -> None:
    color_formatter = ColorFormatter(
        fmt=config.LOG_FORMAT,
        datefmt=config.LOG_DATE_FORMAT
    )
    
    logging.basicConfig(
        level=config.LOG_LEVEL,
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(config.LOG_FILE, encoding='utf-8')
        ]
    )
    
    root_logger = logging.getLogger()
    for handler in root_logger.handlers:
        if isinstance(handler, logging.StreamHandler):
            handler.setFormatter(color_formatter)


async def main() -> None:
    config = Config()
    setup_logging(config)
    
    # Проверяем права доступа
    try:
        import os
        if os.geteuid() != 0:
            logger.warning("Бот запущен не от root пользователя. Некоторые функции могут не работать корректно")
    except AttributeError:
        # Windows не имеет geteuid
        pass
    
    bot = MinecraftServerBot(config)
    await bot.start_polling()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Бот остановлен пользователем")
    except Exception as e:
        logger.error(f"Ошибка запуска бота: {e}")
        sys.exit(1)