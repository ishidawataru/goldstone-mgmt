import queue
import os
import aioredis

from .sonic import *

from goldstone.lib.core import *
from goldstone.lib.errors import (
    InvalArgError,
    LockedError,
    NotFoundError,
    CallbackFailedError,
)

logger = logging.getLogger(__name__)

REDIS_SERVICE_HOST = os.getenv("REDIS_SERVICE_HOST")
REDIS_SERVICE_PORT = os.getenv("REDIS_SERVICE_PORT")

SINGLE_LANE_INTERFACE_TYPES = ["CR", "LR", "SR", "KR"]
DOUBLE_LANE_INTERFACE_TYPES = ["CR2", "LR2", "SR2", "KR2"]
QUAD_LANE_INTERFACE_TYPES = ["CR4", "LR4", "SR4", "KR4"]


class IfChangeHandler(ChangeHandler):
    def __init__(self, server, change):
        super().__init__(server, change)
        xpath = change.xpath

        xpath = list(libyang.xpath_split(xpath))
        assert xpath[0][0] == "goldstone-interfaces"
        assert xpath[0][1] == "interfaces"
        assert xpath[1][1] == "interface"
        assert xpath[1][2][0][0] == "name"
        self.xpath = xpath
        ifname = xpath[1][2][0][1]

        if ifname not in server.sonic.get_ifnames():
            raise InvalArgError("Invalid Interface name")

        self.ifname = ifname

    def valid_speeds(self):
        valid_speeds = [40000, 100000]
        breakout_valid_speeds = []  # no speed change allowed for sub-interfaces
        if not self.ifname.endswith("_1"):
            return breakout_valid_speeds
        else:
            return valid_speeds


class AdminStatusHandler(IfChangeHandler):
    def apply(self, user):
        if self.type in ["created", "modified"]:
            value = self.change.value
        else:
            value = self.server.get_default("admin-status")
        logger.debug(f"set {self.ifname}'s admin-status to {value}")
        self.server.sonic.set_config_db(self.ifname, "admin-status", value)

    def revert(self, user):
        # TODO
        pass


class MTUHandler(IfChangeHandler):
    def apply(self, user):
        if self.type in ["created", "modified"]:
            value = self.change.value
        else:
            value = self.server.get_default("mtu")
        logger.debug(f"set {self.ifname}'s mtu to {value}")
        self.server.sonic.set_config_db(self.ifname, "mtu", value)


class FECHandler(IfChangeHandler):
    def apply(self, user):
        if self.type in ["created", "modified"]:
            value = self.change.value
        else:
            value = self.server.get_default("fec")
        logger.debug(f"set {self.ifname}'s fec to {value}")
        self.server.sonic.set_config_db(self.ifname, "fec", value)


class IfTypeHandler(IfChangeHandler):
    def validate(self, user):
        if self.type in ["created", "modified"]:
            if self.change.value != "IF_ETHERNET":
                raise InvalArgError(f"Unsupported interface type {self.change.value}")


class LoopbackModeHandler(IfChangeHandler):
    def validate(self, user):
        if self.type in ["created", "modified"]:
            if self.change.value != "NONE":
                raise InvalArgError(f"Unsupported loopback mode {self.change.value}")


class PRBSModeHandler(IfChangeHandler):
    def validate(self, user):
        if self.type in ["created", "modified"]:
            if self.change.value != "NONE":
                raise InvalArgError(f"Unsupported PRBS mode {self.change.value}")


class EthernetIfTypeHandler(IfChangeHandler):
    def validate(self, user):
        if self.type in ["created", "modified"]:
            self.server.validate_interface_type(self.ifname, self.change.value)

    async def apply(self, user):
        if self.type in ["created", "modified"]:
            value = self.change.value
        else:
            value = self.server.sonic.k8s.get_default_iftype(self.ifname)
        await self.server.sonic.k8s.run_bcmcmd_port(self.ifname, "if=" + value)


class SpeedHandler(IfChangeHandler):
    def validate(self, user):
        if self.type in ["created", "modified"]:
            value = speed_yang_to_redis(self.change.value)
            valids = self.valid_speeds()
            if value not in valids:
                valids = [speed_redis_to_yang(v) for v in valids]
                raise InvalArgError(
                    f"Invalid speed: {self.change.value}, candidates: {','.join(valids)}"
                )

    async def apply(self, user):
        if self.type in ["created", "modified"]:
            value = self.change.value
        else:
            value = "100G"
        self.server.sonic.set_config_db(self.ifname, "speed", value)
        await self.server.sonic.k8s.update_bcm_portmap()


