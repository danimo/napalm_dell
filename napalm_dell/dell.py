"""NAPALM handler for DellEMC PowerConnect and Force 10 Switches"""
# Copyright 2018 Daniel Molkentin. All rights reserved.
# based on the NAPALM Cisco IOS Handler, Copyright 2015 Spotify AB.
#
# The contents of this file are licensed under the Apache License, Version 2.0
# (the "License"); you may not use this file except in compliance with the
# License. You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations under
# the License.


from __future__ import print_function
from __future__ import unicode_literals

import re
import socket

import collections
from netmiko import ConnectHandler, FileTransfer, InLineTransfer
# Import NAPALM base
from napalm.base import NetworkDriver
import napalm.base.utils.string_parsers
import napalm.base.constants as C
from napalm.base.utils import py23_compat
from napalm.base.helpers import ip as cast_ip
from napalm.base.helpers import mac as cast_mac
from napalm.base.helpers import canonical_interface_name
from napalm.base.exceptions import ConnectionException

class DNOS6Driver(NetworkDriver):
    def __init__(self, hostname, username, password, timeout=60, optional_args=None):
        self.hostname = hostname
        self.username = username
        self.password = password
        self.timeout = timeout
        optional_args = optional_args or dict()

        self.transport = optional_args.get('transport', 'ssh')

        # Retrieve file names
        self.candidate_cfg = optional_args.get('candidate_cfg', 'candidate_config.txt')
        self.merge_cfg = optional_args.get('merge_cfg', 'merge_config.txt')
        self.rollback_cfg = optional_args.get('rollback_cfg', 'rollback_config.txt')
        self.inline_transfer = optional_args.get('inline_transfer', False)
        if self.transport == 'telnet':
            # Telnet only supports inline_transfer
            self.inline_transfer = True

        # None will cause autodetection of dest_file_system
        self._dest_file_system = optional_args.get('dest_file_system', None)
        self.auto_rollback_on_error = optional_args.get('auto_rollback_on_error', True)

        # Control automatic toggling of 'file prompt quiet' for file operations
        self.auto_file_prompt = optional_args.get('auto_file_prompt', True)

        # Netmiko possible arguments
        netmiko_argument_map = {
            'port': None,
            'secret': '',
            'verbose': False,
            'keepalive': 30,
            'global_delay_factor': 1,
            'use_keys': False,
            'key_file': None,
            'ssh_strict': False,
            'system_host_keys': False,
            'alt_host_keys': False,
            'alt_key_file': '',
            'ssh_config_file': None,
            'allow_agent': False,
        }

        # Build dict of any optional Netmiko args
        self.netmiko_optional_args = {}
        for k, v in netmiko_argument_map.items():
            try:
                self.netmiko_optional_args[k] = optional_args[k]
            except KeyError:
                pass

        default_port = {
            'ssh': 22,
            'telnet': 23
        }
        self.port = optional_args.get('port', default_port[self.transport])


        self.device = None
        self.config_replace = False

        self.profile = ["dnos6"]
        self.use_canonical_interface = optional_args.get('canonical_int', False)

    def open(self):
        """Open a connection to the device."""
        device_type = 'dell_dnos6'
        if self.transport == 'telnet':
            device_type = 'dell_dnos6_telnet'
        self.device = ConnectHandler(device_type=device_type,
                                     host=self.hostname,
                                     username=self.username,
                                     password=self.password,
                                     **self.netmiko_optional_args)
        # ensure in enable mode
        self.device.enable()

    def _discover_file_system(self):
        try:
            return self.device._autodetect_fs()
        except Exception:
            msg = "Netmiko _autodetect_fs failed (to workaround specify " \
                  "dest_file_system in optional_args.)"
            raise CommandErrorException(msg)

    def close(self):
        """Close the connection to the device."""
        self.device.disconnect()

    def _send_command(self, command):
        """Wrapper for self.device.send.command().

        If command is a list will iterate through commands until valid command.
        """
        try:
            if isinstance(command, list):
                for cmd in command:
                    output = self.device.send_command(cmd)
                    if "% Invalid" not in output:
                        break
            else:
                output = self.device.send_command(command)
            #return self._send_command_postprocess(output)
            return output 
        except (socket.error, EOFError) as e:
            raise ConnectionClosedException(str(e))

    def is_alive(self):
        """Returns a flag with the state of the connection."""
        null = chr(0)
        if self.device is None:
            return {'is_alive': False}
        if self.transport == 'telnet':
            try:
                # Try sending IAC + NOP (IAC is telnet way of sending command
                # IAC = Interpret as Command (it comes before the NOP)
                self.device.write_channel(telnetlib.IAC + telnetlib.NOP)
                return {'is_alive': True}
            except UnicodeDecodeError:
                # Netmiko logging bug (remove after Netmiko >= 1.4.3)
                return {'is_alive': True}
            except AttributeError:
                return {'is_alive': False}
        else:
            # SSH
            try:
                # Try sending ASCII null byte to maintain the connection alive
                self.device.write_channel(null)
                return {'is_alive': self.device.remote_conn.transport.is_active()}
            except (socket.error, EOFError):
                # If unable to send, we can tell for sure that the connection is unusable
                return {'is_alive': False}
        return {'is_alive': False}

    @staticmethod
    def _create_tmp_file(config):
        """Write temp file and for use with inline config and SCP."""
        tmp_dir = tempfile.gettempdir()
        rand_fname = py23_compat.text_type(uuid.uuid4())
        filename = os.path.join(tmp_dir, rand_fname)
        with open(filename, 'wt') as fobj:
            fobj.write(config)
        return filename

    def get_config(self, retrieve='all'):
        """Implementation of get_config for DNOS6.

        Returns the startup or/and running configuration as dictionary.
        The keys of the dictionary represent the type of configuration
        (startup or running). The candidate is always empty string,
        since IOS does not support candidate configuration.
        """

        configs = {
            'startup': '',
            'running': '',
            'candidate': '',
        }

        if retrieve in ('startup', 'all'):
            command = 'show startup-config'
            output = self._send_command(command)
            configs['startup'] = output

        if retrieve in ('running', 'all'):
            command = 'show running-config'
            output = self._send_command(command)
            configs['running'] = output

        return configs

    def get_environment(self):
        """
        Get environment facts.

        power and fan are currently not implemented
        cpu is using 1-minute average
        cpu hard-coded to cpu0 (i.e. only a single CPU)
        """
        environment = {}
        cpu_cmd = 'show proc cpu'
        temp_cmd = 'show system temperature'

        output = self._send_command(cpu_cmd)
        environment.setdefault('cpu', {})
        environment['cpu'][0] = {}
        environment['cpu'][0]['%usage'] = 0.0

        for line in output.splitlines():
            if 'Total CPU Utilization' in line:
                # ['Total', 'CPU', 'Utilization', '9.26%', '9.75%', '9.72%']
                oneminute = float(line.split()[4][:-1])
                environment['cpu'][0]['%usage'] = float(oneminute)
                break
            if 'alloc' in line:
                used_mem = int(line.split()[1])
            if 'free' in line:
                avail_mem = int(line.split()[1])

        environment.setdefault('memory', {})
        environment['memory']['used_ram'] = used_mem
        environment['memory']['available_ram'] = used_mem+avail_mem 

        environment.setdefault('temperature', {})
        re_temp_value = re.compile('(.*) Temperature Value')
        ### TODO
        output = self._send_command(temp_cmd)
        env_value = {'is_alert': False, 'is_critical': False, 'temperature': -1.0}
        environment['temperature']['invalid'] = env_value

        # Initialize 'power' and 'fan' to default values (not implemented)
        environment.setdefault('power', {})
        environment['power']['invalid'] = {'status': True, 'output': -1.0, 'capacity': -1.0}
        environment.setdefault('fans', {})
        environment['fans']['invalid'] = {'status': True}

        return environment

    def get_mac_address_table(self):
        """
        Returns a lists of dictionaries. Each dictionary represents an entry in the MAC Address
        Table, having the following keys
            * mac (string)
            * interface (string)
            * vlan (int)
            * active (boolean)
            * static (boolean)
            * moves (int)
            * last_move (float)

        Format1:
        Aging time is 300 Sec
        
        Vlan     Mac Address           Type        Port
        -------- --------------------- ----------- ---------------------
        1        0025.90C2.88ED        Dynamic     Gi1/0/48
        1        F48E.3841.9628        Management  Vl1
        """

        def _process_mac_fields(vlan, mac, mac_type, interface):
            """Return proper data for mac address fields."""
            if mac_type.lower() in ['management', 'static']:
                static=True
            else:
                static=False

            if mac_type.lower() in ['dynamic']:
                active = True
            else:
                active = False

            return {
                'mac': napalm.base.helpers.mac(mac),
                'interface': self._canonical_int(interface),
                'vlan': int(vlan),
                'static': static,
                'active': active,
                'moves': -1,
                'last_move': -1.0
            }

        output = self._send_command("show mac address-table")
        output = re.split(r'^----.*', output, flags=re.M)[1:]
        output = re.split(r'\n\nTotal.*', output[0], flags=re.M)[0]
        lines = output.split('\n')
        entries = []
        [entries.append(_process_mac_fields(*i.split())) for i in lines if i != '']
        return entries

    def get_arp_table(self):
        """
        Returns a list of dictionaries having the following set of keys:
            * interface (string)
            * mac (string)
            * ip (string)
            * age (float)

        Example::

            [
                {
                    'interface' : 'MgmtEth0/RSP0/CPU0/0',
                    'mac'       : '5C:5E:AB:DA:3C:F0',
                    'ip'        : '172.17.17.1',
                    'age'       : 1454496274.84
                },
                {
                    'interface' : 'MgmtEth0/RSP0/CPU0/0',
                    'mac'       : '5C:5E:AB:DA:3C:FF',
                    'ip'        : '172.17.17.2',
                    'age'       : 1435641582.49
                }
            ]

        """

        def _process_arp_fields(ip, mac, interface, mac_type, h, m='', s=''):
            if h == 'n/a':
                age = -1
            else:
                h = int(h[:-1])
                m = int(m[:-1])
                s = int(s[:-1])
                age=h*3600+m*60+s
            return {
                'interface' : self._canonical_int(interface),
                'mac'       : napalm.base.helpers.mac(mac),
                'ip'        : ip,
                'age'       : float(age)
            }

        output = self._send_command("show arp")
        output = re.split(r'^----.*', output, flags=re.M)[1]
        lines = output.split('\n')
        entries = []
        [entries.append(_process_arp_fields(*i.split())) for i in lines if i != '']
        return entries

    def get_interfaces(self):
        def config_for_iface(iface, configs):
            for config in configs:
                if config[0] == iface:
                    iface_config = config[1]
                    m = re.search('description: "(.+?)"', iface_config)
                    descr = ''
                    if m != None:
                        descr = m.group(1)
                    m = re.search('((no )?shutdown)', iface_config)
                    enabled = not bool(m != None and m.group(1) == 'shutdown')
                    return (descr, enabled)
            return ('', True)


        config_raw = self._send_command("show running-config")
        config_ifaces = re.findall("!\ninterface (.*)\n((.|\n)*?)\nexit", config_raw)
        ifaces_raw = self._send_command("show interfaces")
        ifaces = ifaces_raw.split('\n\n')
        iface_list = []
        for iface in ifaces:
            if iface == '':
                continue
            name   = re.search('Interface Name : \\.+\s(.*)\n', iface).group(1)
            speed  = re.search('Port Speed : \\.+\s(.*)\n', iface).group(1)
            mac    = re.search('L3 MAC Address\\.+\s(.*)\n', iface).group(1)
            status = re.search('Link Status : \\.+\s(.*)\n', iface).group(1)
            description, enabled = config_for_iface(name, config_ifaces) 
            if speed == 'Unknown':
                speed = 0
            iface_list.append( { self._canonical_int(name) : {
                'is_up': bool(status is 'Up'),
                'is_enable': enabled, 
                'description': description,
                'last_flapped': -1,
                'speed': int(speed),
                'mac_address': napalm.base.helpers.mac(mac) }
            } )
        return iface_list


    def get_lldp_neighbors(self):
        result = collections.defaultdict(list)
        output = self._send_command("show lldp remote-device all")
        output = re.split(r'^----.*', output, flags=re.M)[1]
        lines = output.split('\n')
        for line in lines:
            if line == '':
                continue
            iface=line[0:10].strip()
            portid=line[38:55]
            systemname=line[57:].strip()
            if systemname == '':
                systemname = None
            result[iface].append({'hostname' : systemname,
                                  'port': portid
                                 })
        return dict(result)

    def _get_lldp_neighbor_detail_iface(self, interface):
        caps_map = {
                'bridge': 'B',
                'router': 'R',
                'WLAN access point': 'W',
                'station only': 'S',
                }
        ### support empty interface
        output = self._send_command("show lldp remote-device detail %s" % interface)
        chassis_id = re.search(r'Chassis ID: (.+)', output).group(1)
        try:
            system_name = re.search(r'System Name: (.+)', output).group(1)
        except:
            system_name = ''
        try:
            port_description = re.search(r'Port Description: (.+)', output).group(1)
        except:
            port_description = ''
        try:
            caps_supported_output = re.search(r'System Capabilities Supported: (.+)', output).group(1)
        except:
            caps_supported_output = ''
        try:
            caps_enabled_output = re.search(r'System Capabilities Enabled: (.+)', output).group(1)
        except:
            caps_enabled_output = ''

        caps_supported = caps_supported_output.split(', ')
        caps_supported = [[caps_maps[cap] for cap in caps_supported]]
        caps_enabled = caps_enabled_output.split(', ')
        caps_enabled = [[caps_enabled[cap] for cap in caps_enabled]]

        return {
#                'parent_interface': u''
                'remote_chassis_id': chassis_id, 
                'remote_system_name': system_name,
                'remote_port': port_description,
                'remote_port_description': port_description,
                'remote_system_description': system_decription,
                'remote_system_capab': caps_supported.join(', '),
                'remote_system_enable_capab': caps_enabled.join(', ') 
                }

    def get_lldp_neighbor_detail(self, interface=''):
        if interface == '':
            ifaces = []
            neighs = self.get_lldp_neighbors();
            for iface in neighs:
                ifaces.append(self._get_lldp_neighbor_detail_iface(iface))
            return ifaces
        else:
            return _get_lldp_neighbor_detail_iface(iface)

    def get_ntp_peers(self):

        """
        Returns the NTP peers configuration as dictionary.
        The keys of the dictionary represent the IP Addresses of the peers.
        Inner dictionaries do not have yet any available keys.

        Example::

            {
                '192.168.0.1': {},
                '17.72.148.53': {},
                '37.187.56.220': {},
                '162.158.20.18': {}
            }

        """
        output = self._send_command("show sntp server")
        output = re.findall('Host Address:\s(\S+)', output)
        entries = dict()
        for i in output:
            entries[i]={}
        return entries


    def getnfacts(self):
        """
        Returns a dictionary containing the following information:
         * uptime - Uptime of the device in seconds.
         * vendor - Manufacturer of the device.
         * model - Device model.
         * hostname - Hostname of the device
         * fqdn - Fqdn of the device
         * os_version - String with the OS version running on the device.
         * serial_number - Serial number of the device
         * interface_list - List of the interfaces of the device

        Example::

            {
            'uptime': 151005.57332897186,
            'vendor': u'Arista',
            'os_version': u'4.14.3-2329074.gaatlantarel',
            'serial_number': u'SN0123A34AS',
            'model': u'vEOS',
            'hostname': u'eos-router',
            'fqdn': u'eos-router',
            'interface_list': [u'Ethernet2', u'Management1', u'Ethernet1', u'Ethernet3']
            }

        """
 
        return {
                'uptime': 151005.57332897186,
                'vendor': u'Dell',
                'os_version': u'4.14.3-2329074.gaatlantarel',
                'serial_number': u'SN0123A34AS',
                'model': u'vEOS',
                'hostname': u'eos-router',
                'fqdn': u'eos-router',
                'interface_list': [u'Ethernet2', u'Management1', u'Ethernet1', u'Ethernet3']

                }
