#!/usr/bin/python
#
# Copyright (c) 2019 Wind River Systems, Inc.
#
# SPDX-License-Identifier: Apache-2.0
#

import os
import json
import subprocess

from controllerconfig import openstack

OSD_ROOT_DIR = "/var/lib/ceph/osd"
MON_ROOT_DIR = "/var/lib/ceph/mon"
CEPH_LV_PATH = '/dev/mapper/cgts--vg-ceph--mon--lv'
CEPH_MON_VG = 'cgts-vg'
CEPH_MON_LV = 'ceph-mon-lv'


def get_ceph_mon_size():
    with openstack.OpenStack() as client:
        ceph_mons = client.sysinv.ceph_mon.list()
        # All Ceph monitor partitions have the same size, so grab one and return.
        if ceph_mons:
            return ceph_mons[0].ceph_mon_gib
        else:
            raise Exception("No ceph monitor defined!")


def mount_osds():
    cmd_line = ['ceph-disk', 'list', '--format=json']

    with open(os.devnull, "w") as fnull:
        config_data = json.loads(subprocess.check_output(cmd_line,
                                 stderr=fnull).decode('UTF-8'))

    # Filter Ceph OSD partitions from our cluster
    # ceph data partition is always the first, it is part of the
    # cluster called 'ceph' and it is of type 'data'.
    ceph_parts = [e for e in config_data
                  if 'partitions' in e and 'cluster' in e['partitions'][0] and
                  e['partitions'][0]['cluster'] == 'ceph' and
                  e['partitions'][0]['type'] == 'data']

    for ceph_part in ceph_parts:
        # e.g: 'path: /dev/sdc1' => the osd that should be mounted
        disk_to_mount = ceph_part['partitions'][0]['path']
        fs_type = ceph_part['partitions'][0]['fs_type']

        # 'whoami' - the osd number (0,1...)
        osd = ceph_part['partitions'][0]['whoami']
        osd_dir = OSD_ROOT_DIR + "/ceph-" + osd

        if not os.path.exists(osd_dir):
            os.mkdir(osd_dir, 0o751)

        # mount the osd in /var/lib/ceph/osd/ceph-(0,1..)
        if not os.path.ismount(osd_dir):
            print("Mounting partition {} to {}".format(disk_to_mount, osd_dir))
            with open(os.devnull, "w") as fnull:
                subprocess.check_output(["mount", "-t",
                                        fs_type, disk_to_mount,
                                        osd_dir], stderr=fnull)
        else:
            print("Directory {} already mounted, skipping.".format(osd_dir))


def prepare_monitor():
    ceph_mon_gib = get_ceph_mon_size()
    with open(os.devnull, "w") as fnull:
        # Cleaning up, in case of replay
        try:
            cmd = ["umount", MON_ROOT_DIR]
            subprocess.check_output(cmd, stderr=fnull)
            print("Unmounted ceph-mon at {}.".format(MON_ROOT_DIR))
        except Exception:
            pass

        try:
            cmd = ["lvremove", "{}/{}".format(CEPH_MON_VG, CEPH_MON_LV), "-y"]
            subprocess.check_output(cmd, stderr=fnull)
            print("Removed Ceph mon logical volume.")
        except Exception:
            pass

        print("Creating ceph-mon lv with size {}GB.".format(ceph_mon_gib))
        cmd = ['timeout', '20', 'lvcreate', '-n', CEPH_MON_LV, '-L',
               '{}G'.format(ceph_mon_gib), CEPH_MON_VG]
        subprocess.check_output(cmd, stderr=fnull)

        print("Formatting ceph-mon lv as ext4.")
        subprocess.check_output(["mkfs.ext4", CEPH_LV_PATH], stderr=fnull)

        print("Mounting ceph-mon lv at {} to {}.".format(CEPH_LV_PATH, MON_ROOT_DIR))
        if not os.path.exists(MON_ROOT_DIR):
            os.mkdir(MON_ROOT_DIR, 0o751)
        subprocess.check_output(['mount', "-t", "ext4", CEPH_LV_PATH, MON_ROOT_DIR],
                                stderr=fnull)

        print("Populating Ceph mon fs structure for controller-0.")
        subprocess.check_output(["ceph-mon", "--mkfs", "-i", "controller-0"], stderr=fnull)


if __name__ == '__main__':
    mount_osds()
    prepare_monitor()