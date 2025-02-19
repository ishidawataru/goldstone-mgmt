import unittest
import logging
import os
import sys
import itertools

logger = logging.getLogger(__name__)

libpath = os.path.join(os.path.dirname(__file__), "../../../lib")
sys.path.insert(0, libpath)

from goldstone.north.cli.base import InvalidInput
from goldstone.north.cli.root import Root
from goldstone.north.cli import interface
from goldstone.north.cli import vlan
from goldstone.north.cli import ufd
from goldstone.north.cli import portchannel

from .test_util import MockConnector

fmt = "%(levelname)s %(module)s %(funcName)s l.%(lineno)d | %(message)s"
logging.basicConfig(level=logging.DEBUG, format=fmt)

INTF_OPER_DATA = [
    {
        "name": "Interface0",
        "state": {"oper-status": "UP", "admin-status": "UP"},
    },
    {
        "name": "Interface1",
        "state": {"oper-status": "DOWN", "admin-status": "UP"},
    },
    {
        "name": "Interface2",
        "state": {"oper-status": "DOWN"},
    },
    {
        "name": "Interface3",
        "state": {"admin-status": "UP"},
    },
]

EXPECTED_RUN_CONF = """interface Interface0
  admin-status up
  fec none
  speed 100G
  interface-type otn foic
  static-macsec-key 0x00000001,0x00000002,0x00000003,0x00000004
  tx-timing-mode synce-ref-clk
  switchport mode access vlan 300
  ufd 1 uplink
  quit
!
interface Interface1
  auto-negotiate enable
  auto-negotiate advertise 100G
  switchport mode trunk vlan 100
  switchport mode trunk vlan 200
  ufd 1 downlink
  portchannel PortChannel10
  quit"""


def ifxpath(ifname):
    return f"/goldstone-interfaces:interfaces/interface[name='{ifname}']"


