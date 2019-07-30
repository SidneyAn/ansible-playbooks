#!/usr/bin/python

#
# Copyright (c) 2019 Wind River Systems, Inc.
#
# SPDX-License-Identifier: Apache-2.0
#
# OpenStack Keystone and Sysinv interactions
#

import os
import pyudev
import re
import subprocess
import sys
import time

# The following imports are to make use of the OpenStack cgtsclient and some
# constants in controllerconfig. When it is time to remove/deprecate these
# packages, classes OpenStack, Token and referenced constants need to be moved
# to this standalone script.
from controllerconfig.common import constants
from controllerconfig import ConfigFail
from controllerconfig import openstack
from controllerconfig import sysinv_api as sysinv

from netaddr import IPNetwork
from sysinv.common import constants as sysinv_constants

try:
    from ConfigParser import ConfigParser
except ImportError:
    from configparser import ConfigParser


COMBINED_LOAD = 'All-in-one'
SUBCLOUD_ROLE = 'subcloud'
RECONFIGURE_SYSTEM = False
RECONFIGURE_NETWORK = False
RECONFIGURE_SERVICE = False
INITIAL_POPULATION = True
INCOMPLETE_BOOTSTRAP = False
CONF = ConfigParser()


def touch(fname):
    with open(fname, 'a'):
        os.utime(fname, None)


def is_subcloud():
    cloud_role = CONF.get('BOOTSTRAP_CONFIG', 'DISTRIBUTED_CLOUD_ROLE', None)
    return cloud_role == SUBCLOUD_ROLE


def wait_system_config(client):
    for _ in range(constants.SYSTEM_CONFIG_TIMEOUT):
        try:
            systems = client.sysinv.isystem.list()
            if systems:
                # only one system (default)
                return systems[0]
        except Exception:
            pass
        time.sleep(1)
    else:
        raise ConfigFail('Timeout waiting for default system '
                         'configuration')


def populate_system_config(client):
    if not INITIAL_POPULATION and not RECONFIGURE_SYSTEM:
        return

    # Wait for pre-populated system
    system = wait_system_config(client)

    if INITIAL_POPULATION:
        print("Populating system config...")
    else:
        print("Updating system config...")
    # Update system attributes
    capabilities = {'region_config': False,
                    'vswitch_type': 'none',
                    'shared_services': '[]',
                    'sdn_enabled': False,
                    'https_enabled': False}

    dc_role = CONF.get('BOOTSTRAP_CONFIG', 'DISTRIBUTED_CLOUD_ROLE')
    if dc_role == 'none':
        dc_role = None

    if is_subcloud():
        capabilities.update({'shared_services': "['identity', ]",
                             'region_config': True})

    values = {
        'system_mode': CONF.get('BOOTSTRAP_CONFIG', 'SYSTEM_MODE'),
        'capabilities': capabilities,
        'timezone': CONF.get('BOOTSTRAP_CONFIG', 'TIMEZONE'),
        'region_name': 'RegionOne',
        'service_project_name': 'services',
        'distributed_cloud_role': dc_role
    }

    if is_subcloud():
        values.update(
            {'region_name': CONF.get('BOOTSTRAP_CONFIG', 'REGION_NAME'),
             'name': CONF.get('BOOTSTRAP_CONFIG', 'REGION_NAME')}
        )

    if INITIAL_POPULATION:
        values.update(
            {'system_type': CONF.get('BOOTSTRAP_CONFIG', 'SYSTEM_TYPE')}
        )

    patch = sysinv.dict_to_patch(values)
    try:
        client.sysinv.isystem.update(system.uuid, patch)
    except Exception as e:
        if INCOMPLETE_BOOTSTRAP:
            # The previous bootstrap might have been interrupted while
            # it was in the middle of persisting the initial system
            # config.
            isystem = client.sysinv.isystem.list()[0]
            print("System type is %s" % isystem.system_type)
            if isystem.system_type != "None":
                # System update in previous play went through
                pass
            else:
                raise e
        else:
            raise e
    print("System config completed.")


