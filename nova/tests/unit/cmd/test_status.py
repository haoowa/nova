# Copyright 2016 IBM Corp.
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
Unit tests for the nova-status CLI interfaces.
"""

# NOTE(cdent): Additional tests of nova-status may be found in
# nova/tests/functional/test_nova_status.py. Those tests use the external
# PlacementFixture, which is only available in functional tests.

from io import StringIO
from unittest import mock

import fixtures
from keystoneauth1 import exceptions as ks_exc
from keystoneauth1 import loading as keystone
from keystoneauth1 import session
from oslo_upgradecheck import upgradecheck
from oslo_utils.fixture import uuidsentinel as uuids
from requests import models

from nova.cmd import status
import nova.conf
from nova import context
from nova import exception
# NOTE(mriedem): We only use objects as a convenience to populate the database
# in the tests, we don't use them in the actual CLI.
from nova import objects
from nova.objects import service
from nova import test
from nova.tests import fixtures as nova_fixtures


CONF = nova.conf.CONF


class TestNovaStatusMain(test.NoDBTestCase):
    """Tests for the basic nova-status command infrastructure."""

    def setUp(self):
        super(TestNovaStatusMain, self).setUp()
        self.output = StringIO()
        self.useFixture(fixtures.MonkeyPatch('sys.stdout', self.output))

    @mock.patch.object(status.config, 'parse_args')
    @mock.patch.object(status, 'CONF')
    def _check_main(self, mock_CONF, mock_parse_args,
                    category_name='check', expected_return_value=0):
        mock_CONF.category.name = category_name
        return_value = status.main()

        self.assertEqual(expected_return_value, return_value)
        mock_CONF.register_cli_opt.assert_called_once_with(
            status.category_opt)

    @mock.patch.object(status.version, 'version_string_with_package',
                       return_value="x.x.x")
    def test_main_version(self, mock_version_string):
        self._check_main(category_name='version')
        self.assertEqual("x.x.x\n", self.output.getvalue())

    @mock.patch.object(status.cmd_common, 'print_bash_completion')
    def test_main_bash_completion(self, mock_print_bash):
        self._check_main(category_name='bash-completion')
        mock_print_bash.assert_called_once_with(status.CATEGORIES)

    @mock.patch.object(status.cmd_common, 'get_action_fn')
    def test_main(self, mock_get_action_fn):
        mock_fn = mock.Mock()
        mock_fn_args = [mock.sentinel.arg]
        mock_fn_kwargs = {'key': mock.sentinel.value}
        mock_get_action_fn.return_value = (mock_fn, mock_fn_args,
                                           mock_fn_kwargs)

        self._check_main(expected_return_value=mock_fn.return_value)
        mock_fn.assert_called_once_with(mock.sentinel.arg,
                                        key=mock.sentinel.value)

    @mock.patch.object(status.cmd_common, 'get_action_fn')
    def test_main_error(self, mock_get_action_fn):
        mock_fn = mock.Mock(side_effect=Exception('wut'))
        mock_get_action_fn.return_value = (mock_fn, [], {})

        self._check_main(expected_return_value=255)
        output = self.output.getvalue()
        self.assertIn('Error:', output)
        # assert the traceback is in the output
        self.assertIn('wut', output)


class TestPlacementCheck(test.NoDBTestCase):
    """Tests the nova-status placement checks.

    These are done with mock as the ability to replicate all failure
    domains otherwise is quite complicated. Using a devstack
    environment you can validate each of these tests are matching
    reality.
    """

    def setUp(self):
        super(TestPlacementCheck, self).setUp()
        self.output = StringIO()
        self.useFixture(fixtures.MonkeyPatch('sys.stdout', self.output))
        self.cmd = status.UpgradeCommands()

    @mock.patch.object(keystone, "load_auth_from_conf_options")
    def test_no_auth(self, auth):
        """Test failure when no credentials are specified.

        Replicate in devstack: start devstack with or without
        placement engine, remove the auth section from the [placement]
        block in nova.conf.
        """
        auth.side_effect = ks_exc.MissingAuthPlugin()
        res = self.cmd._check_placement()
        self.assertEqual(upgradecheck.Code.FAILURE, res.code)
        self.assertIn('No credentials specified', res.details)

    @mock.patch.object(keystone, "load_auth_from_conf_options")
    @mock.patch.object(session.Session, 'request')
    def _test_placement_get_interface(
            self, expected_interface, mock_get, mock_auth):

        def fake_request(path, method, *a, **kw):
            self.assertEqual(mock.sentinel.path, path)
            self.assertEqual('GET', method)
            self.assertIn('endpoint_filter', kw)
            self.assertEqual(expected_interface,
                             kw['endpoint_filter']['interface'])
            return mock.Mock(autospec=models.Response)

        mock_get.side_effect = fake_request
        self.cmd._placement_get(mock.sentinel.path)
        mock_auth.assert_called_once_with(status.CONF, 'placement')
        self.assertTrue(mock_get.called)

    def test_placement_get_interface_default(self):
        """Tests that we try internal, then public interface by default."""
        self._test_placement_get_interface(['internal', 'public'])

    def test_placement_get_interface_internal(self):
        """Tests that "internal" is specified for interface when configured."""
        self.flags(valid_interfaces='internal', group='placement')
        self._test_placement_get_interface(['internal'])

    @mock.patch.object(status.UpgradeCommands, "_placement_get")
    def test_invalid_auth(self, get):
        """Test failure when wrong credentials are specified or service user
        doesn't exist.

        Replicate in devstack: start devstack with or without
        placement engine, specify random credentials in auth section
        from the [placement] block in nova.conf.

        """
        get.side_effect = ks_exc.Unauthorized()
        res = self.cmd._check_placement()
        self.assertEqual(upgradecheck.Code.FAILURE, res.code)
        self.assertIn('Placement service credentials do not work', res.details)

    @mock.patch.object(status.UpgradeCommands, "_placement_get")
    def test_invalid_endpoint(self, get):
        """Test failure when no endpoint exists.

        Replicate in devstack: start devstack without placement
        engine, but create valid placement service user and specify it
        in auth section of [placement] in nova.conf.
        """
        get.side_effect = ks_exc.EndpointNotFound()
        res = self.cmd._check_placement()
        self.assertEqual(upgradecheck.Code.FAILURE, res.code)
        self.assertIn('Placement API endpoint not found', res.details)

    @mock.patch.object(status.UpgradeCommands, "_placement_get")
    def test_discovery_failure(self, get):
        """Test failure when discovery for placement URL failed.

        Replicate in devstack: start devstack with placement
        engine, create valid placement service user and specify it
        in auth section of [placement] in nova.conf. Stop keystone service.
        """
        get.side_effect = ks_exc.DiscoveryFailure()
        res = self.cmd._check_placement()
        self.assertEqual(upgradecheck.Code.FAILURE, res.code)
        self.assertIn('Discovery for placement API URI failed.', res.details)

    @mock.patch.object(status.UpgradeCommands, "_placement_get")
    def test_down_endpoint(self, get):
        """Test failure when endpoint is down.

        Replicate in devstack: start devstack with placement
        engine, disable placement engine apache config.
        """
        get.side_effect = ks_exc.NotFound()
        res = self.cmd._check_placement()
        self.assertEqual(upgradecheck.Code.FAILURE, res.code)
        self.assertIn('Placement API does not seem to be running', res.details)

    @mock.patch.object(status.UpgradeCommands, "_placement_get")
    def test_valid_version(self, get):
        get.return_value = {
            "versions": [
                {
                    "min_version": "1.0",
                    "max_version": status.MIN_PLACEMENT_MICROVERSION,
                    "id": "v1.0"
                }
            ]
        }
        res = self.cmd._check_placement()
        self.assertEqual(upgradecheck.Code.SUCCESS, res.code)

    @mock.patch.object(status.UpgradeCommands, "_placement_get")
    def test_version_comparison_does_not_use_floats(self, get):
        # NOTE(rpodolyaka): previously _check_placement() coerced the version
        # numbers to floats prior to comparison, that would lead to failures
        # in cases like float('1.10') < float('1.4'). As we require 1.4+ now,
        # the _check_placement() call below will assert that version comparison
        # continues to work correctly when Placement API versions 1.10
        # (or newer) is released
        get.return_value = {
             "versions": [
                {
                    "min_version": "1.0",
                    "max_version": status.MIN_PLACEMENT_MICROVERSION,
                    "id": "v1.0"
                }
            ]
        }
        res = self.cmd._check_placement()
        self.assertEqual(upgradecheck.Code.SUCCESS, res.code)

    @mock.patch.object(status.UpgradeCommands, "_placement_get")
    def test_invalid_version(self, get):
        get.return_value = {
            "versions": [
                {
                    "min_version": "0.9",
                    "max_version": "0.9",
                    "id": "v1.0"
                }
            ]
        }
        res = self.cmd._check_placement()
        self.assertEqual(upgradecheck.Code.FAILURE, res.code)
        self.assertIn('Placement API version %s needed, you have 0.9' %
                      status.MIN_PLACEMENT_MICROVERSION, res.details)


class TestUpgradeCheckCellsV2(test.NoDBTestCase):
    """Tests for the nova-status upgrade cells v2 specific check."""

    # We'll setup the API DB fixture ourselves and slowly build up the
    # contents until the check passes.
    USES_DB_SELF = True

    def setUp(self):
        super(TestUpgradeCheckCellsV2, self).setUp()
        self.output = StringIO()
        self.useFixture(fixtures.MonkeyPatch('sys.stdout', self.output))
        self.useFixture(nova_fixtures.Database(database='api'))
        self.cmd = status.UpgradeCommands()

    def test_check_no_cell_mappings(self):
        """The cells v2 check should fail because there are no cell mappings.
        """
        result = self.cmd._check_cellsv2()
        self.assertEqual(upgradecheck.Code.FAILURE, result.code)
        self.assertIn('There needs to be at least two cell mappings',
                      result.details)

    def _create_cell_mapping(self, uuid):
        cm = objects.CellMapping(
            context=context.get_admin_context(),
            uuid=uuid,
            name=uuid,
            transport_url='fake://%s/' % uuid,
            database_connection=uuid)
        cm.create()
        return cm

    def test_check_no_cell0_mapping(self):
        """We'll create two cell mappings but not have cell0 mapped yet."""
        for i in range(2):
            uuid = getattr(uuids, str(i))
            self._create_cell_mapping(uuid)

        result = self.cmd._check_cellsv2()
        self.assertEqual(upgradecheck.Code.FAILURE, result.code)
        self.assertIn('No cell0 mapping found', result.details)

    def test_check_no_host_mappings_with_computes(self):
        """Creates a cell0 and cell1 mapping but no host mappings and there are
        compute nodes in the cell database.
        """
        self._setup_cells()
        cn = objects.ComputeNode(
            context=context.get_admin_context(),
            host='fake-host',
            vcpus=4,
            memory_mb=8 * 1024,
            local_gb=40,
            vcpus_used=2,
            memory_mb_used=2 * 1024,
            local_gb_used=10,
            hypervisor_type='fake',
            hypervisor_version=1,
            cpu_info='{"arch": "x86_64"}')
        cn.create()

        result = self.cmd._check_cellsv2()
        self.assertEqual(upgradecheck.Code.FAILURE, result.code)
        self.assertIn('No host mappings found but there are compute nodes',
                      result.details)

    def test_check_no_host_mappings_no_computes(self):
        """Creates the cell0 and cell1 mappings but no host mappings and no
        compute nodes so it's assumed to be an initial install.
        """
        self._setup_cells()

        result = self.cmd._check_cellsv2()
        self.assertEqual(upgradecheck.Code.SUCCESS, result.code)
        self.assertIn('No host mappings or compute nodes were found',
                      result.details)

    def test_check_success(self):
        """Tests a successful cells v2 upgrade check."""
        # create the cell0 and first cell mappings
        self._setup_cells()
        # Start a compute service and create a hostmapping for it
        svc = self.start_service('compute')
        cell = self.cell_mappings[test.CELL1_NAME]
        hm = objects.HostMapping(context=context.get_admin_context(),
                                 host=svc.host,
                                 cell_mapping=cell)
        hm.create()

        result = self.cmd._check_cellsv2()
        self.assertEqual(upgradecheck.Code.SUCCESS, result.code)
        self.assertIsNone(result.details)


