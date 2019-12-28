import argparse
import hmac
import io
import subprocess
import time
import traceback
from select import select
from threading import Thread
from typing import List, Tuple

import evdev
import flask
import libvirt
import xmltodict
import yaml
from flask import Flask, request

class LibvirtConfig:
    def __init__(self, data: dict):
        self.uri: str = data["uri"]
        self.domain: str = data["domain"]

class HTTPConfig:
    def __init__(self, data: dict):
        addr = data["address"].split(":")
        self.host: str = addr[0]
        self.port: int = int(addr[1])
        self.enabled: bool = data["enabled"]
        self._security = data["security"]

    @property
    def is_secure(self) -> bool:
        return self._security["enabled"]

    @property
    def secret(self) -> str:
        return self._security["secret"]

class EvdevConfig:
    def __init__(self, data: dict):
        self.enabled: bool = data["enabled"]
        self.device: bool = data["device"]

class CommandsConfig:
    def __init__(self, data: dict):
        self.host_commands: List[str] = data.get("host", [])
        self.guest_commands: List[str] = data.get("guest", [])

class Config:
    def __init__(self, data: dict):
        self.http = HTTPConfig(data["http"])
        self.evdev = EvdevConfig(data["evdev"])
        self.devices = [(d["vendor"], d["product"]) for d in data["devices"]]
        self.devices_essential = [(d["vendor"], d["product"]) for d in data["devices"] if not d.get("optional", False)]
        self.displays = data["displays"]
        self.libvirt = LibvirtConfig(data["libvirt"])
        self.commands = CommandsConfig(data.get("commands", {}))

    @staticmethod
    def load(filename: str):
        with io.open(filename) as f:
            return Config(yaml.safe_load(f))

class Virt:
    def __init__(self, uri: str, domain: str):
        self._con = libvirt.open(uri)
        self._dom = self._con.lookupByName(domain)

    def get_devices(self) -> List[dict]:
        devs = []

        desc = xmltodict.parse(self._dom.XMLDesc())
        for dev in desc["domain"]["devices"]["hostdev"]:
            if dev["@type"] == "usb":
                devs.append(dev)

        return devs

    @staticmethod
    def get_device_ids(desc: dict) -> Tuple[int, int]:
        return (int(desc["source"]["vendor"]["@id"], 16),
                int(desc["source"]["product"]["@id"], 16))

    def get_device_by_ids(self, ids: Tuple[int, int]) -> dict:
        for dev in self.get_devices():
            if self.get_device_ids(dev) == ids:
                return dev

        return None

    def attach_devices(self, devs: List[dict]):
        for ids in devs:
            dev = self.get_device_by_ids(ids)
            if dev is None:
                dev = xmltodict.unparse({
                    "hostdev": {
                        "@mode": "subsystem",
                        "@type": "usb",
                        "source": {
                            "vendor": {"@id": hex(ids[0])},
                            "product": {"@id": hex(ids[1])}
                        }
                    }
                })
                self._dom.attachDevice(dev)

    def detach_devices(self, devs: List[dict]):
        for dev in self.get_devices():
            if self.get_device_ids(dev) in devs:
                xml = xmltodict.unparse({"hostdev": dev})
                self._dom.detachDevice(xml)

class Switch:
    def __init__(self, config: Config):
        self.config = config
        self.virt = Virt(config.libvirt.uri, config.libvirt.domain)

    @staticmethod
    def _call_dccutil(display: dict, ident: int):
        return subprocess.call([
            "ddcutil",
            "--bus", str(display["bus"]),
            "setvcp", hex(display["feature"]), hex(ident)
        ])

    @staticmethod
    def _call_commands(command: str):
        return subprocess.call(command, shell=True)

    def switch_to_host(self, skip_optional=False):
        for display in self.config.displays:
            self._call_dccutil(display, display["host"])
        for command in self.config.commands.host_commands:
            self._call_commands(command)
        self.virt.detach_devices(self.config.devices_essential if skip_optional else self.config.devices)

    def switch_to_guest(self, skip_optional=False):
        for display in self.config.displays:
            self._call_dccutil(display, display["guest"])
        for command in self.config.commands.guest_commands:
            self._call_commands(command)
        self.virt.attach_devices(self.config.devices_essential if skip_optional else self.config.devices)

switch: Switch = None
app = Flask(__name__)

@app.route("/switch", methods=["POST"])
def app_switch():
    if switch.config.http.is_secure:
        secret = request.headers.get("X-Secret")
        if secret is None \
           or not hmac.compare_digest(switch.config.http.secret, secret):
            flask.abort(403)

    cases = {
        "host": switch.switch_to_host,
        "guest": switch.switch_to_guest
    }

    if not request.json \
       or not "to" in request.json \
       or not request.json["to"] in cases:
        flask.abort(400)

    error = None
    try:
        cases[request.json["to"]](request.json.get("skip_optional", False))
    except:
        error = traceback.format_exc()

    return flask.jsonify({"success": True, "error": error})

def evdev_loop(device_name: str):
    device = evdev.InputDevice(device_name)
    triggered = False
    grabbed = False
    skip_optional = False
    pressed_keys = {}

    while True:
        select([device], [], [], 0.25)
        try:
            for event in device.read():
                pressed_keys[event.code] = event.value != 0
                if not grabbed:
                    ctrls_pressed = pressed_keys.get(evdev.ecodes.KEY_LEFTCTRL, False) and \
                                    pressed_keys.get(evdev.ecodes.KEY_RIGHTCTRL, False)
                    if ctrls_pressed:
                        triggered = True
                        skip_optional = pressed_keys.get(evdev.ecodes.KEY_LEFTMETA, False) or \
                                        pressed_keys.get(evdev.ecodes.KEY_RIGHTMETA, False)
                    elif triggered and not ctrls_pressed:
                        triggered = False
                        if not grabbed:
                            grabbed = True
                            switch.switch_to_guest(skip_optional)
                            continue
        except BlockingIOError:
            if grabbed:
                try:
                    device.grab()
                except IOError:
                    pass
                else:
                    device.ungrab()
                    grabbed = False
                    switch.switch_to_host()
        except OSError:
            while True:
                try:
                    device = evdev.InputDevice(device_name)
                except OSError:
                    time.sleep(5)
                else:
                    print(f'Reconnected to {device_name}')
                    break


def main():
    parser = argparse.ArgumentParser(description="The poor man's KVM switch for libvirt and VFIO users", formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument("--config", dest="config", required=True, help="the YAML configuration file")
    args = parser.parse_args()

    global switch
    config = Config.load(args.config)
    switch = Switch(config)

    threads = []
    if config.http.enabled:
        print('http thread started')
        threads.append(Thread(target=app.run, args=(config.http.host, config.http.port, )).start())

    if config.evdev.enabled:
        print('evdev thread started')
        threads.append(Thread(target=evdev_loop, args=(config.evdev.device, )).start())

    map(lambda t: t.join(), threads)
