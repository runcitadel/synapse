# -*- coding: utf-8 -*-
# Copyright 2014-2016 OpenMarket Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from ._base import SQLBaseStore
from twisted.internet import defer

from canonicaljson import encode_canonical_json

from synapse.util.caches.descriptors import cachedInlineCallbacks

import logging
import simplejson as json
import types

logger = logging.getLogger(__name__)


class PusherStore(SQLBaseStore):
    def _decode_pushers_rows(self, rows):
        for r in rows:
            dataJson = r['data']
            r['data'] = None
            try:
                if isinstance(dataJson, types.BufferType):
                    dataJson = str(dataJson).decode("UTF8")

                r['data'] = json.loads(dataJson)
            except Exception as e:
                logger.warn(
                    "Invalid JSON in data for pusher %d: %s, %s",
                    r['id'], dataJson, e.message,
                )
                pass

            if isinstance(r['pushkey'], types.BufferType):
                r['pushkey'] = str(r['pushkey']).decode("UTF8")

        return rows

    @defer.inlineCallbacks
    def get_pushers_by_app_id_and_pushkey(self, app_id, pushkey):
        def r(txn):
            sql = (
                "SELECT * FROM pushers"
                " WHERE app_id = ? AND pushkey = ?"
            )

            txn.execute(sql, (app_id, pushkey,))
            rows = self.cursor_to_dict(txn)

            return self._decode_pushers_rows(rows)

        rows = yield self.runInteraction(
            "get_pushers_by_app_id_and_pushkey", r
        )

        defer.returnValue(rows)

    @defer.inlineCallbacks
    def get_all_pushers(self):
        def get_pushers(txn):
            txn.execute("SELECT * FROM pushers")
            rows = self.cursor_to_dict(txn)

            return self._decode_pushers_rows(rows)

        rows = yield self.runInteraction("get_all_pushers", get_pushers)
        defer.returnValue(rows)

    def get_pushers_stream_token(self):
        return self._pushers_id_gen.get_current_token()

    def get_all_updated_pushers(self, last_id, current_id, limit):
        def get_all_updated_pushers_txn(txn):
            sql = (
                "SELECT id, user_name, access_token, profile_tag, kind,"
                " app_id, app_display_name, device_display_name, pushkey, ts,"
                " lang, data"
                " FROM pushers"
                " WHERE ? < id AND id <= ?"
                " ORDER BY id ASC LIMIT ?"
            )
            txn.execute(sql, (last_id, current_id, limit))
            updated = txn.fetchall()

            sql = (
                "SELECT stream_id, user_id, app_id, pushkey"
                " FROM deleted_pushers"
                " WHERE ? < stream_id AND stream_id <= ?"
                " ORDER BY stream_id ASC LIMIT ?"
            )
            txn.execute(sql, (last_id, current_id, limit))
            deleted = txn.fetchall()

            return (updated, deleted)
        return self.runInteraction(
            "get_all_updated_pushers", get_all_updated_pushers_txn
        )

    @cachedInlineCallbacks(num_args=1)
    def get_users_with_pushers_in_room(self, room_id):
        users = yield self.get_users_in_room(room_id)

        result = yield self._simple_select_many_batch(
            'pushers', 'user_name', users, ['user_name']
        )

        defer.returnValue([r['user_name'] for r in result])

    @defer.inlineCallbacks
    def add_pusher(self, user_id, access_token, kind, app_id,
                   app_display_name, device_display_name,
                   pushkey, pushkey_ts, lang, data, last_stream_ordering,
                   profile_tag=""):
        with self._pushers_id_gen.get_next() as stream_id:
            def f(txn):
                txn.call_after(self.get_users_with_pushers_in_room.invalidate_all)
                return self._simple_upsert_txn(
                    txn,
                    "pushers",
                    {
                        "app_id": app_id,
                        "pushkey": pushkey,
                        "user_name": user_id,
                    },
                    {
                        "access_token": access_token,
                        "kind": kind,
                        "app_display_name": app_display_name,
                        "device_display_name": device_display_name,
                        "ts": pushkey_ts,
                        "lang": lang,
                        "data": encode_canonical_json(data),
                        "last_stream_ordering": last_stream_ordering,
                        "profile_tag": profile_tag,
                        "id": stream_id,
                    },
                )
        defer.returnValue((yield self.runInteraction("add_pusher", f)))

    @defer.inlineCallbacks
    def delete_pusher_by_app_id_pushkey_user_id(self, app_id, pushkey, user_id):
        def delete_pusher_txn(txn, stream_id):
            self._simple_delete_one_txn(
                txn,
                "pushers",
                {"app_id": app_id, "pushkey": pushkey, "user_name": user_id}
            )
            self._simple_upsert_txn(
                txn,
                "deleted_pushers",
                {"app_id": app_id, "pushkey": pushkey, "user_id": user_id},
                {"stream_id": stream_id},
            )
        with self._pushers_id_gen.get_next() as stream_id:
            yield self.runInteraction(
                "delete_pusher", delete_pusher_txn, stream_id
            )

    @defer.inlineCallbacks
    def update_pusher_last_stream_ordering(self, app_id, pushkey, user_id,
                                           last_stream_ordering):
        yield self._simple_update_one(
            "pushers",
            {'app_id': app_id, 'pushkey': pushkey, 'user_name': user_id},
            {'last_stream_ordering': last_stream_ordering},
            desc="update_pusher_last_stream_ordering",
        )

    @defer.inlineCallbacks
    def update_pusher_last_stream_ordering_and_success(self, app_id, pushkey,
                                                       user_id,
                                                       last_stream_ordering,
                                                       last_success):
        yield self._simple_update_one(
            "pushers",
            {'app_id': app_id, 'pushkey': pushkey, 'user_name': user_id},
            {
                'last_stream_ordering': last_stream_ordering,
                'last_success': last_success
            },
            desc="update_pusher_last_stream_ordering_and_success",
        )

    @defer.inlineCallbacks
    def update_pusher_failing_since(self, app_id, pushkey, user_id,
                                    failing_since):
        yield self._simple_update_one(
            "pushers",
            {'app_id': app_id, 'pushkey': pushkey, 'user_name': user_id},
            {'failing_since': failing_since},
            desc="update_pusher_failing_since",
        )
