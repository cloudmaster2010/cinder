# Copyright (c) 2012 - 2014 EMC Corporation.
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.
"""
Driver for Dell EMC XtremIO Storage.
supported XtremIO version 2.4 and up

.. code-block:: none

  1.0.0 - initial release
  1.0.1 - enable volume extend
  1.0.2 - added FC support, improved error handling
  1.0.3 - update logging level, add translation
  1.0.4 - support for FC zones
  1.0.5 - add support for XtremIO 4.0
  1.0.6 - add support for iSCSI multipath, CA validation, consistency groups,
          R/O snapshots, CHAP discovery authentication
  1.0.7 - cache glance images on the array
  1.0.8 - support for volume retype, CG fixes
"""

import json
import math
import random
import requests
import string

from oslo_config import cfg
from oslo_log import log as logging
from oslo_utils import units
import six

from cinder import context
from cinder import exception
from cinder.i18n import _, _LE, _LI, _LW
from cinder import interface
from cinder.objects import fields
from cinder import utils
from cinder.volume import driver
from cinder.volume.drivers.san import san
from cinder.zonemanager import utils as fczm_utils


LOG = logging.getLogger(__name__)

CONF = cfg.CONF
DEFAULT_PROVISIONING_FACTOR = 20.0
XTREMIO_OPTS = [
    cfg.StrOpt('xtremio_cluster_name',
               default='',
               help='XMS cluster id in multi-cluster environment'),
    cfg.IntOpt('xtremio_array_busy_retry_count',
               default=5,
               help='Number of retries in case array is busy'),
    cfg.IntOpt('xtremio_array_busy_retry_interval',
               default=5,
               help='Interval between retries in case array is busy'),
    cfg.IntOpt('xtremio_volumes_per_glance_cache',
               default=100,
               help='Number of volumes created from each cached glance image')]

CONF.register_opts(XTREMIO_OPTS)

RANDOM = random.Random()
OBJ_NOT_FOUND_ERR = 'obj_not_found'
VOL_NOT_UNIQUE_ERR = 'vol_obj_name_not_unique'
VOL_OBJ_NOT_FOUND_ERR = 'vol_obj_not_found'
ALREADY_MAPPED_ERR = 'already_mapped'
SYSTEM_BUSY = 'system_is_busy'
TOO_MANY_OBJECTS = 'too_many_objs'
TOO_MANY_SNAPSHOTS_PER_VOL = 'too_many_snapshots_per_vol'


XTREMIO_OID_NAME = 1
XTREMIO_OID_INDEX = 2


class XtremIOClient(object):
    def __init__(self, configuration, cluster_id):
        self.configuration = configuration
        self.cluster_id = cluster_id
        self.verify = (self.configuration.
                       safe_get('driver_ssl_cert_verify') or False)
        if self.verify:
            verify_path = (self.configuration.
                           safe_get('driver_ssl_cert_path') or None)
            if verify_path:
                self.verify = verify_path

    def get_base_url(self, ver):
        if ver == 'v1':
            return 'https://%s/api/json/types' % self.configuration.san_ip
        elif ver == 'v2':
            return 'https://%s/api/json/v2/types' % self.configuration.san_ip

    @utils.retry(exception.XtremIOArrayBusy,
                 CONF.xtremio_array_busy_retry_count,
                 CONF.xtremio_array_busy_retry_interval, 1)
    def req(self, object_type='volumes', method='GET', data=None,
            name=None, idx=None, ver='v1'):
        if not data:
            data = {}
        if name and idx:
            msg = _("can't handle both name and index in req")
            LOG.error(msg)
            raise exception.VolumeDriverException(message=msg)

        url = '%s/%s' % (self.get_base_url(ver), object_type)
        params = {}
        key = None
        if name:
            params['name'] = name
            key = name
        elif idx:
            url = '%s/%d' % (url, idx)
            key = str(idx)
        if method in ('GET', 'DELETE'):
            params.update(data)
            self.update_url(params, self.cluster_id)
        if method != 'GET':
            self.update_data(data, self.cluster_id)
            LOG.debug('data: %s', data)
        LOG.debug('%(type)s %(url)s', {'type': method, 'url': url})
        try:
            response = requests.request(method, url, params=params,
                                        data=json.dumps(data),
                                        verify=self.verify,
                                        auth=(self.configuration.san_login,
                                              self.configuration.san_password))
        except requests.exceptions.RequestException as exc:
            msg = (_('Exception: %s') % six.text_type(exc))
            raise exception.VolumeDriverException(message=msg)

        if 200 <= response.status_code < 300:
            if method in ('GET', 'POST'):
                return response.json()
            else:
                return ''

        self.handle_errors(response, key, object_type)

    def handle_errors(self, response, key, object_type):
        if response.status_code == 400:
            error = response.json()
            err_msg = error.get('message')
            if err_msg.endswith(OBJ_NOT_FOUND_ERR):
                LOG.warning(_LW("object %(key)s of "
                                "type %(typ)s not found, %(err_msg)s"),
                            {'key': key, 'typ': object_type,
                             'err_msg': err_msg, })
                raise exception.NotFound()
            elif err_msg == VOL_NOT_UNIQUE_ERR:
                LOG.error(_LE("can't create 2 volumes with the same name, %s"),
                          err_msg)
                msg = (_('Volume by this name already exists'))
                raise exception.VolumeBackendAPIException(data=msg)
            elif err_msg == VOL_OBJ_NOT_FOUND_ERR:
                LOG.error(_LE("Can't find volume to map %(key)s, %(msg)s"),
                          {'key': key, 'msg': err_msg, })
                raise exception.VolumeNotFound(volume_id=key)
            elif ALREADY_MAPPED_ERR in err_msg:
                raise exception.XtremIOAlreadyMappedError()
            elif err_msg == SYSTEM_BUSY:
                raise exception.XtremIOArrayBusy()
            elif err_msg in (TOO_MANY_OBJECTS, TOO_MANY_SNAPSHOTS_PER_VOL):
                raise exception.XtremIOSnapshotsLimitExceeded()
        msg = _('Bad response from XMS, %s') % response.text
        LOG.error(msg)
        raise exception.VolumeBackendAPIException(message=msg)

    def update_url(self, data, cluster_id):
        return

    def update_data(self, data, cluster_id):
        return

    def get_cluster(self):
        return self.req('clusters', idx=1)['content']

    def create_snapshot(self, src, dest, ro=False):
        """Create a snapshot of a volume on the array.

        XtreamIO array snapshots are also volumes.

        :src: name of the source volume to be cloned
        :dest: name for the new snapshot
        :ro: new snapshot type ro/regular. only applicable to Client4
        """
        raise NotImplementedError()

    def get_extra_capabilities(self):
        return {}

    def get_initiator(self, port_address):
        raise NotImplementedError()

    def add_vol_to_cg(self, vol_id, cg_id):
        pass