def populate_load_config(client):
    if not INITIAL_POPULATION:
        return

    print("Populating load config...")
    patch = {'software_version': CONF.get('BOOTSTRAP_CONFIG', 'SW_VERSION'),
             'compatible_version': "N/A",
             'required_patches': "N/A"}
    try:
        client.sysinv.load.create(**patch)
    except Exception as e:
        if INCOMPLETE_BOOTSTRAP:
            loads = client.sysinv.load.list()
            if len(loads) > 0:
                # Load config in previous play went through
                pass
            else:
                raise e
        else:
            raise e
    print("Load config completed.")


def create_addrpool(client, addrpool_data, network_name):
    try:
        pool = client.sysinv.address_pool.create(**addrpool_data)
        return pool
    except Exception as e:
        if INCOMPLETE_BOOTSTRAP:
            # The previous bootstrap might have been interrupted while
            # it was in the middle of persisting this network config data
            # and the controller host has not been created.
            pools = client.sysinv.address_pool.list()
            if pools:
                for pool in pools:
                    if network_name in pool.name:
                        return pool
        raise e


def create_network(client, network_data, network_name):
    try:
        client.sysinv.network.create(**network_data)
    except Exception as e:
        if INCOMPLETE_BOOTSTRAP:
            # The previous bootstrap might have been interrupted while
            # it was in the middle of persisting this network config data
            # and the controller host has not been created.
            networks = client.sysinv.network.list()
            for network in networks:
                if network.name == network_name:
                    return
        raise e


def delete_network_and_addrpool(client, network_name):
    networks = client.sysinv.network.list()
    network_uuid = addrpool_uuid = None
    for network in networks:
        if network.name == network_name:
            network_uuid = network.uuid
            addrpool_uuid = network.pool_uuid
    if network_uuid:
        print("Deleting network and address pool for network %s..." %
              network_name)
        host = client.sysinv.ihost.get('controller-0')
        host_addresses = client.sysinv.address.list_by_host(host.uuid)
        for addr in host_addresses:
            client.sysinv.address.delete(addr.uuid)
        client.sysinv.network.delete(network_uuid)
        client.sysinv.address_pool.delete(addrpool_uuid)


def populate_mgmt_network(client):
    management_subnet = IPNetwork(
        CONF.get('BOOTSTRAP_CONFIG', 'MANAGEMENT_SUBNET'))
    start_address = CONF.get('BOOTSTRAP_CONFIG',
                             'MANAGEMENT_START_ADDRESS')
    end_address = CONF.get('BOOTSTRAP_CONFIG',
                           'MANAGEMENT_END_ADDRESS')
    dynamic_allocation = CONF.getboolean(
        'BOOTSTRAP_CONFIG', 'DYNAMIC_ADDRESS_ALLOCATION')
    network_name = 'mgmt'

    if RECONFIGURE_NETWORK:
        delete_network_and_addrpool(client, network_name)
        print("Updating management network...")
    else:
        print("Populating management network...")

    # create the address pool
    values = {
        'name': 'management',
        'network': str(management_subnet.network),
        'prefix': management_subnet.prefixlen,
        'ranges': [(start_address, end_address)],
    }
    pool = create_addrpool(client, values, 'management')

    # create the network for the pool
    values = {
        'type': sysinv_constants.NETWORK_TYPE_MGMT,
        'name': sysinv_constants.NETWORK_TYPE_MGMT,
        'dynamic': dynamic_allocation,
        'pool_uuid': pool.uuid,
    }
    create_network(client, values, network_name)


