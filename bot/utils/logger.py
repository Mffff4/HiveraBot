from rich.console import Console
from rich.theme import Theme
from datetime import datetime
from bot.config import settings

custom_theme = Theme({
    'info': 'cyan',
    'warning': 'yellow',
    'error': 'red',
    'critical': 'red reverse',
    'success': 'green',
    'timestamp': 'white'
})

console = Console(theme=custom_theme)

class Logger:
    @staticmethod
    def _get_timestamp() -> str:
        return datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    def info(self, message: str) -> None:
        console.print(
            f"[timestamp]{self._get_timestamp()}[/timestamp]"
            f" | [info]INFO[/info]     | {message}"
        )

    def warning(self, message: str) -> None:
        console.print(
            f"[timestamp]{self._get_timestamp()}[/timestamp]"
            f" | [warning]WARNING[/warning]  | {message}"
        )

    def error(self, message: str) -> None:
        console.print(
            f"[timestamp]{self._get_timestamp()}[/timestamp]"
            f" | [error]ERROR[/error]    | {message}"
        )

    def critical(self, message: str) -> None:
        console.print(
            f"[timestamp]{self._get_timestamp()}[/timestamp]"
            f" | [critical]CRITICAL[/critical] | {message}"
        )

    def success(self, message: str) -> None:
        console.print(
            f"[timestamp]{self._get_timestamp()}[/timestamp]"
            f" | [success]SUCCESS[/success]  | {message}"
        )

    def trace(self, message: str) -> None:
        if settings.DEBUG_LOGGING:
            with open(f"logs/err_tracebacks_{datetime.now().date()}.txt", 'a') as f:
                f.write(f"{self._get_timestamp()} | TRACE | {message}\n")

logger = Logger()

def log_error(text: str) -> None:
    """
    Логирование ошибок с трейсбеком в файл при включенном DEBUG_LOGGING.
    
    Args:
        text: Текст ошибки
    """
    if settings.DEBUG_LOGGING:
        logger.trace(text)
    logger.error(text)