class XtremIOClient3(XtremIOClient):
    def __init__(self, configuration, cluster_id):
        super(XtremIOClient3, self).__init__(configuration, cluster_id)
        self._portals = []

    def find_lunmap(self, ig_name, vol_name):
        try:
            lun_mappings = self.req('lun-maps')['lun-maps']
        except exception.NotFound:
            raise (exception.VolumeDriverException
                   (_("can't find lun-map, ig:%(ig)s vol:%(vol)s") %
                    {'ig': ig_name, 'vol': vol_name}))

        for lm_link in lun_mappings:
            idx = lm_link['href'].split('/')[-1]
            # NOTE(geguileo): There can be races so mapped elements retrieved
            # in the listing may no longer exist.
            try:
                lm = self.req('lun-maps', idx=int(idx))['content']
            except exception.NotFound:
                continue
            if lm['ig-name'] == ig_name and lm['vol-name'] == vol_name:
                return lm

        return None

    def num_of_mapped_volumes(self, initiator):
        cnt = 0
        for lm_link in self.req('lun-maps')['lun-maps']:
            idx = lm_link['href'].split('/')[-1]
            # NOTE(geguileo): There can be races so mapped elements retrieved
            # in the listing may no longer exist.
            try:
                lm = self.req('lun-maps', idx=int(idx))['content']
            except exception.NotFound:
                continue
            if lm['ig-name'] == initiator:
                cnt += 1
        return cnt

    def get_iscsi_portals(self):
        if self._portals:
            return self._portals

        iscsi_portals = [t['name'] for t in self.req('iscsi-portals')
                         ['iscsi-portals']]
        for portal_name in iscsi_portals:
            try:
                self._portals.append(self.req('iscsi-portals',
                                              name=portal_name)['content'])
            except exception.NotFound:
                raise (exception.VolumeBackendAPIException
                       (data=_("iscsi portal, %s, not found") % portal_name))

        return self._portals

    def create_snapshot(self, src, dest, ro=False):
        data = {'snap-vol-name': dest, 'ancestor-vol-id': src}

        self.req('snapshots', 'POST', data)

    def get_initiator(self, port_address):
        try:
            return self.req('initiators', 'GET', name=port_address)['content']
        except exception.NotFound:
            pass