def populate_pxeboot_network(client):
    pxeboot_subnet = IPNetwork(CONF.get('BOOTSTRAP_CONFIG', 'PXEBOOT_SUBNET'))
    start_address = CONF.get('BOOTSTRAP_CONFIG',
                             'PXEBOOT_START_ADDRESS')
    end_address = CONF.get('BOOTSTRAP_CONFIG',
                           'PXEBOOT_END_ADDRESS')
    network_name = 'pxeboot'

    if RECONFIGURE_NETWORK:
        delete_network_and_addrpool(client, network_name)
        print("Updating pxeboot network...")
    else:
        print("Populating pxeboot network...")

    # create the address pool
    values = {
        'name': 'pxeboot',
        'network': str(pxeboot_subnet.network),
        'prefix': pxeboot_subnet.prefixlen,
        'ranges': [(start_address, end_address)],
    }
    pool = create_addrpool(client, values, network_name)

    # create the network for the pool
    values = {
        'type': sysinv_constants.NETWORK_TYPE_PXEBOOT,
        'name': sysinv_constants.NETWORK_TYPE_PXEBOOT,
        'dynamic': True,
        'pool_uuid': pool.uuid,
    }
    create_network(client, values, network_name)


def populate_oam_network(client):
    external_oam_subnet = IPNetwork(CONF.get(
        'BOOTSTRAP_CONFIG', 'EXTERNAL_OAM_SUBNET'))
    start_address = CONF.get('BOOTSTRAP_CONFIG',
                             'EXTERNAL_OAM_START_ADDRESS')
    end_address = CONF.get('BOOTSTRAP_CONFIG',
                           'EXTERNAL_OAM_END_ADDRESS')
    network_name = 'oam'

    if RECONFIGURE_NETWORK:
        delete_network_and_addrpool(client, network_name)
        print("Updating oam network...")
    else:
        print("Populating oam network...")

    # create the address pool
    values = {
        'name': 'oam',
        'network': str(external_oam_subnet.network),
        'prefix': external_oam_subnet.prefixlen,
        'ranges': [(start_address, end_address)],
        'floating_address': CONF.get(
            'BOOTSTRAP_CONFIG', 'EXTERNAL_OAM_FLOATING_ADDRESS'),
    }

    system_mode = CONF.get('BOOTSTRAP_CONFIG', 'SYSTEM_MODE')
    if system_mode != sysinv_constants.SYSTEM_MODE_SIMPLEX:
        values.update({
            'controller0_address': CONF.get(
                'BOOTSTRAP_CONFIG', 'EXTERNAL_OAM_0_ADDRESS'),
            'controller1_address': CONF.get(
                'BOOTSTRAP_CONFIG', 'EXTERNAL_OAM_1_ADDRESS'),
        })
    values.update({
        'gateway_address': CONF.get(
            'BOOTSTRAP_CONFIG', 'EXTERNAL_OAM_GATEWAY_ADDRESS'),
    })
    pool = create_addrpool(client, values, network_name)

    # create the network for the pool
    values = {
        'type': sysinv_constants.NETWORK_TYPE_OAM,
        'name': sysinv_constants.NETWORK_TYPE_OAM,
        'dynamic': False,
        'pool_uuid': pool.uuid,
    }
    create_network(client, values, network_name)


def populate_multicast_network(client):
    management_multicast_subnet = IPNetwork(CONF.get(
        'BOOTSTRAP_CONFIG', 'MANAGEMENT_MULTICAST_SUBNET'))
    start_address = CONF.get('BOOTSTRAP_CONFIG',
                             'MANAGEMENT_MULTICAST_START_ADDRESS')
    end_address = CONF.get('BOOTSTRAP_CONFIG',
                           'MANAGEMENT_MULTICAST_END_ADDRESS')
    network_name = 'multicast'

    if RECONFIGURE_NETWORK:
        delete_network_and_addrpool(client, network_name)
        print("Updating multicast network...")
    else:
        print("Populating multicast network...")

    # create the address pool
    values = {
        'name': 'multicast-subnet',
        'network': str(management_multicast_subnet.network),
        'prefix': management_multicast_subnet.prefixlen,
        'ranges': [(start_address, end_address)],
    }
    pool = create_addrpool(client, values, network_name)

    # create the network for the pool
    values = {
        'type': sysinv_constants.NETWORK_TYPE_MULTICAST,
        'name': sysinv_constants.NETWORK_TYPE_MULTICAST,
        'dynamic': False,
        'pool_uuid': pool.uuid,
    }
    create_network(client, values, network_name)


