# Todo: class for printer states!
from datetime import datetime, timedelta
from io import BytesIO
import logging
import re
import time
from typing import List, Tuple
import urllib

from PIL import Image  # type: ignore
import emoji
import requests
import ujson

from configuration import ConfigWrapper
from power_device import PowerDevice

requests.models.complexjson = ujson  # type: ignore

logger = logging.getLogger(__name__)


class Klippy:
    _DATA_MACRO = "bot_data"

    def __init__(
        self,
        config: ConfigWrapper,
        light_device: PowerDevice,
        psu_device: PowerDevice,
        logging_handler: logging.Handler = None,
    ):
        self._host: str = config.bot.host
        self._hidden_macros: List[str] = config.telegram_ui.hidden_macros + [self._DATA_MACRO]
        self._show_private_macros: bool = config.telegram_ui.show_private_macros
        self._message_parts: List[str] = config.telegram_ui.status_message_content
        self._eta_source: str = config.telegram_ui.eta_source
        self._light_device: PowerDevice = light_device
        self._psu_device: PowerDevice = psu_device
        self._sensors_list: List[str] = config.telegram_ui.status_message_sensors
        self._heaters_list: List[str] = config.telegram_ui.status_message_heaters
        self._heater_fans_list: List[str] = config.telegram_ui.status_message_heater_fans
        self._controller_fans: List[str] = config.telegram_ui.status_message_controller_fans
        self._temp_fans_list: List[str] = config.telegram_ui.status_message_temperature_fans
        self._generic_fans: List[str] = config.telegram_ui.status_message_generic_fans

        self._devices_list: List[str] = config.telegram_ui.status_message_devices
        self._user: str = config.bot.user
        self._passwd: str = config.bot.passwd
        self._api_token: str = config.bot.api_token

        self._dbname: str = "telegram-bot"

        self._connected: bool = False
        self.printing: bool = False
        self.paused: bool = False
        self.state: str = ""
        self.state_message: str = ""

        self.printing_duration: float = 0.0
        self.printing_progress: float = 0.0
        self.printing_height: float = 0.0
        self._printing_filename: str = ""
        self.file_estimated_time: float = 0.0
        self.file_print_start_time: float = 0.0
        self.vsd_progress: float = 0.0

        self.filament_used: float = 0.0
        self.filament_total: float = 0.0
        self.filament_weight: float = 0.0
        self._thumbnail_path: str = ""

        self._jwt_token: str = ""
        self._refresh_token: str = ""

        # Todo: create sensors class!!
        self._sensors_dict: dict = {}
        self._power_devices: dict = {}

        if logging_handler:
            logger.addHandler(logging_handler)
        if config.bot.debug:
            logger.setLevel(logging.DEBUG)

        self._auth_moonraker()

    def prepare_sens_dict_subscribe(self):
        self._sensors_dict = {}
        sens_dict = {}
        for heat in self._heaters_list:
            if heat in ["extruder", "heater_bed"]:
                sens_dict[heat] = None
            else:
                sens_dict[f"heater_generic {heat}"] = None

        for sens in self._sensors_list:
            sens_dict[f"temperature_sensor {sens}"] = None

        for h_fan in self._heater_fans_list:
            if h_fan in ["fan"]:
                sens_dict[h_fan] = None
            else:
                sens_dict[f"heater_fan {h_fan}"] = None

        for c_fan in self._controller_fans:
            sens_dict[f"controller_fan {c_fan}"] = None

        for t_fan in self._temp_fans_list:
            sens_dict[f"temperature_fan {t_fan}"] = None

        for g_fan in self._generic_fans:
            sens_dict[f"fan_generic {g_fan}"] = None
        return sens_dict

    def _filament_weight_used(self) -> float:
        return self.filament_weight * (self.filament_used / self.filament_total)

    @property
    def connected(self) -> bool:
        return self._connected

    @connected.setter
    def connected(self, new_value: bool) -> None:
        self._connected = new_value
        self.printing = False
        self.paused = False
        self._reset_file_info()

    # Todo: save macros list until klippy restart
    @property
    def macros(self) -> List[str]:
        return self._get_marco_list()

    @property
    def macros_all(self) -> List[str]:
        return self._get_full_marco_list()

    @property
    def moonraker_host(self) -> str:
        return self._host

    @property
    def _headers(self):
        heads = {}
        if self._jwt_token:
            heads = {"Authorization": f"Bearer {self._jwt_token}"}
        elif self._api_token:
            heads = {"X-Api-Key": self._api_token}
        return heads

    @property
    def one_shot_token(self) -> str:
        if (not self._user and not self._jwt_token) and not self._api_token:
            return ""

        resp = requests.get(f"http://{self._host}/access/oneshot_token", headers=self._headers)
        if resp.ok:
            res = f"?token={resp.json()['result']}"
        else:
            logger.error(resp.reason)
            res = ""
        return res

    def _reset_file_info(self) -> None:
        self.printing_duration = 0.0
        self.printing_progress = 0.0
        self.printing_height = 0.0
        self._printing_filename = ""
        self.file_estimated_time = 0.0
        self.file_print_start_time = 0.0
        self.vsd_progress = 0.0

        self.filament_used = 0.0
        self.filament_total = 0.0
        self.filament_weight = 0.0
        self._thumbnail_path = ""

    @property
    def printing_filename(self) -> str:
        return self._printing_filename

    @printing_filename.setter
    def printing_filename(self, new_value: str):
        if not new_value:
            self._reset_file_info()
            return

        response = self._make_request(f"http://{self._host}/server/files/metadata?filename={urllib.parse.quote(new_value)}", "GET")
        # Todo: add response status check!
        resp = response.json()["result"]
        self._printing_filename = new_value
        self.file_estimated_time = resp["estimated_time"] if resp["estimated_time"] else 0.0
        self.file_print_start_time = resp["print_start_time"] if resp["print_start_time"] else time.time()
        self.filament_total = resp["filament_total"] if "filament_total" in resp else 0.0
        self.filament_weight = resp["filament_weight_total"] if "filament_weight_total" in resp else 0.0

        if "thumbnails" in resp and "filename" in resp:
            thumb = max(resp["thumbnails"], key=lambda el: el["size"])
            file_dir = resp["filename"].rpartition("/")[0]
            if file_dir:
                self._thumbnail_path = f'{file_dir}/{thumb["relative_path"]}'
            else:
                self._thumbnail_path = thumb["relative_path"]
        else:
            if "filename" not in resp:
                logger.error('"filename" field is not present in response: %s', resp.json())
            if "thumbnails" not in resp:
                logger.error('"thumbnails" field is not present in response: %s', resp.json())

    @property
    def printing_filename_with_time(self) -> str:
        return f"{self._printing_filename}_{datetime.fromtimestamp(self.file_print_start_time):%Y-%m-%d_%H-%M}"

    def _get_full_marco_list(self) -> List[str]:
        resp = self._make_request(f"http://{self._host}/printer/objects/list", "GET")
        if not resp.ok:
            return []
        macro_lines = list(filter(lambda it: "gcode_macro" in it, resp.json()["result"]["objects"]))
        loaded_macros = list(map(lambda el: el.split(" ")[1].upper(), macro_lines))
        return loaded_macros

    def _get_marco_list(self) -> List[str]:
        return [key for key in self._get_full_marco_list() if key not in self._hidden_macros and (True if self._show_private_macros else not key.startswith("_"))]

    def _auth_moonraker(self) -> None:
        if not self._user or not self._passwd:
            return
        # TOdo: add try catch
        res = requests.post(
            f"http://{self._host}/access/login",
            json={"username": self._user, "password": self._passwd},
        )
        if res.ok:
            self._jwt_token = res.json()["result"]["token"]
            self._refresh_token = res.json()["result"]["refresh_token"]
        else:
            logger.error(res.reason)

    def _refresh_moonraker_token(self) -> None:
        if not self._refresh_token:
            return
        res = requests.post(f"http://{self._host}/access/refresh_jwt", json={"refresh_token": self._refresh_token})
        if res.ok:
            logger.debug("JWT token successfully refreshed")
            self._jwt_token = res.json()["result"]["token"]
        else:
            logger.error("Failed to refresh token: %s", res.reason)

    def _make_request(self, url, method, json=None, stream=None, files=None) -> requests.Response:
        res = requests.request(method, url, headers=self._headers, json=json, stream=stream, files=files)
        if res.status_code == 401:  # Unauthorized
            logger.debug("JWT token expired, refreshing...")
            self._refresh_moonraker_token()
            res = requests.request(method, url, headers=self._headers, json=json, stream=stream, files=files)
        return res

    def check_connection(self) -> str:
        try:
            response = requests.get(f"http://{self._host}/printer/info", headers=self._headers, timeout=2)
            return "" if response.ok else f"Connection failed. {response.reason}"
        except Exception as ex:
            logger.error(ex, exc_info=True)
            return "Connection failed."

    def update_sensror(self, name: str, value) -> None:
        if name in self._sensors_dict:
            if "temperature" in value:
                self._sensors_dict[name]["temperature"] = value["temperature"]
            if "target" in value:
                self._sensors_dict[name]["target"] = value["target"]
            if "power" in value:
                self._sensors_dict[name]["power"] = value["power"]
            if "speed" in value:
                self._sensors_dict[name]["speed"] = value["speed"]
            if "rpm" in value:
                self._sensors_dict[name]["rpm"] = value["rpm"]
        elif value:
            self._sensors_dict[name] = value

    @staticmethod
    def _sensor_message(name: str, value) -> str:
        sens_name = re.sub(r"([A-Z]|\d|_)", r" \1", name).replace("_", "")
        message = ""
        if "power" in value:
            message = emoji.emojize(" :hotsprings: ", language="alias") + f"{sens_name.title()}: {round(value['temperature'])}"
            if "target" in value and value["target"] > 0.0 and abs(value["target"] - value["temperature"]) > 2:
                message += emoji.emojize(" :arrow_right: ", language="alias") + f"{round(value['target'])}"
            if value["power"] > 0.0:
                message += emoji.emojize(" :fire: ", language="alias")
        elif "speed" in value:
            message = emoji.emojize(" :tornado: ", language="alias") + f"{sens_name.title()}:"
            if "temperature" in value:
                message += f" {round(value['temperature'])}"
            if "target" in value and value["target"] > 0.0 and abs(value["target"] - value["temperature"]) > 2:
                message += emoji.emojize(" :arrow_right: ", language="alias") + f"{round(value['target'])}"
            if "speed" in value:
                message += f" {round(value['speed'] * 100)}%"
            if "rpm" in value and value["rpm"] is not None:
                message += f" {round(value['rpm'])} RPM"
        elif "temperature" in value:
            message = emoji.emojize(" :thermometer: ", language="alias") + f"{sens_name.title()}: {round(value['temperature'])}"
        if message:
            message += "\n"
        return message

    def update_power_device(self, name: str, value) -> None:
        if name in self._power_devices:
            if "device" in value:
                self._power_devices[name]["device"] = value["device"]
            if "status" in value:
                self._power_devices[name]["status"] = value["status"]
            if "locked_while_printing" in value:
                self._power_devices[name]["locked_while_printing"] = value["locked_while_printing"]
            if "type" in value:
                self._power_devices[name]["type"] = value["type"]
            if "is_shutdown" in value:
                self._power_devices[name]["is_shutdown"] = value["is_shutdown"]
        else:
            self._power_devices[name] = value

    @staticmethod
    def _device_message(name: str, value, emoji_symbol: str = ":vertical_traffic_light:") -> str:
        message = emoji.emojize(f" {emoji_symbol} ", language="alias") + f"{name}: "
        if "status" in value:
            message += f" {value['status']} "
        if "locked_while_printing" in value and value["locked_while_printing"] == "True":
            message += emoji.emojize(" :lock: ", language="alias")
        if message:
            message += "\n"
        return message

    def _get_sensors_message(self) -> str:
        message = ""
        for name, value in self._sensors_dict.items():
            message += self._sensor_message(name, value)
        return message

    def _get_power_devices_mess(self) -> str:
        message = ""
        for name, value in self._power_devices.items():
            if name in self._devices_list:
                if name == self._light_device.name:
                    message += self._device_message(name, value, ":flashlight:")
                elif name == self._psu_device.name:
                    message += self._device_message(name, value, ":electric_plug:")
                else:
                    message += self._device_message(name, value)
        return message

    def execute_command(self, *command) -> None:
        data = {"commands": list(map(lambda el: f"{el}", command))}
        res = self._make_request(f"http://{self._host}/api/printer/command", "POST", json=data)
        if not res.ok:
            logger.error(res.reason)

    def _get_eta(self) -> timedelta:
        if self._eta_source == "slicer":
            eta = int(self.file_estimated_time - self.printing_duration)
        elif self.vsd_progress > 0.0:  # eta by file
            eta = int(self.printing_duration / self.vsd_progress - self.printing_duration)
        else:
            eta = int(self.file_estimated_time)
        eta = max(eta, 0)
        return timedelta(seconds=eta)

    def _populate_with_thumb(self, thumb_path: str, message: str) -> Tuple[str, BytesIO]:
        if not thumb_path:
            img = Image.open("../imgs/nopreview.png").convert("RGB")
            logger.warning("Empty thumbnail_path")
        else:
            response = self._make_request(f"http://{self._host}/server/files/gcodes/{urllib.parse.quote(thumb_path)}", "GET", stream=True)
            if response.ok:
                response.raw.decode_content = True
                img = Image.open(response.raw).convert("RGB")
            else:
                logger.error("Thumbnail download failed for %s \n\n%s", thumb_path, response.reason)
                img = Image.open("../imgs/nopreview.png").convert("RGB")

        bio = BytesIO()
        bio.name = f"{self.printing_filename}.webp"
        img.save(bio, "WebP", quality=0, lossless=True)
        bio.seek(0)
        img.close()
        return message, bio

    def get_file_info(self, message: str = "") -> Tuple[str, BytesIO]:
        message = self.get_print_stats(message)
        return self._populate_with_thumb(self._thumbnail_path, message)

    def _get_printing_file_info(self, message_pre: str = "") -> str:
        message = f"Printing: {self.printing_filename} \n" if not message_pre else f"{message_pre}: {self.printing_filename} \n"
        if "progress" in self._message_parts:
            message += f"Progress {round(self.printing_progress * 100, 0)}%"
        if "height" in self._message_parts:
            message += f", height: {round(self.printing_height, 2)}mm\n" if self.printing_height > 0.0 else "\n"
        if self.filament_total > 0.0:
            if "filament_length" in self._message_parts:
                message += f"Filament: {round(self.filament_used / 1000, 2)}m / {round(self.filament_total / 1000, 2)}m"
            if self.filament_weight > 0.0 and "filament_weight" in self._message_parts:
                message += f", weight: {round(self._filament_weight_used(), 2)}/{self.filament_weight}g"
            message += "\n"
        if "print_duration" in self._message_parts:
            message += f"Printing for {timedelta(seconds=round(self.printing_duration))}\n"

        eta = self._get_eta()
        if "eta" in self._message_parts:
            message += f"Estimated time left: {eta}\n"
        if "finish_time" in self._message_parts:
            message += f"Finish at {datetime.now() + eta:%Y-%m-%d %H:%M}\n"

        return message

    def get_print_stats(self, message_pre: str = "") -> str:
        return self._get_printing_file_info(message_pre) + self._get_sensors_message() + self._get_power_devices_mess()

    def get_status(self) -> str:
        response = self._make_request(f"http://{self._host}/printer/objects/query?webhooks&print_stats&display_status", "GET")
        resp = response.json()["result"]["status"]
        print_stats = resp["print_stats"]
        # webhook = resp['webhooks']
        # message = emoji.emojize(':robot: Klipper status: ', language='alias') + f"{webhook['state']}\n"
        message = ""

        # if 'display_status' in resp and 'message' in resp['display_status']:
        #     msg = resp['display_status']['message']
        #     if msg and msg is not None:
        #         message += f"{msg}\n"
        # if 'state_message' in webhook:
        #     message += f"State message: {webhook['state_message']}\n"

        # message += emoji.emojize(':mechanical_arm: Printing process status: ', language='alias') + f"{print_stats['state']} \n"

        if print_stats["state"] == "printing":
            if not self.printing_filename:
                self.printing_filename = print_stats["filename"]
        elif print_stats["state"] == "paused":
            message += "Printing paused\n"
        elif print_stats["state"] == "complete":
            message += "Printing complete\n"
        elif print_stats["state"] == "standby":
            message += "Printer standby\n"
        elif print_stats["state"] == "error":
            message += "Printing error\n"
            if "message" in print_stats and print_stats["message"]:
                message += f"{print_stats['message']}\n"

        message += "\n"
        if self.printing_filename:
            message += self._get_printing_file_info()

        message += self._get_sensors_message()
        message += self._get_power_devices_mess()

        return message

    def get_file_info_by_name(self, filename: str, message: str) -> Tuple[str, BytesIO]:
        response = self._make_request(f"http://{self._host}/server/files/metadata?filename={urllib.parse.quote(filename)}", "GET")
        # Todo: add response status check!
        resp = response.json()["result"]
        message += "\n"
        if "filament_total" in resp and resp["filament_total"] > 0.0:
            message += f"Filament: {round(resp['filament_total'] / 1000, 2)}m"
            if "filament_weight_total" in resp and resp["filament_weight_total"] > 0.0:
                message += f", weight: {resp['filament_weight_total']}g"
        if "estimated_time" in resp and resp["estimated_time"] > 0.0:
            message += f"\nEstimated printing time: {timedelta(seconds=resp['estimated_time'])}"

        thumb_path = ""
        if "thumbnails" in resp:
            thumb = max(resp["thumbnails"], key=lambda el: el["size"])
            if "relative_path" in thumb and "filename" in resp:
                file_dir = resp["filename"].rpartition("/")[0]
                if file_dir:
                    thumb_path = file_dir + "/"
                thumb_path += thumb["relative_path"]
            else:
                logger.error("Thumbnail relative_path and filename not found in %s", resp)

        return self._populate_with_thumb(thumb_path, message)

    def get_gcode_files(self):
        response = self._make_request(f"http://{self._host}/server/files/list?root=gcodes", "GET")
        resp = response.json()
        files = sorted(resp["result"], key=lambda item: item["modified"], reverse=True)
        return files

    def upload_gcode_file(self, file: BytesIO, upload_path: str) -> bool:
        response = self._make_request(f"http://{self._host}/server/files/upload", "POST", files={"file": file, "root": "gcodes", "path": upload_path})
        return response.ok

    def start_printing_file(self, filename: str) -> bool:
        response = self._make_request(f"http://{self._host}/printer/print/start?filename={urllib.parse.quote(filename)}", "POST")
        return response.ok

    def stop_all(self) -> None:
        self._reset_file_info()

    def add_bot_announcements_feed(self):
        res = self._make_request(f"http://{self._host}/server/announcements/feed?name=moonraker-telegram-bot", "POST")
        if not res.ok:
            logger.warning("Failed adding announcements bot feed.\n\n%s", res.reason)

    # moonraker databse section
    def get_param_from_db(self, param_name: str):
        res = self._make_request(f"http://{self._host}/server/database/item?namespace={self._dbname}&key={param_name}", "GET")
        if res.ok:
            return res.json()["result"]["value"]
        else:
            logger.error("Failed getting %s from %s \n\n%s", param_name, self._dbname, res.reason)
            # Fixme: return default value? check for 404!
            return None

    def save_param_to_db(self, param_name: str, value) -> None:
        data = {"namespace": self._dbname, "key": param_name, "value": value}
        res = self._make_request(f"http://{self._host}/server/database/item", "POST", json=data)
        if not res.ok:
            logger.error("Failed saving %s to %s \n\n%s", param_name, self._dbname, res.reason)

    def delete_param_from_db(self, param_name: str) -> None:
        res = self._make_request(f"http://{self._host}/server/database/item?namespace={self._dbname}&key={param_name}", "DELETE")
        if not res.ok:
            logger.error("Failed getting %s from %s \n\n%s", param_name, self._dbname, res.reason)

    # macro data section
    def save_data_to_marco(self, lapse_size: int, filename: str, path: str) -> None:
        full_macro_list = self._get_full_marco_list()
        if self._DATA_MACRO in full_macro_list:
            self.execute_command(
                f"SET_GCODE_VARIABLE MACRO=bot_data VARIABLE=lapse_video_size VALUE={lapse_size}",
                f"SET_GCODE_VARIABLE MACRO=bot_data VARIABLE=lapse_filename VALUE='\"{filename}\"'",
                f"SET_GCODE_VARIABLE MACRO=bot_data VARIABLE=lapse_path VALUE='\"{path}\"'",
            )

        else:
            logger.error("Marco %s not defined", self._DATA_MACRO)
