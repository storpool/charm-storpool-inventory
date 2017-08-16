from __future__ import print_function

import json
import os
import platform
import subprocess
import tempfile
import time
import urllib.request

from charms import reactive
from charms.reactive import helpers as rhelpers

from charmhelpers.core import hookenv

from spcharms import repo as sprepo

datadir = '/var/lib/storpool'
datafile = datadir + '/collect.json'

def rdebug(s):
	with open('/tmp/storpool-charms.log', 'a') as f:
		print('{tm} [inventory-charm] {s}'.format(tm=time.ctime(), s=s), file=f)

@reactive.hook('install')
def first_install():
	rdebug('install invoked, triggering both a recollection and a resubmission')
	reactive.set_state('storpool-inventory.collecting')
	reactive.remove_state('storpool-inventory.collected')
	reactive.set_state('storpool-inventory.submitting')
	reactive.remove_state('storpool-inventory.submitted')
	hookenv.status_set('maintenance', 'setting up')

@reactive.hook('config-changed')
def have_config():
	rdebug('config-changed')
	config = hookenv.config()

	url = config.get('submit_url', None)
	if url is not None:
		if config.changed('submit_url') or not rhelpers.is_state('storpool-inventory.configured'):
			reactive.set_state('storpool-inventory.configured')
			rdebug('we have a new submission URL address: {url}'.format(url=url))
			reactive.set_state('storpool-inventory.submitting')
			reactive.remove_state('storpool-inventory.submitted')

			if not rhelpers.is_state('storpool-inventory.collected') and not rhelpers.is_state('storpool-inventory.collecting'):
				rdebug('triggering another collection attempt')
				hookenv.status_set('maintenance', 'about to try to collect data again')
				reactive.set_state('storpool-inventory.collecting')
			else:
				hookenv.status_set('maintenance', 'about to resubmit any collected data')
		else:
			rdebug('the submission URL address seems to be the same as before')
	else:
		rdebug('we do not seem to have a submission URL address')
		reactive.remove_state('storpool-inventory.configured')
		reactive.remove_state('storpool-inventory.submitting')
		reactive.remove_state('storpool-inventory.submitted')
		hookenv.status_set('maintenance', 'waiting for configuration')

@reactive.when('storpool-inventory.collecting')
@reactive.when_not('storpool-inventory.collected')
@reactive.when_not('storpool-repo-add.available')
def waiting_for_repo():
	hookenv.status_set('maintenance', 'waiting for the StorPool APT repo to be configured')
	rdebug('no APT repo yet')

@reactive.when('storpool-inventory.collecting')
@reactive.when_not('storpool-inventory.collected')
@reactive.when('storpool-repo-add.available')
def collect():
	rdebug('about to collect some data, are we not')
	reactive.remove_state('storpool-inventory.collecting')

	hookenv.status_set('maintenance', 'installing packages for data collection')
	try:
		(err, newly_installed) = sprepo.install_packages({
			'dmidecode': '*',
			'lshw': '*',
			'nvme-cli': '*',
			'pciutils': '*',
			'usbutils': '*',
		})
		if err is not None:
			raise Exception('{e}'.format(e=err))
		if newly_installed:
			rdebug('it seems we installed some new packages: {lst}'.format(lst=' '.join(newly_installed)))
		else:
			rdebug('it seems we already had everything we needed')
		sprepo.record_packages(newly_installed)
		hookenv.status_set('maintenance', '')
	except Exception as e:
		rdebug('could not install the OS packages: {e}'.format(e=e))
		hookenv.status_set('maintenance', 'failed to install the OS packages')
		return

	hookenv.status_set('maintenance', 'collecting data')
	try:
		with tempfile.TemporaryDirectory(dir='/tmp', prefix='storpool-inventory.') as d:
			rdebug('created a temporary directory {d}'.format(d=d))

			"""
			No need to create a working directory for the present...

			workname = 'collect-' + platform.node()
			workdir = d + '/' + workname
			os.mkdir(workdir, mode=0o700)
			rdebug('created the working directory {w}'.format(w=workdir))
			"""
			workdir = d

			collect_script = workdir + '/collect.sh'
			with open(collect_script, mode='w') as f:
				print('#!/bin/bash\n' +
	'cd {w} || exit 1\n'.format(w=workdir) +
	'[[ $UID -ne 0 ]] && p=sudo\n' +
	'$p dmidecode > dmidecode.txt 2>dmidecode.err\n' +
	'$p free -m > free-m.txt 2>free-m.err\n' +
	'$p lsblk > lsblk.txt 2>lsblk.err\n' +
	'$p lspci > lspci.txt 2>lspci.err\n' +
	'$p lspci -vv > lspci-vv.txt 2>lspci-vv.err\n' +
	'$p lspci -vvnnqD > lspci-vvnnqD.txt 2>lspci-vvnnqD.err\n' +
	'$p lshw > lshw.txt 2>lshw.err\n' +
	'$p lscpu > lscpu.txt 2>lscpu.err\n' +
	'$p lsmod > lsmod.txt 2>lsmod.err\n' +
	'$p nvme list > nvme-list.txt 2>nvme-list.err\n' +
	'$p ls -l /dev/disk/by-id > ls-dev-disk-by-id.txt 2>ls-dev-disk-by-id.err\n' +
	'$p ls -l /dev/disk/by-path > ls-dev-disk-by-path.txt 2>ls-dev-disk-by-path.err\n' +
	'$p ls -l /sys/class/net > ls-sys-class-net.txt 2>ls-sys-class-net.err\n', end='', file=f)
			os.chmod(collect_script, 0o700)
			rdebug('running the collect script'.format(cs=collect_script))
			subprocess.call(['sh', '-c', "{cs} > '{w}/collect.txt' 2>'{w}/collect.err'".format(cs=collect_script, w=workdir)])

			collected = {}
			rdebug('scanning the {w} directory now'.format(w=workdir))
			for e in os.scandir(workdir):
				if not e.is_file():
					continue
				rdebug('- {name}'.format(name=e.name))
				with open(workdir + '/' + e.name, mode='r', encoding='latin1') as f:
					collected[e.name] = ''.join(f.readlines())
			rdebug('collected {ln} entries: {ks}'.format(ln=len(collected), ks=sorted(collected.keys())))
			data = json.dumps(collected)
			rdebug('and dumped them to {ln} characters of data'.format(ln=len(data)))

			global datafile
			rdebug('about to write {df}'.format(df=datafile))
			if not os.path.isdir(datadir):
				os.mkdir(datadir, mode=0o700)
			with open(datafile, mode='w', encoding='latin1') as f:
				rdebug('about to write to the file')
				print(data, file=f)
				rdebug('done writing to the file, it seems')
			rdebug('about to check the size of the collect file')
			st = os.stat(datafile)
			rdebug('it seems we wrote {ln} bytes to the file'.format(ln=st.st_size))

			rdebug('we seem to be done here!')
			reactive.set_state('storpool-inventory.collected')
			hookenv.status_set('maintenance', '')
	except Exception as e:
		rdebug('something bad happened: {e}'.format(e=e))
		hookenv.status_set('maintenance', 'failed to collect the data')