def populate_cluster_host_network(client):
    cluster_host_subnet = IPNetwork(CONF.get(
        'BOOTSTRAP_CONFIG', 'CLUSTER_HOST_SUBNET'))
    start_address = CONF.get('BOOTSTRAP_CONFIG',
                             'CLUSTER_HOST_START_ADDRESS')
    end_address = CONF.get('BOOTSTRAP_CONFIG',
                           'CLUSTER_HOST_END_ADDRESS')
    network_name = 'cluster-host'

    if RECONFIGURE_NETWORK:
        delete_network_and_addrpool(client, network_name)
        print("Updating cluster host network...")
    else:
        print("Populating cluster host network...")

    # create the address pool
    values = {
        'name': 'cluster-host-subnet',
        'network': str(cluster_host_subnet.network),
        'prefix': cluster_host_subnet.prefixlen,
        'ranges': [(start_address, end_address)],
    }
    pool = create_addrpool(client, values, network_name)

    # create the network for the pool
    values = {
        'type': sysinv_constants.NETWORK_TYPE_CLUSTER_HOST,
        'name': sysinv_constants.NETWORK_TYPE_CLUSTER_HOST,
        'dynamic': True,
        'pool_uuid': pool.uuid,
    }
    create_network(client, values, network_name)


def populate_system_controller_network(client):
    system_controller_subnet = IPNetwork(CONF.get(
        'BOOTSTRAP_CONFIG', 'SYSTEM_CONTROLLER_SUBNET'))
    system_controller_floating_ip = CONF.get(
        'BOOTSTRAP_CONFIG', 'SYSTEM_CONTROLLER_FLOATING_ADDRESS')
    network_name = 'system-controller'

    if RECONFIGURE_NETWORK:
        delete_network_and_addrpool(client, 'system-controller')
        print("Updating system controller network...")
    else:
        print("Populating system controller network...")

    # create the address pool
    values = {
        'name': 'system-controller-subnet',
        'network': str(system_controller_subnet.network),
        'prefix': system_controller_subnet.prefixlen,
        'floating_address': str(system_controller_floating_ip),
    }
    pool = create_addrpool(client, values, network_name)

    # create the network for the pool
    values = {
        'type': sysinv_constants.NETWORK_TYPE_SYSTEM_CONTROLLER,
        'name': sysinv_constants.NETWORK_TYPE_SYSTEM_CONTROLLER,
        'dynamic': False,
        'pool_uuid': pool.uuid,
    }
    create_network(client, values, network_name)


def populate_cluster_pod_network(client):
    cluster_pod_subnet = IPNetwork(CONF.get(
        'BOOTSTRAP_CONFIG', 'CLUSTER_POD_SUBNET'))
    start_address = CONF.get('BOOTSTRAP_CONFIG',
                             'CLUSTER_POD_START_ADDRESS')
    end_address = CONF.get('BOOTSTRAP_CONFIG',
                           'CLUSTER_POD_END_ADDRESS')
    network_name = 'cluster-pod'

    if RECONFIGURE_NETWORK:
        delete_network_and_addrpool(client, network_name)
        print("Updating cluster pod network...")
    else:
        print("Populating cluster pod network...")

    # create the address pool
    values = {
        'name': 'cluster-pod-subnet',
        'network': str(cluster_pod_subnet.network),
        'prefix': cluster_pod_subnet.prefixlen,
        'ranges': [(start_address, end_address)],
    }
    pool = create_addrpool(client, values, network_name)

    # create the network for the pool
    values = {
        'type': sysinv_constants.NETWORK_TYPE_CLUSTER_POD,
        'name': sysinv_constants.NETWORK_TYPE_CLUSTER_POD,
        'dynamic': False,
        'pool_uuid': pool.uuid,
    }
    create_network(client, values, network_name)