class Test(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        logging.basicConfig(level=logging.DEBUG)
        conn = MockConnector()
        root = Root(conn)
        root.exec("clear datastore all", no_fail=False)

    async def test_show_interface_brief(self):

        conn = MockConnector()
        root = Root(conn)
        conn.oper_data = {
            "/goldstone-interfaces:interfaces/interface": INTF_OPER_DATA,
        }
        logger = logging.getLogger("stdout")

        with self.assertLogs(logger=logger) as l:
            root.exec("show interface brief", no_fail=False)
            lines = l.records[0].msg.split("\n")
            for i, line in enumerate(lines[3:-1]):
                elems = [e.strip() for e in line.split("|") if e]
                self.assertEqual(elems[0], INTF_OPER_DATA[i]["name"])
                self.assertEqual(
                    elems[1], INTF_OPER_DATA[i]["state"].get("oper-status", "-").lower()
                )
                self.assertEqual(
                    elems[2],
                    INTF_OPER_DATA[i]["state"].get("admin-status", "-").lower(),
                )

    async def test_auto_nego_help(self):
        conn = MockConnector()
        root = Root(conn)
        data = ["Interface0"]
        conn.oper_data = {
            "/goldstone-interfaces:interfaces/interface/name": data,
        }
        logger = logging.getLogger("stderr")

        ifctx = root.exec("interface Interface0", no_fail=False)

        with self.assertRaises(InvalidInput) as cm:
            ifctx.exec("auto-nego", no_fail=False)

        self.assertEqual(
            str(cm.exception), "usage: auto-negotiate [enable|disable|advertise]"
        )

    async def test_show_in_interface_ctx(self):
        conn = MockConnector()
        root = Root(conn)
        data = ["Interface0"]
        conn.oper_data = {
            "/goldstone-interfaces:interfaces/interface": INTF_OPER_DATA,
            "/goldstone-interfaces:interfaces/interface/name": data,
        }
        logger = logging.getLogger("stdout")

        with self.assertLogs(logger=logger) as l:
            root.exec("show interface brief", no_fail=False)
            ifctx = root.exec("interface Interface0", no_fail=False)
            ifctx.exec("show interface brief", no_fail=False)

            # global show and show in the interface ctx must have the same output
            self.assertEqual(l.records[0].msg, l.records[1].msg)

    async def test_vlan_range(self):
        conn = MockConnector()
        root = Root(conn)

        logger = logging.getLogger("stdout")

        def test(cmd, answer):
            v = root.exec(f"vlan range {cmd}", no_fail=False)
            with self.assertLogs(logger=logger) as l:
                v.exec("selected", no_fail=False)
                selected = "\n".join(
                    itertools.chain.from_iterable(r.msg.split("\n") for r in l.records)
                )
                vids = [int(vid.strip()) for vid in selected.split(",")]
                self.assertEqual(vids, answer)

        test("2-7", [2, 3, 4, 5, 6, 7])
        test("3-7", [3, 4, 5, 6, 7])
        test("1,2,3-7", [1, 2, 3, 4, 5, 6, 7])
        test("1-4,3-7", [1, 2, 3, 4, 5, 6, 7])
        test("1-3,5-7", [1, 2, 3, 5, 6, 7])
        test("5-7,1-3", [1, 2, 3, 5, 6, 7])

    async def test_show_run(self):
        conn = MockConnector()
        root = Root(conn)
        data = ["Interface0", "Interface1"]
        conn.oper_data = {
            "/goldstone-interfaces:interfaces/interface": INTF_OPER_DATA,
            "/goldstone-interfaces:interfaces/interface/name": data,
        }
        logger = logging.getLogger("stdout")

        root.exec("vlan 100")
        root.exec("vlan 200")
        root.exec("vlan 300")

        root.exec("ufd 1")
        root.exec("portchannel PortChannel10")

        with self.assertLogs(logger=logger) as l:
            ifctx = root.exec("interface Interface0", no_fail=False)
            ifctx.exec("admin-status up", no_fail=False)
            ifctx.exec("fec none", no_fail=False)
            ifctx.exec("speed 100G", no_fail=False)
            ifctx.exec("interface-type otn foic", no_fail=False)
            #        ifctx.exec("breakout 4X25G", no_fail=False)
            ifctx.exec("static-macsec-key 1,2,3,4", no_fail=False)
            ifctx.exec("tx-timing-mode synce-ref-clk", no_fail=False)

            ifctx.exec("switchport mode access vlan 300", no_fail=False)
            ifctx.exec("ufd 1 uplink", no_fail=False)

            ifctx = root.exec("interface Interface1", no_fail=False)
            ifctx.exec("auto-negotiate enable", no_fail=False)
            ifctx.exec("auto-negotiate advertise 100G", no_fail=False)

            ifctx.exec("switchport mode trunk vlan 100", no_fail=False)
            ifctx.exec("switchport mode trunk vlan 200", no_fail=False)
            ifctx.exec("ufd 1 downlink", no_fail=False)
            ifctx.exec("portchannel PortChannel10", no_fail=False)

            root.exec("show running-config interface")

            run_conf = "\n".join(
                itertools.chain.from_iterable(r.msg.split("\n") for r in l.records)
            )
            self.assertEqual(run_conf, EXPECTED_RUN_CONF)

    async def test_clear_datastore_all(self):
        conn = MockConnector()
        root = Root(conn)
        ifname = "Interface0"
        data = [ifname]
        conn.oper_data = {
            "/goldstone-interfaces:interfaces/interface": INTF_OPER_DATA,
            "/goldstone-interfaces:interfaces/interface/name": data,
        }

        xpath = ifxpath(ifname) + "/config/admin-status"

        ifctx = root.exec("interface Interface0", no_fail=False)
        ifctx.exec("admin-status up", no_fail=False)

        admin_status = conn.get(xpath)
        self.assertEqual(admin_status, "UP")

        root.exec("clear datastore all", no_fail=False)
        admin_status = conn.get(xpath)
        self.assertEqual(admin_status, None)

    async def test_vlan_assignment(self):
        conn = MockConnector()
        root = Root(conn)
        ifname = "Interface0"
        data = [ifname]
        conn.oper_data = {
            "/goldstone-interfaces:interfaces/interface": INTF_OPER_DATA,
            "/goldstone-interfaces:interfaces/interface/name": data,
        }

        root.exec("vlan 1-10", no_fail=False)

        ifctx = root.exec("interface Interface0", no_fail=False)
        with self.assertRaisesRegex(
            InvalidInput, "only one VLAN can be assigned when the mode is access"
        ):
            ifctx.exec("switchport mode access vlan 2-5", no_fail=False)

        ifctx.exec("switchport mode trunk vlan 2-5", no_fail=False)

        v = conn.get(
            "/goldstone-interfaces:interfaces/interface/switched-vlan/config/trunk-vlans",
            one=True,
        )
        self.assertEqual(v, [2, 3, 4, 5])

        ifctx.exec("no switchport mode trunk vlan 2-5", no_fail=False)
        ifctx.exec("switchport mode trunk vlan 1-3", no_fail=False)
        v = conn.get(
            "/goldstone-interfaces:interfaces/interface/switched-vlan/config/trunk-vlans",
            one=True,
        )
        self.assertEqual(v, [1, 2, 3])
