# Copyright (c) 2016 Dell Inc. or its subsidiaries.
# All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

from oslo_log import log
from oslo_utils import excutils
from oslo_utils import importutils

storops = importutils.try_import('storops')
if storops:
    from storops import exception as storops_ex
else:
    # Set storops_ex to be None for unit test
    storops_ex = None

from cinder import exception
from cinder.i18n import _, _LW
from cinder.volume.drivers.dell_emc.unity import utils


LOG = log.getLogger(__name__)


class UnityClient(object):
    def __init__(self, host, username, password, verify_cert=True):
        if storops is None:
            msg = _('Python package storops is not installed which '
                    'is required to run Unity driver.')
            raise exception.VolumeBackendAPIException(data=msg)
        self._system = None
        self.host = host
        self.username = username
        self.password = password
        self.verify_cert = verify_cert

    @property
    def system(self):
        if self._system is None:
            self._system = storops.UnitySystem(
                host=self.host, username=self.username, password=self.password,
                verify=self.verify_cert)
        return self._system

    def get_serial(self):
        return self.system.serial_number

    def create_lun(self, name, size, pool, description=None,
                   io_limit_policy=None):
        """Creates LUN on the Unity system.

        :param name: lun name
        :param size: lun size in GiB
        :param pool: UnityPool object represent to pool to place the lun
        :param description: lun description
        :param io_limit_policy: io limit on the LUN
        :return: UnityLun object
        """
        try:
            lun = pool.create_lun(lun_name=name, size_gb=size,
                                  description=description,
                                  io_limit_policy=io_limit_policy)
        except storops_ex.UnityLunNameInUseError:
            LOG.debug("LUN %s already exists. Return the existing one.",
                      name)
            lun = self.system.get_lun(name=name)
        return lun

    def delete_lun(self, lun_id):
        """Deletes LUN on the Unity system.

        :param lun_id: id of the LUN
        """
        try:
            lun = self.system.get_lun(_id=lun_id)
            lun.delete()
        except storops_ex.UnityResourceNotFoundError:
            LOG.debug("LUN %s doesn't exist. Deletion is not needed.",
                      lun_id)

    def get_lun(self, lun_id=None, name=None):
        """Gets LUN on the Unity system.

        :param lun_id: id of the LUN
        :param name: name of the LUN
        :return: `UnityLun` object
        """
        lun = None
        if lun_id is None and name is None:
            LOG.warning(
                _LW("Both lun_id and name are None to get LUN. Return None."))
        else:
            try:
                lun = self.system.get_lun(_id=lun_id, name=name)
            except storops_ex.UnityResourceNotFoundError:
                LOG.warning(
                    _LW("LUN id=%(id)s, name=%(name)s doesn't exist."),
                    {'id': lun_id, 'name': name})
        return lun

    def extend_lun(self, lun_id, size_gib):
        lun = self.system.get_lun(lun_id)
        try:
            lun.total_size_gb = size_gib
        except storops_ex.UnityNothingToModifyError:
            LOG.debug("LUN %s is already expanded. LUN expand is not needed.",
                      lun_id)
        return lun

    def get_pools(self):
        """Gets all storage pools on the Unity system.

        :return: list of UnityPool object
        """
        return self.system.get_pool()

    def create_snap(self, src_lun_id, name=None):
        """Creates a snapshot of LUN on the Unity system.

        :param src_lun_id: the source LUN ID of the snapshot.
        :param name: the name of the snapshot. The Unity system will give one
                     if `name` is None.
        """
        try:
            lun = self.get_lun(lun_id=src_lun_id)
            snap = lun.create_snap(name, is_auto_delete=False)
        except storops_ex.UnitySnapNameInUseError as err:
            LOG.debug(
                "Snap %(snap_name)s already exists on LUN %(lun_id)s. "
                "Return the existing one. Message: %(err)s",
                {'snap_name': name,
                 'lun_id': src_lun_id,
                 'err': err})
            snap = self.get_snap(name=name)
        return snap

    @staticmethod
    def delete_snap(snap):
        if snap is None:
            LOG.debug("Snap to delete is None, skipping deletion.")
            return

        try:
            snap.delete()
        except storops_ex.UnityResourceNotFoundError as err:
            LOG.debug("Snap %(snap_name)s may be deleted already. "
                      "Message: %(err)s",
                      {'snap_name': snap.name,
                       'err': err})
        except storops_ex.UnityDeleteAttachedSnapError as err:
            with excutils.save_and_reraise_exception():
                LOG.warning(_LW("Failed to delete snapshot %(snap_name)s "
                                "which is in use. Message: %(err)s"),
                            {'snap_name': snap.name, 'err': err})

    def get_snap(self, name=None):
        try:
            return self.system.get_snap(name=name)
        except storops_ex.UnityResourceNotFoundError as err:
            msg = _LW("Snapshot %(name)s doesn't exist. Message: %(err)s")
            LOG.warning(msg, {'name': name, 'err': err})
        return None

    def create_host(self, name, uids):
        """Creates a host on Unity.

        Creates a host on Unity which has the uids associated.

        :param name: name of the host
        :param uids: iqns or wwns list
        :return: UnitHost object
        """

        try:
            host = self.system.get_host(name=name)
        except storops_ex.UnityResourceNotFoundError:
            LOG.debug('Existing host %s not found.  Create a new one.', name)
            host = self.system.create_host(name=name)

        host_initiators_ids = self.get_host_initiator_ids(host)
        un_registered = [h for h in uids if h not in host_initiators_ids]
        for uid in un_registered:
            host.add_initiator(uid, force_create=True)

        host.update()
        return host

    @staticmethod
    def get_host_initiator_ids(host):
        fc = host.fc_host_initiators
        fc_ids = [] if fc is None else fc.initiator_id
        iscsi = host.iscsi_host_initiators
        iscsi_ids = [] if iscsi is None else iscsi.initiator_id
        return fc_ids + iscsi_ids

    @staticmethod
    def attach(host, lun_or_snap):
        """Attaches a `UnityLun` or `UnitySnap` to a `UnityHost`.

        :param host: `UnityHost` object
        :param lun_or_snap: `UnityLun` or `UnitySnap` object
        :return: hlu
        """
        try:
            return host.attach(lun_or_snap, skip_hlu_0=True)
        except storops_ex.UnityResourceAlreadyAttachedError:
            return host.get_hlu(lun_or_snap)

    @staticmethod
    def detach(host, lun_or_snap):
        """Detaches a `UnityLun` or `UnitySnap` from a `UnityHost`.

        :param host: `UnityHost` object
        :param lun_or_snap: `UnityLun` object
        """
        lun_or_snap.update()
        host.detach(lun_or_snap)

    def get_host(self, name):
        return self.system.get_host(name=name)

    def get_iscsi_target_info(self):
        portals = self.system.get_iscsi_portal()
        return [{'portal': utils.convert_ip_to_portal(p.ip_address),
                 'iqn': p.iscsi_node.name}
                for p in portals]

    def get_fc_target_info(self, host=None, logged_in_only=False):
        """Get the ports WWN of FC on array.

        :param host: the host to which the FC port is registered.
        :param logged_in_only: whether to retrieve only the logged-in port.

        :return the WWN of FC ports. For example, the FC WWN on array is like:
        50:06:01:60:89:20:09:25:50:06:01:6C:09:20:09:25.
        This function removes the colons and returns the last 16 bits:
        5006016C09200925.
        """
        ports = []
        if logged_in_only:
            for host_initiator in host.fc_host_initiators:
                paths = host_initiator.paths or []
                for path in paths:
                    if path.is_logged_in:
                        ports.append(path.fc_port)
        else:
            ports = self.system.get_fc_port()
        return [po.wwn.replace(':', '')[16:] for po in ports]

    def create_io_limit_policy(self, name, max_iops=None, max_kbps=None):
        try:
            limit = self.system.create_io_limit_policy(
                name, max_iops=max_iops, max_kbps=max_kbps)
        except storops_ex.UnityPolicyNameInUseError:
            limit = self.system.get_io_limit_policy(name=name)
        return limit

    def get_io_limit_policy(self, qos_specs):
        limit_policy = None
        if qos_specs is not None:
            limit_policy = self.create_io_limit_policy(
                qos_specs['id'],
                qos_specs.get(utils.QOS_MAX_IOPS),
                qos_specs.get(utils.QOS_MAX_BWS))
        return limit_policy

    def get_pool_name(self, lun_name):
        lun = self.system.get_lun(name=lun_name)
        return lun.pool_name
