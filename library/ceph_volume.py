#!/usr/bin/python
import datetime
import os

ANSIBLE_METADATA = {
    'metadata_version': '1.0',
    'status': ['preview'],
    'supported_by': 'community'
}

DOCUMENTATION = '''
---
module: ceph_volume

short_description: Create ceph OSDs with ceph-volume

description:
    - Using the ceph-volume utility available in Ceph this module
      can be used to create ceph OSDs that are backed by logical volumes.
    - Only available in ceph versions luminous or greater.

options:
    cluster:
        description:
            - The ceph cluster name.
        required: false
        default: ceph
    objectstore:
        description:
            - The objectstore of the OSD, either filestore or bluestore
            - Required if action is 'create'
        required: false
        choices: ['bluestore', 'filestore']
        default: bluestore
    action:
        description:
            - The action to take. Either creating OSDs or zapping devices.
        required: true
        choices: ['create', 'zap', 'batch', 'prepare', 'activate', 'list']
        default: create
    data:
        description:
            - The logical volume name or device to use for the OSD data.
        required: true
    data_vg:
        description:
            - If data is a lv, this must be the name of the volume group it belongs to.
        required: false
    journal:
        description:
            - The logical volume name or partition to use as a filestore journal.
            - Only applicable if objectstore is 'filestore'.
        required: false
    journal_vg:
        description:
            - If journal is a lv, this must be the name of the volume group it belongs to.
            - Only applicable if objectstore is 'filestore'.
        required: false
    db:
        description:
            - A partition or logical volume name to use for block.db.
            - Only applicable if objectstore is 'bluestore'.
        required: false
    db_vg:
        description:
            - If db is a lv, this must be the name of the volume group it belongs to.  # noqa E501
            - Only applicable if objectstore is 'bluestore'.
        required: false
    wal:
        description:
            - A partition or logical volume name to use for block.wal.
            - Only applicable if objectstore is 'bluestore'.
        required: false
    wal_vg:
        description:
            - If wal is a lv, this must be the name of the volume group it belongs to.  # noqa E501
            - Only applicable if objectstore is 'bluestore'.
        required: false
    crush_device_class:
        description:
            - Will set the crush device class for the OSD.
        required: false
    dmcrypt:
        description:
            - If set to True the OSD will be encrypted with dmcrypt.
        required: false
    batch_devices:
        description:
            - A list of devices to pass to the 'ceph-volume lvm batch' subcommand.
            - Only applicable if action is 'batch'.
        required: false
    osds_per_device:
        description:
            - The number of OSDs to create per device.
            - Only applicable if action is 'batch'.
        required: false
        default: 1
    list:
        description:
            - List potential Ceph LVM metadata on a device
        required: false

author:
    - Andrew Schoen (@andrewschoen)
    - Sebastien Han <seb@redhat.com>
'''

EXAMPLES = '''
- name: set up a filestore osd with an lv data and a journal partition
  ceph_volume:
    objectstore: filestore
    data: data-lv
    data_vg: data-vg
    journal: /dev/sdc1
    action: create

- name: set up a bluestore osd with a raw device for data
  ceph_volume:
    objectstore: bluestore
    data: /dev/sdc
    action: create


- name: set up a bluestore osd with an lv for data and partitions for block.wal and block.db  # noqa E501
  ceph_volume:
    objectstore: bluestore
    data: data-lv
    data_vg: data-vg
    db: /dev/sdc1
    wal: /dev/sdc2
    action: create
'''


from ansible.module_utils.basic import AnsibleModule  # noqa 4502


def fatal(message, module):
    '''
    Report a fatal error and exit
    '''

    if module:
        module.fail_json(msg=message, changed=False, rc=1)
    else:
        raise(Exception(message))


def container_exec(binary, container_image):
    '''
    Build the docker CLI to run a command inside a container
    '''

    command_exec = ['docker', 'run', '--rm', '--privileged', '--net=host',
                    '-v', '/run/lock/lvm:/run/lock/lvm:z',
                    '-v', '/dev:/dev', '-v', '/etc/ceph:/etc/ceph:z',
                    '-v', '/run/lvm/lvmetad.socket:/run/lvm/lvmetad.socket',
                    '-v', '/var/lib/ceph/:/var/lib/ceph/:z',
                    os.path.join('--entrypoint=' + binary),
                    container_image]
    return command_exec