def populate_cluster_service_network(client):
    cluster_service_subnet = IPNetwork(CONF.get(
        'BOOTSTRAP_CONFIG', 'CLUSTER_SERVICE_SUBNET'))
    start_address = CONF.get('BOOTSTRAP_CONFIG',
                             'CLUSTER_SERVICE_START_ADDRESS')
    end_address = CONF.get('BOOTSTRAP_CONFIG',
                           'CLUSTER_SERVICE_END_ADDRESS')
    network_name = 'cluster-service'

    if RECONFIGURE_NETWORK:
        delete_network_and_addrpool(client, network_name)
        print("Updating cluster service network...")
    else:
        print("Populating cluster service network...")

    # create the address pool
    values = {
        'name': 'cluster-service-subnet',
        'network': str(cluster_service_subnet.network),
        'prefix': cluster_service_subnet.prefixlen,
        'ranges': [(start_address, end_address)],
    }
    pool = create_addrpool(client, values, network_name)

    # create the network for the pool
    values = {
        'type': sysinv_constants.NETWORK_TYPE_CLUSTER_SERVICE,
        'name': sysinv_constants.NETWORK_TYPE_CLUSTER_SERVICE,
        'dynamic': False,
        'pool_uuid': pool.uuid,
    }
    create_network(client, values, network_name)


def populate_network_config(client):
    if not INITIAL_POPULATION and not RECONFIGURE_NETWORK:
        return
    populate_mgmt_network(client)
    populate_pxeboot_network(client)
    populate_oam_network(client)
    populate_multicast_network(client)
    populate_cluster_host_network(client)
    populate_cluster_pod_network(client)
    populate_cluster_service_network(client)
    if is_subcloud():
        populate_system_controller_network(client)
    print("Network config completed.")


def populate_dns_config(client):
    if not INITIAL_POPULATION and not RECONFIGURE_SYSTEM:
        return

    print("Populating/Updating DNS config...")
    nameservers = CONF.get('BOOTSTRAP_CONFIG', 'NAMESERVERS')

    dns_list = client.sysinv.idns.list()
    dns_record = dns_list[0]
    values = {
        'nameservers': nameservers.rstrip(','),
        'action': 'apply'
    }
    patch = sysinv.dict_to_patch(values)
    client.sysinv.idns.update(dns_record.uuid, patch)
    print("DNS config completed.")


