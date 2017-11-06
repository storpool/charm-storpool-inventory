#!/usr/bin/python3

"""
A set of unit tests for the storpool-inventory charm.
"""

import os
import sys
import unittest

import json
import mock

from http import client as http_client

from charmhelpers.core import hookenv

root_path = os.path.realpath('.')
if root_path not in sys.path:
    sys.path.insert(0, root_path)

lib_path = os.path.realpath('unit_tests/lib')
if lib_path not in sys.path:
    sys.path.insert(0, lib_path)


class MockReactive(object):
    def r_clear_states(self):
        self.states = set()

    def __init__(self):
        self.r_clear_states()

    def set_state(self, name):
        self.states.add(name)

    def remove_state(self, name):
        if name in self.states:
            self.states.remove(name)

    def is_state(self, name):
        return name in self.states

    def r_get_states(self):
        return set(self.states)

    def r_set_states(self, states):
        self.states = set(states)


initializing_config = None


class MockConfig(object):
    def r_clear_config(self):
        global initializing_config
        saved = initializing_config
        initializing_config = self
        self.override = {}
        self.changed_attrs = {}
        self.config = {}
        initializing_config = saved

    def __init__(self):
        self.r_clear_config()

    def r_set(self, key, value, changed):
        self.override[key] = value
        self.changed_attrs[key] = changed

    def get(self, key, default):
        return self.override.get(key, self.config.get(key, default))

    def changed(self, key):
        return self.changed_attrs.get(key, False)

    def __getattr__(self, name):
        return self.config.__getattribute__(name)

    def __setattr__(self, name, value):
        if initializing_config == self:
            return super(MockConfig, self).__setattr__(name, value)

        raise AttributeError('Cannot override the MockConfig '
                             '"{name}" attribute'.format(name=name))


r_state = MockReactive()
r_config = MockConfig()

# Do not give hookenv.config() a chance to run at all
hookenv.config = lambda: r_config


def mock_reactive_states(f):
    def inner1(inst, *args, **kwargs):
        @mock.patch('charms.reactive.set_state', new=r_state.set_state)
        @mock.patch('charms.reactive.remove_state', new=r_state.remove_state)
        @mock.patch('charms.reactive.helpers.is_state', new=r_state.is_state)
        def inner2(*args, **kwargs):
            return f(inst, *args, **kwargs)

        return inner2()

    return inner1


from reactive import storpool_inventory_charm as testee


