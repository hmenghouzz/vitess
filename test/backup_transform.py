#!/usr/bin/env python

# Copyright 2017 Google Inc.
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


import json
import logging
import os
import shutil
import unittest

import MySQLdb

import environment
import tablet
import utils
from mysql_flavor import mysql_flavor

use_mysqlctld = False
tablet_master = None
tablet_replica1 = None
tablet_replica2 = None
new_init_db = ''
db_credentials_file = ''


def setUpModule():
  global new_init_db, db_credentials_file
  global tablet_master, tablet_replica1, tablet_replica2

  tablet_master = tablet.Tablet(use_mysqlctld=use_mysqlctld,
                                vt_dba_passwd='VtDbaPass')
  tablet_replica1 = tablet.Tablet(use_mysqlctld=use_mysqlctld,
                                  vt_dba_passwd='VtDbaPass')
  tablet_replica2 = tablet.Tablet(use_mysqlctld=use_mysqlctld,
                                  vt_dba_passwd='VtDbaPass')

  try:
    environment.topo_server().setup()

    credentials = {
        'vt_dba': ['VtDbaPass'],
        'vt_app': ['VtAppPass'],
        'vt_allprivs': ['VtAllprivsPass'],
        'vt_repl': ['VtReplPass'],
        'vt_filtered': ['VtFilteredPass'],
    }
    db_credentials_file = environment.tmproot+'/db_credentials.json'
    with open(db_credentials_file, 'w') as fd:
      fd.write(json.dumps(credentials))

    # Determine which column is used for user passwords in this MySQL version.
    proc = tablet_master.init_mysql()
    if use_mysqlctld:
      tablet_master.wait_for_mysqlctl_socket()
    else:
      utils.wait_procs([proc])
    try:
      tablet_master.mquery('mysql', 'select password from mysql.user limit 0',
                           user='root')
      password_col = 'password'
    except MySQLdb.DatabaseError:
      password_col = 'authentication_string'
    utils.wait_procs([tablet_master.teardown_mysql()])
    tablet_master.remove_tree(ignore_options=True)

    # Create a new init_db.sql file that sets up passwords for all users.
    # Then we use a db-credentials-file with the passwords.
    new_init_db = environment.tmproot + '/init_db_with_passwords.sql'
    with open(environment.vttop + '/config/init_db.sql') as fd:
      init_db = fd.read()
    with open(new_init_db, 'w') as fd:
      fd.write(init_db)
      fd.write(mysql_flavor().change_passwords(password_col))

    # start mysql instance external to the test
    setup_procs = [
        tablet_master.init_mysql(init_db=new_init_db,
                                 extra_args=['-db-credentials-file',
                                             db_credentials_file]),
        tablet_replica1.init_mysql(init_db=new_init_db,
                                   extra_args=['-db-credentials-file',
                                               db_credentials_file]),
        tablet_replica2.init_mysql(init_db=new_init_db,
                                   extra_args=['-db-credentials-file',
                                               db_credentials_file]),
    ]
    if use_mysqlctld:
      tablet_master.wait_for_mysqlctl_socket()
      tablet_replica1.wait_for_mysqlctl_socket()
      tablet_replica2.wait_for_mysqlctl_socket()
    else:
      utils.wait_procs(setup_procs)
  except:
    tearDownModule()
    raise


def tearDownModule():
  utils.required_teardown()
  if utils.options.skip_teardown:
    return

  teardown_procs = [
      tablet_master.teardown_mysql(extra_args=['-db-credentials-file',
                                               db_credentials_file]),
      tablet_replica1.teardown_mysql(extra_args=['-db-credentials-file',
                                                 db_credentials_file]),
      tablet_replica2.teardown_mysql(extra_args=['-db-credentials-file',
                                                 db_credentials_file]),
  ]
  utils.wait_procs(teardown_procs, raise_on_error=False)

  environment.topo_server().teardown()
  utils.kill_sub_processes()
  utils.remove_tmp_files()

  tablet_master.remove_tree()
  tablet_replica1.remove_tree()
  tablet_replica2.remove_tree()