class TestUpgradeCheckCinderAPI(test.NoDBTestCase):

    def setUp(self):
        super(TestUpgradeCheckCinderAPI, self).setUp()
        self.cmd = status.UpgradeCommands()

    def test_cinder_not_configured(self):
        self.flags(auth_type=None, group='cinder')
        self.assertEqual(upgradecheck.Code.SUCCESS,
                         self.cmd._check_cinder().code)

    @mock.patch('nova.volume.cinder.is_microversion_supported',
                side_effect=exception.CinderAPIVersionNotAvailable(
                    version='3.44'))
    def test_microversion_not_available(self, mock_version_check):
        self.flags(auth_type='password', group='cinder')
        result = self.cmd._check_cinder()
        mock_version_check.assert_called_once()
        self.assertEqual(upgradecheck.Code.FAILURE, result.code)
        self.assertIn('Cinder API 3.44 or greater is required.',
                      result.details)

    @mock.patch('nova.volume.cinder.is_microversion_supported',
                side_effect=test.TestingException('oops'))
    def test_unknown_error(self, mock_version_check):
        self.flags(auth_type='password', group='cinder')
        result = self.cmd._check_cinder()
        mock_version_check.assert_called_once()
        self.assertEqual(upgradecheck.Code.WARNING, result.code)
        self.assertIn('oops', result.details)

    @mock.patch('nova.volume.cinder.is_microversion_supported')
    def test_microversion_available(self, mock_version_check):
        self.flags(auth_type='password', group='cinder')
        result = self.cmd._check_cinder()
        mock_version_check.assert_called_once()
        self.assertEqual(upgradecheck.Code.SUCCESS, result.code)