def populate_docker_config(client):
    if not INITIAL_POPULATION and not RECONFIGURE_SERVICE:
        return

    http_proxy = CONF.get('BOOTSTRAP_CONFIG', 'DOCKER_HTTP_PROXY')
    https_proxy = CONF.get('BOOTSTRAP_CONFIG', 'DOCKER_HTTPS_PROXY')
    no_proxy = CONF.get('BOOTSTRAP_CONFIG', 'DOCKER_NO_PROXY')

    # Get rid of the faulty docker proxy entries that might have
    # been created in the previous failed run.
    parameters = client.sysinv.service_parameter.list()
    for parameter in parameters:
        if (parameter.name == 'http_proxy' or
                parameter.name == 'https_proxy' or
                parameter.name == 'no_proxy'):
            client.sysinv.service_parameter.delete(parameter.uuid)

    if http_proxy != 'undef' or https_proxy != 'undef':
        parameters = {}
        if http_proxy != 'undef':
            parameters['http_proxy'] = http_proxy
        if https_proxy != 'undef':
            parameters['https_proxy'] = https_proxy

        parameters['no_proxy'] = no_proxy
        values = {
            'service': sysinv_constants.SERVICE_TYPE_DOCKER,
            'section': sysinv_constants.SERVICE_PARAM_SECTION_DOCKER_PROXY,
            'personality': None,
            'resource': None,
            'parameters': parameters
        }

        print("Populating/Updating docker proxy config...")
        client.sysinv.service_parameter.create(**values)
        print("Docker proxy config completed.")

    use_default_registries = CONF.getboolean(
        'BOOTSTRAP_CONFIG', 'USE_DEFAULT_REGISTRIES')

    # Get rid of any faulty docker registry entries that might have been
    # created in the previous failed run.
    parameters = client.sysinv.service_parameter.list()
    for parameter in parameters:
        if (parameter.name == 'k8s' or
                parameter.name == 'gcr' or
                parameter.name == 'quay' or
                parameter.name == 'docker' or
                parameter.name == 'insecure_registry'):
            client.sysinv.service_parameter.delete(parameter.uuid)

    if not use_default_registries:
        secure_registry = CONF.getboolean('BOOTSTRAP_CONFIG',
                                          'IS_SECURE_REGISTRY')
        parameters = {}

        parameters['k8s'] = CONF.get('BOOTSTRAP_CONFIG', 'K8S_REGISTRY')
        parameters['gcr'] = CONF.get('BOOTSTRAP_CONFIG', 'GCR_REGISTRY')
        parameters['quay'] = CONF.get('BOOTSTRAP_CONFIG', 'QUAY_REGISTRY')
        parameters['docker'] = CONF.get('BOOTSTRAP_CONFIG', 'DOCKER_REGISTRY')

        if not secure_registry:
            parameters['insecure_registry'] = "True"

        values = {
            'service': sysinv_constants.SERVICE_TYPE_DOCKER,
            'section': sysinv_constants.SERVICE_PARAM_SECTION_DOCKER_REGISTRY,
            'personality': None,
            'resource': None,
            'parameters': parameters
        }

        print("Populating/Updating docker registry config...")
        client.sysinv.service_parameter.create(**values)
        print("Docker registry config completed.")

    # Remove any kubernetes entries that might have been created in the
    # previous run.
    parameters = client.sysinv.service_parameter.list()
    for parameter in parameters:
        if (parameter.name ==
                sysinv_constants.SERVICE_PARAM_NAME_KUBERNETES_API_SAN_LIST):
            client.sysinv.service_parameter.delete(parameter.uuid)

    apiserver_san_list = CONF.get('BOOTSTRAP_CONFIG', 'APISERVER_SANS')
    if apiserver_san_list:
        parameters = {}

        parameters[
            sysinv_constants.SERVICE_PARAM_NAME_KUBERNETES_API_SAN_LIST] = \
            apiserver_san_list

        values = {
            'service': sysinv_constants.SERVICE_TYPE_KUBERNETES,
            'section':
                sysinv_constants.SERVICE_PARAM_SECTION_KUBERNETES_CERTIFICATES,
            'personality': None,
            'resource': None,
            'parameters': parameters
        }

        print("Populating/Updating kubernetes config...")
        client.sysinv.service_parameter.create(**values)
        print("Kubernetes config completed.")


def get_management_mac_address():
    ifname = CONF.get('BOOTSTRAP_CONFIG', 'MANAGEMENT_INTERFACE')

    try:
        filename = '/sys/class/net/%s/address' % ifname
        with open(filename, 'r') as f:
            return f.readline().rstrip()
    except Exception:
        raise ConfigFail("Failed to obtain mac address of %s" % ifname)


def get_rootfs_node():
    """Cloned from sysinv"""
    cmdline_file = '/proc/cmdline'
    device = None

    with open(cmdline_file, 'r') as f:
        for line in f:
            for param in line.split():
                params = param.split("=", 1)
                if params[0] == "root":
                    if "UUID=" in params[1]:
                        key, uuid = params[1].split("=")
                        symlink = "/dev/disk/by-uuid/%s" % uuid
                        device = os.path.basename(os.readlink(symlink))
                    else:
                        device = os.path.basename(params[1])

    if device is not None:
        if sysinv_constants.DEVICE_NAME_NVME in device:
            re_line = re.compile(r'^(nvme[0-9]*n[0-9]*)')
        else:
            re_line = re.compile(r'^(\D*)')
        match = re_line.search(device)
        if match:
            return os.path.join("/dev", match.group(1))

    return


