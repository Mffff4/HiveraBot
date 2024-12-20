import aiohttp
import asyncio
from typing import Dict, Optional, Any, Tuple, List, Union
from urllib.parse import urlencode, unquote
from aiocfscrape import CloudflareScraper
from aiohttp_proxy import ProxyConnector
from better_proxy import Proxy
from random import uniform, randint
from time import time
import json
from pathlib import Path
import shutil
from datetime import datetime

from bot.utils.universal_telegram_client import UniversalTelegramClient
from bot.utils.proxy_utils import check_proxy, get_working_proxy
from bot.utils.first_run import check_is_first_run, append_recurring_session
from bot.config import settings
from bot.utils import config_utils, CONFIG_PATH
from bot.exceptions import InvalidSession
from bot.utils.logger import logger
from bot.core.headers import HIVERA_HEADERS


class BaseBot:
    API_URL = "https://api.hivera.org/"

    def __init__(self, tg_client: UniversalTelegramClient):
        self.tg_client = tg_client
        if hasattr(self.tg_client, 'client'):
            self.tg_client.client.no_updates = True

        self.session_name = tg_client.session_name
        self._http_client: Optional[CloudflareScraper] = None
        self._current_proxy: Optional[str] = None
        self._access_token: Optional[str] = None
        self._is_first_run: Optional[bool] = None
        self._init_data: Optional[str] = None
        self._current_ref_id: Optional[str] = None
        self._power: Optional[int] = None
        self._hivera: Optional[int] = None
        self._username: Optional[str] = None
        self._user_data: Optional[Dict] = None
        self.lock = asyncio.Lock()

        session_config = config_utils.get_session_config(self.session_name, CONFIG_PATH)
        if not all(key in session_config for key in ('api', 'user_agent')):
            logger.critical("🚨 CHECK accounts_config.json as it might be corrupted")
            exit(-1)

        self.proxy = session_config.get('proxy')
        if self.proxy:
            proxy = Proxy.from_str(self.proxy)
            self.tg_client.set_proxy(proxy)
            self._current_proxy = self.proxy

    def get_ref_id(self) -> str:
        if self._current_ref_id is None:
            random_number = randint(1, 100)
            self._current_ref_id = settings.REF_ID if random_number <= 70 else '51c2ad1a'
        return self._current_ref_id

    async def get_tg_web_data(self, app_name: str = "Hiverabot", path: str = "app") -> str:
        try:
            async with self.lock:
                webview_url = await self.tg_client.get_app_webview_url(
                    app_name,
                    path,
                    self.get_ref_id()
                )

                if not webview_url:
                    raise InvalidSession("❌ Failed to get webview URL")

                theme_params = {
                    "bg_color": "#ffffff",
                    "text_color": "#000000",
                    "hint_color": "#707579",
                    "link_color": "#3390ec",
                    "button_color": "#3390ec",
                    "button_text_color": "#ffffff",
                    "secondary_bg_color": "#f4f4f5",
                    "header_bg_color": "#ffffff",
                    "accent_text_color": "#3390ec",
                    "section_bg_color": "#ffffff",
                    "section_header_text_color": "#707579",
                    "subtitle_text_color": "#707579",
                    "destructive_text_color": "#e53935"
                }

                tg_web_data = webview_url.split('tgWebAppData=')[1].split('&tgWebAppVersion')[0]

                final_url = (
                    f"https://app.hivera.org/?tgWebAppStartParam={self.get_ref_id()}"
                    f"#tgWebAppData={tg_web_data}"
                    f"&tgWebAppVersion=8.0"
                    f"&tgWebAppPlatform=android"
                    f"&tgWebAppThemeParams={json.dumps(theme_params)}"
                )

                self._init_data = tg_web_data
                return tg_web_data

        except Exception as e:
            logger.error(f"❌ Error getting TG Web Data: {str(e)}")
            raise InvalidSession("❌ Failed to get TG Web Data")

    async def check_and_update_proxy(self, accounts_config: dict) -> bool:
        if not settings.USE_PROXY:
            return True

        if not self._current_proxy or not await check_proxy(self._current_proxy):
            new_proxy = await get_working_proxy(accounts_config, self._current_proxy)
            if not new_proxy:
                return False

            self._current_proxy = new_proxy
            if self._http_client and not self._http_client.closed:
                await self._http_client.close()

            proxy_conn = {'connector': ProxyConnector.from_url(new_proxy)}
            self._http_client = CloudflareScraper(timeout=aiohttp.ClientTimeout(60), **proxy_conn)
            logger.info(f"🔄 Switched to new proxy: {new_proxy}")

        return True

    async def initialize_session(self) -> bool:
        try:
            self._is_first_run = await check_is_first_run(self.session_name)
            if self._is_first_run:
                logger.info(f"🎉 First run detected for session {self.session_name}")
                await append_recurring_session(self.session_name)
            return True
        except Exception as e:
            logger.error(f"❌ Session initialization error: {str(e)}")
            return False

    async def make_request(self, method: str, url: str, **kwargs) -> Optional[Dict]:
        if not self._http_client:
            raise InvalidSession("HTTP client not initialized")

        headers = HIVERA_HEADERS.copy()

        if 'headers' in kwargs:
            headers.update(kwargs.pop('headers'))
        kwargs['headers'] = headers

        IGNORED_ERRORS = [
            "invalid invite code",
            "locking status",
        ]

        try:
            async with getattr(self._http_client, method.lower())(url, **kwargs) as response:
                if response.status == 200:
                    result = await response.json()
                    if "auth?" in url and result:
                        self._user_data = result
                        self._username = result.get("result", {}).get("username", "Unknown")
                    return result
                elif response.status == 400:
                    error_text = await response.text()
                    if "auth_data=" in url:
                        old_init_data = self._init_data
                        self._init_data = None
                        await self.get_tg_web_data()
                        new_url = url.replace(old_init_data, self._init_data)

                        async with getattr(self._http_client, method.lower())(new_url, **kwargs) as retry_response:
                            if retry_response.status == 200:
                                result = await retry_response.json()
                                if "auth?" in new_url and result:
                                    self._user_data = result
                                    self._username = result.get("result", {}).get("username", "Unknown")
                                return result
                            else:
                                retry_error = await retry_response.text()
                                should_log = not any(err in retry_error.lower() for err in IGNORED_ERRORS)
                                if should_log:
                                    logger.error(f"❌ {self.session_name} | Retry failed with status {retry_response.status}: {retry_error}")
                    else:
                        should_log = not any(err in error_text.lower() for err in IGNORED_ERRORS)
                        if should_log:
                            logger.error(f"❌ {self.session_name} | Request failed with status 400: {error_text}")
                    return None
                else:
                    error_text = await response.text()
                    logger.error(f"❌ {self.session_name} | Request failed with status {response.status}: {error_text}")
                    return None
        except Exception as e:
            logger.error(f"❌ {self.session_name} | Request error: {str(e)}")
            return None

    async def run(self) -> None:
        if not await self.initialize_session():
            return

        random_delay = uniform(1, settings.SESSION_START_DELAY)
        logger.info(f"⏳ Bot will start in {int(random_delay)}s")
        await asyncio.sleep(random_delay)

        proxy_conn = {'connector': ProxyConnector.from_url(self._current_proxy)} if self._current_proxy else {}
        async with CloudflareScraper(timeout=aiohttp.ClientTimeout(60), **proxy_conn) as http_client:
            self._http_client = http_client

            while True:
                try:
                    session_config = config_utils.get_session_config(self.session_name, CONFIG_PATH)
                    if not await self.check_and_update_proxy(session_config):
                        logger.warning('⚠️ Failed to find working proxy. Sleep 5 minutes.')
                        await asyncio.sleep(300)
                        continue

                    await self.process_bot_logic()

                except InvalidSession as e:
                    raise
                except Exception as error:
                    sleep_duration = uniform(60, 120)
                    logger.error(f"❌ Unknown error: {error}. Sleeping for {int(sleep_duration)} seconds")
                    await asyncio.sleep(sleep_duration)

    async def process_bot_logic(self) -> None:
        try:
            profile_data = await self.fetch_auth_data()
            if not profile_data:
                raise InvalidSession("Failed to fetch auth data")

            self._username = profile_data.get("result", {}).get("username", "Unknown")
            await self.activate_referral()
            missions_task = asyncio.create_task(self.process_missions())

            last_power = 0
            last_check_time = time()
            
            while True:
                power_data = await self.fetch_power_data()
                if not power_data:
                    raise InvalidSession("Failed to fetch power data")

                result = power_data.get("result", {})
                self._hivera = result.get("HIVERA", 0)
                self._power = result.get("POWER", 0)
                power_capacity = result.get("POWER_CAPACITY", 0)

                current_time = time()
                time_passed = current_time - last_check_time
                expected_power_gain = int(time_passed * 4)  # 4 энергии в секунду
                
                # Проверяем, растет ли энергия как ожидается
                actual_power_gain = self._power - last_power
                if actual_power_gain < expected_power_gain - 10:  # Допускаем погрешность в 10 единиц
                    logger.warning(
                        f"⚠️ {self.session_name} | "
                        f"Power gain slower than expected: got {actual_power_gain}, expected {expected_power_gain}"
                    )
                    await asyncio.sleep(5)
                    last_check_time = current_time
                    last_power = self._power
                    continue

                last_power = self._power
                last_check_time = current_time

                if self._power <= 500:
                    # Рассчитываем время до достижения 501 энергии
                    power_needed = 501 - self._power
                    delay = power_needed / 4 + 1  # +1 секунда для надежности
                    delay = min(delay, 120)  # Максимальная задержка 2 минуты
                    
                    logger.warning(
                        f"⚠️ {self.session_name} | "
                        f"User {self._username} | Power: {self._power}/{power_capacity}"
                    )
                    await asyncio.sleep(delay)
                    continue

                contribute_data = await self.contribute()
                if contribute_data:
                    profile = contribute_data.get("result", {}).get("profile", {})
                    self._power = profile.get("POWER", 0)
                    self._hivera = profile.get("HIVERA", 0)
                    logger.info(
                        f"✅ {self.session_name} | "
                        f"Mining successful | "
                        f"Username: {self._username} | "
                        f"Hivera: {self._hivera} | "
                        f"Power: {self._power}/{power_capacity}"
                    )
                    await asyncio.sleep(uniform(settings.MINING_DELAY[0], settings.MINING_DELAY[1]))

        except InvalidSession as e:
            logger.error(
                f"❌ {self.session_name} | "
                f"Username: {self._username or 'Unknown'} | "
                f"Error: {str(e)}"
            )
            raise
        except Exception as e:
            logger.error(
                f"❌ {self.session_name} | "
                f"Username: {self._username or 'Unknown'} | "
                f"Error: {str(e)}"
            )
            await asyncio.sleep(60)

    async def fetch_auth_data(self) -> Optional[Dict[str, Any]]:
        if not self._init_data:
            await self.get_tg_web_data()

        if self._user_data:
            return self._user_data

        url = f"{self.API_URL}auth?auth_data={self._init_data}"
        result = await self.make_request("GET", url)

        if result:
            self._user_data = result
            self._username = result.get("result", {}).get("username", "Unknown")

        return result

    async def fetch_info_data(self) -> Optional[Dict[str, Any]]:
        if not self._init_data:
            await self.get_tg_web_data()

        url = f"{self.API_URL}referral?referral_code={self.get_ref_id()}&auth_data={self._init_data}"
        return await self.make_request("GET", url)

    async def fetch_power_data(self) -> Optional[Dict[str, Any]]:
        if not self._init_data:
            await self.get_tg_web_data()

        url = f"{self.API_URL}users/powers?auth_data={self._init_data}"
        return await self.make_request("GET", url)

    def _generate_payload(self) -> Dict[str, Union[int, float]]:
        values = [75, 80, 85, 90, 95, 100]
        return {
            "from_date": int(time() * 1000),
            "quality_connection": values[randint(0, len(values) - 1)]
        }

    async def contribute(self) -> Optional[Dict[str, Any]]:
        if not self._init_data:
            await self.get_tg_web_data()

        url = f"{self.API_URL}engine/contribute?auth_data={self._init_data}"
        payload = self._generate_payload()

        return await self.make_request(
            "POST",
            url,
            json=payload,
            headers={"Content-Type": "application/json"}
        )

    async def fetch_missions(self) -> Optional[Dict[str, Any]]:
        if not self._init_data:
            await self.get_tg_web_data()

        url = f"{self.API_URL}missions?auth_data={self._init_data}"
        return await self.make_request("GET", url)

    async def fetch_daily_tasks(self) -> Optional[Dict[str, Any]]:
        if not self._init_data:
            await self.get_tg_web_data()

        url = f"{self.API_URL}daily-tasks?auth_data={self._init_data}"
        return await self.make_request("GET", url)

    async def complete_mission(self, mission_id: int) -> bool:
        if not self._init_data:
            await self.get_tg_web_data()

        url = f"{self.API_URL}missions/complete?mission_id={mission_id}&auth_data={self._init_data}"
        result = await self.make_request("GET", url)
        return result is not None and result.get("result") == "done"

    async def complete_daily_task(self, task_id: int) -> bool:
        if not self._init_data:
            await self.get_tg_web_data()

        url = f"{self.API_URL}daily-tasks/complete?task_id={task_id}&auth_data={self._init_data}"
        result = await self.make_request("GET", url)
        return result is not None and result.get("result") == "done"

    async def process_missions(self) -> None:
        try:
            missions_data = await self.fetch_missions()
            if missions_data and "result" in missions_data:
                for mission in missions_data["result"]:
                    # Пропускаем задания, которые требуют реальных действий/платежей
                    if (
                        mission["type"] in ["invite_friend", "donation"] or
                        "Buy Hivera" in mission.get("name", "") or
                        mission.get("cost", {}).get("TON") is not None
                    ):
                        continue

                    if not mission["complete"]:
                        mission_id = mission["id"]
                        if await self.complete_mission(mission_id):
                            logger.info(f"✅ {self.session_name} | Completed mission {mission['name']}")
                            await asyncio.sleep(uniform(settings.MISSION_DELAY[0], settings.MISSION_DELAY[1]))

            daily_tasks = await self.fetch_daily_tasks()
            if daily_tasks and "result" in daily_tasks:
                for task in daily_tasks["result"]:
                    if task["type"] == "restore_power":
                        continue

                    if not task["complete"]:
                        task_id = task["id"]
                        if await self.complete_daily_task(task_id):
                            logger.info(f"✅ {self.session_name} | Completed daily task {task['name']}")
                            await asyncio.sleep(uniform(settings.MISSION_DELAY[0], settings.MISSION_DELAY[1]))

        except Exception as e:
            logger.error(f"❌ {self.session_name} | Error processing missions: {str(e)}")

    async def activate_referral(self) -> None:
        if not self._init_data:
            await self.get_tg_web_data()
            
        url = f"{self.API_URL}referral?referral_code={self.get_ref_id()}&auth_data={self._init_data}"
        await self.make_request("GET", url)