class XtremIOClient4(XtremIOClient):
    def __init__(self, configuration, cluster_id):
        super(XtremIOClient4, self).__init__(configuration, cluster_id)
        self._cluster_name = None

    def req(self, object_type='volumes', method='GET', data=None,
            name=None, idx=None, ver='v2'):
        return super(XtremIOClient4, self).req(object_type, method, data,
                                               name, idx, ver)

    def get_extra_capabilities(self):
        return {'consistencygroup_support': True}

    def find_lunmap(self, ig_name, vol_name):
        try:
            return (self.req('lun-maps',
                             data={'full': 1,
                                   'filter': ['vol-name:eq:%s' % vol_name,
                                              'ig-name:eq:%s' % ig_name]})
                    ['lun-maps'][0])
        except (KeyError, IndexError):
            raise exception.VolumeNotFound(volume_id=vol_name)

    def num_of_mapped_volumes(self, initiator):
        return len(self.req('lun-maps',
                            data={'filter': 'ig-name:eq:%s' % initiator})
                   ['lun-maps'])

    def update_url(self, data, cluster_id):
        if cluster_id:
            data['cluster-name'] = cluster_id

    def update_data(self, data, cluster_id):
        if cluster_id:
            data['cluster-id'] = cluster_id

    def get_iscsi_portals(self):
        return self.req('iscsi-portals',
                        data={'full': 1})['iscsi-portals']

    def get_cluster(self):
        if not self.cluster_id:
            self.cluster_id = self.req('clusters')['clusters'][0]['name']

        return self.req('clusters', name=self.cluster_id)['content']

    def create_snapshot(self, src, dest, ro=False):
        data = {'snapshot-set-name': dest, 'snap-suffix': dest,
                'volume-list': [src],
                'snapshot-type': 'readonly' if ro else 'regular'}

        res = self.req('snapshots', 'POST', data, ver='v2')
        typ, idx = res['links'][0]['href'].split('/')[-2:]

        # rename the snapshot
        data = {'name': dest}
        try:
            self.req(typ, 'PUT', data, idx=int(idx))
        except exception.VolumeBackendAPIException:
            # reverting
            msg = _LE('Failed to rename the created snapshot, reverting.')
            LOG.error(msg)
            self.req(typ, 'DELETE', idx=int(idx))
            raise

    def add_vol_to_cg(self, vol_id, cg_id):
        add_data = {'vol-id': vol_id, 'cg-id': cg_id}
        self.req('consistency-group-volumes', 'POST', add_data, ver='v2')

    def get_initiator(self, port_address):
        inits = self.req('initiators',
                         data={'filter': 'port-address:eq:' + port_address,
                               'full': 1})['initiators']
        if len(inits) == 1:
            return inits[0]
        else:
            pass