def find_boot_device():
    """Determine boot device """
    boot_device = None

    context = pyudev.Context()

    # Get the boot partition
    # Unfortunately, it seems we can only get it from the logfile.
    # We'll parse the device used from a line like the following:
    # BIOSBoot.create: device: /dev/sda1 ; status: False ; type: biosboot ;
    # or
    # EFIFS.create: device: /dev/sda1 ; status: False ; type: efi ;
    #
    logfile = '/var/log/anaconda/storage.log'

    re_line = re.compile(r'(BIOSBoot|EFIFS).create: device: ([^\s;]*)')
    boot_partition = None
    with open(logfile, 'r') as f:
        for line in f:
            match = re_line.search(line)
            if match:
                boot_partition = match.group(2)
                break
    if boot_partition is None:
        raise ConfigFail("Failed to determine the boot partition")

    # Find the boot partition and get its parent
    for device in context.list_devices(DEVTYPE='partition'):
        if device.device_node == boot_partition:
            boot_device = device.find_parent('block').device_node
            break

    if boot_device is None:
        raise ConfigFail("Failed to determine the boot device")

    return boot_device


def device_node_to_device_path(dev_node):
    device_path = None
    cmd = ["find", "-L", "/dev/disk/by-path/", "-samefile", dev_node]

    try:
        out = subprocess.check_output(cmd)
    except subprocess.CalledProcessError as e:
        print("Could not retrieve device information: %s" % e)
        return device_path

    device_path = out.rstrip()
    return device_path


def get_device_from_function(get_disk_function):
    device_node = get_disk_function()
    device_path = device_node_to_device_path(device_node)
    device = device_path if device_path else os.path.basename(device_node)

    return device


def get_console_info():
    """Determine console info """
    cmdline_file = '/proc/cmdline'

    re_line = re.compile(r'^.*\s+console=([^\s]*)')

    with open(cmdline_file, 'r') as f:
        for line in f:
            match = re_line.search(line)
            if match:
                console_info = match.group(1)
                return console_info
    return ''


def get_tboot_info():
    """Determine whether we were booted with a tboot value """
    cmdline_file = '/proc/cmdline'

    # tboot=true, tboot=false, or no tboot parameter expected
    re_line = re.compile(r'^.*\s+tboot=([^\s]*)')

    with open(cmdline_file, 'r') as f:
        for line in f:
            match = re_line.search(line)
            if match:
                tboot = match.group(1)
                return tboot
    return ''


def get_orig_install_mode():
    """Determine original install mode, text vs graphical """
    # Post-install, the only way to detemine the original install mode
    # will be to check the anaconda install log for the parameters passed
    logfile = '/var/log/anaconda/anaconda.log'

    search_str = 'Display mode = t'
    try:
        subprocess.check_call(['grep', '-q', search_str, logfile])
        return 'text'
    except subprocess.CalledProcessError:
        return 'graphical'


def populate_controller_config(client):
    if not INITIAL_POPULATION:
        return

    mgmt_mac = get_management_mac_address()
    print("Management mac = %s" % mgmt_mac)
    rootfs_device = get_device_from_function(get_rootfs_node)
    print("Root fs device = %s" % rootfs_device)
    boot_device = get_device_from_function(find_boot_device)
    print("Boot device = %s" % boot_device)
    console = get_console_info()
    print("Console = %s" % console)
    tboot = get_tboot_info()
    print("Tboot = %s" % tboot)
    install_output = get_orig_install_mode()
    print("Install output = %s" % install_output)

    provision_state = sysinv.HOST_PROVISIONED
    system_type = CONF.get('BOOTSTRAP_CONFIG', 'SYSTEM_TYPE')
    if system_type == COMBINED_LOAD:
        provision_state = sysinv.HOST_PROVISIONING

    values = {
        'personality': sysinv.HOST_PERSONALITY_CONTROLLER,
        'hostname': CONF.get('BOOTSTRAP_CONFIG', 'CONTROLLER_HOSTNAME'),
        'mgmt_ip': CONF.get('BOOTSTRAP_CONFIG', 'CONTROLLER_0_ADDRESS'),
        'mgmt_mac': mgmt_mac,
        'administrative': sysinv.HOST_ADMIN_STATE_LOCKED,
        'operational': sysinv.HOST_OPERATIONAL_STATE_DISABLED,
        'availability': sysinv.HOST_AVAIL_STATE_OFFLINE,
        'invprovision': provision_state,
        'rootfs_device': rootfs_device,
        'boot_device': boot_device,
        'console': console,
        'tboot': tboot,
        'install_output': install_output,
    }
    print("Host values = %s" % values)
    try:
        controller = client.sysinv.ihost.create(**values)
    except Exception as e:
        if INCOMPLETE_BOOTSTRAP:
            # The previous bootstrap might have been interrupted while
            # it was in the middle of creating the controller-0 host.
            controller = client.sysinv.ihost.get('controller-0')
            if controller:
                pass
            else:
                raise e
        else:
            raise e
    print("Host controller-0 created.")
    return controller


