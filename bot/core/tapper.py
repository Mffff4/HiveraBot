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
from datetime import datetime, timedelta
import traceback

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
        self._user_agent: Optional[str] = None
        self.lock = asyncio.Lock()

        session_config = config_utils.get_session_config(self.session_name, CONFIG_PATH)
        if not all(key in session_config for key in ('api', 'user_agent')):
            logger.critical("🚨 CHECK accounts_config.json as it might be corrupted")
            exit(-1)

        self.proxy = session_config.get('proxy')
        self._user_agent = session_config.get('user_agent')
        
        if self.proxy:
            proxy = Proxy.from_str(self.proxy)
            self.tg_client.set_proxy(proxy)
            self._current_proxy = self.proxy

        self._last_power_check = None
        self._last_power_value = None
        self._power_restore_rate = 4.0

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
                
                if not '%' in tg_web_data:
                    tg_web_data = urlencode({'auth_data': tg_web_data})
                    tg_web_data = tg_web_data.split('auth_data=')[1]

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

    async def make_request(self, method: str, url: str, skip_cache: bool = False, **kwargs) -> Optional[Dict]:
        if not self._http_client:
            raise InvalidSession("HTTP client not initialized")

        headers = HIVERA_HEADERS.copy()
        
        if skip_cache:
            headers.update({
                'cache-control': 'no-cache, no-store, must-revalidate',
                'pragma': 'no-cache',
                'expires': '0'
            })

        if 'headers' in kwargs:
            headers.update(kwargs.pop('headers'))
        kwargs['headers'] = headers

        VALID_ERRORS = {
            "insufficient power",
            "invalid invite code",
            "locking status"
        }

        max_retries = 3
        retry_delay = 10

        for attempt in range(max_retries):
            try:
                if "auth_data=" in url and not '%' in url:
                    parts = url.split("auth_data=")
                    url = f"{parts[0]}auth_data={urlencode({'data': parts[1]}).split('data=')[1]}"

                async with getattr(self._http_client, method.lower())(url, **kwargs) as response:
                    response_text = await response.text()
                    
                    if response.status != 200:
                        logger.error(
                            f"❌ {self.session_name} | Response error:\n"
                            f"Status: {response.status}\n"
                            f"URL: {url}\n"
                            f"Headers: {json.dumps(dict(response.headers), indent=2)}\n"
                            f"Body: {response_text}"
                        )

                    if "Error 1015" in response_text or "You are being rate limited" in response_text:
                        if attempt < max_retries - 1:
                            delay = retry_delay * (attempt + 1)
                            logger.warning(f"⚠️ {self.session_name} | Cloudflare block, waiting {delay}s...")
                            await asyncio.sleep(delay)
                            continue
                        return None

                    try:
                        result = json.loads(response_text) if response_text else None
                    except json.JSONDecodeError:
                        if "<!DOCTYPE html>" in response_text:
                            if attempt < max_retries - 1:
                                delay = retry_delay * (attempt + 1)
                                logger.warning(f"⚠️ {self.session_name} | Got HTML response, retrying in {delay}s")
                                await asyncio.sleep(delay)
                                continue
                            return None
                        
                        if not response_text.strip():
                            if attempt < max_retries - 1:
                                logger.warning(f"⚠️ {self.session_name} | Empty response, retrying...")
                                await asyncio.sleep(retry_delay)
                                continue
                            return None
                        
                        logger.error(
                            f"❌ {self.session_name} | JSON parse error:\n"
                            f"URL: {url}\n"
                            f"Response text: {response_text}"
                        )
                        if attempt < max_retries - 1:
                            await asyncio.sleep(retry_delay)
                            continue
                        return None

                    if hasattr(self._http_client, 'cookie_jar'):
                        self._http_client.cookie_jar.update_cookies(response.cookies)

                    if result and "error" in result and result["error"] in VALID_ERRORS:
                        return result

                    if response.status != 200:
                        if attempt < max_retries - 1:
                            await asyncio.sleep(retry_delay)
                            continue
                        return None

                    return result

            except aiohttp.ClientError as e:
                logger.error(f"❌ {self.session_name} | Network error: {str(e)}")
                if attempt < max_retries - 1:
                    await asyncio.sleep(retry_delay)
                    continue
                return None
            except Exception as e:
                logger.error(f"❌ {self.session_name} | Request error: {str(e)}")
                if attempt < max_retries - 1:
                    await asyncio.sleep(retry_delay)
                    continue
                return None

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

            activities_task = None
            logo_task = None

            while True:
                if activities_task is None or activities_task.done():
                    activities_task = asyncio.create_task(self.send_activities())
                
                if logo_task is None or logo_task.done():
                    logo_task = asyncio.create_task(self.request_logo())

                contribute_data = await self.contribute()
                if not contribute_data:
                    logger.error(f"❌ {self.session_name} | Failed to get contribute data")
                    await asyncio.sleep(10)
                    continue

                if "error" in contribute_data:
                    error = contribute_data["error"]
                    if error == "insufficient power":
                        restore_delay = uniform(
                            settings.POWER_RESTORE_DELAY[0],
                            settings.POWER_RESTORE_DELAY[1]
                        )
                        next_mining_time = datetime.now() + timedelta(seconds=restore_delay)
                        logger.warning(
                            f"⚠️ {self.session_name} | "
                            f"Power: {self._power}/{self._power_capacity} | "
                            f"Insufficient power, waiting {int(restore_delay)}s | "
                            f"Next mining at: {next_mining_time.strftime('%H:%M:%S')}"
                        )
                        await asyncio.sleep(restore_delay)
                    elif error == "locking status":
                        await asyncio.sleep(60)
                    elif error == "invalid invite code":
                        continue
                    else:
                        logger.error(f"❌ {self.session_name} | Mining error: {error}")
                        await asyncio.sleep(10)
                else:
                    logger.success(
                        f"✅ {self.session_name} | "
                        f"Mining successful | "
                        f"Username: {self._username} | "
                        f"Hivera: {self._hivera} | "
                        f"Power: {self._power}/{self._power_capacity}"
                    )
                    await asyncio.sleep(uniform(settings.MINING_DELAY[0], settings.MINING_DELAY[1]))

        except InvalidSession as e:
            logger.error(f"❌ {self.session_name} | Username: {self._username or 'Unknown'} | Error: {str(e)}")
            raise
        except Exception as e:
            logger.error(f"❌ {self.session_name} | Username: {self._username or 'Unknown'} | Error: {str(e)}")
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
            if not missions_data or "result" not in missions_data:
                logger.warning(f"⚠️ {self.session_name} | Failed to fetch missions data")
                return

            logger.debug(f"📝 {self.session_name} | Received missions data: {json.dumps(missions_data, indent=2)}")

            for mission in missions_data.get("result", []):
                try:
                    if not isinstance(mission, dict):
                        logger.warning(f"⚠️ {self.session_name} | Invalid mission format: {mission}")
                        continue

                    mission_name = mission.get("name", "Unknown")
                    mission_type = mission.get("type")
                    mission_complete = mission.get("complete")
                    mission_cost = mission.get("cost")

                    logger.debug(
                        f"📝 {self.session_name} | Processing mission: "
                        f"name={mission_name}, "
                        f"type={mission_type}, "
                        f"complete={mission_complete}, "
                        f"cost={mission_cost}"
                    )

                    if (
                        mission_type in ["invite_friend", "donation"] or
                        "Buy Hivera" in mission_name or
                        (mission_cost and mission_cost.get("TON") is not None) or
                        mission_complete is True
                    ):
                        continue

                    mission_id = mission.get("id")
                    if mission_id is None:
                        logger.warning(f"⚠️ {self.session_name} | Missing mission ID for {mission_name}")
                        continue

                    result = await self.complete_mission(mission_id)
                    if result:
                        logger.info(
                            f"✅ {self.session_name} | "
                            f"Completed mission: {mission_name} | "
                            f"Reward: {mission.get('reward', {})}"
                        )
                        await asyncio.sleep(uniform(
                            settings.MISSION_DELAY[0], 
                            settings.MISSION_DELAY[1]
                        ))

                except Exception as mission_error:
                    logger.error(
                        f"❌ {self.session_name} | "
                        f"Error processing mission {mission.get('name', 'Unknown')}: "
                        f"{str(mission_error)}\n"
                        f"Mission data: {json.dumps(mission, indent=2)}"
                    )
                    continue

            daily_tasks = await self.fetch_daily_tasks()
            if not daily_tasks or "result" not in daily_tasks:
                logger.warning(f"⚠️ {self.session_name} | Failed to fetch daily tasks")
                return

            logger.debug(f"📝 {self.session_name} | Received daily tasks: {json.dumps(daily_tasks, indent=2)}")

            for task in daily_tasks.get("result", []):
                try:
                    if not isinstance(task, dict):
                        logger.warning(f"⚠️ {self.session_name} | Invalid task format: {task}")
                        continue

                    task_name = task.get("name", "Unknown")
                    task_type = task.get("type")
                    task_complete = task.get("complete")

                    logger.debug(
                        f"📝 {self.session_name} | Processing task: "
                        f"name={task_name}, "
                        f"type={task_type}, "
                        f"complete={task_complete}"
                    )

                    if task_type == "restore_power" or task_complete is True:
                        continue

                    task_id = task.get("id")
                    if task_id is None:
                        logger.warning(f"⚠️ {self.session_name} | Missing task ID for {task_name}")
                        continue

                    result = await self.complete_daily_task(task_id)
                    if result:
                        logger.info(
                            f"✅ {self.session_name} | "
                            f"Completed daily task: {task_name} | "
                            f"Reward: {task.get('reward', {})}"
                        )
                        await asyncio.sleep(uniform(
                            settings.MISSION_DELAY[0],
                            settings.MISSION_DELAY[1]
                        ))

                except Exception as task_error:
                    logger.error(
                        f"❌ {self.session_name} | "
                        f"Error processing daily task {task.get('name', 'Unknown')}: "
                        f"{str(task_error)}\n"
                        f"Task data: {json.dumps(task, indent=2)}"
                    )
                    continue

        except Exception as e:
            logger.error(
                f"❌ {self.session_name} | "
                f"Error processing missions: {str(e)}\n"
                f"Traceback: {traceback.format_exc()}"
            )

    async def activate_referral(self) -> None:
        if not self._init_data:
            await self.get_tg_web_data()
            
        url = f"{self.API_URL}referral?referral_code={self.get_ref_id()}&auth_data={self._init_data}"
        await self.make_request("GET", url)

    async def contribute(self) -> Optional[Dict]:
        """Майнинг через v2/engine/contribute"""
        if not self._init_data:
            await self.get_tg_web_data()
        
        timestamp = int(time() * 1000)
        url = f"{self.API_URL}v2/engine/contribute?auth_data={self._init_data}"
        
        headers = {
            'accept': 'application/json',
            'accept-language': 'ru,en-US;q=0.9,en;q=0.8',
            'cache-control': 'no-cache',
            'content-type': 'application/json',
            'dnt': '1',
            'origin': 'https://app.hivera.org',
            'pragma': 'no-cache',
            'priority': 'u=1, i',
            'referer': 'https://app.hivera.org/',
            'sec-ch-ua': '"Chromium";v="131", "Not_A Brand";v="24"',
            'sec-ch-ua-mobile': '?0',
            'sec-ch-ua-platform': '"macOS"',
            'sec-fetch-dest': 'empty',
            'sec-fetch-mode': 'cors',
            'sec-fetch-site': 'same-site',
            'user-agent': self._user_agent
        }
        
        data = {
            "from_date": timestamp,
            "quality_connection": randint(85, 95),
            "times": 1
        }
        
        try:
            result = await self.make_request(
                method="POST",
                url=url,
                headers=headers,
                json=data
            )

            if result:
                if "result" in result and "profile" in result["result"]:
                    profile = result["result"]["profile"]
                    self._power = profile.get("POWER", 0)
                    self._hivera = profile.get("HIVERA", 0)
                    self._power_capacity = profile.get("POWER_CAPACITY", 0)
                elif "error" in result:
                    info_url = f"{self.API_URL}engine/info?auth_data={self._init_data}"
                    info_data = await self.make_request("GET", info_url)
                    if info_data and "result" in info_data and "profile" in info_data["result"]:
                        profile = info_data["result"]["profile"]
                        self._power = profile.get("POWER", 0)
                        self._hivera = profile.get("HIVERA", 0)
                        self._power_capacity = profile.get("POWER_CAPACITY", 0)
                    else:
                        logger.error(f"❌ {self.session_name} | Failed to get info data")
                else:
                    logger.error(f"❌ {self.session_name} | Empty response from contribute")

            return result
        except Exception as e:
            logger.error(f"❌ {self.session_name} | Contribute error: {str(e)}")
            return None

    async def calculate_power_restore_rate(self) -> float:
        current_time = time()
        
        if self._last_power_check is None or self._last_power_value is None:
            self._last_power_check = current_time
            self._last_power_value = self._power
            return self._power_restore_rate
        
        time_diff = current_time - self._last_power_check
        power_diff = self._power - self._last_power_value
        
        if time_diff > 0:
            new_rate = power_diff / time_diff
            
            if time_diff >= 5 and power_diff > 0:
                if 0.5 <= new_rate <= 10:
                    self._power_restore_rate = new_rate
                    logger.debug(
                        f"📊 {self.session_name} | "
                        f"Power restore rate updated: {new_rate:.2f} power/s | "
                        f"Power diff: {power_diff} | Time diff: {time_diff:.1f}s"
                    )
                else:
                    logger.debug(
                        f"📊 {self.session_name} | "
                        f"Skipped invalid rate: {new_rate:.2f} power/s | "
                        f"Power diff: {power_diff} | Time diff: {time_diff:.1f}s"
                    )
        
        if time_diff >= 5:
            self._last_power_check = current_time
            self._last_power_value = self._power
        
        return self._power_restore_rate

    async def send_activities(self) -> None:
        """Отправка activities каждые 30 секунд"""
        while True:
            try:
                url = f"{self.API_URL}engine/activities?auth_data={self._init_data}"
                await self.make_request("GET", url)
                await asyncio.sleep(30)
            except Exception as e:
                logger.error(f"❌ {self.session_name} | Activities error: {str(e)}")
                await asyncio.sleep(30)

    async def request_logo(self) -> None:
        """Запрос logo.png каждую секунду"""
        while True:
            try:
                headers = {
                    'accept': '*/*',
                    'accept-language': 'ru,en-US;q=0.9,en;q=0.8',
                    'baggage': 'sentry-environment=production,sentry-release=5d7515cb326716016e6209d847ff4d6a484fe6e2,sentry-public_key=980928cd76789d6bd22ca2fbf961fa16,sentry-trace_id=c079ec0e17134a40b50ec0f9dacbf329',
                    'cache-control': 'no-cache',
                    'dnt': '1',
                    'pragma': 'no-cache',
                    'priority': 'u=1, i',
                    'referer': 'https://app.hivera.org/',
                    'sec-ch-ua': '"Chromium";v="131", "Not_A Brand";v="24"',
                    'sec-ch-ua-mobile': '?0',
                    'sec-ch-ua-platform': '"macOS"',
                    'sec-fetch-dest': 'empty',
                    'sec-fetch-mode': 'cors',
                    'sec-fetch-site': 'same-origin',
                    'sentry-trace': 'c079ec0e17134a40b50ec0f9dacbf329-bf5161f066015c3b',
                    'user-agent': self._user_agent
                }
                
                url = "https://app.hivera.org/logo.png"
                async with self._http_client.get(url, headers=headers) as response:
                    await response.read()
                await asyncio.sleep(1)
            except Exception as e:
                logger.error(f"❌ {self.session_name} | Logo request error: {str(e)}")
                await asyncio.sleep(1)

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
