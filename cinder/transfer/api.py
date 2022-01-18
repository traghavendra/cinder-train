# Copyright (C) 2013 Hewlett-Packard Development Company, L.P.
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
Handles all requests relating to transferring ownership of volumes.
"""


import hashlib
import hmac
import os

from oslo_config import cfg
from oslo_log import log as logging
from oslo_utils import excutils
from oslo_utils import strutils
import six

from cinder.db import base
from cinder import exception
from cinder.i18n import _
from cinder import objects
from cinder.policies import volume_transfer as policy
from cinder import quota
from cinder import quota_utils
from cinder.volume import api as volume_api
from cinder.volume import volume_utils


volume_transfer_opts = [
    cfg.IntOpt('volume_transfer_salt_length', default=8,
               help='The number of characters in the salt.'),
    cfg.IntOpt('volume_transfer_key_length', default=16,
               help='The number of characters in the '
               'autogenerated auth key.'), ]

CONF = cfg.CONF
CONF.register_opts(volume_transfer_opts)

LOG = logging.getLogger(__name__)
QUOTAS = quota.QUOTAS


class API(base.Base):
    """API for interacting volume transfers."""

    def __init__(self, db_driver=None):
        self.volume_api = volume_api.API()
        super(API, self).__init__(db_driver)

    def get(self, context, transfer_id):
        context.authorize(policy.GET_POLICY)
        rv = self.db.transfer_get(context, transfer_id)
        return dict(rv)

    def delete(self, context, transfer_id):
        """Make the RPC call to delete a volume transfer."""
        transfer = self.db.transfer_get(context, transfer_id)

        volume_ref = objects.Volume.get_by_id(context,
                                              transfer.volume_id)
        context.authorize(policy.DELETE_POLICY, target_obj=volume_ref)
        volume_utils.notify_about_volume_usage(context, volume_ref,
                                               "transfer.delete.start")
        if volume_ref['status'] != 'awaiting-transfer':
            LOG.error("Volume in unexpected state")
        self.db.transfer_destroy(context, transfer_id)
        volume_utils.notify_about_volume_usage(context, volume_ref,
                                               "transfer.delete.end")

    def get_all(self, context, marker=None,
                limit=None, sort_keys=None,
                sort_dirs=None, filters=None, offset=None):
        filters = filters or {}
        context.authorize(policy.GET_ALL_POLICY)
        all_tenants = strutils.bool_from_string(filters.pop('all_tenants',
                                                            'false'))
        if context.is_admin and all_tenants:
            transfers = self.db.transfer_get_all(context, marker=marker,
                                                 limit=limit,
                                                 sort_keys=sort_keys,
                                                 sort_dirs=sort_dirs,
                                                 filters=filters,
                                                 offset=offset)
        else:
            transfers = self.db.transfer_get_all_by_project(
                context, context.project_id, marker=marker,
                limit=limit, sort_keys=sort_keys, sort_dirs=sort_dirs,
                filters=filters, offset=offset)
        return transfers

    def _get_random_string(self, length):
        """Get a random hex string of the specified length."""
        rndstr = ""

        # Note that the string returned by this function must contain only
        # characters that the recipient can enter on their keyboard. The
        # function ssh224().hexdigit() achieves this by generating a hash
        # which will only contain hexadecimal digits.
        while len(rndstr) < length:
            rndstr += hashlib.sha224(os.urandom(255)).hexdigest()

        return rndstr[0:length]

    def _get_crypt_hash(self, salt, auth_key):
        """Generate a random hash based on the salt and the auth key."""
        if not isinstance(salt, (six.binary_type, six.text_type)):
            salt = str(salt)
        if isinstance(salt, six.text_type):
            salt = salt.encode('utf-8')
        if not isinstance(auth_key, (six.binary_type, six.text_type)):
            auth_key = str(auth_key)
        if isinstance(auth_key, six.text_type):
            auth_key = auth_key.encode('utf-8')
        return hmac.new(salt, auth_key, hashlib.sha1).hexdigest()

    def create(self, context, volume_id, display_name, no_snapshots=False):
        """Creates an entry in the transfers table."""
        LOG.info("Generating transfer record for volume %s", volume_id)
        volume_ref = objects.Volume.get_by_id(context, volume_id)
        context.authorize(policy.CREATE_POLICY, target_obj=volume_ref)
        if volume_ref['status'] != "available":
            raise exception.InvalidVolume(reason=_("status must be available"))
        if volume_ref['encryption_key_id'] is not None:
            raise exception.InvalidVolume(
                reason=_("transferring encrypted volume is not supported"))

        if not no_snapshots:
            snapshots = self.db.snapshot_get_all_for_volume(context, volume_id)
            for snapshot in snapshots:
                if snapshot['status'] != "available":
                    msg = _("snapshot: %s status must be "
                            "available") % snapshot['id']
                    raise exception.InvalidSnapshot(reason=msg)
                if snapshot.get('encryption_key_id'):
                    msg = _("snapshot: %s encrypted snapshots cannot be "
                            "transferred") % snapshot['id']
                    raise exception.InvalidSnapshot(reason=msg)

        volume_utils.notify_about_volume_usage(context, volume_ref,
                                               "transfer.create.start")
        # The salt is just a short random string.
        salt = self._get_random_string(CONF.volume_transfer_salt_length)
        auth_key = self._get_random_string(CONF.volume_transfer_key_length)
        crypt_hash = self._get_crypt_hash(salt, auth_key)

        # TODO(ollie): Transfer expiry needs to be implemented.
        transfer_rec = {'volume_id': volume_id,
                        'display_name': display_name,
                        'salt': salt,
                        'crypt_hash': crypt_hash,
                        'expires_at': None,
                        'no_snapshots': no_snapshots,
                        'source_project_id': volume_ref['project_id']}

        try:
            transfer = self.db.transfer_create(context, transfer_rec)
        except Exception:
            LOG.error("Failed to create transfer record for %s", volume_id)
            raise
        volume_utils.notify_about_volume_usage(context, volume_ref,
                                               "transfer.create.end")
        return {'id': transfer['id'],
                'volume_id': transfer['volume_id'],
                'display_name': transfer['display_name'],
                'auth_key': auth_key,
                'created_at': transfer['created_at'],
                'no_snapshots': transfer['no_snapshots'],
                'source_project_id': transfer['source_project_id'],
                'destination_project_id': transfer['destination_project_id'],
                'accepted': transfer['accepted']}

    def _handle_snapshot_quota(self, context, snapshots, volume_type_id,
                               donor_id):
        snapshots_num = len(snapshots)
        volume_sizes = 0
        if not CONF.no_snapshot_gb_quota:
            for snapshot in snapshots:
                volume_sizes += snapshot.volume_size
        try:
            reserve_opts = {'snapshots': snapshots_num,
                            'gigabytes': volume_sizes}
            QUOTAS.add_volume_type_opts(context,
                                        reserve_opts,
                                        volume_type_id)
            reservations = QUOTAS.reserve(context, **reserve_opts)
        except exception.OverQuota as e:
            quota_utils.process_reserve_over_quota(
                context, e,
                resource='snapshots',
                size=volume_sizes)

        try:
            reserve_opts = {'snapshots': -snapshots_num,
                            'gigabytes': -volume_sizes}
            QUOTAS.add_volume_type_opts(context.elevated(),
                                        reserve_opts,
                                        volume_type_id)
            donor_reservations = QUOTAS.reserve(context,
                                                project_id=donor_id,
                                                **reserve_opts)
        except exception.OverQuota as e:
            donor_reservations = None
            LOG.exception("Failed to update volume providing snapshots quota:"
                          " Over quota.")

        return reservations, donor_reservations

    def accept(self, context, transfer_id, auth_key):
        """Accept a volume that has been offered for transfer."""
        # We must use an elevated context to see the volume that is still
        # owned by the donor.
        context.authorize(policy.ACCEPT_POLICY)
        transfer = self.db.transfer_get(context.elevated(), transfer_id)

        crypt_hash = self._get_crypt_hash(transfer['salt'], auth_key)
        if crypt_hash != transfer['crypt_hash']:
            msg = (_("Attempt to transfer %s with invalid auth key.") %
                   transfer_id)
            LOG.error(msg)
            raise exception.InvalidAuthKey(reason=msg)

        volume_id = transfer['volume_id']
        vol_ref = objects.Volume.get_by_id(context.elevated(), volume_id)
        if vol_ref['consistencygroup_id']:
            msg = _("Volume %s must not be part of a consistency "
                    "group.") % vol_ref['id']
            LOG.error(msg)
            raise exception.InvalidVolume(reason=msg)

        try:
            values = {'per_volume_gigabytes': vol_ref.size}
            QUOTAS.limit_check(context, project_id=context.project_id,
                               **values)
        except exception.OverQuota as e:
            quotas = e.kwargs['quotas']
            raise exception.VolumeSizeExceedsLimit(
                size=vol_ref.size, limit=quotas['per_volume_gigabytes'])

        try:
            reserve_opts = {'volumes': 1, 'gigabytes': vol_ref.size}
            QUOTAS.add_volume_type_opts(context,
                                        reserve_opts,
                                        vol_ref.volume_type_id)
            reservations = QUOTAS.reserve(context, **reserve_opts)
        except exception.OverQuota as e:
            quota_utils.process_reserve_over_quota(context, e,
                                                   resource='volumes',
                                                   size=vol_ref.size)
        try:
            donor_id = vol_ref['project_id']
            reserve_opts = {'volumes': -1, 'gigabytes': -vol_ref.size}
            QUOTAS.add_volume_type_opts(context,
                                        reserve_opts,
                                        vol_ref.volume_type_id)
            donor_reservations = QUOTAS.reserve(context.elevated(),
                                                project_id=donor_id,
                                                **reserve_opts)
        except Exception:
            donor_reservations = None
            LOG.exception("Failed to update quota donating volume"
                          " transfer id %s", transfer_id)

        snap_res = None
        snap_donor_res = None
        if transfer['no_snapshots'] is False:
            snapshots = objects.SnapshotList.get_all_for_volume(
                context.elevated(), volume_id)
            volume_type_id = vol_ref.volume_type_id
            snap_res, snap_donor_res = self._handle_snapshot_quota(
                context, snapshots, volume_type_id, vol_ref['project_id'])

        volume_utils.notify_about_volume_usage(context, vol_ref,
                                               "transfer.accept.start")
        try:
            # Transfer ownership of the volume now, must use an elevated
            # context.
            self.volume_api.accept_transfer(context,
                                            vol_ref,
                                            context.user_id,
                                            context.project_id,
                                            transfer['no_snapshots'])
            self.db.transfer_accept(context.elevated(),
                                    transfer_id,
                                    context.user_id,
                                    context.project_id,
                                    transfer['no_snapshots'])
            QUOTAS.commit(context, reservations)
            if snap_res:
                QUOTAS.commit(context, snap_res)
            if donor_reservations:
                QUOTAS.commit(context, donor_reservations, project_id=donor_id)
            if snap_donor_res:
                QUOTAS.commit(context, snap_donor_res, project_id=donor_id)
            LOG.info("Volume %s has been transferred.", volume_id)
        except Exception:
            with excutils.save_and_reraise_exception():
                QUOTAS.rollback(context, reservations)
                if snap_res:
                    QUOTAS.rollback(context, snap_res)
                if donor_reservations:
                    QUOTAS.rollback(context, donor_reservations,
                                    project_id=donor_id)
                if snap_donor_res:
                    QUOTAS.rollback(context, snap_donor_res,
                                    project_id=donor_id)

        vol_ref = objects.Volume.get_by_id(context.elevated(),
                                           volume_id)
        volume_utils.notify_about_volume_usage(context, vol_ref,
                                               "transfer.accept.end")
        return {'id': transfer_id,
                'display_name': transfer['display_name'],
                'volume_id': vol_ref['id']}