class XtremIOVolumeDriver(san.SanDriver):
    """Executes commands relating to Volumes."""

    VERSION = '1.0.8'

    # ThirdPartySystems wiki
    CI_WIKI_NAME = "EMC_XIO_CI"

    driver_name = 'XtremIO'
    MIN_XMS_VERSION = [3, 0, 0]

    def __init__(self, *args, **kwargs):
        super(XtremIOVolumeDriver, self).__init__(*args, **kwargs)
        self.configuration.append_config_values(XTREMIO_OPTS)
        self.protocol = None
        self.backend_name = (self.configuration.safe_get('volume_backend_name')
                             or self.driver_name)
        self.cluster_id = (self.configuration.safe_get('xtremio_cluster_name')
                           or '')
        self.provisioning_factor = (self.configuration.
                                    safe_get('max_over_subscription_ratio')
                                    or DEFAULT_PROVISIONING_FACTOR)
        self._stats = {}
        self.client = XtremIOClient3(self.configuration, self.cluster_id)

    def _obj_from_result(self, res):
        typ, idx = res['links'][0]['href'].split('/')[-2:]
        return self.client.req(typ, idx=int(idx))['content']

    def check_for_setup_error(self):
        try:
            name = self.client.req('clusters')['clusters'][0]['name']
            cluster = self.client.req('clusters', name=name)['content']
            version_text = cluster['sys-sw-version']
        except exception.NotFound:
            msg = _("XtremIO not initialized correctly, no clusters found")
            raise (exception.VolumeBackendAPIException
                   (data=msg))
        ver = [int(n) for n in version_text.split('-')[0].split('.')]
        if ver < self.MIN_XMS_VERSION:
            msg = (_('Invalid XtremIO version %(cur)s,'
                     ' version %(min)s or up is required') %
                   {'min': self.MIN_XMS_VERSION,
                    'cur': ver})
            LOG.error(msg)
            raise exception.VolumeBackendAPIException(data=msg)
        else:
            LOG.info(_LI('XtremIO SW version %s'), version_text)
        if ver[0] >= 4:
            self.client = XtremIOClient4(self.configuration, self.cluster_id)

    def create_volume(self, volume):
        "Creates a volume"
        data = {'vol-name': volume['id'],
                'vol-size': str(volume['size']) + 'g'
                }
        self.client.req('volumes', 'POST', data)

        if volume.get('consistencygroup_id'):
            self.client.add_vol_to_cg(volume['id'],
                                      volume['consistencygroup_id'])

    def create_volume_from_snapshot(self, volume, snapshot):
        """Creates a volume from a snapshot."""
        if snapshot.get('cgsnapshot_id'):
            # get array snapshot id from CG snapshot
            snap_by_anc = self._get_snapset_ancestors(snapshot.cgsnapshot)
            snapshot_id = snap_by_anc[snapshot['volume_id']]
        else:
            snapshot_id = snapshot['id']

        self.client.create_snapshot(snapshot_id, volume['id'])

        # add new volume to consistency group
        if (volume.get('consistencygroup_id') and
                self.client is XtremIOClient4):
            self.client.add_vol_to_cg(volume['id'],
                                      snapshot['consistencygroup_id'])

    def create_cloned_volume(self, volume, src_vref):
        """Creates a clone of the specified volume."""
        vol = self.client.req('volumes', name=src_vref['id'])['content']
        ctxt = context.get_admin_context()
        cache = self.db.image_volume_cache_get_by_volume_id(ctxt,
                                                            src_vref['id'])
        limit = self.configuration.safe_get('xtremio_volumes_per_glance_cache')
        if cache and limit and limit > 0 and limit <= vol['num-of-dest-snaps']:
            raise exception.CinderException('Exceeded the configured limit of '
                                            '%d snapshots per volume' % limit)
        try:
            self.client.create_snapshot(src_vref['id'], volume['id'])
        except exception.XtremIOSnapshotsLimitExceeded as e:
            raise exception.CinderException(e.message)

        if volume.get('consistencygroup_id') and self.client is XtremIOClient4:
            self.client.add_vol_to_cg(volume['id'],
                                      volume['consistencygroup_id'])

    def delete_volume(self, volume):
        """Deletes a volume."""
        try:
            self.client.req('volumes', 'DELETE', name=volume.name_id)
        except exception.NotFound:
            LOG.info(_LI("volume %s doesn't exist"), volume.name_id)

    def create_snapshot(self, snapshot):
        """Creates a snapshot."""
        self.client.create_snapshot(snapshot.volume_id, snapshot.id, True)

    def delete_snapshot(self, snapshot):
        """Deletes a snapshot."""
        try:
            self.client.req('volumes', 'DELETE', name=snapshot.id)
        except exception.NotFound:
            LOG.info(_LI("snapshot %s doesn't exist"), snapshot.id)

    def update_migrated_volume(self, ctxt, volume, new_volume,
                               original_volume_status):
        # as the volume name is used to id the volume we need to rename it
        name_id = None
        provider_location = None
        current_name = new_volume['id']
        original_name = volume['id']
        try:
            data = {'name': original_name}
            self.client.req('volumes', 'PUT', data, name=current_name)
        except exception.VolumeBackendAPIException:
            LOG.error(_LE('Unable to rename the logical volume '
                          'for volume: %s'), original_name)
            # If the rename fails, _name_id should be set to the new
            # volume id and provider_location should be set to the
            # one from the new volume as well.
            name_id = new_volume['_name_id'] or new_volume['id']
            provider_location = new_volume['provider_location']

        return {'_name_id': name_id, 'provider_location': provider_location}

    def _update_volume_stats(self):
        sys = self.client.get_cluster()
        physical_space = int(sys["ud-ssd-space"]) / units.Mi
        used_physical_space = int(sys["ud-ssd-space-in-use"]) / units.Mi
        free_physical = physical_space - used_physical_space
        actual_prov = int(sys["vol-size"]) / units.Mi
        self._stats = {'volume_backend_name': self.backend_name,
                       'vendor_name': 'Dell EMC',
                       'driver_version': self.VERSION,
                       'storage_protocol': self.protocol,
                       'total_capacity_gb': physical_space,
                       'free_capacity_gb': (free_physical *
                                            self.provisioning_factor),
                       'provisioned_capacity_gb': actual_prov,
                       'max_over_subscription_ratio': self.provisioning_factor,
                       'thin_provisioning_support': True,
                       'thick_provisioning_support': False,
                       'reserved_percentage':
                       self.configuration.reserved_percentage,
                       'QoS_support': False,
                       'multiattach': True,
                       }
        self._stats.update(self.client.get_extra_capabilities())

    def get_volume_stats(self, refresh=False):
        """Get volume stats.

        If 'refresh' is True, run update the stats first.
        """
        if refresh:
            self._update_volume_stats()
        return self._stats

    def manage_existing(self, volume, existing_ref, is_snapshot=False):
        """Manages an existing LV."""
        lv_name = existing_ref['source-name']
        # Attempt to locate the volume.
        try:
            vol_obj = self.client.req('volumes', name=lv_name)['content']
            if (
                is_snapshot and
                (not vol_obj['ancestor-vol-id'] or
                 vol_obj['ancestor-vol-id'][XTREMIO_OID_NAME] !=
                 volume.volume_id)):
                kwargs = {'existing_ref': lv_name,
                          'reason': 'Not a snapshot of vol %s' %
                          volume.volume_id}
                raise exception.ManageExistingInvalidReference(**kwargs)
        except exception.NotFound:
            kwargs = {'existing_ref': lv_name,
                      'reason': 'Specified logical %s does not exist.' %
                      'snapshot' if is_snapshot else 'volume'}
            raise exception.ManageExistingInvalidReference(**kwargs)

        # Attempt to rename the LV to match the OpenStack internal name.
        self.client.req('volumes', 'PUT', data={'vol-name': volume['id']},
                        idx=vol_obj['index'])

    def manage_existing_get_size(self, volume, existing_ref,
                                 is_snapshot=False):
        """Return size of an existing LV for manage_existing."""
        # Check that the reference is valid
        if 'source-name' not in existing_ref:
            reason = _('Reference must contain source-name element.')
            raise exception.ManageExistingInvalidReference(
                existing_ref=existing_ref, reason=reason)
        lv_name = existing_ref['source-name']
        # Attempt to locate the volume.
        try:
            vol_obj = self.client.req('volumes', name=lv_name)['content']
        except exception.NotFound:
            kwargs = {'existing_ref': lv_name,
                      'reason': 'Specified logical %s does not exist.' %
                      'snapshot' if is_snapshot else 'volume'}
            raise exception.ManageExistingInvalidReference(**kwargs)
        # LV size is returned in gigabytes.  Attempt to parse size as a float
        # and round up to the next integer.
        lv_size = int(math.ceil(float(vol_obj['vol-size']) / units.Mi))

        return lv_size

    def unmanage(self, volume, is_snapshot=False):
        """Removes the specified volume from Cinder management."""
        # trying to rename the volume to [cinder name]-unmanged
        try:
            self.client.req('volumes', 'PUT', name=volume['id'],
                            data={'vol-name': volume['name'] + '-unmanged'})
        except exception.NotFound:
            LOG.info(_LI("%(typ)s with the name %(name)s wasn't found, "
                         "can't unmanage") %
                     {'typ': 'Snapshot' if is_snapshot else 'Volume',
                      'name': volume['id']})
            raise exception.VolumeNotFound(volume_id=volume['id'])

    def manage_existing_snapshot(self, snapshot, existing_ref):
        self.manage_existing(snapshot, existing_ref, True)

    def manage_existing_snapshot_get_size(self, snapshot, existing_ref):
        return self.manage_existing_get_size(snapshot, existing_ref, True)

    def unmanage_snapshot(self, snapshot):
        self.unmanage(snapshot, True)

    def extend_volume(self, volume, new_size):
        """Extend an existing volume's size."""
        data = {'vol-size': six.text_type(new_size) + 'g'}
        try:
            self.client.req('volumes', 'PUT', data, name=volume['id'])
        except exception.NotFound:
            msg = _("can't find the volume to extend")
            raise exception.VolumeDriverException(message=msg)

    def check_for_export(self, context, volume_id):
        """Make sure volume is exported."""
        pass

    def terminate_connection(self, volume, connector, **kwargs):
        """Disallow connection from connector"""
        tg = self.client.req('target-groups', name='Default')['content']
        vol = self.client.req('volumes', name=volume['id'])['content']

        for ig_idx in self._get_ig_indexes_from_initiators(connector):
            lm_name = '%s_%s_%s' % (six.text_type(vol['index']),
                                    six.text_type(ig_idx),
                                    six.text_type(tg['index']))
            LOG.debug('Removing lun map %s.', lm_name)
            try:
                self.client.req('lun-maps', 'DELETE', name=lm_name)
            except exception.NotFound:
                LOG.warning(_LW("terminate_connection: lun map not found"))

    def _get_password(self):
        return ''.join(RANDOM.choice
                       (string.ascii_uppercase + string.digits)
                       for _ in range(12))

    def create_lun_map(self, volume, ig, lun_num=None):
        try:
            data = {'ig-id': ig, 'vol-id': volume['id']}
            if lun_num:
                data['lun'] = lun_num
            res = self.client.req('lun-maps', 'POST', data)

            lunmap = self._obj_from_result(res)
            LOG.info(_LI('Created lun-map:\n%s'), lunmap)
        except exception.XtremIOAlreadyMappedError:
            LOG.info(_LI('Volume already mapped, retrieving %(ig)s, %(vol)s'),
                     {'ig': ig, 'vol': volume['id']})
            lunmap = self.client.find_lunmap(ig, volume['id'])
        return lunmap

    def _get_ig_name(self, connector):
        raise NotImplementedError()

    def _get_ig_indexes_from_initiators(self, connector):
        initiator_names = self._get_initiator_names(connector)
        ig_indexes = set()

        for initiator_name in initiator_names:
            initiator = self.client.get_initiator(initiator_name)

            ig_indexes.add(initiator['ig-id'][XTREMIO_OID_INDEX])

        return list(ig_indexes)

    def _get_initiator_names(self, connector):
        raise NotImplementedError()

    def create_consistencygroup(self, context, group):
        """Creates a consistency group.

        :param context: the context
        :param group: the group object to be created
        :returns: dict -- modelUpdate = {'status': 'available'}
        :raises: VolumeBackendAPIException
        """
        create_data = {'consistency-group-name': group['id']}
        self.client.req('consistency-groups', 'POST', data=create_data,
                        ver='v2')
        return {'status': fields.ConsistencyGroupStatus.AVAILABLE}

    def delete_consistencygroup(self, context, group, volumes):
        """Deletes a consistency group."""
        self.client.req('consistency-groups', 'DELETE', name=group['id'],
                        ver='v2')

        volumes = self.db.volume_get_all_by_group(context, group['id'])

        for volume in volumes:
            self.delete_volume(volume)
            volume.status = 'deleted'

        model_update = {'status': group['status']}

        return model_update, volumes

    def _get_snapset_ancestors(self, snapset_name):
        snapset = self.client.req('snapshot-sets',
                                  name=snapset_name)['content']
        volume_ids = [s[XTREMIO_OID_INDEX] for s in snapset['vol-list']]
        return {v['ancestor-vol-id'][XTREMIO_OID_NAME]: v['name'] for v
                in self.client.req('volumes',
                                   data={'full': 1,
                                         'props':
                                         'ancestor-vol-id'})['volumes']
                if v['index'] in volume_ids}

    def create_consistencygroup_from_src(self, context, group, volumes,
                                         cgsnapshot=None, snapshots=None,
                                         source_cg=None, source_vols=None):
        """Creates a consistencygroup from source.

        :param context: the context of the caller.
        :param group: the dictionary of the consistency group to be created.
        :param volumes: a list of volume dictionaries in the group.
        :param cgsnapshot: the dictionary of the cgsnapshot as source.
        :param snapshots: a list of snapshot dictionaries in the cgsnapshot.
        :param source_cg: the dictionary of a consistency group as source.
        :param source_vols: a list of volume dictionaries in the source_cg.
        :returns: model_update, volumes_model_update
        """
        if not (cgsnapshot and snapshots and not source_cg or
                source_cg and source_vols and not cgsnapshot):
            msg = _("create_consistencygroup_from_src only supports a "
                    "cgsnapshot source or a consistency group source. "
                    "Multiple sources cannot be used.")
            raise exception.InvalidInput(msg)

        if cgsnapshot:
            snap_name = self._get_cgsnap_name(cgsnapshot)
            snap_by_anc = self._get_snapset_ancestors(snap_name)
            for volume, snapshot in zip(volumes, snapshots):
                real_snap = snap_by_anc[snapshot['volume_id']]
                self.create_volume_from_snapshot(volume, {'id': real_snap})

        elif source_cg:
            data = {'consistency-group-id': source_cg['id'],
                    'snapshot-set-name': group['id']}
            self.client.req('snapshots', 'POST', data, ver='v2')
            snap_by_anc = self._get_snapset_ancestors(group['id'])
            for volume, src_vol in zip(volumes, source_vols):
                snap_vol_name = snap_by_anc[src_vol['id']]
                self.client.req('volumes', 'PUT', {'name': volume['id']},
                                name=snap_vol_name)

        create_data = {'consistency-group-name': group['id'],
                       'vol-list': [v['id'] for v in volumes]}
        self.client.req('consistency-groups', 'POST', data=create_data,
                        ver='v2')

        return None, None

    def update_consistencygroup(self, context, group,
                                add_volumes=None, remove_volumes=None):
        """Updates a consistency group.

        :param context: the context of the caller.
        :param group: the dictionary of the consistency group to be updated.
        :param add_volumes: a list of volume dictionaries to be added.
        :param remove_volumes: a list of volume dictionaries to be removed.
        :returns: model_update, add_volumes_update, remove_volumes_update
        """
        add_volumes = add_volumes if add_volumes else []
        remove_volumes = remove_volumes if remove_volumes else []
        for vol in add_volumes:
            add_data = {'vol-id': vol['id'], 'cg-id': group['id']}
            self.client.req('consistency-group-volumes', 'POST', add_data,
                            ver='v2')
        for vol in remove_volumes:
            remove_data = {'vol-id': vol['id'], 'cg-id': group['id']}
            self.client.req('consistency-group-volumes', 'DELETE', remove_data,
                            name=group['id'], ver='v2')
        return None, None, None

    def _get_cgsnap_name(self, cgsnapshot):
        return '%(cg)s%(snap)s' % {'cg': cgsnapshot['consistencygroup_id']
                                   .replace('-', ''),
                                   'snap': cgsnapshot['id'].replace('-', '')}

    def create_cgsnapshot(self, context, cgsnapshot, snapshots):
        """Creates a cgsnapshot."""
        data = {'consistency-group-id': cgsnapshot['consistencygroup_id'],
                'snapshot-set-name': self._get_cgsnap_name(cgsnapshot)}
        self.client.req('snapshots', 'POST', data, ver='v2')

        return None, None

    def delete_cgsnapshot(self, context, cgsnapshot, snapshots):
        """Deletes a cgsnapshot."""
        self.client.req('snapshot-sets', 'DELETE',
                        name=self._get_cgsnap_name(cgsnapshot), ver='v2')

        return None, None

    def _get_ig(self, name):
        try:
            return self.client.req('initiator-groups', 'GET',
                                   name=name)['content']
        except exception.NotFound:
            pass

    def _create_ig(self, name):
        # create an initiator group to hold the initiator
        data = {'ig-name': name}
        self.client.req('initiator-groups', 'POST', data)
        try:
            return self.client.req('initiator-groups', name=name)['content']
        except exception.NotFound:
            raise (exception.VolumeBackendAPIException
                   (data=_("Failed to create IG, %s") % name))