class TestUpgradeCheckOldCompute(test.NoDBTestCase):

    def setUp(self):
        super(TestUpgradeCheckOldCompute, self).setUp()
        self.cmd = status.UpgradeCommands()

    def test_no_compute(self):
        self.assertEqual(
            upgradecheck.Code.SUCCESS, self.cmd._check_old_computes().code)

    def test_only_new_compute(self):
        last_supported_version = service.SERVICE_VERSION_ALIASES[
            service.OLDEST_SUPPORTED_SERVICE_VERSION]
        with mock.patch(
                "nova.objects.service.get_minimum_version_all_cells",
                return_value=last_supported_version):
            self.assertEqual(
                upgradecheck.Code.SUCCESS, self.cmd._check_old_computes().code)

    def test_old_compute(self):
        too_old = service.SERVICE_VERSION_ALIASES[
            service.OLDEST_SUPPORTED_SERVICE_VERSION] - 1
        with mock.patch(
                "nova.objects.service.get_minimum_version_all_cells",
                return_value=too_old):
            result = self.cmd._check_old_computes()
            self.assertEqual(upgradecheck.Code.FAILURE, result.code)


class TestCheckMachineTypeUnset(test.NoDBTestCase):

    def setUp(self):
        super().setUp()
        self.cmd = status.UpgradeCommands()

    @mock.patch(
        'nova.virt.libvirt.machine_type_utils.get_instances_without_type',
        new=mock.Mock(return_value=[mock.Mock(spec=objects.Instance)]))
    def test_instances_found_without_hw_machine_type(self):
        result = self.cmd._check_machine_type_set()
        self.assertEqual(
            upgradecheck.Code.WARNING,
            result.code
        )

    @mock.patch(
        'nova.virt.libvirt.machine_type_utils.get_instances_without_type',
        new=mock.Mock(return_value=[]))
    def test_instances_not_found_without_hw_machine_type(self):
        result = self.cmd._check_machine_type_set()
        self.assertEqual(
            upgradecheck.Code.SUCCESS,
            result.code
        )