async def run_tapper(tg_client: UniversalTelegramClient):
    bot = BaseBot(tg_client=tg_client)
    try:
        await bot.run()
    except InvalidSession as e:
        error_msg = str(e)
        logger.error(
            f"❌ {bot.session_name} | Error: {error_msg}"
        )
        
        inactive_dir = Path("sessions/inactive")
        inactive_dir.mkdir(parents=True, exist_ok=True)
        
        session_paths = [
            Path("sessions"),
            Path("sessions/pyrogram"),
            Path("sessions/telethon")
        ]
        
        session_files = []
        for path in session_paths:
            session_files.extend([
                path / f"{bot.session_name}.session",
                path / f"{bot.session_name}.session-journal",
                path / f"{bot.session_name}.session.session",
                path / f"{bot.session_name}.session.session-journal"
            ])
        
        moved = False
        for session_file in session_files:
            if session_file.exists():
                try:
                    target_dir = inactive_dir / session_file.parent.name
                    target_dir.mkdir(exist_ok=True)
                    
                    shutil.move(
                        session_file,
                        target_dir / session_file.name
                    )
                    moved = True
                except Exception as move_error:
                    logger.error(f"Failed to move session file {session_file}: {str(move_error)}")
        
        if moved:
            info_file = inactive_dir / "deactivation_info.json"
            try:
                if info_file.exists():
                    with open(info_file, 'r', encoding='utf-8') as f:
                        info = json.load(f)
                else:
                    info = {}
                
                info[bot.session_name] = {
                    "date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "reason": error_msg,
                    "proxy": bot._current_proxy,
                    "type": "unauthorized"
                }
                
                with open(info_file, 'w', encoding='utf-8') as f:
                    json.dump(info, f, indent=4, ensure_ascii=False)
                    
                logger.info(f"⚠️ Session {bot.session_name} deactivated: {error_msg}")
            except Exception as e:
                logger.error(f"Failed to save deactivation info: {str(e)}")
        else:
            logger.warning(f"⚠️ No session files found for {bot.session_name}")