@interface.volumedriver
class XtremIOISCSIDriver(XtremIOVolumeDriver, driver.ISCSIDriver):
    """Executes commands relating to ISCSI volumes.

    We make use of model provider properties as follows:

    ``provider_location``
      if present, contains the iSCSI target information in the same
      format as an ietadm discovery
      i.e. '<ip>:<port>,<portal> <target IQN>'

    ``provider_auth``
      if present, contains a space-separated triple:
      '<auth method> <auth username> <auth password>'.
      `CHAP` is the only auth_method in use at the moment.
    """
    driver_name = 'XtremIO_ISCSI'

    def __init__(self, *args, **kwargs):
        super(XtremIOISCSIDriver, self).__init__(*args, **kwargs)
        self.protocol = 'iSCSI'

    def _add_auth(self, data, login_chap, discovery_chap):
        login_passwd, discovery_passwd = None, None
        if login_chap:
            data['initiator-authentication-user-name'] = 'chap_user'
            login_passwd = self._get_password()
            data['initiator-authentication-password'] = login_passwd
        if discovery_chap:
            data['initiator-discovery-user-name'] = 'chap_user'
            discovery_passwd = self._get_password()
            data['initiator-discovery-password'] = discovery_passwd
        return login_passwd, discovery_passwd

    def _create_initiator(self, connector, login_chap, discovery_chap):
        initiator = self._get_initiator_names(connector)[0]
        # create an initiator
        data = {'initiator-name': initiator,
                'ig-id': initiator,
                'port-address': initiator}
        l, d = self._add_auth(data, login_chap, discovery_chap)
        self.client.req('initiators', 'POST', data)
        return l, d

    def initialize_connection(self, volume, connector):
        try:
            sys = self.client.get_cluster()
        except exception.NotFound:
            msg = _("XtremIO not initialized correctly, no clusters found")
            raise exception.VolumeBackendAPIException(data=msg)
        login_chap = (sys.get('chap-authentication-mode', 'disabled') !=
                      'disabled')
        discovery_chap = (sys.get('chap-discovery-mode', 'disabled') !=
                          'disabled')
        initiator_name = self._get_initiator_names(connector)[0]
        initiator = self.client.get_initiator(initiator_name)
        if initiator:
            login_passwd = initiator['chap-authentication-initiator-password']
            discovery_passwd = initiator['chap-discovery-initiator-password']
            ig = self._get_ig(initiator['ig-id'][XTREMIO_OID_NAME])
        else:
            ig = self._get_ig(self._get_ig_name(connector))
            if not ig:
                ig = self._create_ig(self._get_ig_name(connector))
            (login_passwd,
             discovery_passwd) = self._create_initiator(connector,
                                                        login_chap,
                                                        discovery_chap)
        # if CHAP was enabled after the initiator was created
        if login_chap and not login_passwd:
            LOG.info(_LI('initiator has no password while using chap,'
                         'adding it'))
            data = {}
            (login_passwd,
             d_passwd) = self._add_auth(data, login_chap, discovery_chap and
                                        not discovery_passwd)
            discovery_passwd = (discovery_passwd if discovery_passwd
                                else d_passwd)
            self.client.req('initiators', 'PUT', data, idx=initiator['index'])

        # lun mappping
        lunmap = self.create_lun_map(volume, ig['ig-id'][XTREMIO_OID_NAME])

        properties = self._get_iscsi_properties(lunmap)

        if login_chap:
            properties['auth_method'] = 'CHAP'
            properties['auth_username'] = 'chap_user'
            properties['auth_password'] = login_passwd
        if discovery_chap:
            properties['discovery_auth_method'] = 'CHAP'
            properties['discovery_auth_username'] = 'chap_user'
            properties['discovery_auth_password'] = discovery_passwd
        LOG.debug('init conn params:\n%s', properties)
        return {
            'driver_volume_type': 'iscsi',
            'data': properties
        }

    def _get_iscsi_properties(self, lunmap):
        """Gets iscsi configuration.

        :target_discovered:    boolean indicating whether discovery was used
        :target_iqn:    the IQN of the iSCSI target
        :target_portal:    the portal of the iSCSI target
        :target_lun:    the lun of the iSCSI target
        :volume_id:    the id of the volume (currently used by xen)
        :auth_method:, :auth_username:, :auth_password:
            the authentication details. Right now, either auth_method is not
            present meaning no authentication, or auth_method == `CHAP`
            meaning use CHAP with the specified credentials.
        multiple connection return
        :target_iqns, :target_portals, :target_luns, which contain lists of
        multiple values. The main portal information is also returned in
        :target_iqn, :target_portal, :target_lun for backward compatibility.
        """
        portals = self.client.get_iscsi_portals()
        if not portals:
            msg = _("XtremIO not configured correctly, no iscsi portals found")
            LOG.error(msg)
            raise exception.VolumeDriverException(message=msg)
        portal = RANDOM.choice(portals)
        portal_addr = ('%(ip)s:%(port)d' %
                       {'ip': portal['ip-addr'].split('/')[0],
                        'port': portal['ip-port']})

        tg_portals = ['%(ip)s:%(port)d' % {'ip': p['ip-addr'].split('/')[0],
                                           'port': p['ip-port']}
                      for p in portals]
        properties = {'target_discovered': False,
                      'target_iqn': portal['port-address'],
                      'target_lun': lunmap['lun'],
                      'target_portal': portal_addr,
                      'target_iqns': [p['port-address'] for p in portals],
                      'target_portals': tg_portals,
                      'target_luns': [lunmap['lun']] * len(portals)}
        return properties

    def _get_initiator_names(self, connector):
        return [connector['initiator']]

    def _get_ig_name(self, connector):
        return connector['initiator']