class TestUpgradeCheckServiceUserToken(test.NoDBTestCase):

    def setUp(self):
        super().setUp()
        self.cmd = status.UpgradeCommands()

    def test_service_user_token_not_configured(self):
        result = self.cmd._check_service_user_token()
        self.assertEqual(upgradecheck.Code.FAILURE, result.code)

    def test_service_user_token_configured(self):
        self.flags(send_service_user_token=True, group='service_user')
        result = self.cmd._check_service_user_token()
        self.assertEqual(upgradecheck.Code.SUCCESS, result.code)


class TestObjectLinkage(test.NoDBTestCase):

    def setUp(self):
        super().setUp()
        self.cmd = status.UpgradeCommands()

    @mock.patch('nova.db.main.api.compute_nodes_get_by_service_id')
    @mock.patch('nova.db.main.api.instance_get_all_by_filters')
    def test_all_good(self, mock_get_inst, mock_get_cn):
        mock_get_inst.return_value = []
        mock_get_cn.side_effect = exception.ServiceNotFound(service_id=None)
        result = self.cmd._check_compute_object_linkage()
        self.assertEqual(upgradecheck.Code.SUCCESS, result.code)

    @mock.patch('nova.db.main.api.compute_nodes_get_by_service_id')
    def test_missing_service_id(self, mock_get):
        mock_get.return_value = ['foo']
        result = self.cmd._check_compute_object_linkage()
        self.assertEqual(upgradecheck.Code.FAILURE, result.code)

    @mock.patch('nova.db.main.api.compute_nodes_get_by_service_id')
    @mock.patch('nova.db.main.api.instance_get_all_by_filters')
    def test_missing_compute_id(self, mock_get_inst, mock_get_cn):
        mock_get_cn.side_effect = exception.ServiceNotFound(service_id=None)
        mock_get_inst.return_value = ['foo']
        result = self.cmd._check_compute_object_linkage()
        self.assertEqual(upgradecheck.Code.FAILURE, result.code)