class VLANIfModeHandler(IfChangeHandler):
    def validate(self, user):
        cache = self.setup_cache(user)

        xpath = f"/goldstone-interfaces:interfaces/interface[name='{self.ifname}']"
        cache = libyang.xpath_get(cache, xpath, None)

        if self.type in ["created", "modified"]:
            config = cache["switched-vlan"]["config"]
            if config["interface-mode"] == "TRUNK" and "access-vlan" in config:
                raise InvalArgError(
                    "invalid VLAN configuration. can't set TRUNK mode and access-vlan at the same time"
                )
            elif config["interface-mode"] == "ACCESS" and "trunk-vlans" in config:
                raise InvalArgError(
                    "invalid VLAN configuration. can't set ACCESS mode and trunk-vlans at the same time"
                )
        else:
            if cache == None:
                return
            cache = cache.get("switched-vlan")
            if cache == None:
                return
            cache = cache.get("config")
            if cache == None:
                return
            if "access-vlan" in cache or "trunk-vlans" in cache:
                raise InvalArgError(
                    "invalid VLAN configuration. must remove interface-mode, access-vlan, trunk-vlans leaves at once"
                )


class AccessVLANHandler(IfChangeHandler):
    def apply(self, user):
        for key in self.server.sonic.get_keys(f"VLAN_MEMBER|*|{self.ifname}"):
            v = self.server.sonic.hgetall("CONFIG_DB", key)
            if v.get("tagging_mode") == "untagged":
                vid = int(key.split("|")[1].replace("Vlan", ""))
                self.server.sonic.remove_vlan_member(self.ifname, vid)

        if self.type in ["created", "modified"]:
            self.server.sonic.set_vlan_member(
                self.ifname, self.change.value, "untagged"
            )


class TrunkVLANsHandler(IfChangeHandler):
    def apply(self, user):
        if self.type == "created":
            self.server.sonic.set_vlan_member(self.ifname, self.change.value, "tagged")
        elif self.type == "modified":
            logger.warn("trunk-vlans leaf-list should not trigger modified event.")
        else:
            vid = int(self.xpath[-1][2][0][1])
            v = self.server.sonic.hgetall(
                "CONFIG_DB", f"VLAN_MEMBER|Vlan{vid}|{self.ifname}"
            )
            if v.get("tagging_mode") == "tagged":
                self.server.sonic.remove_vlan_member(self.ifname, vid)


class AutoNegotiateHandler(IfChangeHandler):
    async def apply(self, user):
        if self.type in ["created", "modified"]:
            v = self.change.value
        else:
            v = self.server.get_default("enabled")
        value = "yes" if v else "no"

        await self.server.sonic.k8s.run_bcmcmd_port(self.ifname, "an=" + value)


class AutoNegotiateAdvertisedSpeedsHandler(IfChangeHandler):
    def validate(self, user):
        if self.type in ["created", "modified"]:
            value = speed_yang_to_redis(self.change.value)
            valids = self.valid_speeds()
            if value not in valids:
                valids = [speed_redis_to_yang(v) for v in valids]
                raise InvalArgError(
                    f"Invalid speed: {change.value}, candidates: {','.join(valids)}"
                )

    def apply(self, user):
        self.setup_cache(user)
        v = user.get("needs_adv_speed_config", set())
        v.add(self.ifname)
        user["needs_adv_speed_config"] = v


class BreakoutHandler(IfChangeHandler):
    def validate(self, user):
        cache = self.setup_cache(user)

        if self.type in ["created", "modified"]:

            if "_1" not in self.ifname:
                raise InvalArgError("breakout cannot be configured on a sub-interface")

            if self.server.is_ufd_port(self.ifname):
                raise InvalArgError(
                    "Breakout cannot be configured on the interface that is part of UFD"
                )

            xpath = f"/goldstone-interfaces:interfaces/interface[name='{self.ifname}']"
            cache = libyang.xpath_get(cache, xpath, None)
            config = cache["ethernet"]["breakout"]["config"]
            if "num-channels" not in config or "channel-speed" not in config:
                raise InvalArgError(
                    "both num-channels and channel-speed must be set at once"
                )

    def apply(self, user):
        if self.type == "deleted":
            xpath = self.change.xpath
            data = self.server.get_running_data(xpath)
            if data:
                user["update-sonic"] = True
        else:
            user["update-sonic"] = True