@interface.volumedriver
class XtremIOFCDriver(XtremIOVolumeDriver,
                      driver.FibreChannelDriver):

    def __init__(self, *args, **kwargs):
        super(XtremIOFCDriver, self).__init__(*args, **kwargs)
        self.protocol = 'FC'
        self._targets = None

    def get_targets(self):
        if not self._targets:
            try:
                target_list = self.client.req('targets')["targets"]
                targets = [self.client.req('targets',
                                           name=target['name'])['content']
                           for target in target_list
                           if '-fc' in target['name']]
                self._targets = [target['port-address'].replace(':', '')
                                 for target in targets
                                 if target['port-state'] == 'up']
            except exception.NotFound:
                raise (exception.VolumeBackendAPIException
                       (data=_("Failed to get targets")))
        return self._targets

    def _get_free_lun(self, igs):
        luns = []
        for ig in igs:
            luns.extend(lm['lun'] for lm in
                        self.client.req('lun-maps',
                                        data={'full': 1, 'prop': 'lun',
                                              'filter': 'ig-name:eq:%s' % ig})
                        ['lun-maps'])
        uniq_luns = set(luns + [0])
        seq = range(len(uniq_luns) + 1)
        return min(set(seq) - uniq_luns)

    @fczm_utils.AddFCZone
    def initialize_connection(self, volume, connector):
        wwpns = self._get_initiator_names(connector)
        ig_name = self._get_ig_name(connector)
        i_t_map = {}
        found = []
        new = []
        for wwpn in wwpns:
            init = self.client.get_initiator(wwpn)
            if init:
                found.append(init)
            else:
                new.append(wwpn)
            i_t_map[wwpn.replace(':', '')] = self.get_targets()
        # get or create initiator group
        if new:
            ig = self._get_ig(ig_name)
            if not ig:
                ig = self._create_ig(ig_name)
            for wwpn in new:
                data = {'initiator-name': wwpn, 'ig-id': ig_name,
                        'port-address': wwpn}
                self.client.req('initiators', 'POST', data)
        igs = list(set([i['ig-id'][XTREMIO_OID_NAME] for i in found]))
        if new and ig['ig-id'][XTREMIO_OID_NAME] not in igs:
            igs.append(ig['ig-id'][XTREMIO_OID_NAME])

        if len(igs) > 1:
            lun_num = self._get_free_lun(igs)
        else:
            lun_num = None
        for ig in igs:
            lunmap = self.create_lun_map(volume, ig, lun_num)
            lun_num = lunmap['lun']
        return {'driver_volume_type': 'fibre_channel',
                'data': {
                    'target_discovered': False,
                    'target_lun': lun_num,
                    'target_wwn': self.get_targets(),
                    'initiator_target_map': i_t_map}}

    @fczm_utils.RemoveFCZone
    def terminate_connection(self, volume, connector, **kwargs):
        (super(XtremIOFCDriver, self)
         .terminate_connection(volume, connector, **kwargs))
        num_vols = (self.client
                    .num_of_mapped_volumes(self._get_ig_name(connector)))
        if num_vols > 0:
            data = {}
        else:
            i_t_map = {}
            for initiator in self._get_initiator_names(connector):
                i_t_map[initiator.replace(':', '')] = self.get_targets()
            data = {'target_wwn': self.get_targets(),
                    'initiator_target_map': i_t_map}

        return {'driver_volume_type': 'fibre_channel',
                'data': data}

    def _get_initiator_names(self, connector):
        return [wwpn if ':' in wwpn else
                ':'.join(wwpn[i:i + 2] for i in range(0, len(wwpn), 2))
                for wwpn in connector['wwpns']]

    def _get_ig_name(self, connector):
        return connector['host']