def build_ceph_volume_cmd(subcommand, container_image, cluster=None):
    '''
    Build the ceph-volume command
    '''

    if container_image:
        binary = 'ceph-volume'
        cmd = container_exec(
            binary, container_image)
    else:
        binary = ['ceph-volume']
        cmd = binary

    if cluster:
        cmd.extend(['--cluster', cluster])

    cmd.append('lvm')
    cmd.append(subcommand)

    return cmd


def exec_command(module, cmd):
    '''
    Execute command
    '''

    rc, out, err = module.run_command(cmd)
    if rc != 0:
        return rc, cmd, out, err

    return rc, cmd, out, err


def is_containerized():
    '''
    Check if we are running on a containerized cluster
    '''

    if 'CEPH_CONTAINER_IMAGE' in os.environ:
        container_image = os.getenv('CEPH_CONTAINER_IMAGE')
    else:
        container_image = None

    return container_image


def get_data(data, data_vg):
    if data_vg:
        data = '{0}/{1}'.format(data_vg, data)
    return data


def get_journal(journal, journal_vg):
    if journal_vg:
        journal = '{0}/{1}'.format(journal_vg, journal)
    return journal


def get_db(db, db_vg):
    if db_vg:
        db = '{0}/{1}'.format(db_vg, db)
    return db


def get_wal(wal, wal_vg):
    if wal_vg:
        wal = '{0}/{1}'.format(wal_vg, wal)
    return wal


def batch(module, container_image):
    '''
    Batch prepare OSD devices
    '''

    # get module variables
    cluster = module.params['cluster']
    objectstore = module.params['objectstore']
    batch_devices = module.params.get('batch_devices', None)
    crush_device_class = module.params.get('crush_device_class', None)
    dmcrypt = module.params.get('dmcrypt', None)
    osds_per_device = module.params.get('osds_per_device', None)

    if not osds_per_device:
        fatal('osds_per_device must be provided if action is "batch"', module)

    if osds_per_device < 1:
        fatal('osds_per_device must be greater than 0 if action is "batch"', module)  # noqa E501

    if not batch_devices:
        fatal('batch_devices must be provided if action is "batch"', module)

    # Build the CLI
    subcommand = 'batch'
    cmd = build_ceph_volume_cmd(subcommand, container_image, cluster)
    cmd.extend(['--%s' % objectstore])
    cmd.extend('--yes')
    cmd.extend('--no-systemd')

    if crush_device_class:
        cmd.extend(['--crush-device-class', crush_device_class])

    if dmcrypt:
        cmd.append('--dmcrypt')

    if osds_per_device > 1:
        cmd.extend(['--osds-per-device', osds_per_device])

    cmd.extend(batch_devices)

    return cmd


def prepare_osd(module, container_image):
    '''
    Prepare OSD devices
    '''

    # get module variables
    cluster = module.params['cluster']
    objectstore = module.params['objectstore']
    data = module.params['data']
    journal = module.params.get('journal', None)
    journal_vg = module.params.get('journal_vg', None)
    db = module.params.get('db', None)
    db_vg = module.params.get('db_vg', None)
    wal = module.params.get('wal', None)
    wal_vg = module.params.get('wal_vg', None)
    crush_device_class = module.params.get('crush_device_class', None)
    dmcrypt = module.params.get('dmcrypt', None)

    # Build the CLI
    subcommand = 'prepare'
    cmd = build_ceph_volume_cmd(subcommand, container_image, cluster)
    cmd.extend(['--%s' % objectstore])
    cmd.append('--data')
    cmd.append(data)

    if journal:
        journal = get_journal(journal, journal_vg)
        cmd.extend(['--journal', journal])

    if db:
        db = get_db(db, db_vg)
        cmd.extend(['--block.db', db])

    if wal:
        wal = get_wal(wal, wal_vg)
        cmd.extend(['--block.wal', wal])

    if crush_device_class:
        cmd.extend(['--crush-device-class', crush_device_class])

    if dmcrypt:
        cmd.append('--dmcrypt')

    return cmd


def list_osd(module, container_image):
    '''
    List will detect wether or not a device has Ceph LVM Metadata
    '''

    # get module variables
    data = module.params['data']
    cluster = module.params['cluster']
    data_vg = module.params.get('data_vg', None)
    data = get_data(data, data_vg)

    # Build the CLI
    subcommand = 'list'
    cmd = build_ceph_volume_cmd(subcommand, container_image, cluster)
    cmd.append(data)
    cmd.append('--format=json')

    return cmd


def activate_osd():
    '''
    Activate all the OSDs on a machine
    '''

    # build the CLI
    subcommand = 'activate'
    container_image = None
    cmd = build_ceph_volume_cmd(subcommand, container_image)
    cmd.append('--all')

    return cmd