class InterfaceServer(ServerBase):
    def __init__(self, conn, sonic, servers, platform_info):
        super().__init__(conn, "goldstone-interfaces")
        info = {}
        for i in platform_info:
            if "interface" in i:
                ifname = f"Ethernet{i['interface']['suffix']}"
                info[ifname] = i
        self.platform_info = info
        self.task_queue = queue.Queue()
        self.sonic = sonic
        self.servers = servers
        self.handlers = {
            "interfaces": {
                "interface": {
                    "name": NoOp,
                    "config": {
                        "admin-status": AdminStatusHandler,
                        "name": NoOp,
                        "description": NoOp,
                        "interface-type": IfTypeHandler,
                        "loopback-mode": LoopbackModeHandler,
                        "prbs-mode": PRBSModeHandler,
                    },
                    "ethernet": {
                        "config": {
                            "mtu": MTUHandler,
                            "fec": FECHandler,
                            "interface-type": EthernetIfTypeHandler,
                            "speed": SpeedHandler,
                        },
                        "breakout": {
                            "config": BreakoutHandler,
                        },
                        "auto-negotiate": {
                            "config": {
                                "enabled": AutoNegotiateHandler,
                                "advertised-speeds": AutoNegotiateAdvertisedSpeedsHandler,
                            },
                        },
                    },
                    "switched-vlan": {
                        "config": {
                            "interface-mode": VLANIfModeHandler,
                            "access-vlan": AccessVLANHandler,
                            "trunk-vlans": TrunkVLANsHandler,
                        }
                    },
                    "component-connection": NoOp,
                }
            }
        }

    def breakout_update_usonic(self, config):

        logger.debug("Starting to Update usonic's configMap and deployment")

        intfs = {}

        for i in config.get("interfaces", {}).get("interface", []):
            name = i["name"]
            b = i.get("ethernet", {}).get("breakout", {}).get("config", {})
            numch = b.get("num-channels", None)
            speed = speed_yang_to_redis(b.get("channel-speed", None))
            intfs[name] = (numch, speed)

        is_updated = self.sonic.k8s.update_usonic_config(intfs)

        # Restart deployment if configmap update is successful
        if is_updated:
            self.sonic.restart()

        return is_updated

    def pre(self, user):
        if self.sonic.is_rebooting:
            raise LockedError("uSONiC is rebooting")

    async def post(self, user):
        logger.info(f"post: {user}")
        if user.get("update-sonic"):
            self.sonic.is_rebooting = True
            self.task_queue.put(self.reconcile())
            return  # usonic will reboot. no need to proceed

        for ifname in user.get("needs_adv_speed_config", []):
            xpath = f"/goldstone-interfaces:interfaces/interface[name='{ifname}']/ethernet/auto-negotiate/config/advertised-speeds"
            config = libyang.xpath_get(user["cache"], xpath)
            if config:
                speeds = ",".join(v.replace("SPEED_", "").lower() for v in config)
                logger.debug(f"speeds: {speeds}")
            else:
                speeds = ""
            await self.sonic.k8s.run_bcmcmd_port(ifname, f"adv={speeds}")

    async def reconcile(self):
        self.sonic.is_rebooting = True

        config = self.get_running_data(self.conn.top, default={}, strip=False)
        is_updated = self.breakout_update_usonic(config)
        if is_updated:
            await self.sonic.wait()
        else:
            self.sonic.cache_counters()

        prefix = "/goldstone-interfaces:interfaces/interface"
        for ifname in self.sonic.get_ifnames():
            xpath = f"{prefix}[name='{ifname}']"
            data = self.get_running_data(xpath, {})
            logger.debug(f"{ifname} interface config: {data}")

            autoneg = (
                data.get("ethernet", {}).get("auto-negotiate", {}).get("config", {})
            )

            # default setting
            if "enabled" not in autoneg:
                autoneg["enabled"] = self.get_default("enabled") == "true"

            for key, value in autoneg.items():
                if key == "enabled":
                    v = "yes" if value else "no"
                    await self.sonic.k8s.run_bcmcmd_port(ifname, "an=" + v)
                elif key == "advertised-speeds":
                    speeds = ",".join(v.replace("SPEED_", "").lower() for v in value)
                    await self.sonic.k8s.run_bcmcmd_port(ifname, f"adv={speeds}")

            ethernet = data.get("ethernet", {}).get("config", {})
            # default setting
            for key in ["mtu", "fec"]:
                if key not in ethernet:
                    ethernet[key] = self.get_default(key)

            key = "interface-type"
            if key not in ethernet:
                iftype = self.sonic.k8s.get_default_iftype(ifname)
                if iftype:
                    ethernet[key] = iftype

            for key in ethernet:
                if key == "interface-type":
                    await self.sonic.k8s.run_bcmcmd_port(ifname, "if=" + ethernet[key])
                elif key in ["mtu", "fec", "speed"]:
                    self.sonic.set_config_db(ifname, key, ethernet[key])
                else:
                    logger.warn(f"unhandled configuration: {key}, {config[key]}")

            config = data.get("config", {})

            # default setting
            if "admin-status" not in config:
                config["admin-status"] = self.get_default("admin-status")

            for key in config:
                if key in ["admin-status", "description"]:
                    self.sonic.set_config_db(ifname, key, config[key])
                elif key in ["name"]:
                    pass
                else:
                    logger.warn(f"unhandled configuration: {key}, {config[key]}")

        for server in self.servers:
            await server.reconcile()

        self.sonic.is_rebooting = False

    def get_default(self, key):
        keys = [
            ["interfaces", "interface", "config", key],
            ["interfaces", "interface", "ethernet", "config", key],
            ["interfaces", "interface", "ethernet", "auto-negotiate", "config", key],
        ]

        for k in keys:
            xpath = "".join(f"/goldstone-interfaces:{v}" for v in k)
            node = self.conn.find_node(xpath)
            if not node:
                continue

            if node.type() == "boolean":
                return node.default() == "true"
            return node.default()

        raise Exception(f"default value not found for {key}")

    async def handle_tasks(self):
        while True:
            await asyncio.sleep(1)
            try:
                task = self.task_queue.get(False)
                await task
                self.task_queue.task_done()
            except queue.Empty:
                pass

    async def event_handler(self):

        redis = aioredis.from_url(f"redis://{REDIS_SERVICE_HOST}:{REDIS_SERVICE_PORT}")
        psub = redis.pubsub()
        await psub.psubscribe("__keyspace@0__:PORT_TABLE:Ethernet*")

        async for msg in psub.listen():
            if msg.get("pattern") == None:
                continue

            ifname = msg["channel"].decode().split(":")[-1]
            oper_status = self.sonic.get_oper_status(ifname)
            curr_oper_status = self.sonic.notif_if.get(ifname, "unknown")

            if oper_status == None or curr_oper_status == oper_status:
                continue

            eventname = "goldstone-interfaces:interface-link-state-notify-event"
            notif = {
                "if-name": ifname,
                "oper-status": self.get_oper_status(ifname),
            }
            self.send_notification(eventname, notif)
            self.sonic.notif_if[ifname] = oper_status

    def clear_counters(self, xpath, input_params, event, priv):
        logger.debug(
            f"clear_counters: xpath: {xpath}, input: {input}, event: {event}, priv: {priv}"
        )
        self.sonic.cache_counters()

    def stop(self):
        super().stop()

    async def start(self):
        await self.reconcile()
        tasks = await super().start()
        tasks.append(self.handle_tasks())
        tasks.append(self.event_handler())

        self.conn.subscribe_rpc_call(
            "/goldstone-interfaces:clear-counters",
            self.clear_counters,
        )

        return tasks

    def get_breakout_detail(self, ifname):
        xpath = f"/goldstone-interfaces:interfaces/interface[name='{ifname}']/ethernet/breakout/state"
        data = self.get_running_data(xpath)
        if not data:
            return None

        logger.debug(f"data: {data}")
        if data.get("num-channels", 1) > 1:
            return {
                "num-channels": data["num-channels"],
                "channel-speed": data["channel-speed"],
            }
        elif not name.endswith("_1"):
            _name = name.split("_")
            parent = _name[0] + "_1"
            return self.get_breakout_detail(parent)

        return None

    def validate_interface_type(self, ifname, iftype):
        xpath = f"/goldstone-interfaces:interfaces/interface[name='{ifname}']"
        err = InvalArgError("Unsupported interface type")

        try:
            self.get_running_data(xpath)
            detail = self.get_breakout_detail(ifname)
            if not detail:
                raise KeyError

            numch = int(detail["num-channels"])
        except (KeyError, NotFoundError):
            numch = 1

        if numch == 4:
            if detail["channel-speed"].endswith("10GB") and iftype == "SR":
                raise err
            elif iftype not in SINGLE_LANE_INTERFACE_TYPES:
                raise err
        elif numch == 2:
            if iftype not in DOUBLE_LANE_INTERFACE_TYPES:
                raise err
        elif numch == 1:
            if iftype not in QUAD_LANE_INTERFACE_TYPES:
                raise err
        else:
            raise err

    def get_ufd(self):
        xpath = "/goldstone-uplink-failure-detection:ufd-groups/ufd-group"
        return self.get_running_data(xpath, [])

    def is_ufd_port(self, ifname, ufd_list=None):
        if ufd_list == None:
            ufd_list = self.get_ufd()

        for ufd_id in ufd_list:
            if ifname in ufd_id.get("config", {}).get("uplink", []):
                return True
            if ifname in ufd_id.get("config", {}).get("downlink", []):
                return True
        return False

    def is_downlink_port(self, ifname):
        ufd_list = self.get_ufd()
        for data in ufd_list:
            try:
                if ifname in data["config"]["downlink"]:
                    return True, list(data["config"]["uplink"])
            except:
                pass

        return False, None

    def get_oper_status(self, ifname):
        oper_status = self.sonic.get_oper_status(ifname)
        downlink_port, uplink_port = self.is_downlink_port(ifname)

        if downlink_port and uplink_port:
            uplink_oper_status = self.sonic.get_oper_status(uplink_port[0])
            if uplink_oper_status == "down":
                return "DORMANT"

        if oper_status != None:
            return oper_status.upper()

    async def oper_cb(self, xpath, priv):
        logger.debug(f"xpath: {xpath}")
        if self.sonic.is_rebooting:
            raise CallbackFailedError("uSONiC is rebooting")

        counter_only = "counters" in xpath

        req_xpath = list(libyang.xpath_split(xpath))
        ifnames = self.sonic.get_ifnames()

        if (
            len(req_xpath) == 3
            and req_xpath[1][1] == "interface"
            and req_xpath[2][1] == "name"
        ):
            interfaces = [{"name": name} for name in ifnames]
            return {"goldstone-interfaces:interfaces": {"interface": interfaces}}

        if (
            len(req_xpath) > 1
            and req_xpath[1][1] == "interface"
            and len(req_xpath[1][2]) == 1
        ):
            cond = req_xpath[1][2][0]
            assert cond[0] == "name"
            if cond[1] not in ifnames:
                return None
            ifnames = [cond[1]]

        interfaces = []
        for name in ifnames:
            interface = {
                "name": name,
                "config": {"name": name},
                "state": {"name": name},
                "ethernet": {"state": {}, "breakout": {"state": {}}},
            }

            p = self.platform_info.get(name)
            if p:
                v = {}
                if "component" in p:
                    v["platform"] = {"component": p["component"]["name"]}
                if "tai" in p:
                    t = {
                        "module": p["tai"]["module"]["name"],
                        "host-interface": p["tai"]["hostif"]["name"],
                    }
                    v["transponder"] = t
                interface["component-connection"] = v

            # FIXME using "_1" is vulnerable to the interface nameing schema change
            if not name.endswith("_1") and name.find("_") != -1:
                _name = name.split("_")
                parent = _name[0] + "_1"
                interface["ethernet"]["breakout"]["state"] = {"parent": parent}
            else:
                config = self.get_running_data(
                    f"/goldstone-interfaces:interfaces/interface[name='{name}']/ethernet/breakout/config"
                )
                if config:
                    interface["ethernet"]["breakout"]["state"] = config

            interfaces.append(interface)

        if not counter_only:
            bcminfo = await self.sonic.k8s.bcm_ports_info(
                list(i["name"] for i in interfaces)
            )

        for intf in interfaces:
            ifname = intf["name"]
            intf["state"]["counters"] = self.sonic.get_counters(ifname)

            if not counter_only:

                intf["state"]["oper-status"] = self.get_oper_status(ifname)

                config = self.sonic.hgetall("APPL_DB", f"PORT_TABLE:{ifname}")
                for key, value in config.items():
                    if key in ["alias", "lanes"]:
                        intf["state"][key] = value
                    elif key == "speed":
                        intf["ethernet"]["state"][key] = speed_redis_to_yang(value)
                    elif key == "admin_status":
                        intf["state"]["admin-status"] = value.upper()
                    elif key == "mtu":
                        intf["ethernet"]["state"]["mtu"] = value.upper()

                info = bcminfo.get(ifname, {})
                logger.debug(f"bcminfo: {info}")

                iftype = info.get("iftype")
                if iftype:
                    intf["ethernet"]["state"]["interface-type"] = iftype

                fec = info.get("fec")
                if fec:
                    intf["ethernet"]["state"]["fec"] = fec

                auto_nego = info.get("auto-nego")
                if auto_nego:
                    intf["ethernet"]["auto-negotiate"] = {"state": {"enabled": True}}
                    v = auto_nego.get("local", {}).get("fd")
                    if v:
                        intf["ethernet"]["auto-negotiate"]["state"][
                            "advertised-speeds"
                        ] = [speed_bcm_to_yang(e) for e in v]
                else:
                    intf["ethernet"]["auto-negotiate"] = {"state": {"enabled": False}}

        return {"goldstone-interfaces:interfaces": {"interface": interfaces}}