class TestBackupTransform(unittest.TestCase):

  def setUp(self):
    for t in tablet_master, tablet_replica1:
      t.create_db('vt_test_keyspace')

    tablet_master.init_tablet('replica', 'test_keyspace', '0', start=True,
                              supports_backups=True,
                              extra_args=['-db-credentials-file',
                                           db_credentials_file])
    tablet_replica1.init_tablet('replica', 'test_keyspace', '0', start=True,
                                supports_backups=True,
                                extra_args=['-db-credentials-file',
                                             db_credentials_file])
    utils.run_vtctl(['InitShardMaster', '-force', 'test_keyspace/0',
                     tablet_master.tablet_alias])

  def tearDown(self):
    for t in tablet_master, tablet_replica1, tablet_replica2:
      t.kill_vttablet()

    tablet.Tablet.check_vttablet_count()
    environment.topo_server().wipe()
    for t in [tablet_master, tablet_replica1, tablet_replica2]:
      t.reset_replication()
      t.set_semi_sync_enabled(master=False, slave=False)
      t.clean_dbs()

    for backup in self._list_backups():
      self._remove_backup(backup)

  _create_vt_insert_test = '''create table vt_insert_test (
  id bigint auto_increment,
  msg varchar(64),
  primary key (id)
  ) Engine=InnoDB'''

  def _insert_data(self, t, index):
    """Add a single row with value 'index' to the given tablet."""
    t.mquery(
        'vt_test_keyspace',
        "insert into vt_insert_test (msg) values ('test %s')" %
        index, write=True)

  def _check_data(self, t, count, msg):
    """Check that the specified tablet has the expected number of rows."""
    timeout = 10
    while True:
      try:
        result = t.mquery(
            'vt_test_keyspace', 'select count(*) from vt_insert_test')
        if result[0][0] == count:
          break
      except MySQLdb.DatabaseError:
        # ignore exceptions, we'll just timeout (the tablet creation
        # can take some time to replicate, and we get a 'table vt_insert_test
        # does not exist exception in some rare cases)
        logging.exception('exception waiting for data to replicate')
      timeout = utils.wait_step(msg, timeout)

  def _restore(self, t, tablet_type='replica'):
    """Erase mysql/tablet dir, then start tablet with restore enabled."""
    self._reset_tablet_dir(t)

    t.start_vttablet(wait_for_state='SERVING',
                     init_tablet_type=tablet_type,
                     init_keyspace='test_keyspace',
                     init_shard='0',
                     supports_backups=True,
                     extra_args=['-db-credentials-file', db_credentials_file])

    # check semi-sync is enabled for replica, disabled for rdonly.
    if tablet_type == 'replica':
      t.check_db_var('rpl_semi_sync_slave_enabled', 'ON')
      t.check_db_status('rpl_semi_sync_slave_status', 'ON')
    else:
      t.check_db_var('rpl_semi_sync_slave_enabled', 'OFF')
      t.check_db_status('rpl_semi_sync_slave_status', 'OFF')

  def _reset_tablet_dir(self, t):
    """Stop mysql, delete everything including tablet dir, restart mysql."""
    extra_args = ['-db-credentials-file', db_credentials_file]
    utils.wait_procs([t.teardown_mysql(extra_args=extra_args)])
    # Specify ignore_options because we want to delete the tree even
    # if the test's -k / --keep-logs was specified on the command line.
    t.remove_tree(ignore_options=True)
    proc = t.init_mysql(init_db=new_init_db, extra_args=extra_args)
    if use_mysqlctld:
      t.wait_for_mysqlctl_socket()
    else:
      utils.wait_procs([proc])

  def _list_backups(self):
    """Get a list of backup names for the test shard."""
    backups, _ = utils.run_vtctl(tablet.get_backup_storage_flags() +
                                 ['ListBackups', 'test_keyspace/0'],
                                 mode=utils.VTCTL_VTCTL, trap_output=True)
    return backups.splitlines()

  def _remove_backup(self, backup):
    """Remove a named backup from the test shard."""
    utils.run_vtctl(
        tablet.get_backup_storage_flags() +
        ['RemoveBackup', 'test_keyspace/0', backup],
        auto_log=True, mode=utils.VTCTL_VTCTL)

  def test_backup_transform(self):
    """Use a transform, tests we backup and restore properly."""

    # Insert data on master, make sure slave gets it.
    tablet_master.mquery('vt_test_keyspace', self._create_vt_insert_test)
    self._insert_data(tablet_master, 1)
    self._check_data(tablet_replica1, 1, 'replica1 tablet getting data')

    # Restart the replica with the transform parameter.
    tablet_replica1.kill_vttablet()

    xtra_args = ['-db-credentials-file', db_credentials_file]
    hook_args = ['-backup_storage_hook',
                 'test_backup_transform',
                 '-backup_storage_compress=false']
    xtra_args.extend(hook_args)

    tablet_replica1.start_vttablet(supports_backups=True,
                                   extra_args=xtra_args)

    # Take a backup, it should work.
    utils.run_vtctl(['Backup', tablet_replica1.tablet_alias], auto_log=True)

    # Insert more data on the master.
    self._insert_data(tablet_master, 2)

    # Make sure we have the TransformHook in the MANIFEST, and that
    # every file starts with 'header'.
    backups = self._list_backups()
    self.assertEqual(len(backups), 1, 'invalid backups: %s' % backups)
    location = os.path.join(environment.tmproot, 'backupstorage',
                            'test_keyspace', '0', backups[0])
    with open(os.path.join(location, 'MANIFEST')) as fd:
      contents = fd.read()
    manifest = json.loads(contents)
    self.assertEqual(manifest['TransformHook'], 'test_backup_transform')
    self.assertEqual(manifest['SkipCompress'], True)
    for i in xrange(len(manifest['FileEntries'])):
      name = os.path.join(location, '%d' % i)
      with open(name) as fd:
        line = fd.readline()
        self.assertEqual(line, 'header\n', 'wrong file contents for %s' % name)

    # Then start replica2 from backup, make sure that works.
    # Note we don't need to pass in the backup_storage_transform parameter,
    # as it is read from the MANIFEST.
    self._restore(tablet_replica2)

    # Check the new slave has all the data.
    self._check_data(tablet_replica2, 2, 'replica2 tablet getting data')

  def test_backup_transform_error(self):
    """Use a transform, force an error, make sure the backup fails."""

    # Restart the replica with the transform parameter.
    tablet_replica1.kill_vttablet()
    xtra_args = ['-db-credentials-file', db_credentials_file]
    hook_args = ['-backup_storage_hook','test_backup_error']
    xtra_args.extend(hook_args)
    tablet_replica1.start_vttablet(supports_backups=True,
                                   extra_args=xtra_args)

    # This will fail, make sure we get the right error.
    _, err = utils.run_vtctl(['Backup', tablet_replica1.tablet_alias],
                             auto_log=True, expect_fail=True)
    self.assertIn('backup is not usable, aborting it', err)

    # And make sure there is no backup left.
    backups = self._list_backups()
    self.assertEqual(len(backups), 0, 'invalid backups: %s' % backups)

if __name__ == '__main__':
  utils.main()