@reactive.when_not('storpool-inventory.configured')
@reactive.when('storpool-inventory.collected')
@reactive.when('storpool-inventory.submitting')
@reactive.when_not('storpool-inventory.submitted')
def nowhere_to_submit_to():
	rdebug('collected some data, but nowhere to submit it to')

@reactive.when('storpool-inventory.configured')
@reactive.when('storpool-inventory.collected')
@reactive.when('storpool-inventory.submitting')
@reactive.when_not('storpool-inventory.submitted')
def try_to_submit():
	url = hookenv.config().get('submit_url', None)
	rdebug('trying to submit to {url}'.format(url=url))
	reactive.remove_state('storpool-inventory.submitting')

	if url is None:
		rdebug('erm, how did we get here with no submit URL?')
		return

	hookenv.status_set('maintenance', 'submitting the collected data')
	try:
		global datafile
		rdebug('about to read {df}'.format(df=datafile))
		with open(datafile, mode='r', encoding='latin1') as f:
			contents = ''.join(f.readlines())
		rdebug('read {ln} characters of data from the collect file'.format(ln=len(contents)))
		data = json.dumps({'filename': platform.node(), 'contents': contents})
		rdebug('encoded stuff into {ln} characters of data to submit'.format(ln=len(data)))
		data_enc = data.encode('latin1')
		rdebug('submitting {ln} bytes of data to {url}'.format(ln=len(data_enc), url=url))
		with urllib.request.urlopen(url, data=data_enc) as resp:
			rdebug('got some kind of an HTTP response')
			code = resp.getcode()
			rdebug('got response code {code}'.format(code=code))
			if code is not None and code >= 200 and code < 300:
				rdebug('success!')
				reactive.set_state('storpool-inventory.submitted')
				hookenv.status_set('active', 'here, have a blob of data')
	except Exception as e:
		rdebug('could not submit the data: {e}'.format(e=e))
		hookenv.status_set('maintenance', 'failed to submit the collected data')

@reactive.hook('update-status')
def submit_if_needed():
	rdebug('update-status invoked')

	if not rhelpers.is_state('storpool-inventory.collected'):
		rdebug('triggering a new collection attempt')
		reactive.set_state('storpool-inventory.collecting')
	else:
		rdebug('already collected!')

	if not rhelpers.is_state('storpool-inventory.submitted'):
		rdebug('triggering a new submission attempt')
		reactive.set_state('storpool-inventory.submitting')
	else:
		rdebug('already submitted!')

@reactive.hook('upgrade-charm')
def recollect_and_resubmit():
	rdebug('upgrade-charm invoked, resetting all the flags')
	reactive.set_state('storpool-inventory.collecting')
	reactive.remove_state('storpool-inventory.collected')
	reactive.set_state('storpool-inventory.submitting')
	reactive.remove_state('storpool-inventory.submitted')
	reactive.remove_state('storpool-inventory.configured')

@reactive.hook('stop')
def stop():
	rdebug('stop invoked, letting storpool-repo-add know')
	reactive.set_state('storpool-repo-add.stop')
	rdebug('and also removing the file with the collected data')
	try:
		os.unlink(datafile)
	except Exception as e:
		rdebug('could not remove {name}: {e}'.format(name=datafile, e=e))