class TestInventory(unittest.TestCase):
    def setUp(self):
        super(TestInventory, self).setUp()
        r_state.r_clear_states()
        r_config.r_clear_config()

    @mock_reactive_states
    @mock.patch('spcharms.utils.rdebug')
    def test_hook_install(self, rdebug):
        """
        Test the two routines in the candleholder charm.
        """

        r_state.set_state('storpool-inventory.collected')
        r_state.set_state('storpool-inventory.submitted')

        testee.first_install()
        self.assertEquals(1, rdebug.call_count)

        states = r_state.r_get_states()
        self.assertEquals(set(['storpool-inventory.collecting',
                               'storpool-inventory.submitting']),
                          states)

    @mock_reactive_states
    @mock.patch('spcharms.utils.rdebug')
    def test_hook_config_changed(self, rdebug):
        states = {
            'weird': set([
                'storpool-inventory.configured',
                'storpool-inventory.submitting',
                'storpool-inventory.submitted',
            ]),
            'start': set(),
            'collect': set([
                'storpool-inventory.configured',
                'storpool-inventory.collecting',
                'storpool-inventory.submitting',
            ]),
            'collected-ns': set([
                'storpool-inventory.configured',
                'storpool-inventory.collected',
            ]),
            'collected': set([
                'storpool-inventory.configured',
                'storpool-inventory.collected',
                'storpool-inventory.submitting',
            ]),
        }

        # If no config supplied, clear all the "done something" states
        r_state.r_set_states(states['weird'])
        testee.have_config()
        self.assertEquals(set(), r_state.r_get_states())

        # Same with a config missing the "submit_url" key
        r_state.r_set_states(states['weird'])
        r_config.r_set('something', 'else', True)
        testee.have_config()
        self.assertEquals(set(), r_state.r_get_states())

        r_state.r_set_states(states['weird'])
        r_config.r_set('submit_url', '', True)
        testee.have_config()
        self.assertEquals(set(), r_state.r_get_states())

        # Let's give it an URL, but make it seem the same as before
        r_state.r_set_states(states['weird'])
        r_config.r_set('submit_url', 'something', False)
        testee.have_config()
        self.assertEquals(states['weird'], r_state.r_get_states())

        # OK then, make things work!
        r_state.r_set_states(states['start'])
        r_config.r_set('submit_url', 'something', True)
        testee.have_config()
        self.assertEquals(states['collect'], r_state.r_get_states())

        # Or have we collected the data already?
        r_state.r_set_states(states['collected-ns'])
        r_config.r_set('submit_url', 'something', True)
        testee.have_config()
        self.assertEquals(states['collected'], r_state.r_get_states())

    def fail_on_err(self, *args):
        self.fail('sputils.err() invoked: {args}'.format(args=args))

    @mock_reactive_states
    @mock.patch('urllib.request.urlopen')
    @mock.patch('spcharms.utils.err')
    @mock.patch('spcharms.repo.install_packages')
    @mock.patch('spcharms.repo.record_packages')
    @mock.patch('subprocess.call')
    def test_collect_and_submit(self, sub_call, sprepo_record, sprepo_install,
                                sputils_err, urlopen):
        installed = ('a-package', 'another-package')
        sprepo_install.return_value = (None, installed)

        sputils_err.side_effect = lambda *args: self.fail_on_err(*args)

        r_state.r_set_states(set(['storpool-inventory.collecting']))
        testee.collect()
        self.assertEquals(set(['storpool-inventory.collected']),
                          r_state.r_get_states())

        self.assertEquals(1, sprepo_install.call_count)
        sprepo_record.assert_called_once_with('storpool-inventory-charm',
                                              installed)
        self.assertEquals(1, sub_call.call_count)

        # We did not actually run any commands, so it has not collected
        # any data, but still it should have created a file.
        datafile = testee.datafile
        self.assertTrue(os.path.isfile(datafile))
        with open(datafile, mode='r') as f:
            data = json.loads(f.read())
            self.assertIsInstance(data, dict)
            self.assertEquals(1, len(data))
            self.assertEquals('collect.sh', list(data.keys())[0])

        # First, a submission with no config URL
        r_state.set_state('storpool-inventory.submitting')
        testee.try_to_submit()
        self.assertEquals(set(['storpool-inventory.collected']),
                          r_state.r_get_states())
        self.assertEquals(0, urlopen.call_count)

        # Now make the submission fail
        r_config.r_set('submit_url', 'something', False)
        mock_client = mock.MagicMock(spec=http_client.HTTPResponse)
        mock_client.getcode.return_value = 300
        mock_client.__enter__.return_value = mock_client
        urlopen.return_value = mock_client
        r_state.set_state('storpool-inventory.submitting')
        testee.try_to_submit()
        self.assertEquals(set(['storpool-inventory.collected']),
                          r_state.r_get_states())
        self.assertEquals(1, urlopen.call_count)
        self.assertEquals(1, mock_client.getcode.call_count)

        # And now let it succeed
        mock_client.getcode.return_value = 200
        r_state.set_state('storpool-inventory.submitting')
        testee.try_to_submit()
        self.assertEquals(set([
                              'storpool-inventory.collected',
                              'storpool-inventory.submitted'
                              ]),
                          r_state.r_get_states())
        self.assertEquals(2, urlopen.call_count)
        self.assertEquals(2, mock_client.getcode.call_count)