def wait_initial_inventory_complete(client, host):
    for _ in range(constants.SYSTEM_CONFIG_TIMEOUT / 10):
        try:
            host = client.sysinv.ihost.get('controller-0')
            if host and (host.inv_state ==
                         sysinv_constants.INV_STATE_INITIAL_INVENTORIED):
                return host
        except Exception:
            pass
        time.sleep(10)
    else:
        raise ConfigFail('Timeout waiting for controller inventory '
                         'completion')


def inventory_config_complete_wait(client, controller):
    # Wait for sysinv-agent to populate initial inventory
    if not INITIAL_POPULATION:
        return

    wait_initial_inventory_complete(client, controller)


def populate_default_storage_backend(client, controller):
    if not INITIAL_POPULATION:
        return

    print("Populating ceph-mon config for controller-0...")
    values = {'ihost_uuid': controller.uuid}
    client.sysinv.ceph_mon.create(**values)

    print("Populating ceph storage backend config...")
    values = {'confirmed': True}
    try:
        client.sysinv.storage_ceph.create(**values)
    except Exception as e:
        if INCOMPLETE_BOOTSTRAP:
            storage_backends = client.sysinv.storage_backend.list()
            for storage_backend in storage_backends:
                if storage_backend.name == "ceph-store":
                    break
            else:
                raise e
        else:
            raise e
    print("Default storage backend provisioning completed.")


def handle_invalid_input():
    raise Exception("Invalid input!\nUsage: <bootstrap-config-file> "
                    "[--system] [--network] [--service]")


if __name__ == '__main__':

    argc = len(sys.argv)
    if argc < 2 or argc > 5:
        print("Failed")
        handle_invalid_input()

    arg = 2
    while arg < argc:
        if sys.argv[arg] == "--system":
            RECONFIGURE_SYSTEM = True
        elif sys.argv[arg] == "--network":
            RECONFIGURE_NETWORK = True
        elif sys.argv[arg] == "--service":
            RECONFIGURE_SERVICE = True
        else:
            handle_invalid_input()
        arg += 1

    INITIAL_POPULATION = not (RECONFIGURE_SYSTEM or RECONFIGURE_NETWORK or
                              RECONFIGURE_SERVICE)

    config_file = sys.argv[1]
    if not os.path.exists(config_file):
        raise Exception("Config file is not found!")

    CONF.read(config_file)
    INCOMPLETE_BOOTSTRAP = CONF.getboolean('BOOTSTRAP_CONFIG',
                                           'INCOMPLETE_BOOTSTRAP')

    # Puppet manifest might be applied as part of initial host
    # config, set INITIAL_CONFIG_PRIMARY variable just in case.
    os.environ["INITIAL_CONFIG_PRIMARY"] = "true"

    try:
        with openstack.OpenStack() as client:
            populate_system_config(client)
            populate_load_config(client)
            populate_network_config(client)
            populate_dns_config(client)
            populate_docker_config(client)
            controller = populate_controller_config(client)
            inventory_config_complete_wait(client, controller)
            populate_default_storage_backend(client, controller)
            os.remove(config_file)
            if INITIAL_POPULATION:
                print("Successfully updated the initial system config.")
            else:
                print("Successfully provisioned the initial system config.")
    except Exception:
        # Print the marker string for Ansible and re raise the exception
        if INITIAL_POPULATION:
            print("Failed to update the initial system config.")
        else:
            print("Failed to provision the initial system config.")
        raise