def zap_devices(module, container_image):
    '''
    Will run 'ceph-volume lvm zap' on all devices, lvs and partitions
    used to create the OSD. The --destroy flag is always passed so that
    if an OSD was originally created with a raw device or partition for
    'data' then any lvs that were created by ceph-volume are removed.
    '''

    # get module variables
    data = module.params['data']
    data_vg = module.params.get('data_vg', None)
    journal = module.params.get('journal', None)
    journal_vg = module.params.get('journal_vg', None)
    db = module.params.get('db', None)
    db_vg = module.params.get('db_vg', None)
    wal = module.params.get('wal', None)
    wal_vg = module.params.get('wal_vg', None)
    data = get_data(data, data_vg)

    # build the CLI
    subcommand = 'zap'
    cmd = build_ceph_volume_cmd(subcommand, container_image)
    cmd.append('--destroy')
    cmd.append(data)

    if journal:
        journal = get_journal(journal, journal_vg)
        cmd.extend([journal])

    if db:
        db = get_db(db, db_vg)
        cmd.extend([db])

    if wal:
        wal = get_wal(wal, wal_vg)
        cmd.extend([wal])

    return cmd


def run_module():
    module_args = dict(
        cluster=dict(type='str', required=False, default='ceph'),
        objectstore=dict(type='str', required=False, choices=[
                         'bluestore', 'filestore'], default='bluestore'),
        action=dict(type='str', required=False, choices=[
                    'create', 'zap', 'batch', 'prepare', 'activate', 'list'], default='create'),  # noqa 4502
        data=dict(type='str', required=False),
        data_vg=dict(type='str', required=False),
        journal=dict(type='str', required=False),
        journal_vg=dict(type='str', required=False),
        db=dict(type='str', required=False),
        db_vg=dict(type='str', required=False),
        wal=dict(type='str', required=False),
        wal_vg=dict(type='str', required=False),
        crush_device_class=dict(type='str', required=False),
        dmcrypt=dict(type='bool', required=False, default=False),
        batch_devices=dict(type='list', required=False, default=[]),
        osds_per_device=dict(type='int', required=False, default=1),
    )

    module = AnsibleModule(
        argument_spec=module_args,
        supports_check_mode=True
    )

    result = dict(
        changed=False,
        stdout='',
        stderr='',
        rc='',
        start='',
        end='',
        delta='',
    )

    if module.check_mode:
        return result

    # start execution
    startd = datetime.datetime.now()

    # get the desired action
    action = module.params['action']

    # will return either the image name or None
    container_image = is_containerized()

    if action == 'create' or action == 'prepare':
        data = module.params['data']
        rc, cmd, out, err = exec_command(
            module, list_osd(module, container_image))

        if rc == 0:
            result['stdout'] = 'skipped, since {0} is already used for an osd'.format(  # noqa E501
            data)
            result['rc'] = 0
            module.exit_json(**result)

    elif action == 'create':
        rc, cmd, out, err = exec_command(
            module, prepare_osd(module, container_image))

        if rc != 0:
            module.fail_json(msg='non-zero return code', **result)

        rc, cmd, out, err = exec_command(
            module, activate_osd(module, container_image))

    elif action == 'prepare':
        rc, cmd, out, err = exec_command(
            module, prepare_osd(module, container_image))

    elif action == 'activate':
        if container_image:
            fatal(
                "This is not how container's activation happens, nothing to activate", module)  # noqa E501
        rc, cmd, out, err = exec_command(
            module, activate_osd())

    elif action == 'zap':
        rc, cmd, out, err = exec_command(
            module, zap_devices(module, container_image))

    elif action == 'list':
        rc, cmd, out, err = exec_command(
            module, list_osd(module, container_image))

    elif action == 'batch':
        rc, cmd, out, err = exec_command(
            module, batch(module, container_image))
    else:
        module.fail_json(
            msg='State must either be "create" or "prepare" or "activate" or "list" or "zap" or "batch".', changed=False, rc=1)  # noqa E501

    endd = datetime.datetime.now()
    delta = endd - startd

    result = dict(
        cmd=cmd,
        start=str(startd),
        end=str(endd),
        delta=str(delta),
        rc=rc,
        stdout=out.rstrip(b'\r\n'),
        stderr=err.rstrip(b'\r\n'),
        changed=True,
    )

    if rc != 0:
        module.fail_json(msg='non-zero return code', **result)

    module.exit_json(**result)


def main():
    run_module()


if __name__ == '__main__':
    main()
